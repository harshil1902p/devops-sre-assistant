"""Agent graph tests: scripted LLM + real in-process MCP server (fake telemetry backend). No network, no cost."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from mcp.client import Client

from sre_assistant.agent.context import compact, note
from sre_assistant.agent.diagnosis import Diagnosis, check_grounding
from sre_assistant.agent.graph import build_graph
from sre_assistant.agent.mcp_tools import load_mcp_tools
from sre_assistant.agent.timeline import build_timeline
from sre_assistant.config import Settings
from sre_assistant.mcp_server.server import build_server
from tests.fakes import ScriptedChatModel
from tests.test_mcp_server import CORPUS, FakeBackend, FakeRetriever

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def diag(**over) -> Diagnosis:
    base = dict(
        service="auth-service", status="degraded", summary="Redis stalls cause login timeouts.",
        evidence=["inspect_live_logs: redis.exceptions.TimeoutError x5"], root_cause="Redis blocked by slow command",
        confidence="high", immediate_mitigation=["Stop the offending job (requires approval)"],
        permanent_fix=["Fail-open rate limiter"], citations=["[RB-AUTH-REDIS-TIMEOUT] Root Cause"], separate_issues=[],
        open_questions=[],
    )  # fmt: skip
    return Diagnosis(**(base | over))


def call(name: str, args: dict, i: int = 1) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": name, "args": args}])


async def run_graph(script, question, thread="t1", saver=None, **state):
    server = build_server(FakeBackend(), FakeRetriever, CORPUS, Settings(_env_file=None, mcp_audit_log=None))
    model = ScriptedChatModel(script)
    async with Client(server) as client:
        graph = build_graph(
            model, await load_mcp_tools(client), checkpointer=saver or InMemorySaver(), max_tool_calls=3
        )
        cfg = {"configurable": {"thread_id": thread}}
        result = await graph.ainvoke({"messages": [HumanMessage(question)], **state}, cfg)
    return result, model, graph, cfg


async def test_happy_path_triage_search_respond_and_citation_check():
    script = [
        call(
            "search_runbooks",
            {"query": "redis.exceptions.TimeoutError: Timeout reading from socket", "service": "auth-service"},
        ),  # fmt: skip
        AIMessage("READY"),
        diag(citations=["[RB-AUTH-REDIS-TIMEOUT] Root Cause", "[RB-INVENTED-999] Fix"]),
    ]
    result, model, _, _ = await run_graph(script, "auth-service is throwing 500s")

    first_prompt = model.prompts[0]
    tool_names = [m.name for m in first_prompt if isinstance(m, ToolMessage)]
    assert tool_names == ["check_system_health", "inspect_live_logs", "get_recent_events"], "triage prefetch"
    assert any("TIMELINE for auth-service" in str(m.content) for m in first_prompt)
    assert result["service"] == "auth-service", "service named in question: no intake LLM call"
    assert result["diagnosis"]["citations"] == ["[RB-AUTH-REDIS-TIMEOUT] Root Cause"]
    assert result["grounding"]["invalid_citations"] == ["[RB-INVENTED-999] Fix"]
    assert "Diagnosis: `auth-service` is **degraded**" in result["messages"][-1].content
    assert [c["node"] for c in result["llm_calls"]] == ["agent", "agent", "respond"]


async def test_agent_is_reminded_to_search_before_answering():
    script = [AIMessage("READY"), call("search_runbooks", {"query": "redis timeout"}), AIMessage("READY"), diag()]
    result, model, _, _ = await run_graph(script, "auth-service is slow")
    assert any("must call search_runbooks" in str(m.content) for m in model.prompts[1])
    assert "search_runbooks" in result["tools_used"] and result["grounding"]["warnings"] == []


async def test_duplicate_calls_are_skipped_and_budget_forces_an_answer():
    q = {"query": "redis timeout"}
    script = [call("search_runbooks", q, 1), call("search_runbooks", q, 2),
              call("check_system_health", {"service_name": "auth-service"}, 3),  # duplicate of the triage prefetch
              call("read_env_config", {"service_name": "auth-service"}, 4),
              call("get_recent_events", {"service_name": "auth-service", "since_minutes": 5}, 5), diag()]  # fmt: skip
    result, _, _, _ = await run_graph(script, "auth-service errors")
    skipped = [m for m in result["messages"] if isinstance(m, ToolMessage) and "identical call" in str(m.content)]
    assert [m.name for m in skipped] == ["search_runbooks", "check_system_health"]
    assert result["tool_calls"] == 3 and result["diagnosis"] is not None, "budget reached -> forced final answer"


async def test_parallel_tool_calls_beyond_budget_are_refused():
    many = AIMessage("", tool_calls=[
        {"id": f"p{i}", "name": "search_runbooks", "args": {"query": f"query number {i}"}} for i in range(5)])  # fmt: skip
    result, _, _, _ = await run_graph([many, diag()], "auth-service errors")
    refused = [m for m in result["messages"] if isinstance(m, ToolMessage) and "budget exhausted" in str(m.content)]
    assert len(refused) == 2 and result["tool_calls"] == 3


async def test_intake_llm_picks_service_when_not_named():
    from sre_assistant.agent.graph import Intake

    script = [Intake(service="auth-service", since_minutes=15), call("search_runbooks", {"query": "redis timeout"}),
              AIMessage("READY"), diag()]  # fmt: skip
    result, _, _, _ = await run_graph(script, "logins are failing for customers")
    assert result["service"] == "auth-service" and result["since_minutes"] == 15


async def test_followup_reuses_thread_and_compacts_old_tool_outputs():
    saver = InMemorySaver()
    first = [call("search_runbooks", {"query": "redis timeout"}), AIMessage("READY"), diag()]
    await run_graph(first, "auth-service is failing", saver=saver)
    second = [
        AIMessage("Set socket_timeout to 1s and make the limiter fail-open [[RB-AUTH-REDIS-TIMEOUT] Root Cause].")
    ]
    result, model, _, _ = await run_graph(second, "what exactly should I change?", saver=saver)
    assert result["messages"][-1].content.startswith("Set socket_timeout")
    stubs = [m for m in model.prompts[0] if isinstance(m, ToolMessage) and "elided (previous turn" in str(m.content)]
    assert len(stubs) >= 4, "previous-turn tool outputs are compacted"
    assert result["diagnosis"] is not None, "first-turn diagnosis persists in the thread"


# ------------------------------------------------------------------ pure units
def test_grounding_flags_missing_search_and_invented_ids():
    g = check_grounding(diag(citations=["[KB-0004] Q", "[RB-NOPE] X"]), {"KB-0004"}, {"inspect_live_logs"})
    assert g.valid_citations == ["[KB-0004] Q"] and g.invalid_citations == ["[RB-NOPE] X"]
    assert any("no runbook search" in w for w in g.warnings)


def test_timeline_orders_events_errors_and_trends():
    logs = {"error_groups": [{"count": 48, "first_seen": "2025-01-01T19:56:03Z", "last_seen": "2025-01-01T19:56:18Z",
                              "search_hint": "sqlalchemy.exc.TimeoutError: QueuePool limit"}],
            "patterns": [{"count": 1, "last_seen": "2025-01-01T19:58:29Z", "pattern": "starting auth-service", "numeric": {}},
                         {"count": 30, "last_seen": "2025-01-01T19:58:20Z", "pattern": "runtime stats",
                          "numeric": {"rss_mb": {"min": 80, "max": 276, "last": 80}}}]}  # fmt: skip
    events = {"events": [{"time": "2025-01-01T19:58:27+00:00", "action": "oom", "detail": "x"},
                         {"time": "2025-01-01T19:58:27+00:00", "action": "die", "detail": "exitCode=137"}]}  # fmt: skip
    tl, windows_struct = build_timeline({"status": "running"}, logs, events)
    rows = [line for line in tl if line[:2].isdigit()]
    assert [r[:8] + " " + r.split(" ")[1] for r in rows] == [
        "19:56:03 ERRORS", "19:58:20 TREND", "19:58:27 EVENT", "19:58:27 EVENT", "19:58:29 LOG"]  # fmt: skip
    windows = [line for line in tl if line.startswith("  #")]
    assert len(windows) == 2, "error burst and OOM are separated by a quiet gap"
    assert "error burst" in windows[0] and "OOM kill" in windows[1] and "memory growth" in windows[1]
    from sre_assistant.agent.timeline import uncovered_windows

    assert [w["index"] for w in uncovered_windows(windows_struct, ["sqlalchemy QueuePool TimeoutError"])] == [2]
    assert uncovered_windows(windows_struct, ["QueuePool limit", "container OOMKilled exit code 137"]) == []


def test_compaction_keeps_current_turn_and_respects_budget():
    big = "x " * 5000
    msgs = [HumanMessage("q1"), AIMessage("", tool_calls=[{"id": "a", "name": "t", "args": {}}]),
            ToolMessage(big, tool_call_id="a", name="t"), AIMessage("answer"), HumanMessage("q2"),
            note("timeline"), AIMessage("", tool_calls=[{"id": "b", "name": "t", "args": {}}]),
            ToolMessage(big, tool_call_id="b", name="t")]  # fmt: skip
    out = compact(msgs, max_tokens=100_000)
    assert "elided (previous turn" in out[2].content and out[7].content == big
    assert "internal_note" in out[5].additional_kwargs, "internal notes don't start a new turn"


async def test_followup_strips_stray_ready_marker():
    saver = InMemorySaver()
    await run_graph(
        [call("search_runbooks", {"query": "redis timeout"}), AIMessage("READY"), diag()],
        "auth-service down",
        saver=saver,
    )
    result, _, _, _ = await run_graph([AIMessage("Change DB_POOL_SIZE.\n\nREADY")], "what to change?", saver=saver)
    assert result["messages"][-1].content == "Change DB_POOL_SIZE."


async def test_coverage_guard_reminds_about_uninvestigated_window():
    # FakeBackend logs contain a redis TimeoutError burst; searching only something unrelated must trigger a reminder.
    script = [call("search_runbooks", {"query": "disk full"}, 1), AIMessage("READY"),
              call("search_runbooks", {"query": "redis.exceptions.TimeoutError Timeout reading"}, 2), AIMessage("READY"),
              diag()]  # fmt: skip
    result, model, _, _ = await run_graph(script, "auth-service errors")
    reminder = [m for m in model.prompts[2] if "Not yet investigated" in str(m.content)]
    assert reminder and "error burst" in reminder[0].content
    assert result["search_queries"] == ["disk full", "redis.exceptions.TimeoutError Timeout reading"]
