"""Retrieval benchmark: compare chunking strategies and retrieval modes on hand-written queries.

    uv run python scripts/retrieval_benchmark.py            # prints a table + writes eval/results/retrieval.json

Metrics (per configuration, averaged over queries):
  hit@1 / hit@3  correct scenario is the 1st / within the first 3 returned parents
  MRR            mean reciprocal rank of the first correct parent (over 10 candidates)
  fix_in_ctx     the context sent to the LLM contains the scenario's actual fix text
                 (a "hit" on the log section is useless if the fix isn't in the context)
  ctx_tokens     tokens of context sent to the LLM (cost and latency driver)
"""

import argparse
import json
import statistics
import time
from pathlib import Path

import yaml

from sre_assistant.config import get_settings
from sre_assistant.corpus.generate import load_scenarios
from sre_assistant.rag.factory import build_retriever

ROOT = Path(__file__).resolve().parents[1]
QUERIES = yaml.safe_load((ROOT / "data" / "eval" / "retrieval_queries.yaml").read_text())
OUT = ROOT / "eval" / "results" / "retrieval.json"

# (label, collection, mode, rerank, number of parents/windows given to the LLM)
CONFIGS = [
    ("naive  | vector         | top5", "naive", "vector", False, 5),
    ("naive  | hybrid         | top5", "naive", "hybrid", False, 5),
    ("parent | vector         | top3", "parent_child", "vector", False, 3),
    ("parent | keyword        | top3", "parent_child", "keyword", False, 3),
    ("parent | hybrid         | top3", "parent_child", "hybrid", False, 3),
    ("parent | hybrid+rerank  | top3", "parent_child", "hybrid", True, 3),
    ("parent | hybrid+rerank  | top2", "parent_child", "hybrid", True, 2),
]
BEST = "parent | hybrid+rerank  | top3"

# Azure AI Search (Phase 7). rerank column: none | cross_encoder (local ONNX) | semantic (Azure semantic ranker)
AZURE_CONFIGS = [
    ("azure  | vector         | top3", "vector", "none", 3),
    ("azure  | keyword (BM25) | top3", "keyword", "none", 3),
    ("azure  | hybrid (RRF)   | top3", "hybrid", "none", 3),
    ("azure  | hybrid+semantic| top3", "hybrid", "semantic", 3),
    ("azure  | hybrid+xencoder| top3", "hybrid", "cross_encoder", 3),
    ("azure  | hybrid+xencoder| top2", "hybrid", "cross_encoder", 2),
]
AZURE_BEST = "azure  | hybrid+xencoder| top3"


def fix_markers(scenario) -> list[str]:
    """Distinctive fix sentences; at least one must appear in the context for fix_in_ctx."""
    return [
        scenario.permanent_fix[0][:50],
        scenario.postmortem.resolution[:50],
        scenario.kb.answer.strip().splitlines()[0][:50],
    ]


def evaluate(label, collection, mode, rerank, top, scenarios, settings=None) -> dict:
    retriever = build_retriever(settings, collection=collection)
    rows = []
    for q in QUERIES:
        t = time.perf_counter()
        ranked = retriever.retrieve(q["query"], mode, top_parents=10, rerank=rerank)
        latency = (time.perf_counter() - t) * 1000
        ctx = ranked.sections[:top]
        scen = [s.metadata["scenario"] for s in ranked.sections]
        first = next((i for i, s in enumerate(scen, 1) if s == q["scenario"]), None)
        context = "\n".join(s.content for s in ctx)
        rows.append(
            {
                "query": q["query"],
                "kind": q["kind"],
                "expected": q["scenario"],
                "got": scen[:top],
                "hit@1": int(first == 1),
                "hit@3": int(first is not None and first <= min(3, top)),
                "rr": 1 / first if first else 0.0,
                "fix_in_ctx": int(any(m in context for m in fix_markers(scenarios[q["scenario"]]))),
                "ctx_tokens": sum(s.token_count for s in ctx),
                "latency_ms": latency,
            }
        )
    if hasattr(retriever, "store"):
        retriever.store.close()

    def agg(rs, key):
        return statistics.mean(r[key] for r in rs)

    summary = {"config": label}
    for subset, rs in [("all", rows), ("log", [r for r in rows if r["kind"] == "log"]),
                       ("symptom", [r for r in rows if r["kind"] == "symptom"])]:  # fmt: skip
        summary[subset] = {k: round(agg(rs, k), 3) for k in ("hit@1", "hit@3", "rr", "fix_in_ctx", "ctx_tokens")}
    summary["p50_latency_ms"] = round(statistics.median(r["latency_ms"] for r in rows), 1)
    summary["misses"] = [(r["expected"], r["got"][:2], r["query"][:70]) for r in rows if not r["hit@1"]]
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", choices=["pgvector", "azure"], default="pgvector")
    backend = p.parse_args().backend
    s = get_settings()
    scenarios = {sc.id: sc for sc in load_scenarios(ROOT / "data" / "scenarios")}
    if backend == "azure":
        results = []
        for label, mode, strategy, top in AZURE_CONFIGS:
            sa = s.model_copy(update={"retriever_backend": "azure_search", "azure_search_rerank": strategy})
            results.append(evaluate(label, "parent_child", mode, strategy != "none", top, scenarios, sa))
        best_label, out, model = AZURE_BEST, OUT.with_name("retrieval_azure.json"), s.azure_search_embedding_model
    else:
        results = [evaluate(*c, scenarios) for c in CONFIGS]
        best_label, out, model = BEST, OUT, s.embedding_model

    print(
        f"\n{len(QUERIES)} queries ({sum(q['kind'] == 'log' for q in QUERIES)} log, "
        f"{sum(q['kind'] == 'symptom' for q in QUERIES)} symptom), embeddings={model}\n"
    )
    cols = [("hit@1", 7), ("hit@3", 7), ("MRR", 7), ("fix_in_ctx", 12), ("ctx_tok", 9), ("h@1 log", 9),
            ("h@1 sym", 9), ("p50 ms", 8)]  # fmt: skip
    hdr = f"{'config':<32}" + "".join(f"{name:>{width}}" for name, width in cols)
    print(hdr + "\n" + "-" * len(hdr))
    for r in results:
        a = r["all"]
        print(
            f"{r['config']:<32}{a['hit@1']:>7.2f}{a['hit@3']:>7.2f}{a['rr']:>7.2f}{a['fix_in_ctx']:>12.2f}"
            f"{a['ctx_tokens']:>9.0f}{r['log']['hit@1']:>9.2f}{r['symptom']['hit@1']:>9.2f}{r['p50_latency_ms']:>8.0f}"
        )

    best = next(r for r in results if r["config"] == best_label)
    print(f"\nMisses for '{best['config']}':")
    for m in best["misses"] or ["(none)"]:
        print("  ", m)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"embedding_model": model, "results": results}, indent=2))
    print(f"\nwrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
