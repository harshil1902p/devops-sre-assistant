# Intelligent DevOps & SRE Assistant

An AI production companion that diagnoses crashes, server errors and infrastructure bugs by combining:

- **Live telemetry** via a custom **MCP server** (Docker / Kubernetes / Azure Monitor logs, health, config)
- **Institutional memory** via **hybrid RAG** (pgvector + BM25 → Azure AI Search) over runbooks and post-mortems
- A **ReAct agent** (LangGraph) that chains the two and returns a cited, step-by-step fix
- **Guardrails** (secret/PII redaction, read-only tools, human approval) and an **evaluation harness** (golden set + LLM-as-a-Judge) in CI

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
| 4 | LangGraph ReAct agent wired to MCP tools, cited answers, memory | ⏳ |
| 5 | Guardrails: spaCy + regex redaction, prompt-injection defence, human-in-the-loop | ⏳ |
| 6 | Evaluation: golden set, retrieval metrics, DeepEval LLM-as-a-Judge, CI gate | ⏳ |
| 7 | Azure: Azure OpenAI, AI Search, Monitor/KQL, Key Vault, Managed Identity, Container Apps, Bicep | ⏳ |
| 8 | Production hardening: API + auth, OpenTelemetry, cost tracking, AKS | ⏳ |

## Retrieval benchmark (34 hand-written queries, 51 docs, nomic-embed-text, CPU)

| Config | hit@1 | fix in context | context tokens | p50 latency |
|---|---|---|---|---|
| naive 256-token windows, vector, top 5 | 0.85 | 0.91 | 1113 | 56 ms |
| naive windows, hybrid, top 5 | 0.97 | 0.94 | 1123 | 67 ms |
| parent-child, hybrid (RRF), top 3 | 0.94 | 0.94 | 1406 | 77 ms |
| **parent-child, hybrid + cross-encoder rerank, top 2** | **1.00** | **1.00** | **1003** | 995 ms |

Saturated scores mean this query set is too easy; Phase 6 builds a harder golden set. Raw results: `eval/results/retrieval.json`.

## Quick start (local, free)

```bash
ollama pull qwen2.5:7b && ollama pull nomic-embed-text
cp .env.example .env
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
```

Learning notes and interview prep for each phase live in [docs/learning](docs/learning).
