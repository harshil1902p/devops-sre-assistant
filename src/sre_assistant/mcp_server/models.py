"""Typed tool outputs. MCP v2 publishes these as each tool's `outputSchema` and returns them as
`structuredContent`, so clients and LLMs get a machine-checkable contract, not free text."""

from pydantic import BaseModel, Field


class ServiceInfo(BaseModel):
    service: str
    environment: str
    container: str
    status: str
    health: str | None = None
    restart_count: int = 0
    image: str
    started_at: str | None = None


class ServiceList(BaseModel):
    services: list[ServiceInfo]


class ErrorGroup(BaseModel):
    signature: str = Field(description="normalised error fingerprint used for grouping")
    level: str
    exc_type: str | None = None
    search_hint: str = Field(description="clean error text; pass this to search_runbooks")
    example: str
    count: int
    first_seen: str | None = None
    last_seen: str | None = None
    traceback_tail: str | None = None


class NumericSummary(BaseModel):
    min: float
    max: float
    last: float


class LogPattern(BaseModel):
    pattern: str = Field(description="normalised message shared by these lines")
    level: str
    count: int
    first_seen: str | None = None
    last_seen: str | None = None
    latest: str
    numeric: dict[str, NumericSummary] = Field({}, description="min/max/last of numeric fields, e.g. rss_mb")


class LogReport(BaseModel):
    service: str
    lines_scanned: int
    counts_by_level: dict[str, int]
    error_groups: list[ErrorGroup] = Field(description="distinct errors, most frequent first")
    patterns: list[LogPattern] = Field(description="non-error message patterns with counts and numeric trends")
    lines: list[str] = Field(description="most recent log lines at >= min_level (compact, redacted)")
    lines_omitted: int = Field(0, description="older lines dropped to fit the token budget")
    redactions: int = 0
    pii_pseudonymised: dict[str, int] = Field({}, description="PII values replaced by stable tokens, by type")
    security_flags: list[str] = Field([], description="possible prompt injections found in the logs (neutralised)")
    raw_tokens: int = Field(0, description="tokens the raw logs would have cost")
    output_tokens: int = 0


class Event(BaseModel):
    time: str
    action: str
    detail: str | None = None


class EventList(BaseModel):
    service: str
    since_minutes: int
    events: list[Event]
    note: str | None = None


class HealthProbe(BaseModel):
    end: str | None = None
    exit_code: int | None = None
    output: str | None = None


class HealthReport(BaseModel):
    service: str
    container: str
    status: str
    health: str | None = None
    failing_streak: int | None = None
    recent_probes: list[HealthProbe] = []
    restart_count: int = 0
    oom_killed_last_exit: bool = Field(description="Docker state of the CURRENT run; resets on restart. Check events.")
    exit_code: int | None = None
    started_at: str | None = None
    uptime_seconds: int | None = None
    cpu_percent: float | None = None
    memory_usage_mb: float | None = None
    memory_limit_mb: float | None = None
    memory_percent: float | None = None
    ports: list[str] = []
    recent_events: list[Event] = Field([], description="lifecycle events in the last 30 min (oom, die, restart...)")


class ConfigReport(BaseModel):
    service: str
    env: dict[str, str] = Field(description="container environment variables, secrets redacted")
    config_dir: str | None = None
    available_files: list[str] = []
    file: str | None = None
    content: str | None = None
    redactions: int = 0
    precedence: str = Field(
        "Environment variables override config-file values (12-factor). Recommend changing the source that wins.",
        description="how this service resolves configuration",
    )


class RunbookHit(BaseModel):
    citation: str = Field(description="cite exactly as given, e.g. '[RB-AUTH-DB-POOL] Permanent Fix'")
    doc_id: str
    doc_type: str
    title: str
    service: str
    created: str
    score: float
    content: str


class RunbookResults(BaseModel):
    query: str
    results: list[RunbookHit]
    context_tokens: int
    security_flags: list[str] = []


class RemediationResult(BaseModel):
    action: str
    service: str
    status: str
    approved_by: str
    container: str
    restart_count: int
    detail: str
