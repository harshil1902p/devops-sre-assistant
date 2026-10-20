# Example: overlapping incidents (pool exhaustion, then an unrelated OOM kill)

Real run against the demo stack (Phase 4). Question: *"auth-service seems unstable in staging. What happened recently?"*

Graph path: `start_turn → intake → triage → agent → tools → agent → remind (coverage guard: OOM window not investigated) → agent → tools (2 parallel searches) → agent → respond`

Usage: `{"llm_calls": 5, "input_tokens": 49157, "output_tokens": 1417, "llm_latency_ms": 16804, "cost_usd_upper_bound": 0.02193, "project_spend_usd": 0.044163, "wall_seconds": 25.7}`

---

## Diagnosis: `auth-service` is **degraded** (confidence: high)

The auth-service in staging experienced two main issues recently. First, a burst of SQLAlchemy connection pool timeout errors occurred due to exhaustion of the client-side connection pool, causing some login requests to fail. Shortly after, the container was OOM killed due to memory usage exceeding the 256 MB limit, causing a restart. The root causes are client-side connection pool exhaustion and an unbounded in-memory cache causing memory growth and OOM kill.

### Evidence

- functions.check_system_health at 2026-10-20T22:45:00.032638373-04:00: service running and healthy with memory usage 69.2/256.0 MB
- functions.get_recent_events at 2026-10-20T22:39:17+00:00: OOM kill event - container exceeded its memory limit; kernel OOM killer fired
- functions.get_recent_events at 2026-10-20T22:39:18+00:00: container died with exitCode=137 (SIGKILL by OOM killer)
- functions.inspect_live_logs at 2026-10-20T22:37:54.853+00:00 to 2026-10-20T22:38:10.383+00:00: 48 errors of sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 2 reached, connection timed out, timeout 5.00
- functions.inspect_live_logs at 2026-10-20T22:39:13.525+00:00: memory usage grew from 85.5 MB to 229.5 MB before OOM kill
- TIMELINE 22:36:59-22:37:54: error burst of sqlalchemy.exc.TimeoutError
- TIMELINE 22:39:13-22:39:20: OOM kill with memory growth

### Root cause

The client-side SQLAlchemy connection pool is exhausted due to all 7 connections (pool_size 5 + max_overflow 2) being checked out, causing timeout errors. Separately, an unbounded in-memory cache caused memory to grow beyond the container limit, triggering the kernel OOM killer and container restart.

### Immediate mitigation

- Restart the auth-service container to release leaked connections and clear memory (requires approval)
- Temporarily increase the container memory limit from 256Mi to 512Mi to reduce OOM kill frequency (requires approval)

### Permanent fix

- Increase SQLAlchemy pool_size to 20 and max_overflow to 10 to handle higher concurrency
- Ensure all database sessions are properly closed using context managers to avoid connection leaks
- Set a server-side statement_timeout (e.g., 3s) to prevent long-running queries from holding connections
- Add monitoring and alerting on connection pool saturation and memory usage above 85% of limit
- Bound the in-memory cache size using TTLCache or LRU cache to prevent unbounded memory growth
- Add memory regression and soak tests in CI to detect leaks early

### Sources

- [RB-AUTH-DB-POOL] Runbook: SQLAlchemy QueuePool exhaustion in auth-service
- [PM-INC-2025-0142] Post-mortem INC-2025-0142: SQLAlchemy QueuePool exhaustion in auth-service
- [RB-AUTH-OOMKILLED] Runbook: Container OOMKilled (exit code 137) from an unbounded in-memory cache
- [PM-INC-2025-0164] Post-mortem INC-2025-0164: Container OOMKilled (exit code 137) from an unbounded in-memory cache
- [KB-0008] Kubernetes pod restarts with exit code 137 and no error in logs

### Open questions

- What are the current values of pool_size and max_overflow in the auth-service database configuration?
- Is the memory limit of 256Mi appropriate for the current workload or should it be permanently increased?
