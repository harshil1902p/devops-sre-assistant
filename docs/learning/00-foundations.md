# Phase 0 — Foundations: config, provider abstraction, smoke test

## What we built

| File | Purpose |
|---|---|
| `pyproject.toml` | Single source of truth for deps (uv), pytest and ruff config. `src/` layout. |
| `src/sre_assistant/config.py` | Typed settings via **pydantic-settings**: env vars (`SRE_*`) or `.env`, validated at startup. |
| `src/sre_assistant/llm/providers.py` | **Provider abstraction**: returns LangChain `BaseChatModel` / `Embeddings` for Ollama now, Azure OpenAI in Phase 7. |
| `scripts/smoke_test.py` | Proves chat + embeddings work and the embedding dimension matches config. |
| `tests/test_config.py` | Unit tests for config validation, secret masking, provider factory. |

Local stack: **Ollama** serving `qwen2.5:7b` (chat, supports tool calling) and `nomic-embed-text` (768-dim embeddings).

### Environment setup done in this phase
| Tool | How installed | Why |
|---|---|---|
| Ollama | already installed (`open -a Ollama`) | Free local LLM + embeddings server on `localhost:11434` |
| `qwen2.5:7b` | `ollama pull qwen2.5:7b` (4.7 GB) | Chat model with good **tool-calling** support for its size |
| `nomic-embed-text` | `ollama pull nomic-embed-text` (274 MB) | 768-dim embedding model, 8k-token context |
| Colima + Docker CLI + Compose | `brew install colima docker docker-compose` | Lightweight Docker runtime (no Docker Desktop licence) for pgvector & demo services |
| Compose plugin path | `~/.docker/config.json` → `"cliPluginsExtraDirs": ["/usr/local/lib/docker/cli-plugins"]` | So `docker compose` finds the brew plugin |
| uv | already installed | Fast Python package/venv manager; `uv.lock` makes builds reproducible |

Daily start-up: `open -a Ollama` and `colima start` (stop with `colima stop`).

### Smoke-test results (your machine, Intel i7-9750H, 16 GB)
```
[chat]  32.4s (cold start, includes loading the model into RAM) -> correct explanation of QueuePool exhaustion
[chat]  token usage: 58 in / 40 out
[embed] 0.9s, dim=768 (configured 768)
[embed] cos(query, related)   = 0.699   "connection pool exhausted" vs "QueuePool limit overflowed"
[embed] cos(query, unrelated) = 0.372   "connection pool exhausted" vs "rotate TLS certificates"
```
Takeaway: almost no shared words, yet the related pair scores far higher → embeddings capture **meaning**,
which is why RAG can find a runbook even when the log wording differs from the document wording.

## Concepts you must be able to explain

### 1. Why a provider abstraction?
The agent, RAG and eval code depend on interfaces (`BaseChatModel`, `Embeddings`), not on a vendor SDK.
That's the **Dependency Inversion Principle** / **Strategy pattern**. Benefits:
- Develop free and offline on Ollama, deploy on Azure OpenAI with *only* env changes.
- Run evals across models (e.g., compare `qwen2.5:7b` vs `gpt-4o-mini`) with the same harness.
- Avoid vendor lock-in; a bank will want a fallback if one model endpoint is throttled or deprecated.

### 2. 12-factor config and secrets
- Config lives in the environment, not code. Validation **fails fast** at startup (e.g., Azure provider without endpoint).
- Secrets are `SecretStr` → masked in logs/reprs. `.env` is git-ignored; `.env.example` documents keys.
- In Azure (Phase 7) we replace API keys with **Managed Identity** + **Key Vault** → no secrets at all in config.

### 3. Embeddings in one minute
- An embedding model maps text → a fixed-length vector (here 768 floats). Similar meaning ⇒ high **cosine similarity**.
- Smoke-test result: *"connection pool exhausted"* vs *"QueuePool limit overflowed"* = **0.70**, vs *"rotate TLS certs"* = **0.37**.
  The words barely overlap, yet the vectors are close; that's **semantic** search.
- The **dimension is a schema contract**: the pgvector column is `vector(768)`. Switch to `text-embedding-3-small` (1536)
  and you must **re-embed and re-index everything**. That's why `SRE_EMBEDDING_DIM` is validated.
- Never mix vectors from two different models in one index; the spaces aren't comparable.

### 4. Why temperature = 0 for an SRE assistant
Diagnostics should be **reproducible and conservative**. Low temperature reduces variance (not hallucination itself);
groundedness comes from RAG + citations (Phase 2/4) and is *measured* by evals (Phase 6).

### 5. Local vs hosted model trade-offs
| | Ollama (qwen2.5:7b) | Azure OpenAI (gpt-4o-mini / gpt-4o) |
|---|---|---|
| Cost | Free | Pay per token |
| Data residency | Never leaves the laptop | Stays in your Azure tenant/region |
| Quality / tool calling | Decent | Stronger |
| Latency (this Intel i7) | ~30 s cold, several s warm | ~1–3 s |
| Ops | You run it | Managed, SLA, quotas (TPM/RPM) |

## Interview questions you should be able to answer

1. **"How would you switch LLM vendors without rewriting the app?"** → Provider abstraction behind interfaces + config-driven factory; evals guard regressions when swapping.
2. **"What happens if you change embedding models?"** → Dimension/space changes → full re-embed + re-index; run blue/green indexes and switch an alias after eval passes.
3. **"How do you handle secrets for LLM keys?"** → Never in code; `SecretStr` locally; Managed Identity + Key Vault in Azure; rotate; least privilege.
4. **"What is cosine similarity and why not Euclidean?"** → Measures the angle (direction = meaning) and ignores magnitude; most embedding models are trained/normalised for it.
5. **"Why would a bank prefer a local or private model endpoint?"** → Data residency, PII/regulatory constraints, no data used for training, network isolation (Private Endpoints).

## Try it yourself
```bash
uv run python scripts/smoke_test.py        # run twice: notice the warm start is much faster
SRE_CHAT_MODEL=gemma3:270m uv run python scripts/smoke_test.py   # tiny model: compare answer quality
```

## Quiz (answer first, then expand to check)

**Q1. Your pgvector column is `vector(768)` and you switch to `text-embedding-3-small`. What breaks, and how do you migrate with zero downtime?**
<details><summary>Model answer</summary>

- **What breaks:** `text-embedding-3-small` outputs **1536** dims → inserts into `vector(768)` fail. Even if dims matched,
  the two models live in **different vector spaces**: a query embedded with model B compared to documents embedded
  with model A gives meaningless similarity scores. The HNSW index is also tied to the column's dimension.
- **Migration (blue/green re-index):**
  1. Create a new table/index (e.g., `chunks_v2` with `vector(1536)`), or a new Azure AI Search index.
  2. Re-embed **all** child chunks with the new model (batch, rate-limited, idempotent so it can resume).
  3. Run the **evaluation suite** (recall@k, faithfulness) against v2 and compare to v1.
  4. Flip a config flag/alias (`SRE_EMBEDDING_MODEL`, `SRE_EMBEDDING_DIM`, index name) so queries go to v2.
  5. Keep v1 for rollback, then drop it after a soak period.
- **Interview bonus:** store `embedding_model` + `embedding_version` as metadata on every row so you can detect mixed spaces.
</details>

**Q2. Why does the codebase call `get_chat_model()` instead of `ChatOllama(...)` directly?**
<details><summary>Model answer</summary>

Dependency inversion: business logic depends on the `BaseChatModel` interface; a factory picks the concrete vendor from
config. Swap Ollama → Azure OpenAI by env vars only, run the same evals across models, and mock it easily in tests.
</details>

**Q3. Does `temperature=0` prevent hallucinations?**
<details><summary>Model answer</summary>

No. It reduces **randomness** (more repeatable output). A model can still confidently produce a wrong answer at
temperature 0. Hallucinations are reduced by **grounding** (RAG context + required citations), constrained tools,
and they are **measured** with faithfulness evals (Phase 6).
</details>

**Q4. Why is the first chat call ~30 s but later calls much faster?**
<details><summary>Model answer</summary>

Cold start: Ollama loads ~4.7 GB of weights from disk into RAM on the first request, then keeps the model resident
(default ~5 min keep-alive). In production the equivalent is model warm-up / provisioned throughput; serverless
containers scaled to zero have the same cold-start problem.
</details>

**Q5. Where should an Azure OpenAI key live in production?**
<details><summary>Model answer</summary>

Ideally nowhere: use **Managed Identity** with Entra ID RBAC (`Cognitive Services OpenAI User` role). If a key is
unavoidable, store it in **Key Vault**, reference it from the app, rotate it, and never log it (`SecretStr`).
</details>
