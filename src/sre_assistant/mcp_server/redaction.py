"""Backwards-compatible import path. The implementation lives in sre_assistant.guardrails."""

from sre_assistant.guardrails.secrets import MASK, Redacted, is_secret_key, redact, redact_mapping, redact_text

__all__ = ["MASK", "Redacted", "is_secret_key", "redact", "redact_mapping", "redact_text"]
