---
doc_id: RB-PAY-DNS-EAI-AGAIN
doc_type: runbook
title: 'Runbook: Intermittent DNS resolution failures - Temporary failure in name resolution'
scenario: dns-resolution
service: payments-api
environment: any
language: python
infrastructure: kubernetes
error_type: DNSError
error_codes:
- 'socket.gaierror: [Errno -3] Temporary failure in name resolution'
- getaddrinfo EAI_AGAIN
- Temporary failure in name resolution
severity: SEV3
created: 2025-06-05
tags:
- coredns
- dns
- kubernetes
- network
---

# Runbook: Intermittent DNS resolution failures - Temporary failure in name resolution

> **Service:** `payments-api` | **Severity:** SEV3 | **Error type:** `DNSError` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `payments-api` (or a similar python service) reports any of the error signatures below.

```text
socket.gaierror: [Errno -3] Temporary failure in name resolution
getaddrinfo EAI_AGAIN
Temporary failure in name resolution
```

## Symptoms
- Random outbound call failures to external APIs; rate correlates with traffic.

## Example Log Output
```text
httpx.ConnectError: [Errno -3] Temporary failure in name resolution
socket.gaierror: [Errno -3] Temporary failure in name resolution
```

## Root Cause
CoreDNS was overloaded: `ndots:5` made every external lookup try 4 cluster search domains first,
multiplying DNS queries, and CoreDNS had only 2 replicas.

## Diagnosis Steps
1. `kubectl -n kube-system top pod -l k8s-app=kube-dns`.
2. Check CoreDNS logs for timeouts; check pod `/etc/resolv.conf` ndots.

```bash
kubectl -n kube-system logs -l k8s-app=kube-dns --since=15m | grep -i timeout
kubectl -n payments exec deploy/payments-api -- cat /etc/resolv.conf
```

## Mitigation (Stop The Bleeding)
- Scale CoreDNS replicas.

## Permanent Fix
- Use FQDNs with trailing dot or set `dnsConfig.options ndots: 2`; deploy NodeLocal DNSCache.

```yaml
dnsConfig:
  options:
    - name: ndots
      value: "2"
```

## Related Documents
- Post-mortem: PM-INC-2025-0204 (2025-05-29)
- Knowledge base: KB-0016
