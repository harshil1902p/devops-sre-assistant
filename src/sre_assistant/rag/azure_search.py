"""Azure AI Search backend for the same parent-child hybrid retrieval as pgvector (Phase 2).

    query ─▶ ONE Azure request: BM25 (full text) + HNSW vector, fused with RRF server-side
          ─▶ optional rerank: local cross-encoder (same as pgvector) or Azure semantic ranker
          ─▶ group children by parent (small-to-big) ─▶ top P parents

Index design: one document per CHILD chunk. Children carry their parent's text in a retrievable-but-not-searchable
field, so small-to-big needs no second lookup (denormalisation is normal in search indexes: no joins).
Metadata fields are filterable, so the agent can scope a search to a service / environment like with pgvector.
"""

import base64
import contextlib
import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from langchain_core.embeddings import Embeddings

from sre_assistant.config import Settings
from sre_assistant.corpus.schema import Document
from sre_assistant.rag.chunking import Chunked
from sre_assistant.rag.rerank import Reranker
from sre_assistant.rag.retriever import Mode, RetrievalResult, RetrievedSection, group_by_parent
from sre_assistant.rag.store import FILTERABLE

SEARCH_SCOPE = "https://search.azure.com/.default"
SEMANTIC_CONFIG = "sre-semantic"
META_FIELDS = ("service", "doc_type", "environment", "infrastructure", "language", "error_type", "scenario", "severity")


def doc_key(chunk_id: str) -> str:
    """Azure keys allow only letters, digits, '_', '-', '='; chunk ids contain '#' and '/'."""
    return base64.urlsafe_b64encode(chunk_id.encode()).decode()


def odata_filter(filters: dict[str, str | list[str]] | None) -> str | None:
    """Build an OData filter from a whitelist of fields with escaped values (no filter injection)."""
    if not filters:
        return None
    unknown = set(filters) - FILTERABLE
    if unknown:
        raise ValueError(f"unsupported filter(s): {sorted(unknown)}; allowed: {sorted(FILTERABLE)}")
    clauses = []
    for key, value in sorted(filters.items()):
        values = [value] if isinstance(value, str) else list(value)
        if any("|" in v for v in values):
            raise ValueError("filter values must not contain '|'")
        joined = "|".join(v.replace("'", "''") for v in values)
        clauses.append(f"search.in({key}, '{joined}', '|')")
    return " and ".join(clauses)


def doc_fingerprint(doc: Document, chunker_version: str, embedding_model: str) -> str:
    raw = f"{doc.metadata.model_dump_json()}|{doc.body}|{chunker_version}|{embedding_model}"
    return hashlib.sha256(raw.encode()).hexdigest()


def to_index_docs(doc: Document, chunked: Chunked, vectors: Sequence[list[float]], fingerprint: str) -> list[dict]:
    parents = {p.parent_id: p for p in chunked.parents}
    meta = doc.metadata.model_dump(mode="json")
    out = []
    for child, vec in zip(chunked.children, vectors, strict=True):
        parent = parents[child.parent_id]
        out.append(
            {
                "id": doc_key(child.chunk_id),
                "chunk_id": child.chunk_id,
                "parent_id": child.parent_id,
                "doc_id": doc.doc_id,
                "title": doc.metadata.title,
                "section": child.section,
                "content": child.content,
                "embedding": vec,
                "parent_content": parent.content,
                "parent_tokens": parent.token_count,
                "error_codes": meta["error_codes"],
                "created": meta["created"],
                "fingerprint": fingerprint,
                **{k: meta[k] for k in META_FIELDS},
            }
        )
    return out


def index_definition(name: str, dim: int):
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        HnswParameters,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    S = SearchFieldDataType
    fields = [
        SimpleField(name="id", type=S.String, key=True),
        SimpleField(name="chunk_id", type=S.String),
        SimpleField(name="parent_id", type=S.String, filterable=True),
        SimpleField(name="doc_id", type=S.String, filterable=True),
        SearchableField(name="title", type=S.String),
        SearchableField(name="section", type=S.String),
        # Standard Lucene analyzer: keeps tokens like ERR_CONNECTION_REFUSED / QueuePool intact enough for BM25.
        SearchableField(name="content", type=S.String),
        SearchField(
            name="embedding", type=S.Collection(S.Single), searchable=True,
            vector_search_dimensions=dim, vector_search_profile_name="hnsw-cosine",
        ),
        SimpleField(name="parent_content", type=S.String),  # retrievable only: what the LLM reads
        SimpleField(name="parent_tokens", type=S.Int32),
        SearchableField(name="error_codes", collection=True, filterable=True),  # NOT type=Collection(...): ignored
        SimpleField(name="created", type=S.String),
        SimpleField(name="fingerprint", type=S.String, filterable=True),
        *[SimpleField(name=k, type=S.String, filterable=True, facetable=True) for k in META_FIELDS],
    ]  # fmt: skip
    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="hnsw", parameters=HnswParameters(metric="cosine"))],
            profiles=[VectorSearchProfile(name="hnsw-cosine", algorithm_configuration_name="hnsw")],
        ),
        semantic_search=SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name=SEMANTIC_CONFIG,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="section"),
                        content_fields=[SemanticField(field_name="content")],
                        keywords_fields=[SemanticField(field_name="error_codes")],
                    ),
                )
            ]
        ),
    )


def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def azure_embeddings(s: Settings) -> Embeddings:
    from langchain_openai import AzureOpenAIEmbeddings

    from sre_assistant.llm.providers import _azure_auth

    return AzureOpenAIEmbeddings(
        azure_deployment=s.azure_search_embedding_model,
        azure_endpoint=s.azure_openai_endpoint,
        api_version=s.azure_openai_api_version,
        **_azure_auth(s),
    )


@dataclass
class AzureIngestReport:
    index: str
    embedded_docs: list[str] = field(default_factory=list)
    skipped_docs: int = 0
    deleted_chunks: int = 0
    chunks_uploaded: int = 0
    seconds: float = 0.0


def ingest_azure(
    s: Settings,
    docs: list[Document],
    chunker: Callable[[Document], Chunked],
    chunker_version: str,
    embeddings: Embeddings | None = None,
    reset: bool = False,
    progress: Callable[[str], None] = lambda _: None,
) -> AzureIngestReport:
    """Incremental: only documents whose fingerprint changed are re-chunked and re-embedded (embeddings cost money)."""
    from azure.core.exceptions import ResourceNotFoundError
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient

    start = time.perf_counter()
    cred = _credential()
    idx_client = SearchIndexClient(s.azure_search_endpoint, cred)
    if reset:
        with contextlib.suppress(ResourceNotFoundError):
            idx_client.delete_index(s.azure_search_index)
    idx_client.create_or_update_index(index_definition(s.azure_search_index, s.azure_search_embedding_dim))
    client = SearchClient(s.azure_search_endpoint, s.azure_search_index, cred)
    embeddings = embeddings or azure_embeddings(s)
    report = AzureIngestReport(s.azure_search_index)

    existing: dict[str, set[str]] = {}  # doc_id -> fingerprints present
    keys_by_doc: dict[str, list[str]] = {}
    for r in client.search(search_text="*", select=["id", "doc_id", "fingerprint"], top=100_000):
        existing.setdefault(r["doc_id"], set()).add(r["fingerprint"])
        keys_by_doc.setdefault(r["doc_id"], []).append(r["id"])

    wanted = {d.doc_id for d in docs}
    stale = [k for doc_id, keys in keys_by_doc.items() if doc_id not in wanted for k in keys]
    for doc in docs:
        fp = doc_fingerprint(doc, chunker_version, s.azure_search_embedding_model)
        if existing.get(doc.doc_id) == {fp}:
            report.skipped_docs += 1
            continue
        chunked = chunker(doc)
        vectors = embeddings.embed_documents([c.content for c in chunked.children])
        new_docs = to_index_docs(doc, chunked, vectors, fp)
        new_keys = {d["id"] for d in new_docs}
        stale += [k for k in keys_by_doc.get(doc.doc_id, []) if k not in new_keys]
        client.merge_or_upload_documents(new_docs)
        report.embedded_docs.append(doc.doc_id)
        report.chunks_uploaded += len(new_docs)
        progress(f"  embedded {doc.doc_id:<32} {len(new_docs):>3} chunks")
    if stale:
        client.delete_documents([{"id": k} for k in stale])
        report.deleted_chunks = len(stale)
    report.seconds = time.perf_counter() - start
    return report


class AzureSearchRetriever:
    """Drop-in replacement for HybridRetriever (same `retrieve()` contract and RetrievalResult)."""

    def __init__(
        self, s: Settings, embeddings: Embeddings | None = None, reranker: Reranker | None = None, client=None
    ):
        from azure.search.documents import SearchClient

        self.s = s
        self.embeddings = embeddings or azure_embeddings(s)
        self.reranker = reranker
        self.client = client or SearchClient(s.azure_search_endpoint, s.azure_search_index, _credential())

    def retrieve(
        self,
        query: str,
        mode: Mode = "hybrid",
        filters: dict | None = None,
        top_parents: int | None = None,
        rerank: bool | None = None,
    ) -> RetrievalResult:
        from azure.search.documents.models import VectorizedQuery

        timings: dict[str, float] = {}
        strategy = self.s.azure_search_rerank if rerank is not False else "none"
        if strategy == "cross_encoder" and self.reranker is None:
            strategy = "none"
        k = self.s.candidate_k
        kwargs: dict = {"filter": odata_filter(filters), "top": k, "select": [
            "chunk_id", "parent_id", "doc_id", "doc_type", "title", "section", "content", "parent_content",
            "parent_tokens", "created", *META_FIELDS]}  # fmt: skip
        if mode in ("hybrid", "vector"):
            t = time.perf_counter()
            qvec = self.embeddings.embed_query(query)
            timings["embed"] = round((time.perf_counter() - t) * 1000, 1)
            kwargs["vector_queries"] = [VectorizedQuery(vector=qvec, k_nearest_neighbors=k, fields="embedding")]
        if strategy == "semantic":
            kwargs |= {"query_type": "semantic", "semantic_configuration_name": SEMANTIC_CONFIG}

        t = time.perf_counter()
        hits = list(self.client.search(search_text=query if mode != "vector" else None, **kwargs))
        timings["search"] = round((time.perf_counter() - t) * 1000, 1)

        by_id = {h["chunk_id"]: h for h in hits}
        if strategy == "semantic":
            ranked = [(h["chunk_id"], float(h.get("@search.reranker_score") or 0.0)) for h in hits]
            ranked.sort(key=lambda kv: -kv[1])
        else:
            ranked = [(h["chunk_id"], float(h["@search.score"])) for h in hits]  # already RRF-fused by Azure
        reranked = False
        if strategy == "cross_encoder" and ranked:
            candidates = [cid for cid, _ in ranked[: self.s.rerank_candidates]]
            t = time.perf_counter()
            scores = self.reranker.score(query, [by_id[c]["content"] for c in candidates])
            timings["rerank"] = round((time.perf_counter() - t) * 1000, 1)
            ranked = sorted(zip(candidates, scores, strict=True), key=lambda kv: (-kv[1], kv[0]))
            reranked = True

        top = group_by_parent(ranked, {c: h["parent_id"] for c, h in by_id.items()}, top_parents or self.s.top_parents)
        sections = []
        for pid, cid, score in top:
            h = by_id[cid]
            sections.append(
                RetrievedSection(
                    parent_id=pid, doc_id=h["doc_id"], doc_type=h["doc_type"], title=h["title"], section=h["section"],
                    content=h["parent_content"], token_count=h["parent_tokens"], score=score,
                    vector_rank=None, keyword_rank=None,  # Azure fuses server-side; per-retriever ranks aren't exposed
                    rerank_score=score if reranked or strategy == "semantic" else None,
                    metadata={k: h.get(k) for k in ("service", "environment", "infrastructure", "error_type",
                                                     "scenario", "created")},
                )
            )  # fmt: skip
        return RetrievalResult(query, mode, reranked or strategy == "semantic", sections, timings)
