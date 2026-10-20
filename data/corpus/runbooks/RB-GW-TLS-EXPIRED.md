---
doc_id: RB-GW-TLS-EXPIRED
doc_type: runbook
title: 'Runbook: TLS certificate expired - x509 certificate has expired'
scenario: tls-cert-expired
service: api-gateway
environment: any
language: any
infrastructure: kubernetes
error_type: CertificateExpired
error_codes:
- 'x509: certificate has expired or is not yet valid'
- 'SSL: CERTIFICATE_VERIFY_FAILED'
- certificate has expired
- NET::ERR_CERT_DATE_INVALID
severity: SEV1
created: 2025-01-26
tags:
- cert-manager
- certificates
- letsencrypt
- security
- tls
---

# Runbook: TLS certificate expired - x509 certificate has expired

> **Service:** `api-gateway` | **Severity:** SEV1 | **Error type:** `CertificateExpired` | **Infrastructure:** kubernetes

## When To Use This Runbook
Use this runbook when `api-gateway` (or a similar any service) reports any of the error signatures below.

```text
x509: certificate has expired or is not yet valid
SSL: CERTIFICATE_VERIFY_FAILED
certificate has expired
NET::ERR_CERT_DATE_INVALID
```

## Symptoms
- All HTTPS clients fail at once at a specific time; browsers show NET::ERR_CERT_DATE_INVALID.

## Example Log Output
```text
ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1006)
cert-manager: Failed to create Order: 403 urn:ietf:params:acme:error:unauthorized: Invalid response from http://api.contoso.com/.well-known/acme-challenge/
```

## Root Cause
cert-manager failed to renew because the HTTP-01 challenge path was blocked by a new WAF rule; the
certificate expired 30 days later.

## Diagnosis Steps
1. `openssl s_client -connect api.contoso.com:443 -servername api.contoso.com | openssl x509 -noout -dates`.
2. `kubectl describe certificate` and `kubectl get challenges` for renewal errors.

```bash
echo | openssl s_client -connect api.contoso.com:443 -servername api.contoso.com 2>/dev/null | openssl x509 -noout -dates
kubectl -n ingress describe certificate api-tls | tail -20
kubectl get challenges -A
```

## Mitigation (Stop The Bleeding)
- Unblock the challenge path and force renewal: `cmctl renew api-tls -n ingress`.

## Permanent Fix
- Alert on certificates expiring within 14 days; monitor cert-manager Ready condition.
- Prefer DNS-01 challenges to avoid HTTP path dependencies.

```yaml
# Prometheus alert
- alert: CertificateExpiringSoon
  expr: certmanager_certificate_expiration_timestamp_seconds - time() < 14*24*3600
```

## Related Documents
- Post-mortem: PM-INC-2025-0031 (2025-01-19)
- Knowledge base: KB-0015
