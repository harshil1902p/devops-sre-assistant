"""Phase 7 unit tests: pure functions + fakes, no Azure calls."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from sre_assistant.config import Settings, load_key_vault_secrets
from sre_assistant.mcp_server.backends.azure_monitor import AzureMonitorBackend, kql_string, logs_query
from sre_assistant.mcp_server.backends.base import HealthState, RawEvent, ResourceStats, ServiceNotFound, ServiceRecord
from sre_assistant.rag.azure_search import AzureSearchRetriever, doc_key, odata_filter
from sre_assistant.shipping import event_rows, health_row, log_rows, split_ts, to_iso_micro

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- AI Search
def test_doc_key_is_url_safe_and_reversible():
    key = doc_key("RB-AUTH-POOL#root-cause/0")
    assert all(c.isalnum() or c in "-_=" for c in key)
    assert key != doc_key("RB-AUTH-POOL#root-cause/1")


def test_odata_filter_whitelists_fields_and_escapes_values():
    assert odata_filter(None) is None
    f = odata_filter({"service": "auth-service", "environment": ["staging", "o'brien"]})
    assert f == "search.in(environment, 'staging|o''brien', '|') and search.in(service, 'auth-service', '|')"
    with pytest.raises(ValueError, match="unsupported filter"):
        odata_filter({"content": "x"})
    with pytest.raises(ValueError):
        odata_filter({"service": "a|b"})


class FakeSearch:
    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def search(self, **kw):
        self.calls.append(kw)
        return self.hits


class FakeEmb:
    def embed_query(self, q):
        return [0.1, 0.2]


def _hit(cid, pid, score, rr=None):
    return {"chunk_id": cid, "parent_id": pid, "doc_id": pid.split("#")[0], "doc_type": "runbook", "title": "t",
            "section": "Fix", "content": f"child {cid}", "parent_content": f"parent {pid}", "parent_tokens": 100,
            "created": "2026-01-01", "service": "auth-service", "environment": "staging", "infrastructure": "docker",
            "error_type": "TimeoutError", "scenario": "s", "@search.score": score, "@search.reranker_score": rr}  # fmt: skip


def _settings(**kw):
    return Settings(retriever_backend="azure_search", azure_search_endpoint="https://x", **kw)


def test_retriever_groups_children_by_parent_small_to_big():
    hits = [_hit("A#1/0", "A#1", 0.9), _hit("A#1/1", "A#1", 0.8), _hit("B#2/0", "B#2", 0.7)]
    client = FakeSearch(hits)
    r = AzureSearchRetriever(_settings(azure_search_rerank="none"), FakeEmb(), None, client).retrieve(
        "QueuePool overflow", filters={"service": "auth-service"}, top_parents=2
    )
    assert [s.parent_id for s in r.sections] == ["A#1", "B#2"]
    assert r.sections[0].content == "parent A#1"  # the LLM reads the parent, not the matched child
    call = client.calls[0]
    assert call["search_text"] == "QueuePool overflow" and call["vector_queries"]  # one hybrid request
    assert "search.in(service" in call["filter"]


def test_retriever_semantic_mode_orders_by_reranker_score():
    hits = [_hit("A#1/0", "A#1", 0.9, rr=1.0), _hit("B#2/0", "B#2", 0.5, rr=3.2)]
    client = FakeSearch(hits)
    r = AzureSearchRetriever(_settings(azure_search_rerank="semantic"), FakeEmb(), None, client).retrieve("q")
    assert r.sections[0].parent_id == "B#2" and r.reranked
    assert client.calls[0]["query_type"] == "semantic"


# ------------------------------------------------------------ Azure Monitor
@pytest.mark.parametrize("bad", ["auth' | drop", "a b", "", "x" * 80, "auth\nservice"])
def test_kql_names_are_allowlisted(bad):
    with pytest.raises(ValueError):
        kql_string(bad)


def test_logs_query_clamps_tail():
    assert "top 5000 by" in logs_query("auth-service", 10**9)
    assert "top 1 by" in logs_query("auth-service", 0)


class FakeLogs:
    def __init__(self, tables_by_prefix):
        self.tables_by_prefix, self.queries = tables_by_prefix, []

    def query_workspace(self, ws, kql, timespan):
        self.queries.append((kql, timespan))
        for prefix, (cols, rows) in self.tables_by_prefix.items():
            if kql.startswith(prefix):
                return SimpleNamespace(tables=[SimpleNamespace(columns=cols, rows=rows)])
        return SimpleNamespace(tables=[])


HEALTH_COLS = ["Service", "TimeGenerated", "Environment", "Container", "Status", "Health", "RestartCount", "Image",
               "StartedAt", "OomKilled", "ExitCode", "FailingStreak", "Probes", "Ports", "CpuPercent",
               "MemoryUsageMb", "MemoryLimitMb"]  # fmt: skip


def _health_row(age_min):
    return ["auth-service", NOW - timedelta(minutes=age_min), "staging", "c1", "running", "healthy", 2, "img", "",
            False, 0, 0, '[{"exit_code": 0}]', '["8000/tcp"]', 12.5, 100.0, 256.0]  # fmt: skip


def test_monitor_backend_health_and_staleness():
    fake = FakeLogs({"SreServiceHealth_CL": (HEALTH_COLS, [_health_row(1)])})
    b = AzureMonitorBackend("ws", client=fake, now=lambda: NOW)
    h = b.get_health("auth-service")
    assert h.record.status == "running" and h.record.restart_count == 2
    assert h.probes == [{"exit_code": 0}] and h.ports == ["8000/tcp"]  # dynamic columns arrive as JSON text
    assert h.stats.memory_limit_mb == 256.0

    stale = AzureMonitorBackend("ws", FakeLogs({"SreServiceHealth_CL": (HEALTH_COLS, [_health_row(10)])}), lambda: NOW)
    assert "stale" in stale.list_services()[0].status


def test_monitor_backend_unknown_service_lists_known_ones():
    b = AzureMonitorBackend("ws", FakeLogs({"SreServiceHealth_CL": (HEALTH_COLS, [_health_row(1)])}), lambda: NOW)
    with pytest.raises(ServiceNotFound, match="auth-service"):
        b.get_logs("billing", 10)
    with pytest.raises(ServiceNotFound):
        b.get_logs("x'; drop", 10)  # never reaches KQL


def test_monitor_backend_logs_and_read_only():
    fake = FakeLogs({"SreServiceHealth_CL": (HEALTH_COLS, [_health_row(1)]),
                     "SreServiceLogs_CL": (["Line"], [["a"], ["b"]])})  # fmt: skip
    b = AzureMonitorBackend("ws", fake, lambda: NOW)
    assert b.get_logs("auth-service", 50, since_minutes=15) == ["a", "b"]
    assert fake.queries[-1][1] == timedelta(minutes=15)
    assert b.get_env("auth-service") == {} and b.config_dir("auth-service") is None
    with pytest.raises(PermissionError):
        b.restart("auth-service")


# ------------------------------------------------------------------ shipper
REC = ServiceRecord("auth-service", "staging", "c1", "running", "healthy", 0, "img", None)


def test_split_ts_and_iso():
    ts, text = split_ts("2026-10-20T23:59:01.123456789Z hello world")
    assert ts == "2026-10-20T23:59:01.123456789Z" and text == "hello world"
    assert split_ts("no timestamp here") == (None, "no timestamp here")
    assert to_iso_micro(ts) == "2026-10-20T23:59:01.123456Z"


def test_log_rows_cursor_and_edge_redaction():
    lines = ["2026-10-20T10:00:00.000000001Z old", "2026-10-20T10:00:01.000000000Z password=hunter2 failed",
             "2026-10-20T10:00:02.000000000Z ok"]  # fmt: skip
    rows, last = log_rows(REC, lines, after="2026-10-20T10:00:00.000000001Z")
    assert [r["Line"] for r in rows][1] == "ok" and len(rows) == 2  # cursor skips already-shipped line
    assert "hunter2" not in rows[0]["Line"]  # secrets never leave the host
    assert last == "2026-10-20T10:00:02.000000000Z"
    assert log_rows(REC, lines, after=last) == ([], last)


def test_event_rows_and_health_row():
    ev = [RawEvent("2026-10-20T10:00:00+00:00", "die", {"exitCode": "137"}),
          RawEvent("2026-10-20T10:01:00+00:00", "start", {})]  # fmt: skip
    rows, last = event_rows("auth-service", ev, after="2026-10-20T10:00:00+00:00")
    assert [r["Action"] for r in rows] == ["start"] and last == "2026-10-20T10:01:00+00:00"
    h = HealthState(REC, True, 137, 3, [{"output": "token=abc123secret"}], ["8000/tcp"], ResourceStats(1.0, 2.0, 3.0))
    row = health_row(h, NOW)
    assert row["OomKilled"] is True and row["ExitCode"] == 137 and "abc123secret" not in str(row["Probes"])


# ---------------------------------------------------------------- Key Vault
class FakeVault:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get_secret(self, name):
        from azure.core.exceptions import ResourceNotFoundError

        self.calls.append(name)
        if name not in self.data:
            raise ResourceNotFoundError("nope")
        return SimpleNamespace(value=self.data[name])


def test_key_vault_fills_only_missing_secrets():
    s = Settings(key_vault_url="https://kv", pii_key=SecretStr("local"), approval_secret=None)
    vault = FakeVault({"sre-approval-secret": "from-kv", "sre-pii-key": "should-not-be-used"})
    out = load_key_vault_secrets(s, vault)
    assert out.pii_key.get_secret_value() == "local"  # explicit local value wins
    assert out.approval_secret.get_secret_value() == "from-kv"
    assert vault.calls == ["sre-approval-secret"]


def test_key_vault_disabled_without_url():
    s = Settings(key_vault_url=None, pii_key=None)
    assert load_key_vault_secrets(s, FakeVault({})) is s
