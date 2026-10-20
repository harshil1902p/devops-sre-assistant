"""Deterministic incident timeline built from triage tool outputs.

LLMs are weak at comparing timestamps scattered across several JSON blobs. Merging events, error groups
and rare log patterns into ONE sorted timeline (in code) lets the model reason about order: e.g. "the
QueuePool errors stopped at 19:56:18, but the OOM kill happened at 19:58:27, so they are separate incidents".
"""

from datetime import UTC, datetime


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
) -> list[str]:
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
        for field, n in (p.get("numeric") or {}).items():
            if field == "rss_mb" and n["max"] > 1.5 * n["min"]:
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
    return header + [f"{t} {text}" for t, text in rows]
