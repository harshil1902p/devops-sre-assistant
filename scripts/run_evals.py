"""Run the evaluation suite.

    uv run python scripts/run_evals.py                       # all cases + LLM judge
    uv run python scripts/run_evals.py --no-judge --limit 3  # quick smoke
    uv run python scripts/run_evals.py --case adv-prompt-injection
    uv run python scripts/run_evals.py --judge-only          # re-score + re-judge existing results (no agent cost)

Writes eval/results/agent_eval.json (full detail) and appends a summary line to eval/results/history.jsonl.
"""

import argparse
import asyncio
import json
import logging
import secrets
import subprocess
import time
from pathlib import Path

from sre_assistant.config import get_settings
from sre_assistant.corpus.generate import load_scenarios
from sre_assistant.evals.cases import build_cases
from sre_assistant.evals.metrics import aggregate, score_case
from sre_assistant.evals.runner import LockedRetriever, run_case
from sre_assistant.llm.providers import get_ledger
from sre_assistant.rag.factory import build_retriever

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"


def leaf(e: BaseException) -> BaseException:
    """TaskGroups wrap errors in ExceptionGroups; report the real cause."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


async def run_agents(cases, s, manifest, concurrency: int) -> list[dict]:
    retriever = LockedRetriever(build_retriever(s))
    pii_key = secrets.token_bytes(32)
    sem = asyncio.Semaphore(concurrency)

    async def one(case):
        async with sem:
            for attempt in range(3):
                try:
                    r = await run_case(case, s, retriever, ROOT / "data" / "corpus", pii_key)
                    break
                except Exception as e:  # noqa: BLE001 - one failing case must not kill the suite
                    err = leaf(e)
                    if "429" in str(err) and attempt < 2:  # TPM quota shared by concurrent cases: back off
                        await asyncio.sleep(30 * (attempt + 1))
                        continue
                    print(f"  ✗ {case.id}: {type(err).__name__}: {str(err)[:200]}")
                    return None
            r["metrics"] = score_case(r, manifest)
            print(f"  ✓ {case.id:<34} {r['wall_seconds']:>5}s ${r['usage']['cost_usd']:.4f}")
            return r

    return [r for r in await asyncio.gather(*(one(c) for c in cases)) if r]


async def run_judge(rows: list[dict], s, only_missing: bool) -> None:
    from langchain_core.rate_limiters import InMemoryRateLimiter

    from sre_assistant.evals.judge import LangChainJudge, judge_case
    from sre_assistant.llm import get_chat_model

    judge_model = get_chat_model(s, tag="judge")
    # ~10k-token judge prompts: 0.2 req/s = 12/min = ~120k TPM, under the 150k deployment limit.
    judge_model.rate_limiter = InMemoryRateLimiter(requests_per_second=0.2, max_bucket_size=2)
    judge = LangChainJudge(judge_model, f"azure:{s.chat_model}")
    sem = asyncio.Semaphore(2)

    async def judged(r):
        if only_missing and r.get("judge") and "error" not in r["judge"]:
            return
        async with sem:
            for attempt in range(3):
                try:
                    r["judge"] = await judge_case(r, judge)
                    return
                except Exception as e:  # noqa: BLE001
                    err = leaf(e)
                    r["judge"] = {"error": f"{type(err).__name__}: {str(err)[:200]}"}
                    await asyncio.sleep(20 * (attempt + 1))

    await asyncio.gather(*(judged(r) for r in rows))


def print_report(rows: list[dict], summary: dict) -> None:
    print(f"\n  {'case':<34}{'cov':>5}{'cite':>6}{'prec':>6}{'corr':>6}{'grnd':>6}  notes")
    fmt = lambda v: f"{v:.2f}" if isinstance(v, int | float) else "  - "
    for r in sorted(rows, key=lambda x: x["id"]):
        m, j = r["metrics"], r.get("judge") or {}
        notes = {k: v for k, v in m.items() if k in ("abstention_ok", "injection_flagged", "injection_resisted",
                                                     "both_incidents_covered", "invalid_citations") and v != 0}  # fmt: skip
        if "error" in j:
            notes["judge_error"] = j["error"][:60]
        print(f"  {r['id']:<34}{fmt(m.get('retrieval_coverage')):>5}{fmt(m.get('cited_primary')):>6}"
              f"{fmt(m.get('citation_precision')):>6}{fmt((j.get('correctness') or {}).get('score')):>6}"
              f"{fmt((j.get('groundedness') or {}).get('score')):>6}  {notes or ''}")  # fmt: skip
    print("\nsummary:", json.dumps(summary, indent=2))


async def main() -> None:
    # Configure logging FIRST: an in-process MCPServer otherwise installs a root handler and every INFO record
    # (audit, azure-identity) floods the console. basicConfig after this point becomes a no-op.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("sre.audit").propagate = False
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--case", action="append", default=[])
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--judge-only", action="store_true", help="re-score + re-judge existing results; no agent runs")
    p.add_argument("--concurrency", type=int, default=3)
    a = p.parse_args()

    s = get_settings()
    ledger = get_ledger(s)
    spend_before = ledger.total()
    manifest = json.loads((ROOT / "data" / "corpus" / "manifest.json").read_text())
    t0 = time.perf_counter()

    if a.judge_only:
        rows = json.loads((RESULTS / "agent_eval.json").read_text())["cases"]
        for r in rows:
            r["metrics"] = score_case(r, manifest)  # re-score with the current metric definitions
        print(f"re-judging {len(rows)} existing results; spend so far ${spend_before:.4f}")
    else:
        cases = build_cases(load_scenarios(ROOT / "data" / "scenarios"))
        if a.case:
            cases = [c for c in cases if c.id in a.case]
        cases = cases[: a.limit] if a.limit else cases
        print(f"running {len(cases)} cases with {s.chat_model} (x{a.concurrency}); spend so far ${spend_before:.4f}")
        rows = await run_agents(cases, s, manifest, a.concurrency)

    if not a.no_judge and rows:
        await run_judge(rows, s, only_missing=a.judge_only)

    summary = aggregate(rows)
    summary["eval_spend_usd"] = round(ledger.total() - spend_before, 4)
    summary["project_spend_usd"] = ledger.total()
    summary["wall_seconds_total"] = round(time.perf_counter() - t0, 1)
    summary["model"] = s.chat_model
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT)
    summary["git_commit"] = commit.stdout.strip()
    print_report(rows, summary)

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "agent_eval.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=2, default=str))
    with open(RESULTS / "history.jsonl", "a") as f:
        f.write(json.dumps(summary) + "\n")
    print(f"\nwrote {RESULTS.relative_to(ROOT)}/agent_eval.json")


if __name__ == "__main__":
    asyncio.run(main())
