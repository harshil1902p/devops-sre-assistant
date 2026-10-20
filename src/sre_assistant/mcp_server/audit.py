"""Audit trail for every tool call: who called what, with which (redacted) arguments, outcome, cost.

In regulated environments (banks) an AI agent's actions must be reconstructable after the fact.
IMPORTANT for stdio transport: stdout carries the JSON-RPC protocol, so audit/log output must go to
stderr or a file. A stray print() to stdout corrupts the MCP session.
"""

import functools
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from sre_assistant.mcp_server.redaction import redact_mapping
from sre_assistant.rag.tokens import count_tokens

audit_log = logging.getLogger("sre.audit")


def configure_audit(path: str | None) -> None:
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False
    if path and not any(isinstance(h, logging.FileHandler) for h in audit_log.handlers):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path)
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit_log.addHandler(handler)


def audited(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "tool": fn.__name__,
                 "args": redact_mapping(dict(kwargs))}  # fmt: skip
        try:
            result = fn(*args, **kwargs)
            payload = result.model_dump_json() if isinstance(result, BaseModel) else str(result)
            entry |= {"ok": True, "output_tokens": count_tokens(payload)}
            return result
        except Exception as e:
            entry |= {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            raise
        finally:
            entry["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
            audit_log.info(json.dumps(entry, default=str))

    return wrapper
