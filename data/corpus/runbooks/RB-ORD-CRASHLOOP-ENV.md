---
doc_id: RB-ORD-CRASHLOOP-ENV
doc_type: runbook
title: 'Runbook: CrashLoopBackOff on startup due to a missing environment variable / secret'
scenario: crashloop-missing-env
service: orders-worker
environment: any
language: python
infrastructure: kubernetes
error_type: KeyError
error_codes:
- CrashLoopBackOff
- 'KeyError: ''DATABASE_URL'''
- Back-off restarting failed container
- CreateContainerConfigError
severity: SEV2
created: 2025-04-03
tags:
- configmap
- deployment
- kubernetes
- secrets
- startup
---

# Runbook: CrashLoopBackOff on startup due to a missing environment variable / secret

> **Service:** `orders-worker` | **Severity:** SEV2 | **Error type:** `KeyError` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `orders-worker` (or a similar python service) reports any of the error signatures below.

```text
CrashLoopBackOff
KeyError: 'DATABASE_URL'
Back-off restarting failed container
CreateContainerConfigError
```

## Symptoms
- Pods never become Ready; status CrashLoopBackOff with exponential back-off between restarts.
- Exit code 1 with a configuration exception at startup.

## Example Log Output
```text
Traceback (most recent call last):
  File "/srv/app/config.py", line 14, in load_config
    database_url=os.environ["DATABASE_URL"],
KeyError: 'DATABASE_URL'
Warning  BackOff  kubelet  Back-off restarting failed container orders-worker
```

## Root Cause
The new Deployment referenced a Secret key renamed during a secret rotation (`DATABASE_URL` -> `DB_URL`).
The app fails fast at startup. If the referenced Secret itself doesn't exist, the pod instead shows
`CreateContainerConfigError`.

## Diagnosis Steps
1. `kubectl logs <pod> --previous` to read the startup exception.
2. `kubectl describe pod` events for `CreateContainerConfigError` / missing secret.
3. Diff the Deployment env with the Secret keys.

```bash
kubectl -n orders get pods -l app=orders-worker
kubectl -n orders logs deploy/orders-worker --previous | tail -20
kubectl -n orders get secret orders-db -o jsonpath='{.data}' | jq 'keys'
```

## Mitigation (Stop The Bleeding)
- `kubectl rollout undo deploy/orders-worker` to restore the previous ReplicaSet.

## Permanent Fix
- Validate config at startup with a clear error message listing missing keys.
- Use External Secrets / Azure Key Vault CSI driver with schema checks in CI.

```yaml
env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: orders-db
        key: DATABASE_URL
```

## Related Documents
- Post-mortem: PM-INC-2025-0120 (2025-03-27)
- Knowledge base: KB-0009
