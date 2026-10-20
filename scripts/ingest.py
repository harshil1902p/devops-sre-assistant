"""Index the knowledge base into pgvector.

uv run python scripts/ingest.py                      # both collections, incremental
uv run python scripts/ingest.py --collection parent_child --reset
"""

import argparse
from pathlib import Path

from sre_assistant.config import get_settings
from sre_assistant.corpus import load_corpus
from sre_assistant.llm import get_embeddings
from sre_assistant.rag.factory import build_store
from sre_assistant.rag.ingest import ingest, make_chunker

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--collection", choices=["parent_child", "naive", "all"], default="all")
    p.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    a = p.parse_args()

    s = get_settings()
    store = build_store(s)
    store.init_schema(reset=a.reset)
    embeddings = get_embeddings(s)
    docs = load_corpus(CORPUS)

    for collection in ["parent_child", "naive"] if a.collection == "all" else [a.collection]:
        chunker, version = make_chunker(
            collection, s.child_max_tokens, s.parent_max_tokens, s.naive_chunk_tokens, s.naive_chunk_overlap
        )
        print(f"[{collection}] chunker={version} model={s.embedding_model}")
        r = ingest(store, embeddings, docs, collection, chunker, version, s.embedding_model, progress=print)
        print(
            f"[{collection}] embedded {len(r.embedded_docs)} docs / {r.chunks_embedded} chunks, "
            f"skipped {r.skipped_docs} unchanged, deleted {len(r.deleted_docs)} in {r.seconds:.1f}s\n"
        )

    for row in store.stats():
        print(row)
    store.close()


if __name__ == "__main__":
    main()
