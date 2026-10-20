"""Context management for long investigations.

Tool outputs are large (a log report is ~2.5k tokens). In a multi-turn thread, re-sending every old tool
output on every LLM call wastes tokens and can overflow the window. Strategy:
  1. Tool outputs from PREVIOUS turns are replaced by a one-line stub (the model can re-call the tool).
  2. If the current turn alone exceeds the budget, the oldest tool outputs of this turn are stubbed too,
     always keeping the most recent ones.
The full outputs stay in the checkpointed state (and in ToolMessage.artifact); only the prompt is compacted.
"""

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage

from sre_assistant.rag.tokens import count_tokens

INTERNAL = "internal_note"


def is_user_message(m: AnyMessage) -> bool:
    return isinstance(m, HumanMessage) and not m.additional_kwargs.get(INTERNAL)


def note(text: str) -> HumanMessage:
    """A message we inject (timeline, reminders). Marked so it is not mistaken for a new user turn."""
    return HumanMessage(content=text, additional_kwargs={INTERNAL: True})


def _stub(m: ToolMessage, why: str) -> ToolMessage:
    return ToolMessage(
        content=(
            f"[{m.name} output elided ({why}, ~{count_tokens(str(m.content))} tokens); call the tool again if needed]"
        ),
        tool_call_id=m.tool_call_id,
        name=m.name,
        status=m.status,
    )


def compact(messages: list[AnyMessage], max_tokens: int = 24_000, keep_recent_tools: int = 3) -> list[AnyMessage]:
    user_idx = [i for i, m in enumerate(messages) if is_user_message(m)]
    turn_start = user_idx[-1] if user_idx else 0
    out = [_stub(m, "previous turn") if isinstance(m, ToolMessage) and i < turn_start else m
           for i, m in enumerate(messages)]  # fmt: skip

    def total() -> int:
        return sum(count_tokens(str(m.content)) for m in out)

    tool_positions = [i for i, m in enumerate(out) if isinstance(m, ToolMessage) and i >= turn_start]
    for i in tool_positions[: max(0, len(tool_positions) - keep_recent_tools)]:
        if total() <= max_tokens:
            break
        out[i] = _stub(out[i], "context budget")
    return out
