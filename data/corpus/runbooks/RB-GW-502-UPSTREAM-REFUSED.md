---
doc_id: RB-GW-502-UPSTREAM-REFUSED
doc_type: runbook
title: 'Runbook: 502 Bad Gateway during deploys - upstream connection refused'
scenario: gateway-502
service: api-gateway
environment: any
language: nginx
infrastructure: kubernetes
error_type: BadGateway
error_codes:
- 502 Bad Gateway
- 'connect() failed (111: Connection refused) while connecting to upstream'
- upstream prematurely closed connection
severity: SEV3
created: 2025-07-08
tags:
- deployment
- graceful-shutdown
- ingress
- nginx
- readiness
---

# Runbook: 502 Bad Gateway during deploys - upstream connection refused

> **Service:** `api-gateway` | **Severity:** SEV3 | **Error type:** `BadGateway` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `api-gateway` (or a similar nginx service) reports any of the error signatures below.

```text
502 Bad Gateway
connect() failed (111: Connection refused) while connecting to upstream
upstream prematurely closed connection
```

## Symptoms
- Bursts of 502 only during rolling deployments.

## Example Log Output
```text
2025/07/01 10:02:13 [error] 29#29: *1201 connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://10.0.5.33:8080/v1/orders"
10.0.1.9 - - [01/Jul/2025:10:02:13 +0000] "POST /v1/orders HTTP/1.1" 502 150
```

## Root Cause
Pods were removed from endpoints after they had already stopped accepting connections (no preStop
delay), and new pods were marked Ready before the app listened (readiness probe hitting a static path).

## Diagnosis Steps
1. Correlate 502 timestamps with rollout events.
2. Check readinessProbe and preStop hook / terminationGracePeriodSeconds.

```bash
kubectl -n orders rollout history deploy/orders-api
kubectl -n orders get deploy orders-api -o yaml | grep -A6 -E 'preStop|readinessProbe'
```

## Mitigation (Stop The Bleeding)
- Pause rollouts; scale up to reduce per-pod impact.

## Permanent Fix
- Add `preStop: sleep 10` so endpoints are removed before shutdown; handle SIGTERM gracefully.
- Readiness probe must hit an endpoint that requires the app to be fully started.

```yaml
lifecycle:
  preStop:
    exec: {command: ["sleep", "10"]}
terminationGracePeriodSeconds: 30
```

## Related Documents
- Post-mortem: PM-INC-2025-0241 (2025-07-01)
- Knowledge base: KB-0014
