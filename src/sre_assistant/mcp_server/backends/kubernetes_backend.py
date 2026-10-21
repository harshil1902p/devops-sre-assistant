"""Kubernetes backend (read-only): the same MCP tools against a cluster, via the API server.

Least privilege: the ServiceAccount (k8s/rbac.yaml) can only get/list/watch pods, pods/log and events in ONE namespace.
It cannot read Secrets, exec into pods, or change anything; that's enforced by the cluster, not by this code.

Conventions: pods are labelled `app.kubernetes.io/name=<service>` and `sre.io/environment=<env>`.
Kubernetes-specific knowledge the tools surface:
  - CrashLoopBackOff: the *current* container has barely any logs; the cause is in the PREVIOUS container's logs;
  - OOMKilled / exit 137 lives in `lastState.terminated`, not in the current state;
  - events (FailedScheduling, BackOff, Unhealthy, Killing...) are separate objects with a ~1 h TTL.
"""

from datetime import UTC, datetime

from sre_assistant.mcp_server.backends.base import (
    HealthState,
    RawEvent,
    ResourceStats,
    ServiceNotFound,
    ServiceRecord,
)

NAME_LABEL = "app.kubernetes.io/name"
ENV_LABEL = "sre.io/environment"
_MEM_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "K": 1e3, "M": 1e6, "G": 1e9}


def parse_mem_mb(q: str | None) -> float | None:
    if not q:
        return None
    for unit, mult in _MEM_UNITS.items():
        if q.endswith(unit):
            return round(float(q[: -len(unit)]) * mult / 2**20, 1)
    return round(float(q) / 2**20, 1)


def parse_cpu_millicores(q: str | None) -> float | None:
    if not q:
        return None
    if q.endswith("n"):
        return float(q[:-1]) / 1e6
    if q.endswith("m"):
        return float(q[:-1])
    return float(q) * 1000


def _iso(t) -> str | None:
    return t.astimezone(UTC).isoformat(timespec="seconds") if isinstance(t, datetime) else (str(t) if t else None)


def pod_status(pod) -> tuple[str, str | None]:
    """Waiting/terminated reason beats the pod phase (a Running pod can hide CrashLoopBackOff)."""
    for cs in pod.status.container_statuses or []:
        if cs.state and cs.state.waiting and cs.state.waiting.reason:
            return cs.state.waiting.reason, "unhealthy"
        if cs.state and cs.state.terminated and cs.state.terminated.reason:
            return cs.state.terminated.reason, "unhealthy"
    ready = all(cs.ready for cs in pod.status.container_statuses or []) and bool(pod.status.container_statuses)
    return (pod.status.phase or "Unknown"), ("healthy" if ready else "unhealthy")


def pod_record(pod) -> ServiceRecord:
    status, health = pod_status(pod)
    statuses = pod.status.container_statuses or []
    return ServiceRecord(
        service=pod.metadata.labels[NAME_LABEL],
        environment=pod.metadata.labels.get(ENV_LABEL, "unknown"),
        container=pod.metadata.name,
        status=status,
        health=health,
        restart_count=sum(cs.restart_count or 0 for cs in statuses),
        image=pod.spec.containers[0].image,
        started_at=_iso(pod.status.start_time),
    )


def _probe(kind: str, p) -> dict | None:
    if p is None:
        return None
    target = (p.http_get and f"GET {p.http_get.path}:{p.http_get.port}") or (p.tcp_socket and "tcp") or "exec"
    return {"kind": kind, "target": target, "period_s": p.period_seconds, "failure_threshold": p.failure_threshold}


def pod_health(pod, usage: dict | None = None) -> HealthState:
    c = pod.spec.containers[0]
    cs = (pod.status.container_statuses or [None])[0]
    last = cs.last_state.terminated if cs and cs.last_state and cs.last_state.terminated else None
    cur = cs.state.terminated if cs and cs.state and cs.state.terminated else None
    term = cur or last
    limits = (c.resources.limits or {}) if c.resources else {}
    return HealthState(
        record=pod_record(pod),
        oom_killed=bool(term and term.reason == "OOMKilled"),
        exit_code=term.exit_code if term else None,
        failing_streak=None,
        probes=[p for p in (_probe("liveness", c.liveness_probe), _probe("readiness", c.readiness_probe)) if p],
        ports=[f"{p.container_port}/{(p.protocol or 'TCP').lower()}" for p in c.ports or []],
        stats=ResourceStats(
            cpu_percent=None,  # needs a CPU limit/request baseline; usage below is reported in millicores
            memory_usage_mb=parse_mem_mb((usage or {}).get("memory")),
            memory_limit_mb=parse_mem_mb(limits.get("memory")),
        ),
    )


class KubernetesBackend:
    def __init__(self, namespace: str, core=None, custom=None):
        if core is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()  # ServiceAccount token when running in the cluster
            except config.ConfigException:
                config.load_kube_config()  # developer kubeconfig
            core, custom = client.CoreV1Api(), client.CustomObjectsApi()
        self.ns, self.core, self.custom = namespace, core, custom

    def _pods(self, selector: str):
        return self.core.list_namespaced_pod(self.ns, label_selector=selector).items

    def _find(self, service: str):
        pods = self._pods(f"{NAME_LABEL}={service}") if service.replace("-", "").isalnum() else []
        if not pods:
            raise ServiceNotFound(service, sorted({p.metadata.labels[NAME_LABEL] for p in self._pods(NAME_LABEL)}))
        # Prefer the most troubled pod: that's where the incident is.
        return max(pods, key=lambda p: (pod_record(p).health != "healthy", pod_record(p).restart_count))

    def list_services(self, environment: str | None = None) -> list[ServiceRecord]:
        recs = [pod_record(p) for p in self._pods(NAME_LABEL)]
        return sorted((r for r in recs if not environment or r.environment == environment), key=lambda r: r.service)

    def get_logs(self, service: str, tail: int, since_minutes: int | None = None) -> list[str]:
        pod = self._find(service)
        kwargs = {"tail_lines": tail} | ({"since_seconds": since_minutes * 60} if since_minutes else {})
        lines = self.core.read_namespaced_pod_log(pod.metadata.name, self.ns, **kwargs).splitlines()
        if pod_record(pod).restart_count and len(lines) < 20:
            # CrashLoopBackOff: the evidence is in the previous (crashed) container instance.
            prev = self.core.read_namespaced_pod_log(pod.metadata.name, self.ns, tail_lines=tail, previous=True)
            lines = ["--- previous container ---", *prev.splitlines(), "--- current container ---", *lines]
        return lines

    def get_health(self, service: str) -> HealthState:
        pod = self._find(service)
        usage = None
        if self.custom is not None:
            try:
                m = self.custom.get_namespaced_custom_object("metrics.k8s.io", "v1beta1", self.ns, "pods",
                                                             pod.metadata.name)  # fmt: skip
                usage = m["containers"][0]["usage"]
            except Exception:  # noqa: BLE001 - metrics-server is optional
                usage = None
        return pod_health(pod, usage)

    def get_events(self, service: str, since_minutes: int) -> list[RawEvent]:
        pod = self._find(service)
        evs = self.core.list_namespaced_event(self.ns, field_selector=f"involvedObject.name={pod.metadata.name}").items
        cutoff = datetime.now(UTC).timestamp() - since_minutes * 60
        out = []
        for e in evs:
            t = e.last_timestamp or e.event_time or e.first_timestamp
            if isinstance(t, datetime) and t.timestamp() < cutoff:
                continue
            out.append(RawEvent(_iso(t), e.reason or "", {"type": e.type or "", "message": (e.message or "")[:200],
                                                          "count": str(e.count or 1)}))  # fmt: skip
        return sorted(out, key=lambda e: e.time or "")

    def get_env(self, service: str) -> dict[str, str]:
        # Literal values only. valueFrom (Secrets/ConfigMaps) is never resolved: the ServiceAccount can't read Secrets.
        c = self._find(service).spec.containers[0]
        return {e.name: e.value for e in c.env or [] if e.value is not None}

    def config_dir(self, service: str) -> str | None:
        return None

    def list_config_files(self, service: str) -> list[str]:
        return []

    def read_file(self, service: str, path: str, max_bytes: int) -> str:
        raise PermissionError("reading files inside pods requires exec, which this ServiceAccount doesn't have")

    def restart(self, service: str) -> ServiceRecord:
        raise PermissionError("the Kubernetes backend is read-only (no patch/delete permissions)")
