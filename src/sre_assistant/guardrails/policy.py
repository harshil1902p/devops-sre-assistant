"""Output policy: recommendations that can destroy data or disrupt service must be explicitly gated.

Deterministic post-processing of the model's Diagnosis: any mitigation/fix matching a dangerous pattern that is
not already labelled "requires approval" gets the label appended and a policy flag is reported.
"""

import re

DANGEROUS: list[tuple[str, re.Pattern[str]]] = [
    ("data deletion", re.compile(r"\b(rm\s+-rf|drop\s+(table|database|schema)|truncate\s+table|delete\s+from\s+\w+\s*;?$)", re.I)),
    ("cache wipe", re.compile(r"\bflush(all|db)\b", re.I)),
    ("resource deletion", re.compile(r"\b(kubectl|oc)\s+delete\b|\baz\s+\w+\s+delete\b|\bdocker\s+(rm|system\s+prune)\b", re.I)),
    ("session termination", re.compile(r"\bpg_terminate_backend\b|\bclient\s+kill\b|\bterminate\b[^.]{0,30}\bsessions?\b", re.I)),
    ("restart/scale", re.compile(r"\b(restart|rollout\s+restart|rollback|roll\s+back|scale|redeploy|failover)\b", re.I)),
]  # fmt: skip

LABEL = "(requires approval)"


def enforce(items: list[str]) -> tuple[list[str], list[str]]:
    """Return (items with approval labels enforced, policy flags)."""
    out, flags = [], []
    for item in items:
        hits = [name for name, p in DANGEROUS if p.search(item)]
        if hits and "requires approval" not in item.lower():
            out.append(f"{item} {LABEL}")
            flags.append(f"added approval label ({', '.join(hits)}): {item[:80]}")
        else:
            out.append(item)
    return out, flags
