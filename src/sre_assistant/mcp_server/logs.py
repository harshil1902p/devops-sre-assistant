"""Turn raw container logs into a compact, LLM-friendly report.

Raw logs are a terrible LLM input: 48 identical 40-line tracebacks burn thousands of tokens and bury
the one line that matters. We:
  1. parse each line (structured JSON first, plain text with a level regex as fallback),
  2. FINGERPRINT lines (normalise *volatile* values: ids, IPs, decimals, long numbers) so identical events group,
     while keeping meaningful small numbers (HTTP status 200 vs 500, "pool size 5"),
  3. errors -> groups with counts, first/last seen, one traceback tail and a clean `search_hint`,
     other lines -> patterns with counts and min/max/last of numeric fields (e.g. rss_mb trend before an OOM),
  4. include only the newest few raw lines and enforce a token budget.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from sre_assistant.guardrails.sanitize import Sanitizer
from sre_assistant.mcp_server.models import ErrorGroup, LogPattern, LogReport, NumericSummary
from sre_assistant.rag.tokens import count_tokens

LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LEVEL_ALIASES = {
    "WARN": "WARNING", "LOG": "INFO", "NOTICE": "INFO", "FATAL": "CRITICAL", "PANIC": "CRITICAL",
    "ERR": "ERROR", "SEVERE": "ERROR", "TRACE": "DEBUG", "STATEMENT": "INFO", "DETAIL": "INFO", "HINT": "INFO",
}  # fmt: skip
_PLAIN_LEVEL = re.compile(
    r"\b(DEBUG|INFO|NOTICE|LOG|WARN(?:ING)?|ERROR|ERR|FATAL|PANIC|CRITICAL|SEVERE|TRACE|STATEMENT|DETAIL|HINT)\b"
)
_PLAIN_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2}| UTC)?)")
_EXC_IN_TEXT = re.compile(r"\b((?:[a-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Exceeded|Detected|Killed))\b:?\s*(.*)")

_NORMALISERS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+\.\d+\b"), "<n>"),  # decimals: durations, timeouts, sizes
    (re.compile(r"\b\d{4,}\b"), "<n>"),  # long integers: pids, txids, ports, counters
]
_NUMERIC_FIELDS = ("duration_ms", "rss_mb", "status")


@dataclass
class LogLine:
    raw: str
    level: str = "INFO"
    ts: str | None = None
    logger: str | None = None
    message: str = ""
    exc_type: str | None = None
    exc_message: str | None = None
    traceback: str | None = None
    extra: dict = field(default_factory=dict)


def normalise_level(level: str | None) -> str:
    lv = (level or "INFO").upper()
    return _LEVEL_ALIASES.get(lv, lv if lv in LEVEL_ORDER else "INFO")


def parse_line(raw: str) -> LogLine:
    raw = raw.rstrip("\n")
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return LogLine(
                raw=raw, level=normalise_level(d.get("level") or d.get("severity")),
                ts=d.get("ts") or d.get("timestamp") or d.get("time"), logger=d.get("logger"),
                message=str(d.get("msg") or d.get("message") or ""), exc_type=d.get("exc_type"),
                exc_message=d.get("exc_message"), traceback=d.get("exc"),
                extra={k: d[k] for k in ("status", "duration_ms", "path", "request_id", "rss_mb") if k in d},
            )  # fmt: skip
        except (json.JSONDecodeError, AttributeError):
            pass
    m = _PLAIN_LEVEL.search(raw)
    ts = _PLAIN_TS.match(raw)
    line = LogLine(
        raw=raw, level=normalise_level(m.group(1) if m else None), ts=ts.group(1) if ts else None, message=raw
    )
    if exc := _EXC_IN_TEXT.search(raw):
        line.exc_type, line.exc_message = exc.group(1), exc.group(2)[:300] or None
    return line


def fingerprint(line: LogLine) -> str:
    """Stable signature for grouping: exception type + normalised message (numbers/ids/IPs replaced)."""
    text = f"{line.exc_type}: {line.exc_message}" if line.exc_type else line.message
    for pattern, repl in _NORMALISERS:
        text = pattern.sub(repl, text)
    return text[:240]


def _render(line: LogLine) -> str:
    parts = [p for p in (line.ts, line.level, line.logger) if p]
    head = " ".join(parts)
    msg = line.message
    if line.exc_type and line.exc_type not in msg:
        msg += f" | {line.exc_type}: {line.exc_message}"
    if line.extra:
        msg += " " + " ".join(f"{k}={v}" for k, v in line.extra.items())
    return f"{head} {msg}"[:400] if head else msg[:400]


def _search_hint(line: LogLine) -> str:
    return f"{line.exc_type}: {line.exc_message}"[:300] if line.exc_type else line.message[:300]


def _tb_tail(tb: str | None, n: int = 12) -> str | None:
    if not tb:
        return None
    lines = tb.strip().splitlines()
    return "\n".join(lines[-n:])


def summarise_logs(
    service: str,
    raw_lines: list[str],
    min_level: str = "DEBUG",
    max_lines: int = 20,
    max_tokens: int = 2500,
    sanitizer: Sanitizer | None = None,
) -> LogReport:
    parsed = [parse_line(r) for r in raw_lines if r.strip()]
    counts = Counter(p.level for p in parsed)

    groups: dict[str, ErrorGroup] = {}
    patterns: dict[str, LogPattern] = {}
    for p in parsed:
        sig = fingerprint(p)
        if LEVEL_ORDER[p.level] >= LEVEL_ORDER["ERROR"] or p.exc_type:
            g = groups.get(sig)
            if g is None:
                groups[sig] = ErrorGroup(
                    signature=sig, level=p.level, exc_type=p.exc_type, search_hint=_search_hint(p),
                    example=_render(p), count=1, first_seen=p.ts, last_seen=p.ts, traceback_tail=_tb_tail(p.traceback),
                )  # fmt: skip
            else:
                g.count += 1
                g.last_seen = p.ts or g.last_seen
            continue
        pat = patterns.get(sig)
        if pat is None:
            pat = patterns[sig] = LogPattern(pattern=sig, level=p.level, count=0, first_seen=p.ts, latest="")
        pat.count += 1
        pat.last_seen, pat.latest = p.ts or pat.last_seen, _render(p)
        for key in _NUMERIC_FIELDS:
            if isinstance(v := p.extra.get(key), int | float):
                n = pat.numeric.get(key)
                pat.numeric[key] = (
                    NumericSummary(min=v, max=v, last=v) if n is None
                    else NumericSummary(min=min(n.min, v), max=max(n.max, v), last=v)
                )  # fmt: skip

    threshold = LEVEL_ORDER[normalise_level(min_level)]
    rendered = [_render(p) for p in parsed if LEVEL_ORDER[p.level] >= threshold][-max_lines:]
    error_groups = sorted(groups.values(), key=lambda g: (-g.count, g.signature))[:10]
    # Frequent patterns show the shape of traffic; RARE ones (startup, shutdown, config loaded) often mark the
    # moment things changed, so always keep the most recent rare patterns too.
    frequent = sorted(patterns.values(), key=lambda x: (-x.count, x.pattern))[:8]
    rare = sorted((x for x in patterns.values() if x.count <= 2 and x not in frequent),
                  key=lambda x: x.last_seen or "", reverse=True)[:6]  # fmt: skip
    top_patterns = frequent + rare

    def size() -> int:
        return count_tokens("\n".join([*(g.model_dump_json() for g in error_groups),
                                        *(x.model_dump_json() for x in top_patterns), *rendered]))  # fmt: skip

    # Budget: drop oldest raw lines first, then rarest patterns, then traceback tails. Error groups stay.
    while size() > max_tokens and rendered:
        rendered.pop(0)
    while size() > max_tokens and len(top_patterns) > 3:
        top_patterns.pop()
    for g in error_groups[3:]:
        if size() <= max_tokens:
            break
        g.traceback_tail = None

    # Everything that will reach the LLM is sanitised in ONE batch (secrets -> PII -> injection).
    sanitizer = sanitizer or Sanitizer()
    fields = [(g, a) for g in error_groups for a in ("signature", "search_hint", "example", "traceback_tail")]
    fields += [(x, a) for x in top_patterns for a in ("pattern", "latest")]
    texts = [getattr(o, a) for o, a in fields] + rendered
    clean, rep = sanitizer.sanitize_many(texts)
    for (o, a), value in zip(fields, clean[: len(fields)], strict=True):
        setattr(o, a, value)
    safe_lines = clean[len(fields) :]
    redactions = rep.redactions

    report = LogReport(
        service=service,
        lines_scanned=len(parsed),
        counts_by_level=dict(sorted(counts.items(), key=lambda kv: -LEVEL_ORDER[kv[0]])),
        error_groups=error_groups,
        patterns=top_patterns,
        lines=safe_lines,
        lines_omitted=max(0, min(max_lines, len(parsed)) - len(safe_lines)),
        redactions=redactions,
        pii_pseudonymised=rep.pii,
        security_flags=rep.injection_flags,
        raw_tokens=count_tokens("\n".join(raw_lines)),
    )
    report.output_tokens = count_tokens(report.model_dump_json())
    return report
