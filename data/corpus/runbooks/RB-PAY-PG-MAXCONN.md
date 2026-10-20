---
doc_id: RB-PAY-PG-MAXCONN
doc_type: runbook
title: 'Runbook: Postgres rejects connections with ''too many clients already'''
scenario: pg-too-many-connections
service: payments-api
environment: any
language: python
infrastructure: kubernetes
error_type: OperationalError
error_codes:
- 'FATAL: sorry, too many clients already'
- psycopg.OperationalError
- remaining connection slots are reserved for non-replication superuser connections
severity: SEV1
created: 2025-06-10
tags:
- autoscaling
- database
- max_connections
- pgbouncer
- postgres
---

# Runbook: Postgres rejects connections with 'too many clients already'

> **Service:** `payments-api` | **Severity:** SEV1 | **Error type:** `OperationalError` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `payments-api` (or a similar python service) reports any of the error signatures below.

```text
FATAL: sorry, too many clients already
psycopg.OperationalError
remaining connection slots are reserved for non-replication superuser connections
```

## Symptoms
- New connections fail immediately (no waiting), unlike client-pool timeouts.
- Postgres server log shows `FATAL: sorry, too many clients already`.
- Usually coincides with a horizontal scale-out (HPA added pods) or a new service sharing the database.

## Example Log Output
```text
psycopg.OperationalError: connection failed: connection to server at "10.0.4.12", port 5432 failed: FATAL:  sorry, too many clients already
psycopg.OperationalError: connection failed: FATAL:  remaining connection slots are reserved for non-replication superuser connections
2025-06-03 09:14:55 UTC [3121]: FATAL:  sorry, too many clients already
```

## Root Cause
Postgres has a hard server-side limit, `max_connections`. Every application replica opens up to
`pool_size + max_overflow` connections, so total demand is `replicas x (pool_size + max_overflow)` plus
cron jobs and admin tools. When the HPA scaled payments-api from 6 to 20 pods, demand exceeded
`max_connections=200` and Postgres refused new clients.

## Diagnosis Steps
1. Count connections per application: `SELECT usename, application_name, count(*) FROM pg_stat_activity GROUP BY 1,2`.
2. Compare with `SHOW max_connections;` and the HPA replica count (`kubectl get hpa -n payments`).
3. Distinguish from client pool exhaustion: this error comes from the server and fails fast.

```bash
kubectl -n payments get hpa payments-api
psql -c "SHOW max_connections;"
psql -c "SELECT usename, application_name, state, count(*) FROM pg_stat_activity GROUP BY 1,2,3 ORDER BY 4 DESC;"
```

## Mitigation (Stop The Bleeding)
- Cap the HPA `maxReplicas` temporarily to bring connection demand under the limit.
- Kill idle connections: `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='idle' AND now()-state_change > interval '5 min'`.

## Permanent Fix
- Put PgBouncer in transaction-pooling mode in front of Postgres so thousands of client connections share ~50 server connections.
- Reduce per-pod pool size (pool_size=5, max_overflow=0) when running behind PgBouncer.
- Document a connection budget per service and alert at 80% of max_connections.

```ini
; pgbouncer.ini
[pgbouncer]
pool_mode = transaction
max_client_conn = 2000
default_pool_size = 50
reserve_pool_size = 10
```

## Related Documents
- Post-mortem: PM-INC-2025-0211 (2025-06-03)
- Knowledge base: KB-0005
