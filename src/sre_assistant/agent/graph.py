"""LangGraph incident-diagnosis agent.

                       ┌──────────────── follow-up turn ─────────────────┐
  START ─▶ start_turn ─┼─▶ intake ─▶ triage ─▶ agent ⇄ tools             │
                       └─▶ triage (service known)   │  ▲                  ▼
                                                    │  └── remind (must search runbooks first)
                                                    ├─▶ respond (structured Diagnosis + grounding check) ─▶ END
                                                    └─▶ END (follow-up answered in prose)

"Workflow + agent": steps that are ALWAYS needed (health, logs, events, timeline) run deterministically and in
parallel (fast, cheap, reliable); the LLM's ReAct loop only makes the decisions that need judgement (what to
search, what to verify, when to stop). Guards: tool-call budget, duplicate-call detection, mandatory runbook
search, structured output, citation verification.
"""

import asyncio
import json
import operator
import re
import time
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from sre_assistant.agent.context import compact, is_user_message, note
from sre_assistant.agent.diagnosis import Diagnosis, check_grounding, render_markdown
from sre_assistant.agent.prompts import FINAL_INSTRUCTION, FOLLOWUP_HINT, SYSTEM_PROMPT
from sre_assistant.agent.timeline import build_timeline, uncovered_windows
from sre_assistant.guardrails import policy
from sre_assistant.guardrails.approval import mint

TRIAGE_TOOLS = ("check_system_health", "inspect_live_logs", "get_recent_events")


def _union(a: list[str], b: list[str]) -> list[str]:
    return sorted(set(a or []) | set(b or []))


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    service: str | None
    since_minutes: int
    followup: bool
    tool_calls: int
    tools_used: list[str]
    reminded: bool
    retrieved_doc_ids: Annotated[list[str], _union]
    timeline: list[str]
    windows: list[dict]
    search_queries: list[str]
    diagnosis: dict | None
    grounding: dict | None
    security_flags: Annotated[list[str], _union]
    remediation: dict | None
    llm_calls: Annotated[list[dict], operator.add]


class Intake(BaseModel):
    service: str = Field(description="exact service name from the list, or 'unknown'")
    since_minutes: int = Field(description="how far back to look, 5-240; default 30")


def _usage(node: str, msg: AIMessage | None, started: float) -> dict:
    u = (msg.usage_metadata if msg is not None else None) or {}
    return {"node": node, "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
            "latency_ms": round((time.perf_counter() - started) * 1000)}  # fmt: skip


def build_graph(model: BaseChatModel, tools: list[BaseTool], checkpointer=None, max_tool_calls: int = 8,
                max_context_tokens: int = 24_000, approval_secret: bytes | None = None):  # fmt: skip
    by_name = {t.name: t for t in tools}
    # CAPABILITY CONTROL: the LLM only ever sees read-only tools. Destructive tools exist solely for the
    # approval node (trusted code), so no prompt injection can make the model call them.
    llm_tools = [t for t in tools if (t.metadata or {}).get("read_only", True)]
    llm_with_tools = model.bind_tools(llm_tools)
    llm_tool_names = {t.name for t in llm_tools}
    structured = model.with_structured_output(Diagnosis, method="json_schema", include_raw=True)
    intake_llm = model.with_structured_output(Intake, method="json_schema", include_raw=True)

    async def run_tool(name: str, args: dict, call_id: str | None = None) -> ToolMessage:
        call = {"type": "tool_call", "id": call_id or f"call_{uuid.uuid4().hex[:12]}", "name": name, "args": args}
        if name not in by_name:
            return ToolMessage(f"ERROR: unknown tool {name}", tool_call_id=call["id"], name=name, status="error")
        return await by_name[name].ainvoke(call)

    # ------------------------------------------------------------------ nodes
    async def start_turn(state: AgentState) -> dict:
        return {"followup": state.get("diagnosis") is not None, "tool_calls": 0, "tools_used": [], "reminded": False}

    async def intake(state: AgentState) -> dict:
        question = next(m.content for m in reversed(state["messages"]) if is_user_message(m))
        listing = await run_tool("list_services", {})
        services = [s["service"] for s in (listing.artifact or {}).get("services", [])]
        named = [s for s in services if s in question]
        if len(named) == 1:  # deterministic fast path, no LLM call needed
            return {"service": named[0], "since_minutes": state.get("since_minutes") or 30}
        t = time.perf_counter()
        out = await intake_llm.ainvoke([
            SystemMessage(f"Pick the affected service. Available services and status: {listing.content}"),
            *[m for m in state["messages"] if is_user_message(m)][-1:],
        ])  # fmt: skip
        parsed: Intake = out["parsed"]
        service = parsed.service if parsed and parsed.service in services else None
        if service is None:  # fall back to the least healthy service
            unhealthy = [s for s in listing.artifact["services"] if s["health"] not in ("healthy", None)]
            service = (unhealthy or listing.artifact["services"])[0]["service"]
        return {"service": service, "since_minutes": max(5, min(240, parsed.since_minutes if parsed else 30)),
                "llm_calls": [_usage("intake", out.get("raw"), t)]}  # fmt: skip

    async def triage(state: AgentState) -> dict:
        svc, since = state["service"], state.get("since_minutes") or 30
        calls = [
            ("check_system_health", {"service_name": svc}),
            ("inspect_live_logs", {"service_name": svc, "lines_count": 2000, "since_minutes": since}),
            (
                "get_recent_events",
                {"service_name": svc, "since_minutes": max(since, 10)},
            ),  # same window as the question
        ]
        ids = [f"triage_{uuid.uuid4().hex[:10]}" for _ in calls]
        results = await asyncio.gather(*(run_tool(n, a, i) for (n, a), i in zip(calls, ids, strict=True)))
        art = {n: r.artifact for (n, _), r in zip(calls, results, strict=True)}
        flags = (art.get("inspect_live_logs") or {}).get("security_flags", [])
        timeline, windows = build_timeline(
            art["check_system_health"], art["inspect_live_logs"], art["get_recent_events"]
        )
        # Prefetched results are injected as if the model had called the tools itself, so the conversation
        # stays a valid tool-calling transcript for any provider.
        ai = AIMessage(
            content="", tool_calls=[{"id": i, "name": n, "args": a} for (n, a), i in zip(calls, ids, strict=True)]
        )
        header = f"TIMELINE for {svc} (UTC, merged from the triage results):\n"
        return {
            "messages": [ai, *results, note(header + "\n".join(timeline))],
            "timeline": timeline,
            "windows": windows,
            "search_queries": [],
            "security_flags": flags,
            "tools_used": list(TRIAGE_TOOLS),
        }  # fmt: skip

    async def agent(state: AgentState) -> dict:
        system = SYSTEM_PROMPT + ("\n" + FOLLOWUP_HINT if state.get("followup") else "")
        msgs = [SystemMessage(system), *compact(state["messages"], max_context_tokens)]
        t = time.perf_counter()
        reply = await llm_with_tools.ainvoke(msgs)
        return {"messages": [reply], "llm_calls": [_usage("agent", reply, t)]}

    async def tools_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        used, n = list(state.get("tools_used", [])), state.get("tool_calls", 0)
        seen = {
            (tc["name"], json.dumps(tc["args"], sort_keys=True))
            for m in state["messages"][:-1] if isinstance(m, AIMessage) for tc in m.tool_calls
        }  # fmt: skip
        out, retrieved, queries, flags = [], [], list(state.get("search_queries", [])), []
        for tc in last.tool_calls:
            key = (tc["name"], json.dumps(tc["args"], sort_keys=True))
            if tc["name"] not in llm_tool_names:
                out.append(ToolMessage(f"REFUSED: '{tc['name']}' is not available to the assistant; changes require "
                                       "a human-approved proposal in the final diagnosis.",
                                       tool_call_id=tc["id"], name=tc["name"], status="error"))  # fmt: skip
                continue
            if n >= max_tool_calls:
                out.append(ToolMessage("SKIPPED: tool budget exhausted. Answer with the evidence you have.",
                                       tool_call_id=tc["id"], name=tc["name"], status="error"))  # fmt: skip
                continue
            if key in seen:
                out.append(ToolMessage("SKIPPED: identical call already made; reuse the earlier result.",
                                       tool_call_id=tc["id"], name=tc["name"], status="error"))  # fmt: skip
                continue
            seen.add(key)
            msg = await run_tool(tc["name"], tc["args"], tc["id"])
            n, used = n + 1, [*used, tc["name"]]
            if isinstance(msg.artifact, dict):
                flags += msg.artifact.get("security_flags", [])
            if tc["name"] == "search_runbooks":
                queries.append(str(tc["args"].get("query", "")))
                if isinstance(msg.artifact, dict):
                    retrieved += [r["doc_id"] for r in msg.artifact.get("results", [])]
            out.append(msg)
        return {"messages": out, "tool_calls": n, "tools_used": used, "retrieved_doc_ids": retrieved,
                "search_queries": queries, "security_flags": flags}  # fmt: skip

    async def remind(state: AgentState) -> dict:
        missing = uncovered_windows(state.get("windows", []), state.get("search_queries", []))
        if "search_runbooks" not in state.get("tools_used", []):
            text = ("Before answering you must call search_runbooks with the most relevant error signature or "
                    "symptom (institutional knowledge often contains the exact fix).")  # fmt: skip
        else:
            text = "Not yet investigated: " + "; ".join(f"window #{w['index']} ({w['label']})" for w in missing)
            text += ". Call search_runbooks for each of these signals before answering."
        return {"reminded": True, "messages": [note(text + " Then reply READY.")]}

    async def finish_followup(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        text = re.sub(r"\s*\bREADY\s*$", "", str(last.content)).rstrip()  # defensive: strip a stray READY marker
        return {"messages": [AIMessage(content=text, id=last.id)]}  # same id -> replaces the message in state

    async def respond(state: AgentState) -> dict:
        msgs = [SystemMessage(SYSTEM_PROMPT), *compact(state["messages"], max_context_tokens), note(FINAL_INSTRUCTION)]
        t = time.perf_counter()
        out = await structured.ainvoke(msgs)
        d: Diagnosis | None = out.get("parsed")
        if d is None:
            err = out.get("parsing_error")
            return {"messages": [AIMessage(f"Could not produce a structured diagnosis: {err}")],
                    "llm_calls": [_usage("respond", out.get("raw"), t)]}  # fmt: skip
        g = check_grounding(d, set(state.get("retrieved_doc_ids", [])), set(state.get("tools_used", [])))
        mitigation, f1 = policy.enforce(d.immediate_mitigation)
        fix, f2 = policy.enforce(d.permanent_fix)
        g = g.model_copy(update={"policy_flags": f1 + f2, "security_flags": state.get("security_flags", [])})
        d = d.model_copy(update={"citations": g.valid_citations, "immediate_mitigation": mitigation,
                                 "permanent_fix": fix})  # fmt: skip
        return {
            "messages": [AIMessage(render_markdown(d, g))],
            "diagnosis": d.model_dump(),
            "grounding": g.model_dump(),
            "llm_calls": [_usage("respond", out.get("raw"), t)],
        }

    async def approval(state: AgentState) -> dict:
        """Human-in-the-loop: pause the graph (checkpointed) until a human approves or rejects the proposal."""
        d, svc = state["diagnosis"], state["service"]
        action = d["proposed_action"]
        decision = interrupt(
            {
                "action": action,
                "service": svc,
                "justification": d["action_justification"],
                "evidence": d["evidence"][:3],
                "security_flags": state.get("security_flags", []),
            }
        )
        decision = decision if isinstance(decision, dict) else {}
        approver = str(decision.get("approver", "unknown"))
        if not decision.get("approved"):
            text = f"Proposed `{action}` was **rejected** by {approver}. No action taken."
            return {"remediation": {"status": "rejected", "approver": approver}, "messages": [AIMessage(text)]}
        if approval_secret is None or action not in by_name:
            text = "Remediation is not enabled on this deployment; no action taken."
            return {"remediation": {"status": "unavailable"}, "messages": [AIMessage(text)]}

        token = mint(approval_secret, action, svc, approver)  # minted by trusted code; never shown to the LLM
        reason = d["action_justification"][:400] or "approved by human"
        msg = await run_tool(action, {"service_name": svc, "approval_token": token, "reason": reason})
        ok = msg.status != "error"
        outcome = "**executed**." if ok else f"**failed**: {str(msg.content)[:200]}"
        return {
            "remediation": {
                "status": "executed" if ok else "failed",
                "approver": approver,
                "result": msg.artifact if ok else str(msg.content)[:300],
            },
            "messages": [AIMessage(f"`{action}` on `{svc}` approved by {approver}: {outcome}")],
        }

    # ---------------------------------------------------------------- routing
    def route_start(state: AgentState) -> str:
        if state.get("followup"):
            return "agent"
        return "triage" if state.get("service") else "intake"

    def route_agent(state: AgentState) -> str:
        last: AIMessage = state["messages"][-1]
        if last.tool_calls:
            return "tools"
        ready = str(last.content).strip().upper().startswith("READY")
        if state.get("followup") and not ready:
            return "finish_followup"  # prose answer to a follow-up question
        if not state.get("reminded") and (
            "search_runbooks" not in state.get("tools_used", [])
            or uncovered_windows(state.get("windows", []), state.get("search_queries", []))
        ):
            return "remind"  # coverage guard: every significant incident window must be investigated
        return "respond"

    def route_respond(state: AgentState) -> str:
        d = state.get("diagnosis") or {}
        return "approval" if d.get("proposed_action", "none") != "none" else END

    def route_tools(state: AgentState) -> str:
        return "respond" if state.get("tool_calls", 0) >= max_tool_calls and not state.get("followup") else "agent"

    g = StateGraph(AgentState)
    for name, fn in [("start_turn", start_turn), ("intake", intake), ("triage", triage), ("agent", agent),
                     ("tools", tools_node), ("remind", remind), ("respond", respond),
                     ("finish_followup", finish_followup), ("approval", approval)]:  # fmt: skip
        g.add_node(name, fn)
    g.add_edge(START, "start_turn")
    g.add_conditional_edges("start_turn", route_start, ["intake", "triage", "agent"])
    g.add_edge("intake", "triage")
    g.add_edge("triage", "agent")
    g.add_conditional_edges("agent", route_agent, ["tools", "remind", "respond", "finish_followup"])
    g.add_edge("finish_followup", END)
    g.add_conditional_edges("tools", route_tools, ["agent", "respond"])
    g.add_edge("remind", "agent")
    g.add_conditional_edges("respond", route_respond, ["approval", END])
    g.add_edge("approval", END)
    return g.compile(checkpointer=checkpointer)


def usage_summary(llm_calls: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "llm_calls": len(llm_calls),
        "input_tokens": sum(c["input_tokens"] for c in llm_calls),
        "output_tokens": sum(c["output_tokens"] for c in llm_calls),
        "llm_latency_ms": sum(c["latency_ms"] for c in llm_calls),
    }
