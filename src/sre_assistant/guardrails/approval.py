"""Human-approval tokens for remediation actions.

Flow: agent proposes an action -> a HUMAN approves in the CLI/API -> trusted code (not the LLM) mints a short-lived,
single-use HMAC token bound to (action, service, approver) -> the MCP server verifies it before acting.
The LLM never sees the signing secret, so neither it nor a prompt injection can forge an approval.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass


class ApprovalError(PermissionError):
    pass


@dataclass(frozen=True)
class Approval:
    action: str
    service: str
    approver: str
    expires_at: int
    nonce: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint(secret: bytes, action: str, service: str, approver: str, ttl_seconds: int = 300) -> str:
    claims = {"a": action, "s": service, "u": approver, "exp": int(time.time()) + ttl_seconds,
              "n": secrets.token_hex(8)}  # fmt: skip
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


class ApprovalVerifier:
    """Verifies signature, expiry, binding to (action, service) and single use (replay protection)."""

    def __init__(self, secret: bytes):
        self.secret = secret
        self._used: dict[str, int] = {}  # nonce -> expiry; use Redis/DB when running several replicas

    def verify(self, token: str, action: str, service: str) -> Approval:
        try:
            body, sig = token.split(".", 1)
            expected = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, expected):  # constant-time comparison
                raise ApprovalError("invalid approval signature")
            c = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError) as e:
            if isinstance(e, ApprovalError):
                raise
            raise ApprovalError("malformed approval token") from e
        now = int(time.time())
        if c["exp"] < now:
            raise ApprovalError("approval token expired")
        if (c["a"], c["s"]) != (action, service):
            raise ApprovalError(f"approval is for {c['a']} on {c['s']}, not {action} on {service}")
        self._used = {n: e for n, e in self._used.items() if e >= now}
        if c["n"] in self._used:
            raise ApprovalError("approval token already used")
        self._used[c["n"]] = c["exp"]
        return Approval(c["a"], c["s"], c["u"], c["exp"], c["n"])
