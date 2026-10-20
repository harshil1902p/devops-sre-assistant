---
doc_id: RB-AUTH-LIVENESS-DEP
doc_type: runbook
title: 'Runbook: Cascading restarts because the liveness probe checks downstream dependencies'
scenario: liveness-probe-dependency
service: auth-service
environment: any
language: python
infrastructure: kubernetes
error_type: ProbeFailure
error_codes:
- 'Liveness probe failed: HTTP probe failed with statuscode: 503'
- Container auth-service failed liveness probe, will be restarted
- Readiness probe failed
severity: SEV1
created: 2025-02-18
tags:
- cascading-failure
- health-checks
- kubernetes
- probes
---

# Runbook: Cascading restarts because the liveness probe checks downstream dependencies

> **Service:** `auth-service` | **Severity:** SEV1 | **Error type:** `ProbeFailure` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `auth-service` (or a similar python service) reports any of the error signatures below.

```text
Liveness probe failed: HTTP probe failed with statuscode: 503
Container auth-service failed liveness probe, will be restarted
Readiness probe failed
```

## Symptoms
- All replicas restart at once when a dependency (DB/Redis) has a brief blip.
- Outage lasts much longer than the dependency blip because pods are restarting and cold.

## Example Log Output
```text
Warning  Unhealthy  kubelet  Liveness probe failed: HTTP probe failed with statuscode: 503
Normal   Killing    kubelet  Container auth-service failed liveness probe, will be restarted
```

## Root Cause
The liveness probe called `/health`, which checked Postgres and Redis. A 30-second Redis failover made
every pod fail liveness; kubelet killed them all, turning a small blip into a full outage.

## Diagnosis Steps
1. Inspect probe config: `kubectl get deploy auth-service -o yaml | grep -A8 livenessProbe`.
2. Check whether the probed endpoint touches dependencies.

```bash
kubectl -n auth get deploy auth-service -o yaml | grep -A8 -E 'livenessProbe|readinessProbe'
kubectl -n auth get events --field-selector reason=Unhealthy --sort-by=.lastTimestamp | tail
```

## Mitigation (Stop The Bleeding)
- Point liveness at a dependency-free endpoint (`/healthz`) or relax failureThreshold.

## Permanent Fix
- Liveness = process alive (no dependency checks). Readiness = can serve traffic (checks DB/Redis) and only removes the pod from load balancing.
- Add a startupProbe for slow starts instead of long initialDelaySeconds.

```yaml
livenessProbe:
  httpGet: {path: /healthz, port: 8080}
  periodSeconds: 10
  failureThreshold: 3
readinessProbe:
  httpGet: {path: /readyz, port: 8080}
  periodSeconds: 5
```

## Related Documents
- Post-mortem: PM-INC-2025-0071 (2025-02-11)
- Knowledge base: KB-0011
