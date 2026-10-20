"""Hybrid retrieval pipeline.

    query ─┬─▶ vector search  (child chunks, top K) ─┐
           └─▶ keyword search (child chunks, top K) ─┴─▶ RRF fusion
               ─▶ cross-encoder rerank (top N children) ─▶ group by parent (small-to-big) ─▶ top P parents

Child chunks are what we match; parents (whole doc or section) are what the LLM reads.
"""

import time
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.embeddings import Embeddings

from sre_assistant.config import Settings
from sre_assistant.rag.fusion import reciprocal_rank_fusion
from sre_assistant.rag.rerank import Reranker
from sre_assistant.rag.store import ChunkHit, VectorStore

Mode = Literal["hybrid", "vector", "keyword"]


@dataclass(frozen=True)
class RetrievedSection:
    """A parent (whole doc or section) returned to the LLM; `section` = where the best child matched."""

    parent_id: str
    doc_id: str
    doc_type: str
    title: str
    section: str
    content: str
    token_count: int
    score: float
    vector_rank: int | None
    keyword_rank: int | None
    rerank_score: float | None
    metadata: dict

    @property
    def citation(self) -> str:
        return f"{self.doc_id} § {self.section}"


@dataclass
class RetrievalResult:
    query: str
    mode: Mode
    reranked: bool
    sections: list[RetrievedSection]
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def context_tokens(self) -> int:
        return sum(s.token_count for s in self.sections)

    @property
    def doc_ids(self) -> list[str]:
        return list(dict.fromkeys(s.doc_id for s in self.sections))


class _Timer:
    def __init__(self, timings: dict[str, float], name: str):
        self.timings, self.name = timings, name

    def __enter__(self):
        self.t = time.perf_counter()

    def __exit__(self, *_):
        self.timings[self.name] = round((time.perf_counter() - self.t) * 1000, 1)


class HybridRetriever:
    def __init__(
        self,
        store: VectorStore,
        embeddings: Embeddings,
        settings: Settings,
        collection: str = "parent_child",
        reranker: Reranker | None = None,
    ):
        self.store = store
        self.embeddings = embeddings
        self.s = settings
        self.collection = collection
        self.reranker = reranker

    def retrieve(
        self,
        query: str,
        mode: Mode = "hybrid",
        filters: dict | None = None,
        top_parents: int | None = None,
        rerank: bool | None = None,
    ) -> RetrievalResult:
        timings: dict[str, float] = {}
        rerank = (self.reranker is not None) if rerank is None else rerank
        if rerank and self.reranker is None:
            raise ValueError("rerank requested but no reranker configured")

        vec_hits: list[ChunkHit] = []
        kw_hits: list[ChunkHit] = []
        if mode in ("hybrid", "vector"):
            with _Timer(timings, "embed"):
                qvec = self.embeddings.embed_query(query)
            with _Timer(timings, "vector"):
                vec_hits = self.store.vector_search(self.collection, qvec, self.s.candidate_k, filters)
        if mode in ("hybrid", "keyword"):
            with _Timer(timings, "keyword"):
                kw_hits = self.store.keyword_search(
                    self.collection, query, self.s.candidate_k, self.s.keyword_max_df, filters
                )

        hits = {h.chunk_id: h for h in [*vec_hits, *kw_hits]}
        rankings = [r for r in ([h.chunk_id for h in vec_hits], [h.chunk_id for h in kw_hits]) if r]
        ranked: list[tuple[str, float]] = list(reciprocal_rank_fusion(rankings, k=self.s.rrf_k).items())

        rerank_scores: dict[str, float] = {}
        if rerank and ranked:
            candidates = [cid for cid, _ in ranked[: self.s.rerank_candidates]]
            with _Timer(timings, "rerank"):
                scores = self.reranker.score(query, [hits[c].content for c in candidates])
            rerank_scores = dict(zip(candidates, scores, strict=True))
            ranked = sorted(rerank_scores.items(), key=lambda kv: (-kv[1], kv[0]))

        top = group_by_parent(ranked, {c: h.parent_id for c, h in hits.items()}, top_parents or self.s.top_parents)

        with _Timer(timings, "fetch_parents"):
            rows = self.store.get_parents(self.collection, [pid for pid, _, _ in top])

        vrank = {h.chunk_id: i for i, h in enumerate(vec_hits, 1)}
        krank = {h.chunk_id: i for i, h in enumerate(kw_hits, 1)}
        sections = []
        for pid, chunk_id, score in top:
            r = rows[pid]
            meta = {k: r[k] for k in ("service", "environment", "infrastructure", "error_type", "scenario", "created")}
            sections.append(
                RetrievedSection(
                    parent_id=pid, doc_id=r["doc_id"], doc_type=r["doc_type"], title=r["title"],
                    section=hits[chunk_id].section, content=r["content"], token_count=r["token_count"],
                    score=score, vector_rank=vrank.get(chunk_id), keyword_rank=krank.get(chunk_id),
                    rerank_score=rerank_scores.get(chunk_id), metadata=meta,
                )
            )  # fmt: skip
        return RetrievalResult(query, mode, bool(rerank_scores), sections, timings)


def group_by_parent(
    ranked_children: list[tuple[str, float]], parent_of: dict[str, str], top_parents: int
) -> list[tuple[str, str, float]]:
    """Small-to-big: walk children best-first, keep each parent once (scored by its best child)."""
    seen: dict[str, tuple[str, float]] = {}
    for chunk_id, score in ranked_children:
        seen.setdefault(parent_of[chunk_id], (chunk_id, score))
        if len(seen) == top_parents:
            break
    return [(pid, cid, score) for pid, (cid, score) in seen.items()]


def format_context(result: RetrievalResult) -> str:
    """Render retrieved parents for an LLM prompt, each tagged with a citable source id."""
    return "\n\n".join(f'<source id="{s.citation}">\n{s.content}\n</source>' for s in result.sections)
