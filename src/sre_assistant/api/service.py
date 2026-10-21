"""Long-lived agent service behind the HTTP API.

Built once per process (not per request): one MCP server subprocess + client session, one compiled graph, one
checkpointer. Concurrent requests use different thread ids; LangGraph keeps their state apart.
"""

import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.types import Command
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from sre_assistant.agent.graph import build_graph, usage_summary
from sre_assistant.agent.mcp_tools import load_mcp_tools, mcp_subprocess_env
from sre_assistant.api.auth import Principal
from sre_assistant.config import Settings
from sre_assistant.llm import get_chat_model
from sre_assistant.llm.spend import price
from sre_assistant.telemetry import diagnose_duration, diagnoses, record_usage, tracer

ROOT = Path(__file__).resolve().parents[3]


class ThreadNotFound(LookupError):
    pass


class Forbidden(PermissionError):
    pass


def turn_answer(messages) -> str:
    from langchain_core.messages import AIMessage

    from sre_assistant.agent.context import is_user_message

    start = max((i for i, m in enumerate(messages) if is_user_message(m)), default=0)
    texts = [str(m.content) for m in messages[start:] if isinstance(m, AIMessage) and not m.tool_calls]
    return "\n\n".join(t for t in texts if t.strip() and t.strip().upper() != "READY")


class AgentService:
    def __init__(self, settings: Settings, graph=None):
        self.s = settings
        self.graph = graph  # injectable for tests
        self.tools: list = []
        self.owners: dict[str, str] = {}  # thread -> requester subject (production: a DB table)
        self._stack = AsyncExitStack()
        self._sem = asyncio.Semaphore(settings.api_max_concurrent)

    async def start(self) -> None:
        if self.graph is not None:
            return
        env = mcp_subprocess_env()
        params = StdioServerParameters(command=sys.executable, args=["-m", "sre_assistant.mcp_server"],
                                       cwd=str(ROOT), env=env)  # fmt: skip
        client = await self._stack.enter_async_context(Client(params, read_timeout_seconds=180))
        self.tools = await load_mcp_tools(client)
        if self.s.checkpointer == "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver = await self._stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(self.s.database_url.get_secret_value())
            )
            await saver.setup()
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            saver = InMemorySaver()
        secret = self.s.approval_secret.get_secret_value().encode() if self.s.approval_secret else None
        self.graph = build_graph(get_chat_model(self.s, tag="api"), self.tools, checkpointer=saver,
                                 approval_secret=secret)  # fmt: skip

    async def stop(self) -> None:
        await self._stack.aclose()

    @property
    def ready(self) -> bool:
        return self.graph is not None

    # ------------------------------------------------------------------ helpers
    def _config(self, thread: str) -> dict:
        return {"configurable": {"thread_id": thread}, "recursion_limit": 40}

    def _check_owner(self, thread: str, principal: Principal, allow_role: str | None = None) -> str:
        owner = self.owners.get(thread)
        if owner is None:
            raise ThreadNotFound(thread)
        if owner != principal.subject and not (allow_role and principal.has(allow_role)):
            raise ThreadNotFound(thread)  # 404, not 403: don't confirm that someone else's thread exists
        return owner

    async def _result(self, thread: str, before: int, started: float) -> dict:
        snap = await self.graph.aget_state(self._config(thread))
        st = snap.values
        usage = usage_summary(st.get("llm_calls", [])[before:])
        paid = self.s.effective_chat_provider != "ollama"
        usage["cost_usd_upper_bound"] = (
            round(price(self.s.chat_model, usage["input_tokens"], usage["output_tokens"]), 5) if paid else 0
        )
        usage["wall_seconds"] = round(time.perf_counter() - started, 1)
        record_usage(self.s.chat_model, usage)
        pending = snap.interrupts[0].value if snap.interrupts else None
        return {
            "thread": thread,
            "status": "approval_required" if pending else "completed",
            "answer": turn_answer(st.get("messages", [])),
            "diagnosis": st.get("diagnosis"),
            "pending_action": pending,
            "remediation": st.get("remediation"),
            "security_flags": st.get("security_flags"),
            "tools_used": st.get("tools_used"),
            "usage": usage,
        }

    async def _stream(self, inputs, thread: str, before: int, started: float) -> AsyncIterator[dict]:
        async for update in self.graph.astream(inputs, self._config(thread), stream_mode="updates"):
            for node, data in update.items():
                calls = [tc["name"] for m in (data or {}).get("messages", []) if isinstance(data, dict)
                         for tc in getattr(m, "tool_calls", [])]  # fmt: skip
                yield {"event": "progress", "node": node, "tool_calls": calls}
        yield {"event": "result", **(await self._result(thread, before, started))}

    # ------------------------------------------------------------------ API
    async def diagnose(self, principal: Principal, question: str, service: str | None, since_minutes: int,
                       thread: str | None) -> AsyncIterator[dict]:  # fmt: skip
        if thread:
            self._check_owner(thread, principal)
        else:
            thread = f"inc-{uuid.uuid4().hex}"  # server-generated, unguessable
            self.owners[thread] = principal.subject
        inputs: dict = {"messages": [("user", question)], "since_minutes": since_minutes}
        if service:
            inputs["service"] = service
        async with self._sem:
            started = time.perf_counter()
            with tracer.start_as_current_span("agent.diagnose", attributes={"sre.thread": thread}) as span:
                before = len((await self.graph.aget_state(self._config(thread))).values.get("llm_calls", []))
                yield {"event": "started", "thread": thread}
                outcome = "error"
                try:
                    async for ev in self._stream(inputs, thread, before, started):
                        if ev["event"] == "result":
                            outcome = ev["status"]
                            span.set_attribute("sre.status", outcome)
                            span.set_attribute("sre.llm.input_tokens", ev["usage"]["input_tokens"])
                        yield ev
                finally:
                    diagnoses.add(1, {"outcome": outcome})
                    diagnose_duration.record(time.perf_counter() - started, {"outcome": outcome})

    async def thread(self, principal: Principal, thread: str, allow_role: str | None = None) -> dict:
        self._check_owner(thread, principal, allow_role)
        return await self._result(thread, 0, time.perf_counter())

    async def decide(self, principal: Principal, thread: str, approved: bool, comment: str = "") -> dict:
        owner = self._check_owner(thread, principal, allow_role="SRE.Approve")
        snap = await self.graph.aget_state(self._config(thread))
        if not snap.interrupts:
            raise LookupError("no action is waiting for approval on this thread")
        if self.s.approval_four_eyes and approved and owner == principal.subject:
            raise Forbidden("four-eyes rule: the requester of an action cannot approve it")
        decision = {"approved": approved, "approver": principal.name, "approver_id": principal.subject,
                    "comment": comment[:500]}  # fmt: skip
        before = len(snap.values.get("llm_calls", []))
        started = time.perf_counter()
        with tracer.start_as_current_span("agent.approval", attributes={"sre.approved": approved}):
            async for _ in self.graph.astream(Command(resume=decision), self._config(thread), stream_mode="updates"):
                pass
        return await self._result(thread, before, started)
