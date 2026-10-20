"""SRE Assistant MCP server.

Primitives exposed:
  TOOLS     (model-controlled actions; all READ-ONLY)
    list_services, inspect_live_logs, check_system_health, get_recent_events, read_env_config, search_runbooks
  RESOURCES (application-controlled data, addressable by URI)
    runbook://{doc_id}
  PROMPTS   (user-controlled templates)
    diagnose_incident

Design rules: least privilege (no write/exec tools, label-scoped discovery, allowlisted config paths),
bounded inputs (pydantic constraints), bounded outputs (token budgets), redaction at the boundary,
actionable errors (ToolError) and an audit record for every call.
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from sre_assistant.config import Settings, get_settings
from sre_assistant.mcp_server.audit import audited
from sre_assistant.mcp_server.backends.base import RawEvent, ServiceNotFound, TelemetryBackend
from sre_assistant.mcp_server.config_files import MAX_CONFIG_BYTES, ConfigAccessError, resolve_config_path
from sre_assistant.mcp_server.logs import summarise_logs
from sre_assistant.mcp_server.models import (
    ConfigReport,
    Event,
    EventList,
    HealthProbe,
    HealthReport,
    LogReport,
    RunbookHit,
    RunbookResults,
    ServiceInfo,
    ServiceList,
)
from sre_assistant.mcp_server.redaction import MASK, is_secret_key, redact, redact_mapping, redact_text

INSTRUCTIONS = """\
Read-only SRE diagnostics for services in the monitored environment.
Recommended workflow when a service misbehaves:
1. check_system_health(service) - state, restarts, memory/CPU and recent lifecycle events (oom, die exit codes).
2. inspect_live_logs(service) - grouped error signatures with counts and traceback tails.
3. get_recent_events(service) - if the container restarted, evidence of the previous run lives here.
4. search_runbooks(<exact error signature>) - past incidents and runbooks; cite results by their `citation`.
5. read_env_config(service, file) - verify configuration values the runbook mentions (secrets are redacted).
Never guess values that a tool can verify. These tools cannot change anything."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

ServiceName = Annotated[
    str,
    Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$", description="service name, e.g. auth-service"),
]

EXIT_CODE_HINTS = {
    0: "clean exit",
    1: "application error (check the last log lines)",
    137: "SIGKILL: killed by the kernel OOM killer or a forced kill (128+9)",
    139: "SIGSEGV: segmentation fault (128+11)",
    143: "SIGTERM: graceful stop requested (128+15)",
}
NOISY_ENV = {"PATH", "LANG", "GPG_KEY", "PYTHON_SHA256", "HOME", "HOSTNAME", "TERM", "PYTHON_VERSION"}


def _event(e: RawEvent) -> Event:
    detail = None
    if "exitCode" in e.attributes:
        code = int(e.attributes["exitCode"])
        detail = f"exitCode={code} ({EXIT_CODE_HINTS.get(code, 'non-zero exit')})"
    elif "signal" in e.attributes:
        detail = f"signal={e.attributes['signal']}"
    if e.action == "oom":
        detail = "container exceeded its memory limit; the kernel OOM killer fired"
    return Event(time=e.time, action=e.action, detail=detail)


def _uptime(started_at: str | None) -> int | None:
    if not started_at or started_at.startswith("0001"):
        return None
    started = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", started_at.replace("Z", "+00:00")))
    return int((datetime.now(UTC) - started).total_seconds())


def build_server(
    backend: TelemetryBackend,
    retriever_factory: Callable[[], object] | None = None,
    corpus_dir: Path | None = None,
    settings: Settings | None = None,
) -> MCPServer:
    s = settings or get_settings()
    srv = MCPServer("sre-assistant", title="SRE Assistant", instructions=INSTRUCTIONS, version="0.3.0")
    lazy: dict[str, object] = {}

    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ServiceNotFound as e:
            raise ToolError(str(e)) from e

    # ------------------------------------------------------------------ tools
    @srv.tool(annotations=READ_ONLY)
    @audited
    def list_services(
        environment: Annotated[str | None, Field(description="filter, e.g. staging")] = None,
    ) -> ServiceList:
        """List monitored services (containers labelled sre.service) with status, health and restart count."""
        records = backend.list_services(environment)
        return ServiceList(services=[ServiceInfo(**{k: v for k, v in r.__dict__.items() if k != "config_dir"})
                                     for r in records])  # fmt: skip

    @srv.tool(annotations=READ_ONLY)
    @audited
    def inspect_live_logs(
        service_name: ServiceName,
        lines_count: Annotated[int, Field(ge=1, le=2000, description="raw lines to scan from the end")] = 300,
        since_minutes: Annotated[int | None, Field(ge=1, le=1440)] = None,
        min_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO",
    ) -> LogReport:
        """Fetch recent logs of a service. Returns: error_groups (distinct errors with counts, first/last seen,
        traceback tail and a `search_hint` to pass to search_runbooks), patterns (other recurring messages with
        counts and numeric trends such as rss_mb or duration_ms) and the newest lines. Token-budgeted, redacted."""
        raw = _call(backend.get_logs, service_name, lines_count, since_minutes)
        return summarise_logs(service_name, raw, min_level, max_lines=20, max_tokens=s.mcp_log_max_tokens)

    @srv.tool(annotations=READ_ONLY)
    @audited
    def check_system_health(service_name: ServiceName) -> HealthReport:
        """Container state, health-check results, restart count, CPU and memory usage vs limit, exposed ports and
        lifecycle events of the last 30 minutes (oom, die with exit code, restarts)."""
        h = _call(backend.get_health, service_name)
        events = [_event(e) for e in _call(backend.get_events, service_name, 30)]
        mem_pct = None
        if h.stats.memory_usage_mb and h.stats.memory_limit_mb:
            mem_pct = round(h.stats.memory_usage_mb / h.stats.memory_limit_mb * 100, 1)
        return HealthReport(
            service=service_name, container=h.record.container, status=h.record.status, health=h.record.health,
            failing_streak=h.failing_streak,
            recent_probes=[HealthProbe(end=p["end"], exit_code=p["exit_code"], output=redact(p["output"] or ""))
                           for p in h.probes],
            restart_count=h.record.restart_count, oom_killed_last_exit=h.oom_killed, exit_code=h.exit_code,
            started_at=h.record.started_at, uptime_seconds=_uptime(h.record.started_at),
            cpu_percent=h.stats.cpu_percent, memory_usage_mb=h.stats.memory_usage_mb,
            memory_limit_mb=h.stats.memory_limit_mb, memory_percent=mem_pct, ports=h.ports, recent_events=events,
        )  # fmt: skip

    @srv.tool(annotations=READ_ONLY)
    @audited
    def get_recent_events(
        service_name: ServiceName,
        since_minutes: Annotated[int, Field(ge=1, le=1440)] = 60,
    ) -> EventList:
        """Container lifecycle events (oom, die + exit code, kill, start, restart, health changes). Use this after a
        restart: the current container state is reset, but events keep the evidence of what killed the last run."""
        events = [_event(e) for e in _call(backend.get_events, service_name, since_minutes)]
        note = None if events else "no lifecycle events in this window"
        if any(e.action == "oom" for e in events):
            note = "OOM kill detected: memory limit exceeded. Check memory trend in logs and the limit in health."
        return EventList(service=service_name, since_minutes=since_minutes, events=events[-50:], note=note)

    @srv.tool(annotations=READ_ONLY)
    @audited
    def read_env_config(
        service_name: ServiceName,
        filepath: Annotated[str | None, Field(max_length=200, description="file relative to the config dir")] = None,
    ) -> ConfigReport:
        """Environment variables of a service and, optionally, one config file from its allowlisted config
        directory (label sre.config_dir). Secret values are always redacted. Lists available files."""
        env = _call(backend.get_env, service_name)
        safe_env, count = {}, 0
        for k, v in sorted(env.items()):
            if k in NOISY_ENV:
                continue
            if is_secret_key(k):
                safe_env[k], count = MASK, count + 1
            else:
                r = redact_text(v)
                safe_env[k], count = r.text, count + r.count
        config_dir = _call(backend.config_dir, service_name)
        report = ConfigReport(service=service_name, env=safe_env, config_dir=config_dir, redactions=count,
                              available_files=_call(backend.list_config_files, service_name))  # fmt: skip
        if filepath:
            if not config_dir:
                raise ToolError(f"{service_name} exposes no config directory (label sre.config_dir)")
            try:
                path = resolve_config_path(config_dir, filepath)
                text = backend.read_file(service_name, path, MAX_CONFIG_BYTES)
            except ConfigAccessError as e:
                raise ToolError(str(e)) from e
            except Exception as e:  # noqa: BLE001 - surface a clean message, no internals
                raise ToolError(f"cannot read {filepath}: {type(e).__name__}") from e
            report.file, report.content, report.redactions = path, *_redact_config(path, text, count)
        return report

    @srv.tool(annotations=READ_ONLY)
    @audited
    def search_runbooks(
        query: Annotated[str, Field(min_length=3, max_length=2000, description="exact error signature or symptoms")],
        service: Annotated[str | None, Field(description="prefer documents about this service")] = None,
        top_k: Annotated[int, Field(ge=1, le=5)] = 3,
    ) -> RunbookResults:
        """Hybrid search (semantic + keyword + rerank) over runbooks, post-mortems and KB threads. Pass the exact
        error signature from the logs for best results. Cite results using their `citation` field."""
        if retriever_factory is None:
            raise ToolError("knowledge base is not configured on this server")
        try:
            retriever = lazy.setdefault("retriever", retriever_factory())
            q = redact(query)
            res = retriever.retrieve(q, filters={"service": service} if service else None, top_parents=top_k)
            if service and not res.sections:
                res = retriever.retrieve(q, top_parents=top_k)
        except Exception as e:  # noqa: BLE001
            lazy.pop("retriever", None)
            raise ToolError(
                f"knowledge base unavailable ({type(e).__name__}); continue diagnosis with live tools"
            ) from e
        return RunbookResults(
            query=q,
            results=[
                RunbookHit(citation=x.citation, doc_id=x.doc_id, doc_type=x.doc_type, title=x.title,
                           service=x.metadata["service"], created=str(x.metadata["created"]),
                           score=round(x.score, 4), content=x.content)
                for x in res.sections
            ],
            context_tokens=res.context_tokens,
        )  # fmt: skip

    # -------------------------------------------------------------- resources
    @srv.resource("runbook://{doc_id}", mime_type="text/markdown", description="Full knowledge-base document by id")
    def runbook(doc_id: str) -> str:
        if not re.fullmatch(r"(RB|PM|KB)-[A-Z0-9-]+", doc_id):
            raise ResourceError(f"invalid document id {doc_id!r}")
        if corpus_dir is None:
            raise ResourceError("corpus not configured")
        if "index" not in lazy:
            lazy["index"] = {p.stem: p for p in corpus_dir.rglob("*.md")}
        path = lazy["index"].get(doc_id)
        if path is None:
            raise ResourceError(f"unknown document {doc_id}")
        return path.read_text()

    # ---------------------------------------------------------------- prompts
    @srv.prompt(title="Diagnose an incident")
    def diagnose_incident(service_name: str, symptom: str = "errors reported") -> str:
        """Standard SRE triage workflow for a misbehaving service."""
        return (
            f"The service `{service_name}` has a problem: {symptom}.\n"
            "Diagnose it using the tools, in this order: check_system_health, inspect_live_logs, "
            "get_recent_events if it restarted, then search_runbooks with the exact error signature, and "
            "read_env_config to verify any setting the runbook mentions.\n"
            "Answer with: 1) what is happening (evidence: quote log lines/metrics), 2) root cause, "
            "3) immediate mitigation, 4) permanent fix, 5) sources (citations). Say 'unknown' instead of guessing."
        )

    return srv


def _redact_config(path: str, text: str, prior: int) -> tuple[str, int]:
    counter = [0]
    try:
        if path.endswith((".yaml", ".yml")):
            data = yaml.safe_load(text)
            return yaml.safe_dump(redact_mapping(data, counter), sort_keys=False), prior + counter[0]
        if path.endswith(".json"):
            data = json.loads(text)
            return json.dumps(redact_mapping(data, counter), indent=2), prior + counter[0]
    except (yaml.YAMLError, json.JSONDecodeError):
        pass
    r = redact_text(text)
    return r.text, prior + r.count
