# Learning Journal: Intelligent DevOps & SRE Assistant

This folder is your study guide for the project. Each phase has one file with the same sections:

1. **What we built:** files, commands, and the reasons behind them
2. **Concepts you must be able to explain:** the theory behind the code
3. **Interview questions:** short answers you can say out loud
4. **Quiz:** the model answers are hidden in collapsible blocks, so answer first and then check

## Phases

Big-picture overview first: [../ARCHITECTURE.md](../ARCHITECTURE.md).

| # | Topic | Notes | Key concepts |
|---|---|---|---|
| 0 | Foundations | [00-foundations.md](00-foundations.md) | 12-factor config, provider abstraction, embeddings & cosine similarity, temperature, local vs hosted LLMs |
| 1 | Demo service & corpus | [01-demo-service-and-corpus.md](01-demo-service-and-corpus.md) | Structured logs, liveness vs readiness, pool vs DB limits, OOMKilled/137, metadata schema, hard negatives, drift tests |
| 2 | Hybrid RAG | [02-hybrid-rag.md](02-hybrid-rag.md) | Parent-child (small-to-big) chunking, pgvector/HNSW, tsvector vs BM25, RRF, cross-encoder reranking, incremental ingestion, retrieval metrics |
| 3 | MCP server | [03-mcp-server.md](03-mcp-server.md) | MCP primitives (tools/resources/prompts), transports, tool design for LLMs, log fingerprinting, redaction, least privilege, socket proxy, event persistence, audit |
| 4 | Agent | [04-agent.md](04-agent.md) | Workflow vs agent, LangGraph state/reducers/checkpointer, ReAct, guards, incident windows, grounding, structured output, keyless Azure OpenAI, spend cap, publishing safely |
| 5 | Guardrails | [05-guardrails.md](05-guardrails.md) | Redaction vs pseudonymisation, Luhn, NER + fairness gap, injection defence in depth, capability control, interrupt()/HITL, signed approvals, output policy |
| 6 | Evaluation | [06-evaluation.md](06-evaluation.md) | Replay vs live evals, golden set + adversarial cases, deterministic metrics vs LLM-as-a-Judge (G-Eval), judge bias & calibration, thresholds before results, 429/TPM, CI gate |
| 7 | Azure | [07-azure.md](07-azure.md) | Ports & adapters, AI Search hybrid (server-side RRF) + semantic ranker, denormalised index, OData/KQL injection, Logs Ingestion API + DCR, edge redaction, ingestion latency & staleness, managed identity, GitHub OIDC federation, Key Vault, cost caps |
| 8 | Production | [08-production.md](08-production.md) | Entra ID JWT + app roles, ownership & four-eyes, rate limiting, SSE, OpenTelemetry → App Insights, container hardening, Container Apps probes/scale-to-zero, managed-identity env bug, read-only Kubernetes backend & RBAC, teardown, **final resume bullets** |

Also see [skills-map.md](skills-map.md) for how the project maps to the Senior AI Engineer job description.

## How to revise
- **Before an interview:** read only the *Interview questions* section in each file, then do every quiz without looking at the answers.
- **For the project deep-dive:** say the end-to-end flow out loud: user question → agent → MCP logs → redaction → hybrid RAG → cited answer → eval.
- **For each phase:** be ready to name one trade-off you made and one thing you would change at bank scale.
