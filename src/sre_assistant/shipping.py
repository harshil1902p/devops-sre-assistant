"""Telemetry shipper: Docker (local "staging") ─▶ Azure Monitor Logs Ingestion API ─▶ DCR ─▶ custom tables.

The same job a Fluent Bit / Azure Monitor Agent DaemonSet does in a cluster, small enough to read in one sitting:
  - incremental: per-service cursors (nanosecond Docker timestamps), so each line is shipped once;
  - secrets are redacted at the edge, before anything leaves the host (PII pseudonymisation still happens later,
    in the MCP server, before the LLM; Log Analytics sits in our own tenant/region like the host logs do);
  - health is shipped as a periodic snapshot; the backend flags snapshots that get old.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sre_assistant.guardrails.secrets import redact
from sre_assistant.mcp_server.backends.base import HealthState, RawEvent, ServiceRecord

log = logging.getLogger("sre.shipper")
STREAMS = {"logs": "Custom-SreServiceLogs", "events": "Custom-SreServiceEvents", "health": "Custom-SreServiceHealth"}


def split_ts(line: str) -> tuple[str | None, str]:
    """'2026-10-20T23:59:01.123456789Z msg' -> ('2026-10-20T23:59:01.123456789Z', 'msg')."""
    head, _, rest = line.partition(" ")
    if len(head) >= 20 and head[4] == "-" and head[10] == "T" and head.endswith("Z"):
        return head, rest
    return None, line


def to_iso_micro(docker_ts: str) -> str:
    """Docker gives nanoseconds; Azure wants ISO 8601 (microseconds are plenty)."""
    base, _, frac = docker_ts.rstrip("Z").partition(".")
    return f"{base}.{(frac + '000000')[:6]}Z"


def log_rows(rec: ServiceRecord, lines: list[str], after: str | None) -> tuple[list[dict], str | None]:
    rows, last = [], after
    for line in lines:
        ts, text = split_ts(line)
        if ts is None or (after and ts <= after) or not text.strip():
            continue
        rows.append({"TimeGenerated": to_iso_micro(ts), "Service": rec.service, "Environment": rec.environment,
                     "Container": rec.container, "Line": redact(text)[:16000]})  # fmt: skip
        last = max(last or ts, ts)
    return rows, last


def event_rows(service: str, events: list[RawEvent], after: str | None) -> tuple[list[dict], str | None]:
    fresh = [e for e in events if not after or e.time > after]
    rows = [
        {"TimeGenerated": e.time, "Service": service, "Action": e.action, "Attributes": e.attributes} for e in fresh
    ]
    return rows, max([after or "", *(e.time for e in fresh)]) or None


def health_row(h: HealthState, now: datetime) -> dict:
    r = h.record
    return {
        "TimeGenerated": now.astimezone(UTC).isoformat(),
        "Service": r.service, "Environment": r.environment, "Container": r.container, "Status": r.status,
        "Health": r.health or "", "RestartCount": r.restart_count, "Image": r.image, "StartedAt": r.started_at or "",
        "OomKilled": h.oom_killed, "ExitCode": h.exit_code, "FailingStreak": h.failing_streak,
        "Probes": [{**p, "output": redact(p.get("output") or "")} for p in h.probes], "Ports": h.ports,
        "CpuPercent": h.stats.cpu_percent, "MemoryUsageMb": h.stats.memory_usage_mb,
        "MemoryLimitMb": h.stats.memory_limit_mb,
    }  # fmt: skip


@dataclass
class Cursor:
    logs: str | None = None
    events: str | None = None


@dataclass
class ShipReport:
    logs: int = 0
    events: int = 0
    health: int = 0
    errors: list[str] = field(default_factory=list)


class Shipper:
    def __init__(self, docker_backend, ingestion_client, rule_id: str, backfill_minutes: int = 15):
        self.backend, self.client, self.rule_id = docker_backend, ingestion_client, rule_id
        self.backfill = timedelta(minutes=backfill_minutes)
        self.cursors: dict[str, Cursor] = {}

    def _upload(self, stream: str, rows: list[dict]) -> None:
        if rows:
            # The SDK batches (1 MB) and gzips; errors per batch are raised unless on_error is given.
            self.client.upload(rule_id=self.rule_id, stream_name=STREAMS[stream], logs=json.loads(json.dumps(rows)))

    def run_once(self, now: datetime | None = None) -> ShipReport:
        now = now or datetime.now(UTC)
        report = ShipReport()
        health = []
        for rec in self.backend.list_services():
            cur = self.cursors.setdefault(rec.service, Cursor())
            try:
                since = now - self.backfill if cur.logs is None else None
                container = self.backend._find(rec.service)
                kwargs = {"stdout": True, "stderr": True, "timestamps": True}
                kwargs |= {"since": since} if since else {"since": datetime.fromisoformat(to_iso_micro(cur.logs))}
                lines = container.logs(**kwargs).decode("utf-8", errors="replace").splitlines()
                rows, last_log = log_rows(rec, lines, cur.logs)
                self._upload("logs", rows)

                minutes = int(self.backfill.total_seconds() // 60) if cur.events is None else 10
                ev_rows, last_ev = event_rows(rec.service, self.backend.get_events(rec.service, minutes), cur.events)
                self._upload("events", ev_rows)

                health.append(health_row(self.backend.get_health(rec.service), now))
                cur.logs, cur.events = last_log, last_ev  # advance cursors only after successful uploads
                report.logs += len(rows)
                report.events += len(ev_rows)
            except Exception as e:  # noqa: BLE001 - one bad service must not stop shipping for the others
                report.errors.append(f"{rec.service}: {type(e).__name__}: {str(e)[:150]}")
                log.warning("shipping %s failed: %s", rec.service, e)
        self._upload("health", health)
        report.health = len(health)
        return report

    def state(self) -> dict:
        return {k: asdict(v) for k, v in self.cursors.items()}
