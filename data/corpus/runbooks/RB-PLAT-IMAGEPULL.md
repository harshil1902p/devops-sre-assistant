---
doc_id: RB-PLAT-IMAGEPULL
doc_type: runbook
title: 'Runbook: ImagePullBackOff - AKS cannot pull image from Azure Container Registry'
scenario: image-pull-backoff
service: orders-api
environment: any
language: any
infrastructure: azure
error_type: ImagePullBackOff
error_codes:
- ImagePullBackOff
- ErrImagePull
- 401 Unauthorized
- manifest unknown
severity: SEV3
created: 2025-07-15
tags:
- acr
- aks
- deployment
- kubernetes
- registry
---

# Runbook: ImagePullBackOff - AKS cannot pull image from Azure Container Registry

> **Service:** `orders-api` | **Severity:** SEV3 | **Error type:** `ImagePullBackOff` | **Infrastructure:** azure

## When To Use This Runbook
Use this runbook when `orders-api` (or a similar any service) reports any of the error signatures below.

```text
ImagePullBackOff
ErrImagePull
401 Unauthorized
manifest unknown
```

## Symptoms
- New pods stuck in ImagePullBackOff / ErrImagePull; old pods keep running.

## Example Log Output
```text
Warning  Failed     kubelet  Failed to pull image "contosoacr.azurecr.io/orders-api:1.42.0": rpc error: code = Unknown desc = failed to authorize: failed to fetch anonymous token: unexpected status: 401 Unauthorized
Warning  Failed     kubelet  Error: ErrImagePull
Normal   BackOff    kubelet  Back-off pulling image "contosoacr.azurecr.io/orders-api:1.42.0"
```

## Root Cause
Either the tag doesn't exist (`manifest unknown`) or the AKS kubelet identity lost `AcrPull` on the
registry (`401 Unauthorized`) after the cluster was recreated.

## Diagnosis Steps
1. `kubectl describe pod` events show the exact pull error.
2. `az acr repository show-tags` to confirm the tag exists.
3. `az aks check-acr` to validate AKS-to-ACR auth.

```bash
kubectl -n orders describe pod -l app=orders-api | grep -A3 Failed
az acr repository show-tags -n contosoacr --repository orders-api -o table
az aks check-acr -g rg-prod -n aks-prod --acr contosoacr.azurecr.io
```

## Mitigation (Stop The Bleeding)
- Re-attach the registry: `az aks update -g rg-prod -n aks-prod --attach-acr contosoacr`.

## Permanent Fix
- Manage the AcrPull role assignment in Bicep/Terraform with the cluster.
- Pin images by digest and verify the tag exists in CI before deploying.

```bash
az aks update -g rg-prod -n aks-prod --attach-acr contosoacr
```

## Related Documents
- Post-mortem: PM-INC-2025-0249 (2025-07-08)
- Knowledge base: KB-0010
