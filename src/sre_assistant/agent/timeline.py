"""Deterministic incident timeline built from triage tool outputs.

LLMs are weak at comparing timestamps scattered across several JSON blobs. Merging events, error groups
and rare log patterns into ONE sorted timeline (in code) lets the model reason about order: e.g. "the
QueuePool errors stopped at 19:56:18, but the OOM kill happened at 19:58:27, so they are separate incidents".
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Window:
    start: str
    end: str
    signals: list[tuple[str, str]] = field(default_factory=list)  # (kind, description)
    keywords: list[str] = field(default_factory=list)  # any of these in a search query = window investigated

    @property
    def needs_investigation(self) -> bool:
        return any(kind in ("oom", "errors", "memory") for kind, _ in self.signals)

    def label(self) -> str:
        return f"{self.start}-{self.end}: " + ("; ".join(d for _, d in self.signals) or "informational")


def _t(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(UTC).strftime("%H:%M:%S")
    except ValueError:
        return ts[11:19] if len(ts) >= 19 else ts


def build_timeline(
    health: dict | None, logs: dict | None, events: dict | None, now: datetime | None = None
) -> tuple[list[str], list[dict]]:
    """Returns (timeline lines for the prompt, structured incident windows for the coverage guard)."""
    rows: list[tuple[str, str]] = []
    for e in (events or {}).get("events", []):
        if e["action"].startswith("health_status"):
            continue
        rows.append((_t(e["time"]), f"EVENT {e['action']}" + (f" - {e['detail']}" if e.get("detail") else "")))
    for g in (logs or {}).get("error_groups", []):
        span = _t(g.get("first_seen"))
        if g.get("last_seen") and g["last_seen"] != g.get("first_seen"):
            span += f"..{_t(g['last_seen'])}"
        rows.append((_t(g.get("first_seen")), f"ERRORS x{g['count']} ({span}) {g['search_hint'][:140]}"))
    for p in (logs or {}).get("patterns", []):
        if p["count"] <= 2:
            rows.append((_t(p.get("last_seen")), f"LOG (rare) {p['pattern'][:120]}"))
        for metric, n in (p.get("numeric") or {}).items():
            if metric == "rss_mb" and n["max"] > 1.5 * n["min"]:
                rows.append((_t(p.get("last_seen")), f"TREND rss_mb {n['min']}->{n['max']} MB (last {n['last']})"))
    rows.sort(key=lambda r: r[0])
    header = []
    if health:
        mem = f"{health.get('memory_usage_mb')}/{health.get('memory_limit_mb')} MB"
        header.append(
            f"NOW {(now or datetime.now(UTC)).strftime('%H:%M:%S')} UTC: status={health.get('status')} "
            f"health={health.get('health')} restarts={health.get('restart_count')} memory={mem} "
            f"uptime={health.get('uptime_seconds')}s"
        )
    windows = segment_windows(rows)
    if len(windows) > 1:
        header.append(
            f"INCIDENT WINDOWS ({len(windows)} separated by quiet gaps; investigate each, do not assume causality):"
        )
    header += [f"  #{i} {w.label()}" for i, w in enumerate(windows, 1)]
    structured = [{"index": i, "label": w.label(), "keywords": sorted(set(w.keywords)),
                   "needs_investigation": w.needs_investigation} for i, w in enumerate(windows, 1)]  # fmt: skip
    return header + [f"{t} {text}" for t, text in rows], structured


def _secs(hms: str) -> int | None:
    try:
        h, m, s = (int(x) for x in hms.split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return None


def segment_windows(rows: list[tuple[str, str]], gap_seconds: int = 45) -> list[Window]:
    """Group timeline rows into windows separated by > gap_seconds of silence and type their signals."""
    timed = [(sec, t, text) for t, text in rows if (sec := _secs(t)) is not None]
    groups: list[list[tuple[int, str, str]]] = []
    for row in timed:
        if groups and row[0] - groups[-1][-1][0] <= gap_seconds:
            groups[-1].append(row)
        else:
            groups.append([row])

    windows = []
    for g in groups:
        w = Window(start=g[0][1], end=g[-1][1])
        texts = " ".join(r[2] for r in g)
        if "EVENT oom" in texts or "exitCode=137" in texts:
            w.signals.append(("oom", "OOM kill / SIGKILL"))
            w.keywords += ["oom", "137", "memory", "killed"]
        for r in g:
            if r[2].startswith("ERRORS"):
                hint = r[2].split(") ", 1)[-1]
                w.signals.append(("errors", f"error burst: {hint[:70]}"))
                exc = re.findall(r"([A-Za-z_]*(?:Error|Exception|Exceeded|Detected))", hint)
                words = [x for x in re.findall(r"[A-Za-z][A-Za-z_]{5,}", hint.split(":", 1)[-1])][:2]
                w.keywords += [k.lower() for k in [*(e.rsplit(".", 1)[-1] for e in exc), *words]]
        if "TREND rss_mb" in texts:
            w.signals.append(("memory", "memory growth"))
            w.keywords += ["memory", "rss", "oom", "leak"]
        if not w.signals and any(f"EVENT {a}" in texts for a in ("start", "die", "kill")):
            w.signals.append(("restart", "container restart/redeploy"))
        windows.append(w)
    return windows


def uncovered_windows(windows: list[dict], queries: list[str]) -> list[dict]:
    """Windows with significant signals for which no runbook search mentioned any of their keywords."""
    qs = " ".join(queries).lower()
    return [w for w in windows if w["needs_investigation"] and not any(k in qs for k in w["keywords"])]
