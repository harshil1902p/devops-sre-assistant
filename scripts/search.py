"""Interactive retrieval explorer: see exactly why each section was retrieved.

uv run python scripts/search.py "QueuePool limit of size 10 overflow 5 reached"
uv run python scripts/search.py "logins fail after 5 seconds but postgres is idle" --mode vector
uv run python scripts/search.py "pod restarts exit 137" --filter service=auth-service --show
"""

import argparse

from sre_assistant.rag.factory import build_retriever


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("query")
    p.add_argument("--mode", choices=["hybrid", "vector", "keyword"], default="hybrid")
    p.add_argument("--collection", choices=["parent_child", "naive"], default="parent_child")
    p.add_argument("--filter", action="append", default=[], help="key=value, e.g. service=auth-service")
    p.add_argument("--top", type=int, default=None)
    p.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder stage")
    p.add_argument("--show", action="store_true", help="print the section content")
    a = p.parse_args()

    filters = dict(f.split("=", 1) for f in a.filter) or None
    r = build_retriever(collection=a.collection).retrieve(a.query, a.mode, filters, a.top, rerank=not a.no_rerank)

    print(
        f"\nmode={r.mode} reranked={r.reranked} context_tokens={r.context_tokens} timings_ms="
        f"{ {k: round(v, 1) for k, v in r.timings_ms.items()} }\n"
    )
    print(f"{'#':<3}{'score':>7} {'vec':>4} {'kw':>4}  {'source':<52} scenario")
    for i, s in enumerate(r.sections, 1):
        print(
            f"{i:<3}{s.score:>7.4f} {s.vector_rank or '-':>4} {s.keyword_rank or '-':>4}  "
            f"{s.citation[:52]:<52} {s.metadata['scenario']}"
        )
        if a.show:
            print("    " + s.content.replace("\n", "\n    ") + "\n")


if __name__ == "__main__":
    main()
