"""Replay telemetry: deterministic, realistic logs/events/health per eval case, served via TelemetryBackend.

Why replay instead of live chaos for evals:
  - reproducible (same inputs every run, so differences come from the model/prompt, not the environment),
  - covers all 17 scenarios (most of them belong to services we don't run locally),
  - fast and free of side effects. Live chaos (Phase 1) remains the end-to-end smoke test.
Timestamps are generated relative to "now" so the agent's time-window logic is exercised for real.
"""

import json
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sre_assistant.corpus.generate import Scenario
from sre_assistant.mcp_server.backends.base import (
    HealthState,
    RawEvent,
    ResourceStats,
    ServiceNotFound,
    ServiceRecord,
)


@dataclass
class ServiceTelemetry:
    record: ServiceRecord
    logs: list[tuple[datetime, str]] = field(default_factory=list)
    events: list[RawEvent] = field(default_factory=list)
    health: dict = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds")


class Synth:
    """Builds telemetry for one service. All randomness is seeded per case id."""

    def __init__(self, service: str, seed: str, now: datetime | None = None, environment: str = "production"):
        self.now = now or datetime.now(UTC)
        self.rng = random.Random(seed)
        self.t = ServiceTelemetry(
            ServiceRecord(
                service,
                environment,
                f"{service}-7d9f8c",
                "running",
                "healthy",
                0,
                f"{service}:2.4.1",
                _iso(self.now - timedelta(hours=6)),
                "/srv/config",
            )  # fmt: skip
        )
        self.t.env = {"SERVICE_NAME": service, "ENVIRONMENT": environment, "LOG_FORMAT": "json"}
        self.t.health = {"cpu": 12.0, "mem": 140.0, "limit": 512.0, "streak": 0}

    @property
    def health(self) -> dict:
        return self.t.health

    def log(self, at: datetime, level: str, msg: str, **extra) -> None:
        self.t.logs.append((at, json.dumps({"ts": _iso(at), "level": level, "service": self.t.record.service,
                                            "msg": msg, **extra})))  # fmt: skip

    def raw(self, at: datetime, line: str) -> None:
        if line.lstrip().startswith("{"):
            try:
                d = json.loads(line)
                d["ts"] = _iso(at)
                self.t.logs.append((at, json.dumps(d)))
                return
            except json.JSONDecodeError:
                pass
        clean = re.sub(r"^\d{4}[/-]\d{2}[/-]\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?( UTC)?\s*", "", line)
        self.t.logs.append((at, f"{_iso(at)} {clean}"))

    def background(self, minutes: int = 12, every_s: int = 3) -> None:
        t = self.now - timedelta(minutes=minutes)
        while t < self.now:
            self.log(t, "INFO", "GET /api/v1/items 200", status=200, duration_ms=round(self.rng.uniform(8, 60), 1))
            t += timedelta(seconds=every_s + self.rng.random())

    def incident(self, excerpt: str, start_min: float, end_min: float, repeats: int = 30) -> None:
        lines = [ln for ln in excerpt.strip().splitlines() if ln.strip()]
        span = (start_min - end_min) * 60
        for r in range(repeats):
            at = self.now - timedelta(minutes=start_min) + timedelta(seconds=span * r / max(repeats - 1, 1))
            for i, line in enumerate(lines):
                self.raw(at + timedelta(milliseconds=3 * i), line)

    def memory_growth(self, start_min: float, end_min: float, limit_mb: float) -> None:
        steps = 8
        for i in range(steps):
            at = self.now - timedelta(minutes=start_min) + timedelta(seconds=(start_min - end_min) * 60 * i / steps)
            self.log(at, "INFO", "runtime stats", rss_mb=round(80 + (limit_mb * 1.05 - 80) * i / (steps - 1), 1))

    def event(self, minutes_ago: float, action: str, **attrs) -> None:
        at = (self.now - timedelta(minutes=minutes_ago)).astimezone(UTC).isoformat(timespec="seconds")
        self.t.events.append(RawEvent(at, action, {k: str(v) for k, v in attrs.items()}))

    def restarted(self, minutes_ago: float, count: int = 1) -> None:
        r = self.t.record
        self.t.record = ServiceRecord(r.service, r.environment, r.container, r.status, r.health,
                                      r.restart_count + count, r.image, _iso(self.now - timedelta(minutes=minutes_ago)),
                                      r.config_dir)  # fmt: skip
        self.log(self.now - timedelta(minutes=minutes_ago), "INFO", f"starting {r.service}")

    def build(self) -> ServiceTelemetry:
        self.t.logs.sort(key=lambda x: x[0])
        return self.t


def apply_scenario(sx: Synth, s: Scenario, start_min: float = 6, end_min: float = 2) -> None:
    """Shape telemetry the way this failure mode looks in real life."""
    sx.incident(s.log_excerpt, start_min, end_min, repeats=1 if s.id == "oom-killed" else 30)
    if s.id == "oom-killed":
        sx.memory_growth(start_min, end_min, limit_mb=256)
        sx.health.update(mem=60.0, limit=256.0)
        sx.event(end_min, "oom")
        sx.event(end_min, "die", exitCode=137)
        sx.event(end_min - 0.02, "start")
        sx.restarted(end_min - 0.02)
    elif s.id == "crashloop-missing-env":
        for k in range(5):
            sx.event(start_min - k, "die", exitCode=1)
            sx.event(start_min - k - 0.1, "start")
        sx.restarted(1, count=5)
        sx.t.record.status = "restarting"
    elif s.id == "liveness-probe-dependency":
        for k in range(3):
            sx.event(start_min - k, "kill", signal=15)
            sx.event(start_min - k, "die", exitCode=143)
            sx.event(start_min - k - 0.1, "start")
        sx.restarted(start_min - 2.1, count=3)
        sx.health.update(streak=3)
    elif s.id == "disk-full":
        sx.event(end_min, "die", exitCode=1)
        sx.t.record.status = "exited"
    elif s.id == "image-pull-backoff":
        sx.t.record.status = "created"
        sx.t.record.health = None


class ReplayBackend:
    """TelemetryBackend over pre-built ServiceTelemetry (read-only; restarts are refused in evals)."""

    def __init__(self, services: list[ServiceTelemetry]):
        self.svc = {t.record.service: t for t in services}

    def _get(self, service: str) -> ServiceTelemetry:
        if service not in self.svc:
            raise ServiceNotFound(service, sorted(self.svc))
        return self.svc[service]

    def list_services(self, environment=None):
        return [t.record for t in self.svc.values() if environment in (None, t.record.environment)]

    def get_logs(self, service, tail, since_minutes=None):
        lines = self._get(service).logs
        if since_minutes:
            cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)
            lines = [x for x in lines if x[0] >= cutoff]
        return [line for _, line in lines][-tail:]

    def get_health(self, service):
        t = self._get(service)
        h = t.health
        probes = [{"end": t.record.started_at, "exit_code": 0 if h["streak"] == 0 else 1,
                   "output": "" if h["streak"] == 0 else "HTTP 503"}]  # fmt: skip
        return HealthState(t.record, False, 0, h["streak"], probes, ["8080/tcp"],
                           ResourceStats(h["cpu"], h["mem"], h["limit"]))  # fmt: skip

    def get_events(self, service, since_minutes):
        cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)
        return [e for e in self._get(service).events if datetime.fromisoformat(e.time) >= cutoff]

    def get_env(self, service):
        return dict(self._get(service).env)

    def config_dir(self, service):
        return self._get(service).record.config_dir

    def list_config_files(self, service):
        return sorted(self._get(service).files)

    def read_file(self, service, path, max_bytes):
        files = self._get(service).files
        name = path.rsplit("/", 1)[-1]
        if name not in files:
            raise FileNotFoundError(path)
        return files[name][:max_bytes]

    def restart(self, service):
        raise PermissionError("remediation is disabled during evaluation")
