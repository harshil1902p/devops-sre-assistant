# Intelligent DevOps & SRE Assistant

[![ci](https://github.com/harshil1902p/devops-sre-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/harshil1902p/devops-sre-assistant/actions/workflows/ci.yml)

An AI production companion that diagnoses crashes, server errors and infrastructure bugs by combining:

- **Live telemetry** via a custom **MCP server** (Docker / Kubernetes / Azure Monitor logs, health, config)
- **Institutional memory** via **hybrid RAG** over runbooks and post-mortems: locally pgvector (HNSW) + Postgres full-text (`ts_rank_cd` with rare-term pruning, a BM25 approximation); in the cloud Azure AI Search (HNSW + true BM25, server-side RRF)
- A **ReAct agent** (LangGraph) that chains the two and returns a cited, step-by-step fix
- **Guardrails** (secret/PII redaction, read-only diagnostic tools, optional restart only with signed human approval) and an **evaluation harness** (golden set + LLM-as-a-Judge) in CI

```
 User ──▶ LangGraph ReAct agent ──▶ MCP server ──▶ Docker / K8s / Azure Monitor (live logs, health)
               │        ▲
               │        └──── redaction guardrails (before any LLM call)
               ▼
          Hybrid RAG  ──▶ pgvector (local) / Azure AI Search (cloud)  ◀── ingest runbooks & post-mortems
```

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Repo, typed config, provider abstraction (Ollama ⇄ Azure OpenAI), smoke test | ✅ |
| 1 | Demo "auth-service" that really fails + synthetic runbook / post-mortem corpus | ✅ |
| 2 | RAG: parent-child chunking, pgvector, hybrid search (vector + full-text, RRF) + cross-encoder rerank | ✅ |
| 3 | MCP server (6 read-only tools, resource, prompt; stdio + streamable HTTP), event collector, socket proxy | ✅ |
| 4 | LangGraph agent (workflow + ReAct), Azure OpenAI keyless, memory, guards, grounding, spend cap | ✅ |
| 5 | Guardrails: secrets + PII pseudonymisation (regex, Luhn, spaCy NER), injection defence in depth, HITL remediation with signed approvals | ✅ |
| 6 | Evaluation: replayed telemetry, 21-case golden set (incl. adversarial), deterministic metrics, DeepEval G-Eval judge, CI quality gate | ✅ |
| 7 | Azure: AI Search hybrid + semantic ranker, Azure Monitor (log shipper, DCR, KQL backend), Key Vault, managed identity, GitHub OIDC cloud evals, Bicep | ✅ |
| 8 | Production: FastAPI (Entra ID app roles, four-eyes approvals, rate limit, SSE), OpenTelemetry → App Insights, hardened container on Container Apps, read-only Kubernetes backend + manifests, teardown | ✅ |

## Retrieval benchmark (34 hand-written queries, 51 docs, nomic-embed-text, CPU)

| Config | hit@1 | fix in context | context tokens | p50 latency |
|---|---|---|---|---|
| naive 256-token windows, vector, top 5 | 0.85 | 0.91 | 1113 | 56 ms |
| naive windows, hybrid, top 5 | 0.97 | 0.94 | 1123 | 67 ms |
| parent-child, hybrid (RRF), top 3 | 0.94 | 0.94 | 1406 | 77 ms |
| **parent-child, hybrid + cross-encoder rerank, top 2** | **1.00** | **1.00** | **1003** | 995 ms |

Saturated scores mean this query set is too easy; Phase 6 builds a harder golden set. Raw results: `eval/results/retrieval.json`.

On **Azure AI Search** (text-embedding-3-small, same queries): vector 0.94, BM25 0.97, **hybrid (server-side RRF) 1.00** hit@1 at 338 ms;
semantic ranker +60 ms, local cross-encoder +450 ms. Raw results: `eval/results/retrieval_azure.json`.

## Agent evaluation (21 golden cases, replayed telemetry, gpt-4.1-mini)

17 single-incident cases (one per failure scenario) + 4 adversarial: overlapping incidents, healthy service, prompt injection in logs, novel error.

| Metric | Result | Gate |
|---|---|---|
| Retrieval coverage / cited primary / citation precision | 1.00 / 1.00 / 1.00 | ≥ 0.90 / 0.85 / 0.80 |
| Hallucinated citations · unsafe unlabelled actions | 0 · 0 | = 0 |
| Injection flagged / resisted · both overlapping incidents covered | 1.00 / 1.00 · 1.00 | = 1.0 |
| **Correctness** (G-Eval vs reference) · **groundedness** (G-Eval vs tool context) | **0.94 · 1.00** | ≥ 0.70 · 0.75 |
| Median time per diagnosis · cost per diagnosis | 12.2 s · ≈ $0.0075 | ≤ 45 s |

CI enforces `eval/thresholds.yaml` on the committed results (`scripts/eval_gate.py`); running the evals costs tokens, so it's on demand.
Details, judge caveats and lessons: [docs/learning/06-evaluation.md](docs/learning/06-evaluation.md).

## Quick start (local, free)

```bash
ollama pull qwen2.5:7b && ollama pull nomic-embed-text
uv run python scripts/bootstrap_local_secrets.py   # generates gitignored .env files with random local credentials
uv sync
uv run pytest
uv run python scripts/smoke_test.py

# Demo "staging" environment + failure injection
colima start
docker compose -f demo/compose.yaml up -d --build
uv run python scripts/demo_chaos.py pool        # or: redis | oom | traffic | reset

# Knowledge base (regenerate after editing data/scenarios/*.yaml)
uv run python scripts/generate_corpus.py

# Vector store + indexing + retrieval
docker compose -f infra/compose.yaml up -d
uv run python scripts/ingest.py                 # incremental; --reset to rebuild
uv run python scripts/search.py "QueuePool limit of size 10 overflow 20 reached"
uv run python scripts/retrieval_benchmark.py
uv run pytest -m integration                    # live checks against the demo stack

# Evaluation (needs Azure OpenAI configured in .env; ~$0.19 per full run, capped by SRE_SPEND_LIMIT_USD)
uv run python scripts/run_evals.py --no-judge --limit 3   # quick smoke
uv run python scripts/run_evals.py                        # full golden set + LLM judge
uv run python scripts/eval_gate.py                        # free: thresholds check

# Azure (Phase 7): deploy, index, ship logs, run the agent against Azure backends (see docs/learning/07-azure.md)
az deployment sub create -l canadaeast -f infra/azure/main.bicep -p infra/azure/main.bicepparam \
  -p principalId=$(az ad signed-in-user show --query id -o tsv) alertEmail=<you@example.com>
uv run python scripts/ingest_azure.py
uv run python scripts/ship_telemetry.py --interval 30
SRE_TELEMETRY_BACKEND=azure_monitor SRE_RETRIEVER_BACKEND=azure_search uv run python -m sre_assistant.agent "auth-service is returning 500s"
```

**Start here:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): flow, models, embeddings, data, technology choices and known limitations.
Learning notes and interview prep for each phase live in [docs/learning](docs/learning).

## Production API (Phase 8)

```bash
# local, no auth (development only)
SRE_API_AUTH=disabled uv run uvicorn sre_assistant.api.app:app --port 8000
curl -N -X POST localhost:8000/v1/diagnose -H 'content-type: application/json' -H 'accept: text/event-stream' \
  -d '{"question": "auth-service is returning 500s"}'

# Azure: Entra app (roles SRE.Diagnose / SRE.Approve), registry, image, Container Apps
uv run python scripts/setup_entra_app.py                       # prints the app (client) id
az deployment sub create ... -p deployRegistry=true            # then: docker build/push to the registry
az deployment sub create ... -p deployRegistry=true deployApps=true imageTag=<tag> apiAudience=api://<app id>
TOKEN=$(az account get-access-token --resource api://<app id> --query accessToken -o tsv)
curl -X POST https://<app>.azurecontainerapps.io/v1/diagnose -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"question": "..."}'

scripts/teardown.sh apps    # stop everything hourly-priced (registry, Container Apps, App Insights)
```

Kubernetes: `kubectl apply -k k8s/` (read-only RBAC, Pod Security `restricted`, Workload Identity). Details: [docs/learning/08-production.md](docs/learning/08-production.md).

## Security notes

- **No credentials are committed.** Local credentials live in gitignored files (`.env`, `demo/.env`, `infra/.env`,
  `demo/auth-service/config/app.yaml`) generated by `scripts/bootstrap_local_secrets.py`. Azure uses keyless
  Entra ID auth (no keys at all). CI runs `gitleaks` over the full git history on every push.
- Fake secrets used to test redaction are assembled at runtime in the tests, so scanners never see literals.
