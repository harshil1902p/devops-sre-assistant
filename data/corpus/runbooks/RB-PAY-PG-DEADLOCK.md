---
doc_id: RB-PAY-PG-DEADLOCK
doc_type: runbook
title: 'Runbook: Postgres deadlocks between concurrent payment transactions'
scenario: pg-deadlock
service: payments-api
environment: any
language: python
infrastructure: kubernetes
error_type: DeadlockDetected
error_codes:
- 'psycopg.errors.DeadlockDetected: deadlock detected'
- Process 4121 waits for ShareLock on transaction
- 40P01
severity: SEV3
created: 2025-05-21
tags:
- database
- locking
- postgres
- transactions
---

# Runbook: Postgres deadlocks between concurrent payment transactions

> **Service:** `payments-api` | **Severity:** SEV3 | **Error type:** `DeadlockDetected` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `payments-api` (or a similar python service) reports any of the error signatures below.

```text
psycopg.errors.DeadlockDetected: deadlock detected
Process 4121 waits for ShareLock on transaction
40P01
```

## Symptoms
- Intermittent 500s on transfer endpoints with SQLSTATE 40P01.
- Only under concurrent traffic touching the same pair of rows.

## Example Log Output
```text
psycopg.errors.DeadlockDetected: deadlock detected
DETAIL:  Process 4121 waits for ShareLock on transaction 99812; blocked by process 4188.
Process 4188 waits for ShareLock on transaction 99807; blocked by process 4121.
HINT:  See server log for query details.
CONTEXT:  while updating tuple (12,7) in relation "accounts"
```

## Root Cause
Two transactions lock the same rows in opposite order (A then B vs B then A). Postgres detects the
cycle after `deadlock_timeout` and aborts one transaction.

## Diagnosis Steps
1. Read the DETAIL lines: they name both processes and the relation.
2. Enable `log_lock_waits = on` to see waits before they become deadlocks.
3. Review code paths that update multiple rows in one transaction.

```bash
kubectl -n payments logs deploy/payments-api --since=30m | grep -A4 'deadlock detected'
psql -c "SELECT pid, wait_event_type, wait_event, left(query,80) FROM pg_stat_activity WHERE wait_event_type='Lock';"
```

## Mitigation (Stop The Bleeding)
- Retry the aborted transaction (40P01 is safe to retry) with jittered backoff.

## Permanent Fix
- Always lock rows in a deterministic order, e.g., `SELECT ... FOR UPDATE ORDER BY id`.
- Keep transactions short; never call external services inside a transaction.

```python
ids = sorted([from_id, to_id])
rows = db.execute(text("SELECT id, balance FROM accounts WHERE id = ANY(:ids) ORDER BY id FOR UPDATE"), {"ids": ids})
```

## Related Documents
- Post-mortem: PM-INC-2025-0187 (2025-05-14)
- Knowledge base: KB-0006
