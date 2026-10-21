"""Per-principal token bucket. In-memory = per replica; with N replicas use a shared store (Redis) or the gateway
(API Management) so the limit is global. LLM calls are the expensive resource we're protecting."""

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucket:
    def __init__(self, per_minute: int, burst: int | None = None, clock=time.monotonic):
        self.rate = per_minute / 60.0
        self.capacity = float(burst or per_minute)
        self.clock = clock
        self.buckets: dict[str, _Bucket] = {}

    def take(self, key: str) -> float:
        """Consume one token. Returns 0 if allowed, else seconds until a token is available (Retry-After)."""
        now = self.clock()
        b = self.buckets.setdefault(key, _Bucket(self.capacity, now))
        b.tokens = min(self.capacity, b.tokens + (now - b.updated) * self.rate)
        b.updated = now
        if b.tokens >= 1:
            b.tokens -= 1
            return 0.0
        return (1 - b.tokens) / self.rate
