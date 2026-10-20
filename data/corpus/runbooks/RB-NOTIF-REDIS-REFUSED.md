---
doc_id: RB-NOTIF-REDIS-REFUSED
doc_type: runbook
title: 'Runbook: notification-service cannot connect to Redis - ECONNREFUSED'
scenario: redis-connection-refused
service: notification-service
environment: any
language: node
infrastructure: kubernetes
error_type: ConnectionRefused
error_codes:
- 'Error: connect ECONNREFUSED 10.0.3.15:6379'
- ECONNREFUSED
- 'redis.exceptions.ConnectionError: Error 111 connecting'
severity: SEV2
created: 2025-06-28
tags:
- failover
- ioredis
- network
- node
- redis
---

# Runbook: notification-service cannot connect to Redis - ECONNREFUSED

> **Service:** `notification-service` | **Severity:** SEV2 | **Error type:** `ConnectionRefused` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `notification-service` (or a similar node service) reports any of the error signatures below.

```text
Error: connect ECONNREFUSED 10.0.3.15:6379
ECONNREFUSED
redis.exceptions.ConnectionError: Error 111 connecting
```

## Symptoms
- Errors start instantly (no timeout wait) with ECONNREFUSED.
- Redis pod restarted, was rescheduled, or its Service has no endpoints.

## Example Log Output
```text
[ioredis] Unhandled error event: Error: connect ECONNREFUSED 10.0.3.15:6379
    at TCPConnectWrapper.afterConnect [as oncomplete] (node:net:1555:16)
{"level":"error","service":"notification-service","msg":"queue publish failed","err":"Connection is closed."}
```

## Root Cause
Nothing is listening on the target IP/port: Redis pod was OOMKilled and restarting, or the client
hard-coded a pod IP that changed after rescheduling instead of using the Service DNS name.

## Diagnosis Steps
1. `kubectl get endpoints redis -n notif`: empty endpoints means no ready Redis pod.
2. Check whether the configured host is a pod IP (bad) or service DNS (good).
3. `kubectl describe pod` on Redis for restarts / OOMKilled.

```bash
kubectl -n notif get endpoints redis
kubectl -n notif describe pod -l app=redis | grep -A5 'Last State'
kubectl -n notif get configmap notification-config -o yaml | grep REDIS
```

## Mitigation (Stop The Bleeding)
- Point clients at the Service DNS name `redis.notif.svc.cluster.local`.
- Restart Redis if crashed; investigate its own OOM/restart reason.

## Permanent Fix
- Use a managed Redis (Azure Cache for Redis) with replicas or Redis Sentinel.
- Configure ioredis `retryStrategy` with backoff and a bounded offline queue.

```javascript
const redis = new Redis({
  host: "redis.notif.svc.cluster.local",
  retryStrategy: (times) => Math.min(times * 200, 2000),
  maxRetriesPerRequest: 3,
});
```

## Related Documents
- Post-mortem: PM-INC-2025-0233 (2025-06-21)
- Knowledge base: KB-0002
