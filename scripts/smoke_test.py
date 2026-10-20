"""Phase 0 smoke test: prove the chat model and embedding model both work end-to-end.

Run:  uv run python scripts/smoke_test.py
"""

import math
import time

from sre_assistant.config import get_settings
from sre_assistant.llm import get_chat_model, get_embeddings


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> None:
    s = get_settings()
    print(f"Provider={s.llm_provider} chat={s.chat_model} embeddings={s.embedding_model}\n")

    t = time.perf_counter()
    reply = get_chat_model().invoke(
        "In one sentence: what does 'sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 "
        "reached' usually mean?"
    )
    print(f"[chat] {time.perf_counter() - t:.1f}s -> {reply.content.strip()}")
    print(f"[chat] token usage: {reply.usage_metadata}\n")

    emb = get_embeddings()
    texts = {
        "query": "database connection pool exhausted, requests timing out",
        "related": "QueuePool limit overflowed: too many concurrent DB sessions",
        "unrelated": "How to rotate TLS certificates for the ingress controller",
    }
    t = time.perf_counter()
    vecs = dict(zip(texts, emb.embed_documents(list(texts.values())), strict=True))
    print(f"[embed] {time.perf_counter() - t:.1f}s, dim={len(vecs['query'])} (configured {s.embedding_dim})")
    print(f"[embed] cos(query, related)   = {cosine(vecs['query'], vecs['related']):.3f}")
    print(f"[embed] cos(query, unrelated) = {cosine(vecs['query'], vecs['unrelated']):.3f}")
    assert len(vecs["query"]) == s.embedding_dim, "SRE_EMBEDDING_DIM does not match the model output!"


if __name__ == "__main__":
    main()
