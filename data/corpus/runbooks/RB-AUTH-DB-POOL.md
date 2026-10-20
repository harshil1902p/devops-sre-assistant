---
doc_id: RB-AUTH-DB-POOL
doc_type: runbook
title: 'Runbook: SQLAlchemy QueuePool exhaustion in auth-service'
scenario: db-pool-exhaustion
service: auth-service
environment: any
language: python
infrastructure: kubernetes
error_type: TimeoutError
error_codes:
- sqlalchemy.exc.TimeoutError
- QueuePool limit of size 5 overflow 2 reached, connection timed out
- sqlalche.me/e/20/3o7r
severity: SEV2
created: 2025-04-19
tags:
- connection-pool
- database
- latency
- postgres
- sqlalchemy
---

# Runbook: SQLAlchemy QueuePool exhaustion in auth-service

> **Service:** `auth-service` | **Severity:** SEV2 | **Error type:** `TimeoutError` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `auth-service` (or a similar python service) reports any of the error signatures below.

```text
sqlalchemy.exc.TimeoutError
QueuePool limit of size 5 overflow 2 reached, connection timed out
sqlalche.me/e/20/3o7r
```

## Symptoms
- `POST /login` returns HTTP 500 for a share of requests while others succeed.
- Failed requests take almost exactly `pool_timeout` (5 s) before failing: the tell-tale sign of waiting for a pooled connection.
- Runtime stats show `Checked out connections` = pool_size + max_overflow (7) and `Connections in pool: 0`.
- Postgres itself is healthy: low CPU, `pg_isready` OK, far below `max_connections`.

## Example Log Output
```text
{"level": "ERROR", "service": "auth-service", "logger": "auth.api", "msg": "unhandled error on POST /login", "exc_type": "sqlalchemy.exc.TimeoutError", "exc_message": "QueuePool limit of size 5 overflow 2 reached, connection timed out, timeout 5.00 (Background on this error at: https://sqlalche.me/e/20/3o7r)"}
{"level": "WARNING", "service": "auth-service", "logger": "auth.access", "msg": "POST /login 500", "duration_ms": 5004.7}
{"level": "INFO", "service": "auth-service", "logger": "auth.runtime", "msg": "runtime stats (db pool: Pool size: 5  Connections in pool: 0 Current Overflow: 2 Current Checked out connections: 7)"}
```

## Root Cause
Every request needs a connection from SQLAlchemy's client-side `QueuePool`. The pool allows
`pool_size` (5) persistent connections plus `max_overflow` (2) temporary ones. When all 7 are checked out,
new requests wait `pool_timeout` seconds and then raise `sqlalchemy.exc.TimeoutError`.
Connections stay checked out because some code path holds them for a long time: slow queries, sessions
that are never closed, or long transactions wrapping remote calls. The database is not the
bottleneck; the **client pool** is.

## Diagnosis Steps
1. Confirm the error string is `QueuePool limit ... reached` (client-side). If you see `FATAL: sorry, too many clients already` instead, use runbook RB-PAY-PG-MAXCONN; that is the server-side limit.
2. Check pool gauges in the runtime stats log line: checked-out connections pinned at the limit confirms exhaustion.
3. Find what is holding connections: query `pg_stat_activity` for long-running or `idle in transaction` sessions from the service's user.
4. Correlate with recent deploys: new endpoints that open a session without a context manager, or added `pg_sleep`/heavy reports.

```bash
# Live logs (staging / Docker)
docker logs sre-demo-auth-service-1 --since 10m | grep -E 'QueuePool|runtime stats'
# Production (AKS)
kubectl -n auth logs deploy/auth-service --since=10m | grep QueuePool
# Who is holding connections?
psql -U auth -d auth -c "SELECT pid, state, now()-query_start AS age, left(query,80)
  FROM pg_stat_activity WHERE usename='auth' ORDER BY age DESC;"
```

## Mitigation (Stop The Bleeding)
- Terminate the offending long-running sessions: `SELECT pg_terminate_backend(pid)` for queries older than a few seconds.
- Restart the pods/containers to release leaked connections (temporary relief only).
- Scale out replicas only if Postgres `max_connections` has headroom: each replica adds pool_size + max_overflow connections.

## Permanent Fix
- Increase `pool_size` to 20 and `max_overflow` to 10 in `app/database.py` (or the `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` env vars). Budget: replicas x (pool_size + max_overflow) must stay below Postgres `max_connections` minus a reserve.
- Always use `with SessionLocal() as db:` so sessions are returned to the pool, including on exceptions.
- Set a server-side `statement_timeout` (e.g., 3s) for the service role so one slow query cannot pin a connection forever.
- Alert on pool saturation (checked-out / capacity > 80% for 2 min), not just on 5xx.

```python
# app/database.py
engine = create_engine(
    DATABASE_URL,
    pool_size=20,          # was 5
    max_overflow=10,       # was 2
    pool_timeout=5,
    pool_pre_ping=True,
    connect_args={"options": "-c statement_timeout=3000"},
)
```

## Related Documents
- Post-mortem: PM-INC-2025-0142 (2025-04-12)
- Knowledge base: KB-0004
