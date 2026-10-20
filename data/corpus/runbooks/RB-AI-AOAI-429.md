---
doc_id: RB-AI-AOAI-429
doc_type: runbook
title: 'Runbook: Azure OpenAI 429 Too Many Requests - rate limit exceeded'
scenario: azure-openai-429
service: sre-assistant
environment: any
language: python
infrastructure: azure
error_type: RateLimitError
error_codes:
- 'openai.RateLimitError: Error code: 429'
- Requests to the ChatCompletions_Create Operation under Azure OpenAI API have exceeded token rate limit
- 429 Too Many Requests
- Retry-After
severity: SEV3
created: 2025-07-29
tags:
- azure-openai
- llm
- quota
- rate-limit
- tpm
---

# Runbook: Azure OpenAI 429 Too Many Requests - rate limit exceeded

> **Service:** `sre-assistant` | **Severity:** SEV3 | **Error type:** `RateLimitError` | **Infrastructure:** azure

## When To Use This Runbook
Use this runbook when `sre-assistant` (or a similar python service) reports any of the error signatures below.

```text
openai.RateLimitError: Error code: 429
Requests to the ChatCompletions_Create Operation under Azure OpenAI API have exceeded token rate limit
429 Too Many Requests
Retry-After
```

## Symptoms
- LLM calls fail in bursts with 429; `Retry-After` header present.

## Example Log Output
```text
openai.RateLimitError: Error code: 429 - {'error': {'code': '429', 'message': 'Requests to the ChatCompletions_Create Operation under Azure OpenAI API version 2024-10-21 have exceeded token rate limit of your current OpenAI S0 pricing tier. Please retry after 6 seconds.'}}
```

## Root Cause
Deployment Tokens-Per-Minute (TPM) quota exceeded because an eval job and live traffic shared one deployment,
and prompts included very large contexts.

## Diagnosis Steps
1. Azure Monitor metric `AzureOpenAIRequests` filtered by StatusCode=429; check `ProcessedPromptTokens`.
2. Inspect token usage per request in app telemetry.

```bash
az monitor metrics list --resource $AOAI_ID --metric AzureOpenAIRequests --filter "StatusCode eq '429'" --interval PT1M
```

## Mitigation (Stop The Bleeding)
- Honour Retry-After with exponential backoff and jitter.

## Permanent Fix
- Separate deployments for batch/eval vs interactive traffic; raise TPM quota or use provisioned throughput.
- Reduce prompt tokens (better chunking, fewer retrieved chunks); cache repeated queries.

```python
client = AzureOpenAI(..., max_retries=5)  # SDK retries 429 honouring Retry-After
```

## Related Documents
- Post-mortem: PM-INC-2025-0262 (2025-07-22)
- Knowledge base: KB-0017
