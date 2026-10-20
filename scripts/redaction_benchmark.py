"""Redaction / pseudonymisation benchmark with PLANTED values (ground truth known by construction).

    uv run python scripts/redaction_benchmark.py      # prints a table + writes eval/results/redaction.json

recall(type)   = planted values of that type that no longer appear in the sanitised output
preserved      = negative-control lines (operational text, no PII) that come out byte-identical
"""

import json
import random
import statistics
import time
from pathlib import Path

from sre_assistant.guardrails.pii import PIIScrubber
from sre_assistant.guardrails.sanitize import Sanitizer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "results" / "redaction.json"
rng = random.Random(42)

NAMES = ["Maria Garcia", "Priya Patel", "Wei Zhang", "Oluwaseun Adeyemi", "Aisha Khan", "Liam O'Brien",
         "Sofia Rossi", "Hiroshi Tanaka", "Fatima Al-Sayed", "Jean-Luc Tremblay", "Anjali Sharma", "Carlos Mendoza",
         "Emily Chen", "Mohammed Rahman", "Olga Ivanova", "Kwame Mensah", "Isabella Silva", "Arjun Mehta",
         "Chloe Martin", "Noah Thompson"]  # fmt: skip
DOMAINS = ["example.com", "bank.example.ca", "mail.example.org"]


def luhn_complete(prefix: str) -> str:
    digits = [int(c) for c in prefix]
    total = 0
    for i, n in enumerate(reversed(digits)):
        if i % 2 == 0:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return prefix + str((10 - total % 10) % 10)


def card() -> str:
    n = luhn_complete("4" + "".join(str(rng.randint(0, 9)) for _ in range(14)))
    return " ".join(n[i : i + 4] for i in range(0, 16, 4)) if rng.random() < 0.5 else n


def sin() -> str:
    n = luhn_complete(str(rng.randint(1, 7)) + "".join(str(rng.randint(0, 9)) for _ in range(7)))
    return f"{n[:3]}-{n[3:6]}-{n[6:]}" if rng.random() < 0.5 else f"{n[:3]} {n[3:6]} {n[6:]}"


def make_cases(n_per_type: int = 40):
    gens = {
        "EMAIL": lambda: f"{rng.choice(NAMES).split()[0].lower()}.{rng.randint(1, 999)}@{rng.choice(DOMAINS)}",
        "IPv4": lambda: ".".join(str(x) for x in (rng.choice([203, 198, 10, 172, 192]), rng.randint(0, 255),
                                                   rng.randint(0, 255), rng.randint(1, 254))),
        "IPv6": lambda: f"2001:db8:{rng.randint(0, 65535):x}::{rng.randint(1, 65535):x}",
        "PHONE": lambda: f"{rng.choice(['416', '647', '905', '514'])}-555-{rng.randint(1000, 9999)}",
        "CARD": card,
        "SIN": sin,
        "PERSON": lambda: rng.choice(NAMES),
        "SECRET": lambda: rng.choice([
            f"postgresql://app:{'pw' + str(rng.randint(10**5, 10**6))}x@db:5432/app",
            "Bearer " + "".join(rng.choice("abcdefghijkLMNOP0123456789") for _ in range(32)),
            "api_key=" + "".join(rng.choice("abcdef0123456789") for _ in range(24)),
            "AKIA" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16)),
        ]),
    }  # fmt: skip
    templates = {
        "EMAIL": ['{{"level":"WARNING","msg":"login failed for user {v}"}}', "password reset email sent to {v}"],
        "IPv4": ["connection from {v} refused", '{{"level":"INFO","msg":"POST /login 401","client":"{v}"}}'],
        "IPv6": ["client {v} disconnected", "upstream {v} timed out"],
        "PHONE": ["SMS 2FA code sent to {v}", "callback number {v} recorded"],
        "CARD": ["payment declined for card {v}", '{{"level":"ERROR","msg":"tokenisation failed pan={v}"}}'],
        "SIN": ["KYC check failed for SIN {v}", "customer record updated (sin={v})"],
        "PERSON": ["Password reset requested by {v}", "Ticket INC-2025-0142 assigned to {v} by on-call",
                   "Account unlocked for {v} after verification"],
        "SECRET": ["connecting with {v}", "outbound call header {v}"],
    }  # fmt: skip
    cases = []
    for kind, gen in gens.items():
        for _ in range(n_per_type):
            v = gen()
            cases.append({"kind": kind, "value": v, "text": rng.choice(templates[kind]).format(v=v)})
    return cases


NEGATIVE = [
    "sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 2 reached, connection timed out, timeout 5.00",
    "Back-off restarting failed container orders-worker in pod orders-worker-7d9f8",
    "runtime stats (db pool: Pool size: 5  Connections in pool: 0 Current Overflow: 2)",
    "GET /healthz 200 duration_ms=1.2", "Liveness probe failed: HTTP probe failed with statuscode: 503",
    "listening on 0.0.0.0:8080", "redis.exceptions.TimeoutError: Timeout reading from socket",
    "order 1234 5678 9012 3456 shipped", "trace_id=4bf92f3577b34da6a3ce929d0e0e4736 span=00f067aa0ba902b7",
    "Deployed version 2.31.0 at 14:02:00", "Kafka consumer group orders-worker rebalanced",
    "pool_size=20 max_overflow=10",
    "Exit Code: 137 Reason: OOMKilled", "PANIC: could not write to file pg_wal/xlogtemp.212",
    "Started server process [1]", "Using Azure OpenAI deployment gpt-4.1-mini in Canada East",
]  # fmt: skip


def run(use_ner: bool, cases, negatives):
    s = Sanitizer(PIIScrubber(key=b"k" * 32, use_ner=use_ner))
    texts = [c["text"] for c in cases] + negatives
    t = time.perf_counter()
    out, _ = s.sanitize_many(texts)
    secs = time.perf_counter() - t
    per_type: dict[str, list[int]] = {}
    misses: dict[str, list[str]] = {}
    for c, o in zip(cases, out[: len(cases)], strict=True):
        hit = int(c["value"] not in o)
        per_type.setdefault(c["kind"], []).append(hit)
        if not hit:
            misses.setdefault(c["kind"], []).append(c["value"])
    changed = [n for n, o in zip(negatives, out[len(cases) :], strict=True) if n != o]
    return {
        "ner": use_ner,
        "recall": {k: round(statistics.mean(v), 3) for k, v in per_type.items()},
        "overall_recall": round(statistics.mean(x for v in per_type.values() for x in v), 3),
        "negatives_preserved": round(1 - len(changed) / len(negatives), 3),
        "false_positives": changed,
        "lines_per_second": round(len(texts) / secs),
        "misses": {k: sorted(set(v))[:5] for k, v in misses.items()},
    }


def main() -> None:
    cases = make_cases()
    results = [run(False, cases, NEGATIVE), run(True, cases, NEGATIVE)]
    kinds = list(results[0]["recall"])
    print(f"\n{len(cases)} planted values ({len(kinds)} types x 40) + {len(NEGATIVE)} negative-control lines\n")
    header = "".join(f"{'NER ' + ('on' if r['ner'] else 'off'):>12}" for r in results)
    print(f"{'type':<8}{header}")
    for k in kinds:
        print(f"{k:<8}" + "".join(f"{r['recall'][k]:>12.2f}" for r in results))
    print(f"{'ALL':<8}" + "".join(f"{r['overall_recall']:>12.2f}" for r in results))
    print(f"{'neg ok':<8}" + "".join(f"{r['negatives_preserved']:>12.2f}" for r in results))
    print(f"{'lines/s':<8}" + "".join(f"{r['lines_per_second']:>12}" for r in results))
    for r in results:
        if r["misses"] or r["false_positives"]:
            print(
                f"\nNER {'on' if r['ner'] else 'off'} misses: {r['misses']}\n  false positives: {r['false_positives']}"
            )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"n_planted": len(cases), "n_negative": len(NEGATIVE), "results": results}, indent=2))
    print(f"\nwrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
