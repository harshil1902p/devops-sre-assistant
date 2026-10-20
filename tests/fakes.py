"""Deterministic test doubles for LLM-driven code."""

from collections import deque
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import PrivateAttr


class ScriptedChatModel(BaseChatModel):
    """Returns pre-scripted replies in order and records every prompt it receives.

    Script items: AIMessage (for normal/tool-calling calls) or pydantic objects (for structured output).
    """

    _script: deque = PrivateAttr()
    _prompts: list = PrivateAttr()

    def __init__(self, script: list[Any], **kwargs):
        super().__init__(**kwargs)
        self._script = deque(script)
        self._prompts = []

    @property
    def prompts(self) -> list[list[BaseMessage]]:
        return self._prompts

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _next(self, messages):
        self._prompts.append(list(messages))
        if not self._script:
            raise AssertionError("ScriptedChatModel: script exhausted")
        return self._script.popleft()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        item = self._next(messages)
        assert isinstance(item, AIMessage), f"expected AIMessage, got {type(item)}"
        return ChatResult(generations=[ChatGeneration(message=item)])

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs):
        def _invoke(messages):
            item = self._next(messages)
            parsed = item if isinstance(item, schema) else schema.model_validate(item)
            raw = AIMessage(content="", usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
            return {"raw": raw, "parsed": parsed, "parsing_error": None} if include_raw else parsed

        return RunnableLambda(_invoke)
