"""Integration tests against the real pgvector + Ollama (+ ONNX reranker).

Prereqs: docker compose -f infra/compose.yaml up -d && uv run python scripts/ingest.py
Run:     uv run pytest -m integration
"""

from pathlib import Path

import pytest

from sre_assistant.config import get_settings
from sre_assistant.corpus import load_corpus
from sre_assistant.llm import get_embeddings
from sre_assistant.rag.factory import build_retriever, build_store
from sre_assistant.rag.ingest import ingest, make_chunker
from sre_assistant.rag.store import VectorStore

pytestmark = pytest.mark.integration
CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus"


@pytest.fixture(scope="module")
def retriever():
    r = build_retriever()
    yield r
    r.store.close()


@pytest.mark.parametrize(
    ("query", "scenario"),
    [
        ("sqlalchemy.exc.TimeoutError: QueuePool limit of size 10 overflow 20 reached", "db-pool-exhaustion"),
        ("FATAL: sorry, too many clients already", "pg-too-many-connections"),
        ("redis.exceptions.TimeoutError: Timeout reading from socket", "redis-timeout"),
        ("Error: connect ECONNREFUSED 10.9.9.9:6379", "redis-connection-refused"),
        ("pod restarted, Reason: OOMKilled, exit code 137", "oom-killed"),
        ("java.lang.OutOfMemoryError: Java heap space", "jvm-heap-oom"),
    ],
)
def test_hard_negative_pairs_resolve_correctly(retriever, query, scenario):
    res = retriever.retrieve(query)
    assert res.sections[0].metadata["scenario"] == scenario


def test_service_filter_restricts_results(retriever):
    res = retriever.retrieve("timeout", filters={"service": "auth-service"}, top_parents=5)
    assert res.sections and {s.metadata["service"] for s in res.sections} == {"auth-service"}


def test_reingest_is_incremental_noop():
    s = get_settings()
    store = build_store(s)
    chunker, version = make_chunker(
        "parent_child", s.child_max_tokens, s.parent_max_tokens, s.naive_chunk_tokens, s.naive_chunk_overlap
    )
    report = ingest(store, get_embeddings(s), load_corpus(CORPUS), "parent_child", chunker, version, s.embedding_model)
    store.close()
    assert report.embedded_docs == [] and report.skipped_docs == 51


def test_dimension_mismatch_is_detected():
    store = VectorStore(get_settings().database_url.get_secret_value(), dim=1536)
    with pytest.raises(RuntimeError, match="different vector spaces"):
        store.check_embedding_dim()
    store.close()
