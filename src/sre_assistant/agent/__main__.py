"""Run the SRE agent from the command line.

    uv run python -m sre_assistant.agent "auth-service is returning 500s"
    uv run python -m sre_assistant.agent "what should I change in the config?" --thread <id from previous run>

The agent spawns the MCP server over stdio (exactly like VS Code would), loads its tools, and keeps
conversation state in Postgres (LangGraph checkpointer) so follow-up questions continue the same thread.
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment

from sre_assistant.agent.graph import build_graph, usage_summary
from sre_assistant.agent.mcp_tools import load_mcp_tools
from sre_assistant.config import get_settings
from sre_assistant.llm import get_chat_model
from sre_assistant.llm.providers import get_ledger
from sre_assistant.llm.spend import price

ROOT = Path(__file__).resolve().parents[3]


def _progress(update: dict) -> None:
    for node, data in update.items():
        if not isinstance(data, dict):
            continue
        msgs = data.get("messages", [])
        calls = [f"{tc['name']}({json.dumps(tc['args'])[:80]})" for m in msgs for tc in getattr(m, "tool_calls", [])]
        extra = f" -> {', '.join(calls)}" if calls else ""
        if node == "intake" and data.get("service"):
            extra = f" -> service={data['service']} since={data.get('since_minutes')}m"
        print(f"  · {node}{extra}", file=sys.stderr)


def _turn_answer(messages) -> str:
    """Everything the agent said to the user in the latest turn (diagnosis + remediation outcome)."""
    from langchain_core.messages import AIMessage

    from sre_assistant.agent.context import is_user_message

    start = max((i for i, m in enumerate(messages) if is_user_message(m)), default=0)
    texts = [str(m.content) for m in messages[start:] if isinstance(m, AIMessage) and not m.tool_calls]
    return "\n\n".join(t for t in texts if t.strip() and t.strip().upper() != "READY")


def _ask_human(payload: dict, auto: str | None) -> dict:
    """Human-in-the-loop decision. --auto-approve/--auto-deny exist for scripted demos and tests only."""
    print("\n" + "=" * 70, file=sys.stderr)
    print(f"APPROVAL REQUIRED: {payload['action']} on {payload['service']}", file=sys.stderr)
    print(f"justification: {payload['justification']}", file=sys.stderr)
    for e in payload.get("evidence", []):
        print(f"  evidence: {e}", file=sys.stderr)
    if payload.get("security_flags"):
        print(f"  ⚠ possible prompt injection in the data: {payload['security_flags']}", file=sys.stderr)
    if auto:
        approved = auto == "approve"
        print(f"(non-interactive: auto-{auto})", file=sys.stderr)
    elif sys.stdin.isatty():
        approved = input("Approve? [y/N] ").strip().lower() in ("y", "yes")
    else:
        approved = False
        print("(no TTY: denied by default)", file=sys.stderr)
    return {"approved": approved, "approver": getpass.getuser()}


async def run(question: str, thread: str, service: str | None, since: int, use_memory: bool, as_json: bool,
              auto: str | None = None) -> dict:  # fmt: skip
    s = get_settings()
    # The MCP SDK passes only a minimal, safe env to stdio servers; forward our own SRE_* settings explicitly
    # (e.g. SRE_TELEMETRY_BACKEND=azure_monitor), nothing else from the parent environment.
    env = get_default_environment() | {k: v for k, v in os.environ.items() if k.startswith("SRE_")}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "sre_assistant.mcp_server"], cwd=str(ROOT), env=env
    )
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(Client(params, read_timeout_seconds=180))
        tools = await load_mcp_tools(client)
        if use_memory:
            saver = InMemorySaver()
        else:
            dsn = s.database_url.get_secret_value()
            saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
            await saver.setup()
        secret = s.approval_secret.get_secret_value().encode() if s.approval_secret else None
        graph = build_graph(get_chat_model(s, tag=f"agent:{thread}"), tools, checkpointer=saver,
                            approval_secret=secret)  # fmt: skip
        config = {"configurable": {"thread_id": thread}, "recursion_limit": 40}

        inputs: dict = {"messages": [("user", question)], "since_minutes": since}
        if service:
            inputs["service"] = service
        t = time.perf_counter()
        before = len((await graph.aget_state(config)).values.get("llm_calls", []))
        async for update in graph.astream(inputs, config, stream_mode="updates"):
            _progress(update)
        snapshot = await graph.aget_state(config)
        while snapshot.interrupts:  # paused at a human-approval step; state is safely checkpointed meanwhile
            decision = _ask_human(snapshot.interrupts[0].value, auto)
            async for update in graph.astream(Command(resume=decision), config, stream_mode="updates"):
                _progress(update)
            snapshot = await graph.aget_state(config)
        state = snapshot.values

    calls = state.get("llm_calls", [])[before:]
    usage = usage_summary(calls)
    paid = s.effective_chat_provider != "ollama"
    usage["cost_usd_upper_bound"] = (
        round(price(s.chat_model, usage["input_tokens"], usage["output_tokens"]), 5) if paid else 0
    )
    usage["project_spend_usd"] = get_ledger(s).total() if paid else 0
    usage["wall_seconds"] = round(time.perf_counter() - t, 1)
    result = {
        "thread": thread,
        "answer": _turn_answer(state["messages"]),
        "diagnosis": state.get("diagnosis"),
        "grounding": state.get("grounding"),
        "tools_used": state.get("tools_used"),
        "security_flags": state.get("security_flags"),
        "remediation": state.get("remediation"),
        "usage": usage,
    }
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("\n" + result["answer"])
        print(f"\n— thread={thread} model={s.chat_model} tools={state.get('tools_used')} usage={usage}")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="SRE incident-diagnosis agent")
    p.add_argument("question")
    p.add_argument("--thread", default=None, help="continue an existing conversation")
    p.add_argument("--service", default=None, help="skip intake and diagnose this service")
    p.add_argument("--since", type=int, default=30, help="minutes of history to inspect")
    p.add_argument("--memory", action="store_true", help="in-memory checkpointer instead of Postgres")
    p.add_argument("--json", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--auto-approve", dest="auto", action="store_const", const="approve", help="demo only")
    g.add_argument("--auto-deny", dest="auto", action="store_const", const="deny")
    a = p.parse_args()
    thread = a.thread or f"inc-{uuid.uuid4().hex[:8]}"
    asyncio.run(run(a.question, thread, a.service, a.since, a.memory, a.json, a.auto))


if __name__ == "__main__":
    main()
