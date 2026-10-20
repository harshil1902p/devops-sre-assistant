---
doc_id: RB-AUTH-REDIS-TIMEOUT
doc_type: runbook
title: 'Runbook: Redis socket timeouts in auth-service caused by a blocked Redis'
scenario: redis-timeout
service: auth-service
environment: any
language: python
infrastructure: kubernetes
error_type: TimeoutError
error_codes:
- 'redis.exceptions.TimeoutError: Timeout reading from socket'
- redis.exceptions.TimeoutError
- Timeout reading from socket
severity: SEV2
created: 2025-05-05
tags:
- cache
- latency
- redis
- sessions
- slowlog
---

# Runbook: Redis socket timeouts in auth-service caused by a blocked Redis

> **Service:** `auth-service` | **Severity:** SEV2 | **Error type:** `TimeoutError` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `auth-service` (or a similar python service) reports any of the error signatures below.

```text
redis.exceptions.TimeoutError: Timeout reading from socket
redis.exceptions.TimeoutError
Timeout reading from socket
```

## Symptoms
- Login returns 500 after ~1 s (the configured `socket_timeout`).
- Redis is reachable (no connection refused) and `PING` eventually answers, but slowly.
- Redis `SLOWLOG` shows a long-running command, or `CLIENT PAUSE` / a large `KEYS *` was executed.

## Example Log Output
```text
{"level": "ERROR", "service": "auth-service", "logger": "auth.api", "msg": "unhandled error on POST /login", "exc_type": "redis.exceptions.TimeoutError", "exc_message": "Timeout reading from socket"}
{"level": "WARNING", "service": "auth-service", "logger": "auth.access", "msg": "POST /login 500", "duration_ms": 1003.2}
```

## Root Cause
Redis is single-threaded for commands. One expensive command (`KEYS *` on millions of keys, a big
`DEL` of a huge set, a Lua script, or a `CLIENT PAUSE` during a failover) blocks every other client. The
auth-service rate-limiter and session writes then exceed the 1 s `socket_timeout` and raise
`redis.exceptions.TimeoutError`. The connection is fine; the server is busy.

## Diagnosis Steps
1. Differentiate from Redis being down: `ConnectionError` / `ECONNREFUSED` means unreachable (see RB-NOTIF-REDIS-REFUSED); `TimeoutError: Timeout reading from socket` means connected but not answering in time.
2. Check `redis-cli SLOWLOG GET 10` for long commands and `redis-cli INFO commandstats`.
3. Check `redis-cli --latency` and `INFO clients` (blocked_clients).
4. Look for a recent cron/maintenance job or admin running `KEYS *`.

```bash
docker exec sre-demo-auth-cache-1 redis-cli SLOWLOG GET 10
docker exec sre-demo-auth-cache-1 redis-cli INFO clients | grep -E 'blocked|connected'
kubectl -n auth exec deploy/auth-cache -- redis-cli --latency -i 1
```

## Mitigation (Stop The Bleeding)
- Stop the offending client/job; `CLIENT KILL` it if necessary.
- If Redis is paused for failover, wait for the pause to end (seconds) and verify recovery.

## Permanent Fix
- Replace `KEYS` with incremental `SCAN`; use `UNLINK` instead of `DEL` for big keys.
- Make the rate limiter fail-open: if Redis times out, allow the login rather than returning 500.
- Add a circuit breaker and keep `socket_timeout` small (0.5-1 s) so the API degrades quickly.
- Rename/disable dangerous commands in production (`rename-command KEYS ""`).

```python
try:
    attempts = cache.incr(attempts_key)
except redis.exceptions.TimeoutError:
    log.warning("rate limiter unavailable, failing open")
    attempts = 0
```

## Related Documents
- Post-mortem: PM-INC-2025-0156 (2025-04-28)
- Knowledge base: KB-0001
