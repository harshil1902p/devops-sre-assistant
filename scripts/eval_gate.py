"""Fail the build if the latest eval run is below the quality bar.

uv run python scripts/eval_gate.py                     # uses eval/results/agent_eval.json
uv run python scripts/eval_gate.py --results other.json
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def check(summary: dict, thresholds: dict) -> list[tuple[str, str, object, object, bool]]:
    rows = []
    for kind, op in (("min", lambda v, t: v >= t), ("max", lambda v, t: v <= t)):
        for metric, target in (thresholds.get(kind) or {}).items():
            value = summary.get(metric)
            ok = value is not None and op(value, target)
            rows.append((metric, kind, value, target, ok))
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--results", default=str(ROOT / "eval" / "results" / "agent_eval.json"))
    p.add_argument("--thresholds", default=str(ROOT / "eval" / "thresholds.yaml"))
    a = p.parse_args()
    summary = json.loads(Path(a.results).read_text())["summary"]
    rows = check(summary, yaml.safe_load(Path(a.thresholds).read_text()))
    print(f"{'metric':<24}{'rule':<6}{'value':>10}{'target':>10}  result")
    for metric, kind, value, target, ok in rows:
        shown = "missing" if value is None else value
        print(f"{metric:<24}{kind:<6}{shown!s:>10}{target!s:>10}  {'PASS' if ok else 'FAIL'}")
    failed = [r[0] for r in rows if not r[4]]
    print(f"\n{'QUALITY GATE PASSED' if not failed else 'QUALITY GATE FAILED: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
