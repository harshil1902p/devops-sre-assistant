"""Incremental, idempotent ingestion.

Fingerprint = hash(document content + chunker settings + embedding model). Only documents whose
fingerprint changed are re-chunked and re-embedded, so editing one runbook re-embeds ~10 chunks,
not the whole corpus. Documents deleted from the corpus are deleted from the index (no ghosts).
"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.embeddings import Embeddings

from sre_assistant.corpus.schema import Document
from sre_assistant.rag.chunking import Chunked, chunk_naive, chunk_parent_child
from sre_assistant.rag.store import VectorStore, content_hash

Chunker = Callable[[Document], Chunked]


@dataclass
class IngestReport:
    collection: str
    embedded_docs: list[str] = field(default_factory=list)
    skipped_docs: int = 0
    deleted_docs: list[str] = field(default_factory=list)
    chunks_embedded: int = 0
    seconds: float = 0.0


def make_chunker(
    collection: str, child_max_tokens: int, parent_max_tokens: int, naive_tokens: int, naive_overlap: int
) -> tuple[Chunker, str]:
    """Returns the chunker for a collection plus a version string that is part of the fingerprint."""
    if collection == "parent_child":
        return (
            (lambda d: chunk_parent_child(d, child_max_tokens, parent_max_tokens)),
            f"parent_child:v2:{child_max_tokens}:{parent_max_tokens}",
        )
    if collection == "naive":
        return (lambda d: chunk_naive(d, naive_tokens, naive_overlap)), f"naive:v1:{naive_tokens}:{naive_overlap}"
    raise ValueError(f"unknown collection {collection!r}")


def ingest(
    store: VectorStore,
    embeddings: Embeddings,
    docs: list[Document],
    collection: str,
    chunker: Chunker,
    chunker_version: str,
    embedding_model: str,
    progress: Callable[[str], None] = lambda _: None,
) -> IngestReport:
    start = time.perf_counter()
    report = IngestReport(collection)
    report.deleted_docs = store.delete_documents_not_in(d.doc_id for d in docs)
    existing = store.fingerprints(collection)

    for doc in docs:
        store.upsert_document(doc)
        fp = hashlib.sha256(f"{content_hash(doc)}|{chunker_version}|{embedding_model}".encode()).hexdigest()
        if existing.get(doc.doc_id) == fp:
            report.skipped_docs += 1
            continue
        chunked = chunker(doc)
        vectors = embeddings.embed_documents([c.content for c in chunked.children])
        store.replace_chunks(collection, doc.doc_id, chunked, vectors, embedding_model, fp)
        report.embedded_docs.append(doc.doc_id)
        report.chunks_embedded += len(chunked.children)
        progress(f"  embedded {doc.doc_id:<32} {len(chunked.children):>3} chunks")

    store.refresh_lexeme_stats(collection)
    report.seconds = time.perf_counter() - start
    return report
