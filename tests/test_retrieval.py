import pytest

from sre_assistant.config import Settings
from sre_assistant.rag.fusion import reciprocal_rank_fusion
from sre_assistant.rag.retriever import HybridRetriever, group_by_parent
from sre_assistant.rag.store import ChunkHit, VectorStore


def test_rrf_math_and_agreement_bonus():
    scores = reciprocal_rank_fusion([["a", "b", "c"], ["b", "d"]], k=60)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61)
    assert list(scores)[0] == "b", "found by both retrievers beats rank-1 in only one"


def test_rrf_ties_are_deterministic_and_empty_is_empty():
    assert list(reciprocal_rank_fusion([["x"], ["y"]])) == ["x", "y"]
    assert reciprocal_rank_fusion([]) == {}


def test_group_by_parent_keeps_best_child_per_parent():
    ranked = [("d1#a/0", 0.9), ("d1#b/0", 0.8), ("d2#a/0", 0.7), ("d3#a/0", 0.6)]
    parent_of = {"d1#a/0": "d1", "d1#b/0": "d1", "d2#a/0": "d2", "d3#a/0": "d3"}
    assert group_by_parent(ranked, parent_of, 2) == [("d1", "d1#a/0", 0.9), ("d2", "d2#a/0", 0.7)]


def test_filter_allowlist_blocks_injection():
    with pytest.raises(ValueError, match="unsupported filter"):
        VectorStore._filter_sql({"service; DROP TABLE chunks": "x"})


# ---------- retriever pipeline with fakes (no DB, no model) ----------
def _hit(cid: str, parent: str, content: str = "") -> ChunkHit:
    return ChunkHit(cid, parent, "Root Cause", content or cid, 0.0)


class FakeStore:
    def __init__(self):
        self.vector = [_hit("d1/0", "d1", "redis timeout"), _hit("d2/0", "d2", "keyerror crashloop")]
        self.keyword = [_hit("d2/0", "d2", "keyerror crashloop"), _hit("d3/0", "d3", "unrelated")]
        self.filters_seen = []

    def vector_search(self, collection, qvec, k, filters=None):
        self.filters_seen.append(filters)
        return self.vector

    def keyword_search(self, collection, text, k, max_df, filters=None):
        return self.keyword

    def get_parents(self, collection, parent_ids):
        return {
            p: {"doc_id": p, "doc_type": "runbook", "title": p, "content": f"content of {p}", "token_count": 10,
                "service": "s", "environment": "any", "infrastructure": "docker", "error_type": "E",
                "scenario": f"scn-{p}", "created": None}
            for p in parent_ids
        }  # fmt: skip


class FakeEmbeddings:
    def embed_query(self, text):
        return [0.0]


class KeywordReranker:
    def score(self, query, texts):
        return [float(sum(w in t for w in query.split())) for t in texts]


def test_hybrid_fuses_and_returns_parents():
    r = HybridRetriever(FakeStore(), FakeEmbeddings(), Settings(_env_file=None), reranker=None)
    res = r.retrieve("anything", top_parents=3)
    assert res.doc_ids[0] == "d2", "d2 was found by both retrievers"
    assert not res.reranked
    top = res.sections[0]
    assert (top.vector_rank, top.keyword_rank) == (2, 1)
    assert top.citation == "[d2] Root Cause"


def test_reranker_reorders_candidates():
    r = HybridRetriever(FakeStore(), FakeEmbeddings(), Settings(_env_file=None), reranker=KeywordReranker())
    res = r.retrieve("redis timeout", top_parents=2)
    assert res.reranked and res.doc_ids[0] == "d1"


def test_rerank_without_reranker_is_an_error():
    r = HybridRetriever(FakeStore(), FakeEmbeddings(), Settings(_env_file=None))
    with pytest.raises(ValueError):
        r.retrieve("x", rerank=True)


def test_filters_are_passed_to_store():
    store = FakeStore()
    HybridRetriever(store, FakeEmbeddings(), Settings(_env_file=None)).retrieve(
        "x", mode="vector", filters={"service": "auth-service"}
    )
    assert store.filters_seen == [{"service": "auth-service"}]
