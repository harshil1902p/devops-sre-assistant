"""Run the SRE agent from the command line.

    uv run python -m sre_assistant.agent "auth-service is returning 500s"
    uv run python -m sre_assistant.agent "what should I change in the config?" --thread <id from previous run>

The agent spawns the MCP server over stdio (exactly like VS Code would), loads its tools, and keeps
conversation state in Postgres (LangGraph checkpointer) so follow-up questions continue the same thread.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from sre_assistant.agent.costs import estimate_cost
from sre_assistant.agent.graph import build_graph, usage_summary
from sre_assistant.agent.mcp_tools import load_mcp_tools
from sre_assistant.config import get_settings
from sre_assistant.llm import get_chat_model

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


async def run(question: str, thread: str, service: str | None, since: int, use_memory: bool, as_json: bool) -> dict:
    s = get_settings()
    params = StdioServerParameters(command=sys.executable, args=["-m", "sre_assistant.mcp_server"], cwd=str(ROOT))
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(Client(params, read_timeout_seconds=180))
        tools = await load_mcp_tools(client)
        if use_memory:
            saver = InMemorySaver()
        else:
            dsn = s.database_url.get_secret_value()
            saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
            await saver.setup()
        graph = build_graph(get_chat_model(s), tools, checkpointer=saver)
        config = {"configurable": {"thread_id": thread}, "recursion_limit": 40}

        inputs: dict = {"messages": [("user", question)], "since_minutes": since}
        if service:
            inputs["service"] = service
        t = time.perf_counter()
        before = len((await graph.aget_state(config)).values.get("llm_calls", []))
        async for update in graph.astream(inputs, config, stream_mode="updates"):
            _progress(update)
        state = (await graph.aget_state(config)).values

    calls = state.get("llm_calls", [])[before:]
    usage = usage_summary(calls)
    usage["cost_usd"] = estimate_cost(s.chat_model, usage["input_tokens"], usage["output_tokens"])
    usage["wall_seconds"] = round(time.perf_counter() - t, 1)
    result = {
        "thread": thread,
        "answer": state["messages"][-1].content,
        "diagnosis": state.get("diagnosis"),
        "grounding": state.get("grounding"),
        "tools_used": state.get("tools_used"),
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
    a = p.parse_args()
    thread = a.thread or f"inc-{uuid.uuid4().hex[:8]}"
    asyncio.run(run(a.question, thread, a.service, a.since, a.memory, a.json))


if __name__ == "__main__":
    main()
