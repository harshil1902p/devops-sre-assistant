"""Telemetry backend interface. Tools depend on this protocol, never on Docker/K8s/Azure SDKs directly.

Implementations: DockerBackend (Phase 3), AzureMonitorBackend (Phase 7), KubernetesBackend (Phase 8).
"""

from dataclasses import dataclass, field
from typing import Protocol


class ServiceNotFound(LookupError):
    def __init__(self, service: str, available: list[str]):
        self.service, self.available = service, available
        super().__init__(f"service '{service}' not found; available services: {', '.join(available) or 'none'}")


@dataclass
class ServiceRecord:
    service: str
    environment: str
    container: str
    status: str
    health: str | None
    restart_count: int
    image: str
    started_at: str | None
    config_dir: str | None = None


@dataclass
class RawEvent:
    time: str
    action: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class ResourceStats:
    cpu_percent: float | None
    memory_usage_mb: float | None
    memory_limit_mb: float | None


@dataclass
class HealthState:
    record: ServiceRecord
    oom_killed: bool
    exit_code: int | None
    failing_streak: int | None
    probes: list[dict]
    ports: list[str]
    stats: ResourceStats


class TelemetryBackend(Protocol):
    def list_services(self, environment: str | None = None) -> list[ServiceRecord]: ...
    def get_logs(self, service: str, tail: int, since_minutes: int | None = None) -> list[str]: ...
    def get_health(self, service: str) -> HealthState: ...
    def get_events(self, service: str, since_minutes: int) -> list[RawEvent]: ...
    def get_env(self, service: str) -> dict[str, str]: ...
    def config_dir(self, service: str) -> str | None: ...
    def list_config_files(self, service: str) -> list[str]: ...
    def read_file(self, service: str, path: str, max_bytes: int) -> str: ...
