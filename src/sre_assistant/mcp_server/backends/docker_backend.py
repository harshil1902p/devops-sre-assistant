"""Docker Engine backend (read-only).

Discovery is LABEL-SCOPED: only containers carrying `sre.service=<name>` are visible. The assistant
cannot read logs of arbitrary containers on the host (e.g. its own vector DB): least privilege.

Production note: the Docker socket is root-equivalent. Expose it through a read-only socket proxy
(e.g. tecnativa/docker-socket-proxy with CONTAINERS=1, EVENTS=1, POST=0), never mount it raw.
"""

import io
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import docker
import psycopg
from docker.errors import NotFound
from docker.models.containers import Container

from sre_assistant.mcp_server.backends.base import (
    HealthState,
    RawEvent,
    ResourceStats,
    ServiceNotFound,
    ServiceRecord,
)

SERVICE_LABEL = "sre.service"
ENV_LABEL = "sre.environment"
CONFIG_DIR_LABEL = "sre.config_dir"
INTERESTING_EVENTS = ("oom", "die", "kill", "start", "restart", "stop", "health_status", "pause", "unpause")


def resolve_docker_host(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if env := os.environ.get("DOCKER_HOST"):
        return env
    colima = Path.home() / ".colima" / "default" / "docker.sock"
    if colima.exists():
        return f"unix://{colima}"
    return "unix:///var/run/docker.sock"


def merge_events(persisted: list[RawEvent], live: list[RawEvent]) -> list[RawEvent]:
    """Union of collector history and live daemon events, de-duplicated on (second, action), time-ordered."""
    merged = {(e.time[:19], e.action): e for e in [*persisted, *live]}
    return sorted(merged.values(), key=lambda e: e.time)


class DockerBackend:
    def __init__(self, base_url: str | None = None, event_store_dsn: str | None = None):
        self.client = docker.DockerClient(base_url=resolve_docker_host(base_url), timeout=15)
        self.event_store_dsn = event_store_dsn

    # ---------- discovery ----------
    def _containers(self, environment: str | None = None) -> list[Container]:
        filters = {"label": [SERVICE_LABEL] + ([f"{ENV_LABEL}={environment}"] if environment else [])}
        return self.client.containers.list(all=True, filters=filters)

    def _record(self, c: Container) -> ServiceRecord:
        state = c.attrs["State"]
        return ServiceRecord(
            service=c.labels[SERVICE_LABEL],
            environment=c.labels.get(ENV_LABEL, "unknown"),
            container=c.name,
            status=c.status,
            health=(state.get("Health") or {}).get("Status"),
            restart_count=c.attrs.get("RestartCount", 0),
            image=(c.image.tags or [c.attrs["Config"]["Image"]])[0] if c.image else c.attrs["Config"]["Image"],
            started_at=state.get("StartedAt"),
            config_dir=c.labels.get(CONFIG_DIR_LABEL),
        )

    def _find(self, service: str) -> Container:
        containers = self._containers()
        matches = [c for c in containers if c.labels.get(SERVICE_LABEL) == service]
        if not matches:
            raise ServiceNotFound(service, sorted({c.labels[SERVICE_LABEL] for c in containers}))
        running = [c for c in matches if c.status == "running"]
        return (running or matches)[0]

    def list_services(self, environment: str | None = None) -> list[ServiceRecord]:
        return sorted((self._record(c) for c in self._containers(environment)), key=lambda r: r.service)

    # ---------- logs ----------
    def get_logs(self, service: str, tail: int, since_minutes: int | None = None) -> list[str]:
        c = self._find(service)
        kwargs = {"tail": tail, "stdout": True, "stderr": True, "timestamps": False}
        if since_minutes:
            kwargs["since"] = datetime.now(UTC) - timedelta(minutes=since_minutes)
        return c.logs(**kwargs).decode("utf-8", errors="replace").splitlines()

    # ---------- health ----------
    def get_health(self, service: str) -> HealthState:
        c = self._find(service)
        c.reload()
        state = c.attrs["State"]
        health = state.get("Health") or {}
        ports: list[str] = []
        for container_port, bindings in (c.attrs["NetworkSettings"].get("Ports") or {}).items():
            if not bindings:
                ports.append(container_port)
            else:
                ports.extend(f"{container_port} -> {b['HostIp']}:{b['HostPort']}" for b in bindings)
        return HealthState(
            record=self._record(c),
            oom_killed=bool(state.get("OOMKilled")),
            exit_code=state.get("ExitCode"),
            failing_streak=health.get("FailingStreak"),
            probes=[
                {"end": p.get("End"), "exit_code": p.get("ExitCode"), "output": (p.get("Output") or "").strip()[:300]}
                for p in (health.get("Log") or [])[-3:]
            ],
            ports=ports,
            stats=self._stats(c) if c.status == "running" else ResourceStats(None, None, None),
        )

    @staticmethod
    def _stats(c: Container) -> ResourceStats:
        s = c.stats(stream=False)
        cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
        cpu_pct = round(cpu_delta / sys_delta * ncpu * 100, 2) if sys_delta > 0 and cpu_delta >= 0 else None
        mem = s.get("memory_stats", {})
        # Working set = usage minus reclaimable page cache (what the OOM killer actually compares to the limit).
        cache = mem.get("stats", {}).get("inactive_file", mem.get("stats", {}).get("total_inactive_file", 0))
        usage = mem.get("usage")
        limit = mem.get("limit")
        return ResourceStats(
            cpu_percent=cpu_pct,
            memory_usage_mb=round((usage - cache) / 2**20, 1) if usage else None,
            memory_limit_mb=round(limit / 2**20, 1) if limit else None,
        )

    # ---------- events ----------
    def get_events(self, service: str, since_minutes: int) -> list[RawEvent]:
        c = self._find(service)
        return merge_events(self._persisted_events(service, since_minutes), self._live_events(c, since_minutes))

    def _persisted_events(self, service: str, since_minutes: int) -> list[RawEvent]:
        """History from the event collector (survives the daemon's in-memory ring buffer and container restarts)."""
        if not self.event_store_dsn:
            return []
        try:
            with psycopg.connect(self.event_store_dsn, connect_timeout=3) as conn:
                rows = conn.execute(
                    "SELECT time, action, attributes FROM container_events "
                    "WHERE service = %s AND time >= now() - make_interval(mins => %s) ORDER BY time",
                    (service, since_minutes),
                ).fetchall()
        except psycopg.Error:
            return []  # degrade gracefully to live events only
        return [RawEvent(t.astimezone(UTC).isoformat(timespec="seconds"), a, dict(attrs)) for t, a, attrs in rows]

    def _live_events(self, c: Container, since_minutes: int) -> list[RawEvent]:
        now = datetime.now(UTC)
        stream = self.client.events(
            since=now - timedelta(minutes=since_minutes),
            until=now,  # without `until` the call blocks forever waiting for new events
            filters={"type": "container", "container": c.id},
            decode=True,
        )
        events = []
        for e in stream:
            action = e.get("Action", "")
            if not action.startswith(INTERESTING_EVENTS):
                continue
            attrs = e.get("Actor", {}).get("Attributes", {})
            keep = {k: v for k, v in attrs.items() if k in ("exitCode", "signal", "name")}
            ts = datetime.fromtimestamp(e.get("timeNano", 0) / 1e9, UTC).isoformat(timespec="seconds")
            events.append(RawEvent(time=ts, action=action, attributes=keep))
        return events

    # ---------- config ----------
    def get_env(self, service: str) -> dict[str, str]:
        env = self._find(service).attrs["Config"].get("Env") or []
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in env)

    def list_config_files(self, service: str) -> list[str]:
        c = self._find(service)
        root = c.labels.get(CONFIG_DIR_LABEL)
        if not root:
            return []
        try:
            stream, _ = c.get_archive(root)
        except NotFound:
            return []
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
            base = os.path.basename(root.rstrip("/"))
            return sorted(m.name.removeprefix(base + "/") for m in tar.getmembers() if m.isfile())

    def read_file(self, service: str, path: str, max_bytes: int) -> str:
        c = self._find(service)
        stream, stat = c.get_archive(path)
        if stat.get("size", 0) > max_bytes:
            raise ValueError(f"{path} is {stat['size']} bytes; limit is {max_bytes}")
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
            member = next(m for m in tar.getmembers() if m.isfile())
            return tar.extractfile(member).read(max_bytes).decode("utf-8", errors="replace")

    # ---------- remediation (only reachable through the approval-gated tool) ----------
    def restart(self, service: str) -> ServiceRecord:
        c = self._find(service)
        c.restart(timeout=10)  # SIGTERM, then SIGKILL after 10 s
        c.reload()
        return self._record(c)

    def config_dir(self, service: str) -> str | None:
        return self._find(service).labels.get(CONFIG_DIR_LABEL)
