---
doc_id: RB-GW-504-UPSTREAM-TIMEOUT
doc_type: runbook
title: 'Runbook: 504 Gateway Timeout from api-gateway - upstream timed out'
scenario: gateway-504
service: api-gateway
environment: any
language: nginx
infrastructure: kubernetes
error_type: GatewayTimeout
error_codes:
- 504 Gateway Timeout
- 'upstream timed out (110: Connection timed out) while reading response header from upstream'
- proxy_read_timeout
severity: SEV2
created: 2025-06-19
tags:
- gateway
- ingress
- latency
- nginx
- timeout
---

# Runbook: 504 Gateway Timeout from api-gateway - upstream timed out

> **Service:** `api-gateway` | **Severity:** SEV2 | **Error type:** `GatewayTimeout` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `api-gateway` (or a similar nginx service) reports any of the error signatures below.

```text
504 Gateway Timeout
upstream timed out (110: Connection timed out) while reading response header from upstream
proxy_read_timeout
```

## Symptoms
- Clients get 504 after exactly `proxy_read_timeout` (60 s default).
- Only specific slow endpoints affected; upstream pods healthy.

## Example Log Output
```text
2025/06/12 13:44:02 [error] 31#31: *88213 upstream timed out (110: Connection timed out) while reading response header from upstream, client: 10.0.1.4, server: api.contoso.com, request: "GET /v1/reports/annual HTTP/1.1", upstream: "http://10.0.5.21:8080/v1/reports/annual"
10.0.1.4 - - [12/Jun/2025:13:44:02 +0000] "GET /v1/reports/annual HTTP/1.1" 504 160 "-" "Mozilla/5.0"
```

## Root Cause
The upstream took longer than the gateway's `proxy_read_timeout` to start responding. The gateway gave up
and returned 504 while the upstream kept working (wasting resources).

## Diagnosis Steps
1. Find which upstream path: the nginx error log line includes `request` and `upstream`.
2. Check upstream latency (p99) for that endpoint; look for slow queries.
3. Compare 504 (upstream too slow) with 502 (upstream refused/closed connection; see RB-GW-502-UPSTREAM-REFUSED).

```bash
kubectl -n ingress logs deploy/ingress-nginx-controller --since=15m | grep 'upstream timed out'
kubectl -n reports logs deploy/reports-api --since=15m | grep -E 'duration_ms|slow'
```

## Mitigation (Stop The Bleeding)
- Raise timeout for that route only via ingress annotation, as a stopgap.

## Permanent Fix
- Make long operations asynchronous (202 Accepted + polling/job id).
- Fix the slow query; add upstream timeouts shorter than the gateway's.

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "120"
```

## Related Documents
- Post-mortem: PM-INC-2025-0219 (2025-06-12)
- Knowledge base: KB-0013
