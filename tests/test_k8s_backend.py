"""Kubernetes backend with fake API objects (no cluster needed)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from sre_assistant.mcp_server.backends.base import ServiceNotFound
from sre_assistant.mcp_server.backends.kubernetes_backend import KubernetesBackend, parse_cpu_millicores, parse_mem_mb

NOW = datetime.now(UTC)


def pod(name, svc, waiting=None, restarts=0, last_term=None, ready=True):
    state = NS(waiting=NS(reason=waiting) if waiting else None, terminated=None)
    cs = NS(ready=ready and not waiting, restart_count=restarts, state=state,
            last_state=NS(terminated=last_term) if last_term else None)  # fmt: skip
    probe = NS(http_get=NS(path="/healthz", port=8000), tcp_socket=None, period_seconds=10, failure_threshold=3)
    container = NS(image="acr/auth:1", resources=NS(limits={"memory": "256Mi"}), liveness_probe=probe,
                   readiness_probe=None, ports=[NS(container_port=8000, protocol="TCP")],
                   env=[NS(name="LOG_LEVEL", value="INFO"), NS(name="DB_PASSWORD", value=None)])  # fmt: skip
    return NS(
        metadata=NS(name=name, labels={"app.kubernetes.io/name": svc, "sre.io/environment": "staging"}),
        status=NS(phase="Running", container_statuses=[cs], start_time=NOW),
        spec=NS(containers=[container]),
    )


class FakeCore:
    def __init__(self, pods, logs=None, prev=None, events=()):
        self.pods, self.logs, self.prev, self.events, self.log_calls = pods, logs or "", prev or "", events, []

    def list_namespaced_pod(self, ns, label_selector):
        if "=" in label_selector:
            svc = label_selector.split("=", 1)[1]
            return NS(items=[p for p in self.pods if p.metadata.labels["app.kubernetes.io/name"] == svc])
        return NS(items=self.pods)

    def read_namespaced_pod_log(self, name, ns, previous=False, **kw):
        self.log_calls.append((name, previous, kw))
        return self.prev if previous else self.logs

    def list_namespaced_event(self, ns, field_selector):
        return NS(items=list(self.events))


def test_quantities():
    assert parse_mem_mb("256Mi") == 256.0 and parse_mem_mb("1Gi") == 1024.0 and parse_mem_mb(None) is None
    assert parse_cpu_millicores("250m") == 250 and parse_cpu_millicores("1") == 1000
    assert parse_cpu_millicores("5000000n") == 5


def test_crashloop_status_beats_running_phase_and_oom_in_last_state():
    oom = NS(reason="OOMKilled", exit_code=137)
    b = KubernetesBackend("staging", FakeCore([pod("auth-1", "auth-service", "CrashLoopBackOff", 4, oom)]), None)
    rec = b.list_services()[0]
    assert rec.status == "CrashLoopBackOff" and rec.health == "unhealthy" and rec.restart_count == 4
    h = b.get_health("auth-service")
    assert h.oom_killed and h.exit_code == 137 and h.stats.memory_limit_mb == 256.0
    assert h.probes == [{"kind": "liveness", "target": "GET /healthz:8000", "period_s": 10, "failure_threshold": 3}]


def test_previous_container_logs_on_crashloop():
    core = FakeCore(
        [pod("auth-1", "auth-service", "CrashLoopBackOff", 3)], logs="starting", prev="KeyError: DATABASE_URL"
    )
    lines = KubernetesBackend("staging", core, None).get_logs("auth-service", 100, 30)
    assert "KeyError: DATABASE_URL" in lines and lines[0] == "--- previous container ---"
    assert core.log_calls[0][2] == {"tail_lines": 100, "since_seconds": 1800}


def test_picks_most_troubled_pod_and_unknown_service():
    core = FakeCore([pod("auth-ok", "auth-service"), pod("auth-bad", "auth-service", "CrashLoopBackOff", 2)])
    b = KubernetesBackend("staging", core, None)
    assert b.get_health("auth-service").record.container == "auth-bad"
    with pytest.raises(ServiceNotFound, match="auth-service"):
        b.get_logs("billing", 10)
    with pytest.raises(ServiceNotFound):
        b.get_logs("x,y=z", 10)  # never becomes a label selector


def test_events_window_env_and_read_only():
    events = [NS(last_timestamp=NOW - timedelta(minutes=5), event_time=None, first_timestamp=None, reason="BackOff",
                 type="Warning", message="Back-off restarting failed container", count=7),
              NS(last_timestamp=NOW - timedelta(hours=3), event_time=None, first_timestamp=None, reason="Pulled",
                 type="Normal", message="old", count=1)]  # fmt: skip
    b = KubernetesBackend("staging", FakeCore([pod("auth-1", "auth-service")], events=events), None)
    ev = b.get_events("auth-service", 30)
    assert [e.action for e in ev] == ["BackOff"] and ev[0].attributes["count"] == "7"
    assert b.get_env("auth-service") == {"LOG_LEVEL": "INFO"}  # secret refs are never resolved
    with pytest.raises(PermissionError):
        b.restart("auth-service")
