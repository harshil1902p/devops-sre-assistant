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
| 1 | Demo "auth-service" that really fails + synthetic runbook / post-mortem corpus | ⏳ |
| 2 | RAG: parent-child chunking, pgvector, hybrid search (vector + BM25, RRF) | ⏳ |
| 3 | MCP server: `inspect_live_logs`, `check_system_health`, `read_env_config`, `search_runbooks` | ⏳ |
| 4 | LangGraph ReAct agent wired to MCP tools, cited answers, memory | ⏳ |
| 5 | Guardrails: spaCy + regex redaction, prompt-injection defence, human-in-the-loop | ⏳ |
| 6 | Evaluation: golden set, retrieval metrics, DeepEval LLM-as-a-Judge, CI gate | ⏳ |
| 7 | Azure: Azure OpenAI, AI Search, Monitor/KQL, Key Vault, Managed Identity, Container Apps, Bicep | ⏳ |
| 8 | Production hardening: API + auth, OpenTelemetry, cost tracking, AKS | ⏳ |

## Quick start (local, free)

```bash
ollama pull qwen2.5:7b && ollama pull nomic-embed-text
cp .env.example .env
uv sync
uv run pytest
uv run python scripts/smoke_test.py
```

Learning notes and interview prep for each phase live in [docs/learning](docs/learning).
