"""Run the real agent (real graph, real MCP server, real retriever, real model) on replayed telemetry."""

import threading
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from mcp.client import Client

from sre_assistant.agent.context import is_user_message
from sre_assistant.agent.graph import build_graph, usage_summary
from sre_assistant.agent.mcp_tools import load_mcp_tools
from sre_assistant.config import Settings
from sre_assistant.evals.cases import EvalCase
from sre_assistant.evals.replay import ReplayBackend
from sre_assistant.guardrails.pii import PIIScrubber
from sre_assistant.guardrails.sanitize import Sanitizer
from sre_assistant.llm import get_chat_model
from sre_assistant.llm.spend import price
from sre_assistant.mcp_server.server import build_server
from sre_assistant.rag.tokens import count_tokens


class LockedRetriever:
    """Share one retriever (DB connection + ONNX reranker) across concurrent cases safely."""

    def __init__(self, inner):
        self.inner, self.lock = inner, threading.Lock()

    def retrieve(self, *args, **kwargs):
        with self.lock:
            return self.inner.retrieve(*args, **kwargs)


def _answer(messages) -> str:
    start = max((i for i, m in enumerate(messages) if is_user_message(m)), default=0)
    texts = [str(m.content) for m in messages[start:] if isinstance(m, AIMessage) and not m.tool_calls]
    return "\n\n".join(t for t in texts if t.strip() and t.strip().upper() != "READY")


def _context(messages, max_tokens: int = 9000) -> str:
    """What the agent actually saw from tools (sanitised outputs + retrieved docs), for the groundedness judge."""
    parts, used = [], 0
    for m in messages:
        if isinstance(m, ToolMessage):
            text = str(m.content)
            n = count_tokens(text)
            if used + n > max_tokens:
                text = text[: max(0, (max_tokens - used) * 3)]
                n = count_tokens(text)
            parts.append(text)
            used += n
            if used >= max_tokens:
                break
    return "\n\n".join(parts)


async def run_case(case: EvalCase, settings: Settings, retriever, corpus_dir, pii_key: bytes) -> dict:
    s = settings.model_copy(update={"mcp_audit_log": None, "enable_remediation": False})
    backend = ReplayBackend(case.build())
    sanitizer = Sanitizer(PIIScrubber(key=pii_key, use_ner=s.pii_ner))  # per case: spaCy isn't thread-safe
    server = build_server(backend, lambda: retriever, corpus_dir, s, sanitizer=sanitizer)
    model = get_chat_model(s, tag=f"eval:{case.id}")
    t0 = time.perf_counter()
    async with Client(server) as client:
        graph = build_graph(model, await load_mcp_tools(client), checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": case.id}, "recursion_limit": 40}
        await graph.ainvoke({"messages": [HumanMessage(case.question)], "since_minutes": 15}, cfg)
        snap = await graph.aget_state(cfg)
        while snap.interrupts:  # never execute remediation during evals
            await graph.ainvoke(Command(resume={"approved": False, "approver": "eval-harness"}), cfg)
            snap = await graph.aget_state(cfg)
    st = snap.values
    usage = usage_summary(st.get("llm_calls", []))
    usage["cost_usd"] = round(price(s.chat_model, usage["input_tokens"], usage["output_tokens"]), 5)
    return {
        "id": case.id,
        "kind": case.kind,
        "service": case.service,
        "question": case.question,
        "expected_scenarios": case.expected_scenarios,
        "reference": case.reference,
        "answer": _answer(st["messages"]),
        "diagnosis": st.get("diagnosis"),
        "grounding": st.get("grounding"),
        "tools_used": st.get("tools_used", []),
        "search_queries": st.get("search_queries", []),
        "retrieved_doc_ids": st.get("retrieved_doc_ids", []),
        "security_flags": st.get("security_flags", []),
        "remediation": st.get("remediation"),
        "usage": usage,
        "wall_seconds": round(time.perf_counter() - t0, 1),
        "context": _context(st["messages"]),
    }
