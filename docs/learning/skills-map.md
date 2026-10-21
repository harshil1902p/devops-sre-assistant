# Skills Map: project ↔ Senior AI Engineer job description

## Must-haves in the job description and where this project covers them

| Job description requirement | Where it's covered |
|---|---|
| Building MCP servers and tools | Phase 3: custom MCP server (`inspect_live_logs`, `check_system_health`, `read_env_config`, `search_runbooks`) |
| Agentic patterns: tool use, ReAct, orchestration, memory | Phase 4: LangGraph ReAct agent with MCP tools and conversation memory |
| LLM API integration & prompt engineering | Phases 0, 4, 7: provider abstraction, system prompts, structured outputs, Azure OpenAI |
| RAG systems: vector DBs, embeddings, retrieval strategies | Phase 2: pgvector, parent-child chunking, hybrid BM25 + vector with RRF. Phase 7: Azure AI Search |
| .NET/C#, Python, SQL, Git, CI/CD | Python + SQL (pgvector/tsvector) throughout; GitHub Actions in Phase 6; C# from your RBC work |
| Cloud + containers | Docker/Compose (Phase 1+), Azure Container Apps and AKS (Phases 7–8) |
| Security, data governance, responsible AI | Phase 5: redaction, read-only tools, human approval, audit log. Phase 7: Managed Identity, Key Vault |
| Model evaluation & governance frameworks | Phase 6: golden set, retrieval metrics, DeepEval LLM-as-a-Judge, CI quality gate |

## Other skills: how they could fit

| Skill | How to use it in this project | Recommendation | How to talk about it |
|---|---|---|---|
| LLM evaluation & testing (golden sets, LLM-as-a-Judge) | 50-incident golden set; a stronger model judges whether answers are correct, faithful and safe | **Must do** (Phase 6) | "I measure RAG quality instead of trusting it; regressions fail CI." |
| Responsible AI / governance | Read-only tools, block destructive commands, human approval for risky actions, audit trail | **Must do** (Phase 5) | Least privilege and safe autonomous agents |
| NLP (spaCy, transformers) | spaCy NER + regex to mask IPs, secrets and PII before logs reach the LLM | **Do** (Phase 5) | Data-privacy engineering |
| Cloud certification (AZ-204 / AI-102) | Azure OpenAI, AI Search, Monitor, Key Vault, Container Apps | **Do AI-102 after Phase 7** | Backs up a project that already runs in the cloud |
| Intelligent Document Processing / OCR | Azure Document Intelligence turns PDF or screenshot post-mortems into Markdown before indexing | Stretch | Multi-format ingestion |
| Open-source contributions | Publish the repo; possibly an upstream fix later (e.g. `langchain-mcp-adapters`) | At the end | Community and code quality |
| UiPath | A UiPath workflow calls the assistant's API when an alert fires | Low (stretch) | Enterprise automation integration |
| SOAP APIs, Go/Rust | Don't fit naturally | Skip | Be honest in interviews and say you can learn them |

## Metrics you will **measure** (not guess) in Phase 6

| KPI | How it's measured | Target |
|---|---|---|
| MTTR reduction | Time 10 incidents diagnosed by hand vs with the assistant | 75–80% |
| Retrieval quality | recall@5 / MRR: is the correct runbook in the top 5 results? | 90%+ (Phase 2: **1.00 hit@1**; Phase 6 agent: retrieval coverage **1.00**, citation precision **1.00**, 0 hallucinated citations over 21 cases) |
| Faithfulness | DeepEval LLM-as-a-Judge over the golden set | 90%+ (Phase 6: groundedness **1.00**, correctness **0.94**, 21 cases) |
| Token cost | Tokens per diagnosis, naive chunking vs parent-child | ~30% fewer (Phase 2 measured: **11% fewer** tokens at higher fix coverage, 1.00 vs 0.94, on short docs) |
| Leakage prevention | Share of planted secrets/IPs masked by the redaction pipeline | 100% (Phase 5: secrets + structured identifiers **1.00**, person names **0.88**) |

Only put a number on your resume after you have measured it. Keep the raw results in `eval/results/`.
