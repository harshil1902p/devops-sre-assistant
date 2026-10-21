"""Index the knowledge base into Azure AI Search (incremental; embeddings via Azure OpenAI, keyless).

uv run python scripts/ingest_azure.py            # only new/changed documents are embedded
uv run python scripts/ingest_azure.py --reset    # drop and rebuild the index
"""

import argparse
from pathlib import Path

from sre_assistant.config import get_settings
from sre_assistant.corpus import load_corpus
from sre_assistant.rag.azure_search import ingest_azure
from sre_assistant.rag.ingest import make_chunker

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()
    s = get_settings()
    if not s.azure_search_endpoint:
        raise SystemExit("set SRE_AZURE_SEARCH_ENDPOINT (see docs/learning/07-azure.md)")
    chunker, version = make_chunker(
        "parent_child", s.child_max_tokens, s.parent_max_tokens, s.naive_chunk_tokens, s.naive_chunk_overlap
    )
    print(f"[{s.azure_search_index}] chunker={version} model={s.azure_search_embedding_model}")
    r = ingest_azure(s, load_corpus(CORPUS), chunker, version, reset=a.reset, progress=print)
    print(
        f"embedded {len(r.embedded_docs)} docs / {r.chunks_uploaded} chunks, skipped {r.skipped_docs} unchanged, "
        f"deleted {r.deleted_chunks} stale chunks in {r.seconds:.1f}s"
    )


if __name__ == "__main__":
    main()
