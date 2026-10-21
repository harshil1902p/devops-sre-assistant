"""Azure Monitor backend (read-only): the MCP tools query Log Analytics with KQL instead of the Docker socket.

Data path:  services ─▶ shipper (redacts secrets at the edge) ─▶ Logs Ingestion API ─▶ DCR ─▶ custom tables
            SreServiceLogs_CL / SreServiceEvents_CL / SreServiceHealth_CL ─▶ this backend (KQL, Entra ID).

Differences from the Docker backend that the agent must live with (and the tools report):
  - ingestion latency: new lines show up after ~1-3 minutes, so "now" is slightly in the past;
  - health is a periodic snapshot, so it carries its age (stale snapshots are flagged);
  - no environment variables / config files are exported (least data leaves the host), and no restarts.
"""

import json
import re
from datetime import UTC, datetime, timedelta

from sre_assistant.mcp_server.backends.base import (
    HealthState,
    RawEvent,
    ResourceStats,
    ServiceNotFound,
    ServiceRecord,
)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
HEALTH_LOOKBACK = timedelta(hours=1)
STALE_AFTER = timedelta(minutes=3)


def kql_string(value: str) -> str:
    """Only well-formed service names reach KQL (allowlist), then they're quoted. No KQL injection."""
    if not _NAME.match(value):
        raise ValueError(f"invalid name: {value[:40]!r}")
    return f"'{value}'"


def latest_health_query() -> str:
    return "SreServiceHealth_CL | summarize arg_max(TimeGenerated, *) by Service"


def logs_query(service: str, tail: int) -> str:
    tail = max(1, min(int(tail), 5000))
    return (
        f"SreServiceLogs_CL | where Service == {kql_string(service)} "
        f"| top {tail} by TimeGenerated desc | sort by TimeGenerated asc | project Line"
    )


def events_query(service: str) -> str:
    return (
        f"SreServiceEvents_CL | where Service == {kql_string(service)} "
        "| sort by TimeGenerated asc | project TimeGenerated, Action, Attributes"
    )


def _rows(response) -> list[dict]:
    tables = getattr(response, "tables", None) or getattr(response, "partial_data", None) or []
    out = []
    for t in tables:
        cols = [c if isinstance(c, str) else getattr(c, "name", str(c)) for c in t.columns]
        out.extend(dict(zip(cols, row, strict=False)) for row in t.rows)
    return out


def _dyn(v, default):
    """KQL `dynamic` columns may arrive as JSON text."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.astimezone(UTC).isoformat(timespec="seconds") if isinstance(v, datetime) else str(v)


class AzureMonitorBackend:
    def __init__(self, workspace_id: str, client=None, now=lambda: datetime.now(UTC)):
        if client is None:
            from azure.identity import DefaultAzureCredential
            from azure.monitor.query import LogsQueryClient

            client = LogsQueryClient(DefaultAzureCredential(exclude_interactive_browser_credential=True))
        self.client, self.workspace_id, self.now = client, workspace_id, now

    def _query(self, kql: str, minutes: float) -> list[dict]:
        return _rows(self.client.query_workspace(self.workspace_id, kql, timespan=timedelta(minutes=minutes)))

    def _snapshots(self) -> dict[str, dict]:
        rows = self._query(latest_health_query(), HEALTH_LOOKBACK.total_seconds() / 60)
        return {r["Service"]: r for r in rows}

    def _snapshot(self, service: str) -> dict:
        if not _NAME.match(service):
            raise ServiceNotFound(service, [])
        snaps = self._snapshots()
        if service not in snaps:
            raise ServiceNotFound(service, sorted(snaps))
        return snaps[service]

    def _record(self, r: dict) -> ServiceRecord:
        status = r.get("Status") or "unknown"
        ts = r.get("TimeGenerated")
        if isinstance(ts, datetime) and self.now() - ts > STALE_AFTER:
            age = int((self.now() - ts).total_seconds() // 60)
            status = f"{status} (stale: last report {age} min ago, the shipper may be down)"
        return ServiceRecord(
            service=r["Service"], environment=r.get("Environment") or "unknown", container=r.get("Container") or "",
            status=status, health=r.get("Health") or None, restart_count=int(r.get("RestartCount") or 0),
            image=r.get("Image") or "", started_at=r.get("StartedAt") or None, config_dir=None,
        )  # fmt: skip

    def list_services(self, environment: str | None = None) -> list[ServiceRecord]:
        records = [self._record(r) for r in self._snapshots().values()]
        return sorted((r for r in records if not environment or r.environment == environment), key=lambda r: r.service)

    def get_logs(self, service: str, tail: int, since_minutes: int | None = None) -> list[str]:
        self._snapshot(service)  # consistent ServiceNotFound with the list of known services
        return [r["Line"] for r in self._query(logs_query(service, tail), since_minutes or 60)]

    def get_health(self, service: str) -> HealthState:
        r = self._snapshot(service)

        def num(k):
            return float(r[k]) if r.get(k) is not None else None

        return HealthState(
            record=self._record(r),
            oom_killed=bool(r.get("OomKilled")),
            exit_code=int(r["ExitCode"]) if r.get("ExitCode") is not None else None,
            failing_streak=int(r["FailingStreak"]) if r.get("FailingStreak") is not None else None,
            probes=list(_dyn(r.get("Probes"), [])),
            ports=list(_dyn(r.get("Ports"), [])),
            stats=ResourceStats(num("CpuPercent"), num("MemoryUsageMb"), num("MemoryLimitMb")),
        )

    def get_events(self, service: str, since_minutes: int) -> list[RawEvent]:
        self._snapshot(service)
        return [
            RawEvent(time=_iso(r["TimeGenerated"]), action=r["Action"], attributes=dict(_dyn(r.get("Attributes"), {})))
            for r in self._query(events_query(service), since_minutes)
        ]

    # Config never leaves the host in this design; remediation needs a backend with write access.
    def get_env(self, service: str) -> dict[str, str]:
        self._snapshot(service)
        return {}

    def config_dir(self, service: str) -> str | None:
        return None

    def list_config_files(self, service: str) -> list[str]:
        return []

    def read_file(self, service: str, path: str, max_bytes: int) -> str:
        raise PermissionError("config files are not exported to Azure Monitor")

    def restart(self, service: str) -> ServiceRecord:
        raise PermissionError("the Azure Monitor backend is read-only")
