"""Hard spend cap for paid LLM/embedding calls.

Azure budgets only ALERT (with hours of delay); they never stop spending. This guard is the real brake:
  - before every chat-model call: refuse (SpendLimitExceeded) if the project ledger is at/over the cap,
  - after every call: price the actual token usage (incl. cached input) and append it to the ledger.
The ledger is a JSON-lines file shared by every process (agent CLI, API, eval runs) with a file lock.
Local models (Ollama) cost $0 and are never blocked.
"""

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

# USD per 1M tokens: (input, cached_input, output). Source: Azure Retail Prices API, Global deployment,
# canadaeast, retrieved 2026-10-20. Unknown paid models use FALLBACK (deliberately pessimistic).
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "text-embedding-3-small": (0.02, 0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.13, 0.0),
}
FALLBACK = (5.00, 5.00, 20.00)


class SpendLimitExceeded(RuntimeError):
    pass


def price(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    p = next((v for k, v in sorted(PRICES.items(), key=lambda kv: -len(kv[0])) if model.startswith(k)), FALLBACK)
    fresh = max(input_tokens - cached_tokens, 0)
    return (fresh * p[0] + cached_tokens * p[1] + output_tokens * p[2]) / 1e6


class SpendLedger:
    def __init__(self, path: str | Path, limit_usd: float):
        self.path = Path(path)
        self.limit_usd = limit_usd
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    @contextmanager
    def _locked(self, mode: str):
        with open(self.path, mode) as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield f
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def total(self) -> float:
        with self._locked("r") as f:
            return round(sum(json.loads(line)["usd"] for line in f if line.strip()), 6)

    def check(self) -> None:
        spent = self.total()
        if spent >= self.limit_usd:
            raise SpendLimitExceeded(
                f"LLM spend cap reached: ${spent:.4f} of ${self.limit_usd:.2f}. "
                "Raise SRE_SPEND_LIMIT_USD only after explicit approval."
            )

    def record(self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0, tag: str = "") -> float:
        usd = price(model, input_tokens, output_tokens, cached_tokens)
        entry = {"ts": time.time(), "model": model, "in": input_tokens, "cached": cached_tokens,
                 "out": output_tokens, "usd": round(usd, 8), "tag": tag}  # fmt: skip
        with self._locked("a") as f:
            f.write(json.dumps(entry) + "\n")
        return usd

    def summary(self) -> dict[str, Any]:
        with self._locked("r") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        by_model: dict[str, float] = {}
        for r in rows:
            by_model[r["model"]] = round(by_model.get(r["model"], 0) + r["usd"], 6)
        return {"calls": len(rows), "total_usd": round(sum(r["usd"] for r in rows), 6),
                "limit_usd": self.limit_usd, "by_model": by_model}  # fmt: skip


class SpendGuard(BaseCallbackHandler):
    """Attach to a paid chat model: blocks calls over the cap and records actual usage."""

    raise_error = True  # exceptions in on_*_start propagate and abort the call (instead of being logged)

    def __init__(self, ledger: SpendLedger, model: str, tag: str = ""):
        self.ledger, self.model, self.tag = ledger, model, tag

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.ledger.check()

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        self.ledger.check()

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        for gens in response.generations:
            for g in gens:
                usage = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                if not usage:
                    continue
                cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
                self.ledger.record(self.model, usage.get("input_tokens", 0), usage.get("output_tokens", 0), cached,
                                   self.tag)  # fmt: skip
