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
