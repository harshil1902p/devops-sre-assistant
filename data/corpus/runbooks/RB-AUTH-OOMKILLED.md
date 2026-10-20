---
doc_id: RB-AUTH-OOMKILLED
doc_type: runbook
title: 'Runbook: Container OOMKilled (exit code 137) from an unbounded in-memory cache'
scenario: oom-killed
service: auth-service
environment: any
language: python
infrastructure: kubernetes
error_type: OOMKilled
error_codes:
- OOMKilled
- exit code 137
- 'Reason: OOMKilled'
- die 137
severity: SEV2
created: 2025-05-09
tags:
- container
- limits
- memory
- memory-leak
- oom
- restarts
---

# Runbook: Container OOMKilled (exit code 137) from an unbounded in-memory cache

> **Service:** `auth-service` | **Severity:** SEV2 | **Error type:** `OOMKilled` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `auth-service` (or a similar python service) reports any of the error signatures below.

```text
OOMKilled
exit code 137
Reason: OOMKilled
die 137
```

## Symptoms
- Pod/container restarts; restart count increases; brief connection resets for in-flight requests.
- Memory (RSS) climbs steadily between restarts, then drops back after each restart (saw-tooth).
- No Python exception in the logs: the kernel kills the process with SIGKILL, so the app cannot log.
- `kubectl describe pod` shows `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`.

## Example Log Output
```text
{"level": "INFO", "logger": "auth.runtime", "msg": "runtime stats", "rss_mb": 115.7}
{"level": "INFO", "logger": "auth.runtime", "msg": "runtime stats", "rss_mb": 175.8}
{"level": "INFO", "logger": "auth.runtime", "msg": "runtime stats", "rss_mb": 223.8}
{"level": "INFO", "logger": "auth.api", "msg": "starting auth-service (env=production)"}
# kubectl describe pod: Last State: Terminated  Reason: OOMKilled  Exit Code: 137
```

## Root Cause
The container exceeded its memory limit (256Mi) and the kernel OOM killer sent SIGKILL (128 + 9 = exit 137).
The process memory grew because an in-process cache had no size bound or TTL, so every unique key stayed
in memory forever. Restarting hides the leak until memory fills again.

## Diagnosis Steps
1. Confirm it was the OOM killer, not a crash: exit code 137 + `OOMKilled` reason. Exit code 1 would be an app error; 143 is SIGTERM (graceful).
2. Evidence disappears on restart: use `kubectl logs --previous` and `kubectl describe pod` (Last State). On Docker, `docker events --filter event=oom` shows `oom` followed by `die` with exitCode 137; `docker inspect` State is reset by the restart.
3. Plot RSS over time (runtime stats or container_memory_working_set_bytes). Steady growth = leak; sudden spike = large request/batch.
4. Profile memory in staging with `tracemalloc` or `memray` to find the growing allocation site.

```bash
# Kubernetes
kubectl -n auth describe pod -l app=auth-service | grep -A6 'Last State'
kubectl -n auth logs deploy/auth-service --previous --tail=50
kubectl -n auth top pod -l app=auth-service
# Docker
docker events --since 30m --filter container=sre-demo-auth-service-1 --filter event=oom --filter event=die
docker stats --no-stream sre-demo-auth-service-1
```

## Mitigation (Stop The Bleeding)
- Temporarily raise the memory limit (e.g., 256Mi -> 512Mi) to reduce restart frequency while you fix the leak.
- Roll back the release that introduced the growth if one is identified.

## Permanent Fix
- Bound the cache: `functools.lru_cache(maxsize=...)`, `cachetools.TTLCache`, or move the cache to Redis.
- Set requests close to real usage and limits with headroom; alert on memory > 85% of limit.
- Add a memory regression test / soak test in CI for long-running endpoints.

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"
```

## Related Documents
- Post-mortem: PM-INC-2025-0164 (2025-05-02)
- Knowledge base: KB-0008
