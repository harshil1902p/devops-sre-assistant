"""Rank fusion. Pure functions, no I/O, fully unit-tested."""

from collections.abc import Sequence


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int = 60) -> dict[str, float]:
    """RRF (Cormack et al., 2009): score(d) = sum over rankers of 1 / (k + rank(d)), rank starting at 1.

    Uses only ranks, never raw scores, so it can combine cosine similarity (0..1) with keyword ranks
    (unbounded) without any normalisation. `k` damps the dominance of top ranks; 60 is the standard default.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return dict(sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])))
