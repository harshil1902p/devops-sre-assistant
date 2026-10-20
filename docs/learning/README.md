# Learning Journal: Intelligent DevOps & SRE Assistant

This folder is your study guide for the project. Each phase has one file with the same sections:

1. **What we built:** files, commands, and the reasons behind them
2. **Concepts you must be able to explain:** the theory behind the code
3. **Interview questions:** short answers you can say out loud
4. **Quiz:** the model answers are hidden in collapsible blocks, so answer first and then check

## Phases

| # | Topic | Notes | Key concepts |
|---|---|---|---|
| 0 | Foundations | [00-foundations.md](00-foundations.md) | 12-factor config, provider abstraction, embeddings & cosine similarity, temperature, local vs hosted LLMs |
| 1 | Demo service & corpus | [01-demo-service-and-corpus.md](01-demo-service-and-corpus.md) | Structured logs, liveness vs readiness, pool vs DB limits, OOMKilled/137, metadata schema, hard negatives, drift tests |
| 2 | Hybrid RAG | *(next)* | Parent-child chunking, pgvector/HNSW, BM25/tsvector, Reciprocal Rank Fusion |
| 3 | MCP server | — | Model Context Protocol, tools/resources, stdio vs HTTP transport, least privilege |
| 4 | Agent | — | ReAct, tool calling, LangGraph state/graph, memory, citations |
| 5 | Guardrails | — | Redaction (spaCy + regex), prompt injection, human-in-the-loop, audit |
| 6 | Evaluation | — | Golden sets, recall@k/MRR, LLM-as-a-Judge, faithfulness, CI gates |
| 7 | Azure | — | Azure OpenAI, AI Search hybrid + semantic ranker, Monitor/KQL, Key Vault, Managed Identity, Bicep |
| 8 | Production | — | AuthN/Z, OpenTelemetry, cost/token tracking, scaling, AKS |

Also see [skills-map.md](skills-map.md) for how the project maps to the Senior AI Engineer job description.

## How to revise
- **Before an interview:** read only the *Interview questions* section in each file, then do every quiz without looking at the answers.
- **For the project deep-dive:** say the end-to-end flow out loud: user question → agent → MCP logs → redaction → hybrid RAG → cited answer → eval.
- **For each phase:** be ready to name one trade-off you made and one thing you would change at bank scale.
