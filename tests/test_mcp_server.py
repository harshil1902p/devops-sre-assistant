"""MCP server contract tests: a real MCP Client talks to the server in-process; the backend is fake."""

import json
from pathlib import Path

import pytest
from mcp.client import Client

from sre_assistant.config import Settings
from sre_assistant.mcp_server.backends.base import (
    HealthState,
    RawEvent,
    ResourceStats,
    ServiceNotFound,
    ServiceRecord,
)
from sre_assistant.mcp_server.server import build_server
from sre_assistant.rag.retriever import RetrievalResult, RetrievedSection

pytestmark = pytest.mark.anyio
FAKE_PW = "fake" + "-pw-" + "0001"
CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus"


@pytest.fixture
def anyio_backend():
    return "asyncio"


REC = ServiceRecord("auth-service", "staging", "auth-1", "running", "healthy", 1, "auth:1", "2025-01-01T00:00:00Z",
                    "/srv/config")  # fmt: skip


class FakeBackend:
    def __init__(self):
        self.files = {"/srv/config/app.yaml": "db:\n  url: postgresql://auth:pw123@db/auth\n  pool_size: 5\n"
                                              "security:\n  jwt_signing_key: abcdef\n"}  # fmt: skip

    def _check(self, service):
        if service != "auth-service":
            raise ServiceNotFound(service, ["auth-service"])

    def list_services(self, environment=None):
        return [REC]

    def get_logs(self, service, tail, since_minutes=None):
        self._check(service)
        err = {"level": "ERROR", "msg": "unhandled", "exc_type": "redis.exceptions.TimeoutError",
               "exc_message": "Timeout reading from socket", "ts": "2025-01-01T00:00:01Z"}  # fmt: skip
        return [json.dumps(err)] * 5 + [json.dumps({"level": "INFO", "msg": "connect redis://u:topsecret@cache"})]

    def get_health(self, service):
        self._check(service)
        return HealthState(REC, False, 0, 0, [{"end": "t", "exit_code": 0, "output": ""}], ["8080/tcp"],
                           ResourceStats(1.5, 128.0, 256.0))  # fmt: skip

    def get_events(self, service, since_minutes):
        self._check(service)
        return [RawEvent("t1", "oom"), RawEvent("t1", "die", {"exitCode": "137"}), RawEvent("t2", "start")]

    def get_env(self, service):
        self._check(service)
        return {
            "DATABASE_URL": "postgresql://auth:pw123@db/auth",
            "API_TOKEN": "xyz",
            "DB_POOL_SIZE": "5",
            "PATH": "/bin",
        }

    def config_dir(self, service):
        return "/srv/config"

    def list_config_files(self, service):
        return ["app.yaml"]

    def read_file(self, service, path, max_bytes):
        return self.files[path]


def _section(doc_id, service="auth-service"):
    return RetrievedSection(doc_id, doc_id, "runbook", f"title {doc_id}", "Root Cause", "content", 10, 0.9, 1, 1, 5.0,
                            {"service": service, "created": "2025-01-01", "scenario": "x", "environment": "any",
                             "infrastructure": "docker", "error_type": "E"})  # fmt: skip


class FakeRetriever:
    def __init__(self):
        self.calls = []

    def retrieve(self, query, filters=None, top_parents=3):
        self.calls.append((query, filters))
        if filters:  # simulate "no docs for this service" -> server must fall back to unfiltered
            return RetrievalResult(query, "hybrid", True, [])
        return RetrievalResult(query, "hybrid", True, [_section("RB-AUTH-REDIS-TIMEOUT")])


@pytest.fixture
def retriever():
    return FakeRetriever()


@pytest.fixture
def server(retriever):
    return build_server(FakeBackend(), lambda: retriever, CORPUS, Settings(_env_file=None, mcp_audit_log=None))


async def test_all_tools_are_read_only_with_output_schemas(server):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"list_services", "inspect_live_logs", "check_system_health", "get_recent_events",
                          "read_env_config", "search_runbooks"}  # fmt: skip
    for t in tools.values():
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert t.output_schema is not None and t.description


async def test_logs_are_grouped_and_redacted(server):
    async with Client(server) as c:
        r = await c.call_tool("inspect_live_logs", {"service_name": "auth-service"})
    sc = r.structured_content
    assert not r.is_error and sc["error_groups"][0]["count"] == 5
    assert sc["error_groups"][0]["search_hint"] == "redis.exceptions.TimeoutError: Timeout reading from socket"
    assert "topsecret" not in json.dumps(sc) and sc["redactions"] >= 1


async def test_unknown_service_returns_actionable_error(server):
    async with Client(server) as c:
        r = await c.call_tool("check_system_health", {"service_name": "payments"})
    assert r.is_error and "available services: auth-service" in r.content[0].text


async def test_invalid_arguments_are_rejected(server):
    async with Client(server) as c:
        bad_name = await c.call_tool("inspect_live_logs", {"service_name": "Auth Service; rm -rf /"})
        too_many = await c.call_tool("inspect_live_logs", {"service_name": "auth-service", "lines_count": 10**6})
    assert bad_name.is_error and too_many.is_error


async def test_health_includes_oom_evidence_from_events(server):
    async with Client(server) as c:
        sc = (await c.call_tool("check_system_health", {"service_name": "auth-service"})).structured_content
    assert sc["memory_percent"] == 50.0
    die = next(e for e in sc["recent_events"] if e["action"] == "die")
    assert "137" in die["detail"] and "OOM" in die["detail"]


async def test_events_note_flags_oom(server):
    async with Client(server) as c:
        sc = (await c.call_tool("get_recent_events", {"service_name": "auth-service"})).structured_content
    assert sc["note"].startswith("OOM kill detected")


async def test_config_is_redacted_and_traversal_blocked(server):
    async with Client(server) as c:
        ok = await c.call_tool("read_env_config", {"service_name": "auth-service", "filepath": "app.yaml"})
        bad = await c.call_tool("read_env_config", {"service_name": "auth-service", "filepath": "../../etc/shadow"})
    sc = ok.structured_content
    blob = json.dumps(sc)
    assert "pw123" not in blob and "abcdef" not in blob and "xyz" not in blob
    assert sc["env"]["DB_POOL_SIZE"] == "5" and "PATH" not in sc["env"] and "pool_size: 5" in sc["content"]
    assert bad.is_error and "outside the allowed config directory" in bad.content[0].text


async def test_search_redacts_query_and_falls_back_when_service_has_no_docs(server, retriever):
    async with Client(server) as c:
        r = await c.call_tool(
            "search_runbooks", {"query": f"timeout on redis://u:{FAKE_PW}@cache", "service": "auth-cache"}
        )
    assert r.structured_content["results"][0]["citation"] == "RB-AUTH-REDIS-TIMEOUT § Root Cause"
    assert all(FAKE_PW not in q for q, _ in retriever.calls)
    assert [f for _, f in retriever.calls] == [{"service": "auth-cache"}, None]


async def test_search_without_knowledge_base_is_a_clean_error():
    srv = build_server(FakeBackend(), None, CORPUS, Settings(_env_file=None, mcp_audit_log=None))
    async with Client(srv) as c:
        r = await c.call_tool("search_runbooks", {"query": "anything"})
    assert r.is_error and "not configured" in r.content[0].text


async def test_runbook_resource_and_prompt(server):
    async with Client(server) as c:
        doc = await c.read_resource("runbook://RB-AUTH-DB-POOL")
        prompt = await c.get_prompt("diagnose_incident", {"service_name": "auth-service", "symptom": "500s"})
        templates = (await c.list_resource_templates()).resource_templates
    assert "QueuePool" in doc.contents[0].text
    assert templates[0].uri_template == "runbook://{doc_id}"
    assert "check_system_health" in prompt.messages[0].content.text
