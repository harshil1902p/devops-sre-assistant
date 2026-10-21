# Phase 7: Azure platform (AI Search, Azure Monitor, Key Vault, Managed Identity, OIDC)

> Goal: run the same agent on Azure-managed building blocks **without changing the agent**. Only the two adapters
> behind interfaces we designed in Phases 2-3 change: the retriever and the telemetry backend.

## What we built

| Path | Purpose |
|---|---|
| `infra/azure/modules/platform.bicep` | AI Search (**free** tier, keyless, semantic ranker free plan), Log Analytics (**0.1 GB/day cap**) + 3 custom tables + **Direct DCR**, Key Vault (RBAC), user-assigned **managed identity** with a **GitHub OIDC federated credential**, least-privilege role assignments. |
| `src/sre_assistant/rag/azure_search.py` | `AzureSearchRetriever` (same `retrieve()` contract as pgvector), index schema, incremental `ingest_azure`, OData filter builder. |
| `src/sre_assistant/mcp_server/backends/azure_monitor.py` | `AzureMonitorBackend`: the MCP tools read logs / health / events with **KQL**; staleness flag; read-only. |
| `src/sre_assistant/shipping.py`, `scripts/ship_telemetry.py` | Log **shipper**: Docker → Logs Ingestion API → DCR → tables; cursors, **secret redaction at the edge**. |
| `src/sre_assistant/config.py` | Backend switches (`SRE_RETRIEVER_BACKEND`, `SRE_TELEMETRY_BACKEND`) + `load_key_vault_secrets()`. |
| `scripts/ingest_azure.py`, `scripts/retrieval_benchmark.py --backend azure` | Build the index; benchmark Azure configs on the same 34 queries. |
| `.github/workflows/cloud-eval.yml` | Manually triggered evals against Azure, **OIDC** login, protected environment, per-run spend cap. |
| `tests/test_azure_units.py` | 18 tests with fakes: filters/KQL injection, small-to-big grouping, semantic ordering, staleness, cursors, edge redaction, Key Vault precedence. |

```
           ┌────────────── local "staging" (Docker) ──────────────┐
 services ─┤ stdout/stderr, events, health ─▶ ship_telemetry.py   │── Logs Ingestion API (Entra ID) ─▶ DCR ─▶ Log Analytics
           └──────────────────────────────────────────────────────┘                                 SreService{Logs,Events,Health}_CL
                                                                                                                │ KQL
 agent (LangGraph) ──stdio── MCP server ── AzureMonitorBackend ─────────────────────────────────────────────────┘
        │                         └─────── AzureSearchRetriever ──▶ Azure AI Search (hybrid BM25 + HNSW, RRF, semantic ranker)
        └── Azure OpenAI (gpt-4.1-mini)       secrets ◀── Key Vault          identity: az login (dev) / managed identity / GitHub OIDC
```

## 1. Ports and adapters paid off

The tools depend on `TelemetryBackend` (Phase 3) and the agent on a retriever with `retrieve()` (Phase 2). Moving to Azure
was two new classes and two config switches. The graph, prompts, guardrails and evals didn't change, which is also why the
**same eval harness** can now compare backends. Interview line: *"design the seams first; the cloud migration becomes an adapter."*

## 2. Azure AI Search: hybrid in one request

| pgvector (Phase 2) | Azure AI Search (Phase 7) |
|---|---|
| Two queries (HNSW + `tsvector`), RRF in Python | **One request**: `search_text` (BM25) + `vector_queries` (HNSW), **RRF server-side** |
| Our own `ts_rank` + document-frequency filter | Real BM25 (Lucene analyzers) |
| Local cross-encoder rerank (ONNX) | Option: **semantic ranker** (Microsoft's cross-encoder, L2 rerank) or keep the local one |
| Rows + joins (children → parents) | **Denormalised**: each child doc carries `parent_content` (no joins in a search index) |

Index design decisions:
- **One document per child chunk**: vectors match small, precise text; `parent_content` (retrievable, not searchable) is what
  the LLM reads. Small-to-big without a second round trip.
- **Keys**: chunk ids contain `#` and `/`, which keys don't allow → base64url key, original id kept as a field.
- **Filters** are built from an **allowlist** of fields with escaped values (`search.in(...)`). Same rule as SQL: never
  concatenate user/LLM input into a query language.
- **Incremental ingestion**: a fingerprint (metadata + body + chunker version + embedding model) per document; unchanged
  documents are skipped (re-run: 51 skipped, 0 embedded, 3.8 s). Embeddings cost money: only pay for what changed.
- Gotcha found while building: `SearchableField(type=Collection(String))` is silently created as a **string**; you must pass
  `collection=True`. Azure then rejected the upload ("StartArray ... PrimitiveValue expected"). Read SDK signatures.

### Measured (`eval/results/retrieval_azure.json`, same 34 queries as Phase 2)

| Config (text-embedding-3-small, 1536-d) | hit@1 | MRR | fix in context | p50 latency |
|---|---|---|---|---|
| vector only | 0.94 | 0.96 | 0.97 | 291 ms |
| keyword (BM25) only | 0.97 | 0.98 | 1.00 | 39 ms |
| **hybrid (RRF)** | **1.00** | **1.00** | **1.00** | 338 ms |
| hybrid + semantic ranker | 1.00 | 1.00 | 1.00 | 398 ms |
| hybrid + local cross-encoder (top 2 parents: 999 tokens) | 1.00 | 1.00 | 1.00 | 762 ms |

Reading it honestly:
- Hybrid beats either retriever alone again (vector misses exact error codes; BM25 misses paraphrased symptoms).
- With a stronger embedding model, **hybrid alone already saturates** this query set; locally (nomic-embed-text) we needed the
  cross-encoder to reach 1.00. So on this set rerankers can't show a gain; a harder set (more near-duplicate runbooks) would.
- The **semantic ranker costs ~60 ms**, the local cross-encoder ~450 ms on a laptop CPU. In the cloud the semantic ranker also
  removes a 90 MB ONNX model from the container (Phase 8 decision). Trade-off: free plan = 1,000 queries/month.

## 3. Azure Monitor: logs in, KQL out

**Ingestion (the modern way)**: Logs Ingestion API → **Data Collection Rule** (DCR) → custom table (`*_CL`).
- The DCR declares the input stream schema and a `transformKql` (here `source`; you could drop columns or filter rows *before*
  you pay for them). A **Direct** DCR has its own ingestion endpoint, so no separate Data Collection Endpoint.
- Permission to upload = `Monitoring Metrics Publisher` on the DCR (the name is historical). Keyless.
- The deprecated way you might still see: HTTP Data Collector API with a workspace **shared key**. Avoid it.

**The shipper** is a tiny version of what Fluent Bit / the Azure Monitor Agent do:
- Per-service **cursors** on Docker's nanosecond timestamps, so each line is sent once (cursor advances only after a successful upload).
- **Redact secrets at the edge**: nothing like `password=...` leaves the host. PII is pseudonymised later in the MCP server before
  the LLM (Log Analytics is inside our tenant and region, like the host's own logs). Interview point: decide *where* each
  data class may live, and enforce it at that boundary.
- Health is a **snapshot** every interval, not a live call.

**Querying**: `LogsQueryClient.query_workspace(workspace_id, kql, timespan=...)`.
```kusto
SreServiceHealth_CL | summarize arg_max(TimeGenerated, *) by Service          // latest snapshot per service
SreServiceLogs_CL | where Service == 'auth-service' | top 200 by TimeGenerated desc | sort by TimeGenerated asc | project Line
```
- `timespan` bounds the scan (cost and speed); `arg_max` is the idiom for "latest row per key".
- **KQL injection**: service names are allowlisted with a regex before they're quoted; invalid names never reach KQL (tested).

**What changes for the agent** (and why the backend reports it):
- **Ingestion latency**: measured ~6 min for the very first batch (tables being provisioned), then ~1-3 min. "Live" logs are a
  few minutes old. For a 3 a.m. outage that matters: a real system keeps both paths (kubectl/Docker for *now*, Log Analytics for *history*).
- **Staleness**: if the newest health snapshot is older than 3 min the status says `stale: last report N min ago, the shipper may be down`.
  Otherwise "healthy" could mean "we stopped hearing from it". (Absence of data is not evidence of health.)
- **Least data**: env vars and config files are *not* exported; `read_env_config` returns nothing and `restart` is refused.
  The cloud backend is read-only by construction.

End-to-end run (pool exhaustion injected locally, shipped, diagnosed from Azure): correct root cause, cited
`RB-AUTH-DB-POOL` + `PM-INC-2025-0142`, 3 LLM calls, 31 s, ≈ $0.009.

## 4. Identity: no keys anywhere

| Who | How they authenticate | What they can do |
|---|---|---|
| You (dev laptop) | `az login` → `DefaultAzureCredential` | Build index, ship logs, query logs, manage secrets |
| Agent identity (`id-sre-agent`, user-assigned MI) | Managed identity in Azure (Phase 8) / **GitHub OIDC** (now) | **Read** index, **read** logs, **read** secrets, call the model |
| GitHub Actions `cloud-eval` | OIDC token → federated credential → agent identity | Same as agent identity, only in the protected environment |

- `disableLocalAuth: true` on Azure OpenAI and AI Search: there are **no API keys** to leak.
- **User-assigned** (not system-assigned) identity: it exists before the apps, roles are granted once, and the same identity is
  reused by CI and Container Apps. System-assigned identities die with their resource.
- **OIDC federation**: GitHub signs a short-lived JWT; Entra trusts it only if `issuer`, `audience` and **subject**
  (`repo:harshil1902p/devops-sre-assistant:environment:cloud-eval`) match. No client secret stored in GitHub. The environment
  has a required reviewer, so a run needs a human click. What's stored in GitHub (client/tenant/subscription ids) are
  identifiers, not credentials.
- Role design: humans get *Contributor-style data roles* where they build things; the agent gets only `*Reader` / `*User` roles.

## 5. Key Vault

- RBAC mode (no access policies): one permission model for everything.
- `get_settings()` → `load_key_vault_secrets()`: precedence **explicit env > Key Vault > default**, so local overrides still work
  and tests never hit Azure. Values stay `SecretStr`.
- We moved `SRE_PII_KEY` and `SRE_APPROVAL_SECRET` out of `.env` into the vault (uploaded from a `umask 077` temp file, never on
  a command line, since command lines end up in shell history and `ps`).
- Soft delete (7 days) protects against accidental deletion; purge protection is off only because this is a demo you'll tear down.

## 6. Cost controls in this phase

| Resource | Price | Control |
|---|---|---|
| AI Search **free** | $0 | 50 MB, 3 indexes, 1 per subscription; semantic ranker free plan (1,000 queries/month) |
| Log Analytics | per GB ingested | **0.1 GB/day cap** in Bicep; this phase shipped ~2 MB |
| Key Vault | $0.03 / 10k operations | Secrets are read once per process (`lru_cache` on settings) |
| Managed identity, DCR, RBAC | $0 | |
| Embeddings for the index | $0.02 / 1M tokens | Incremental ingestion (~$0.002 per full rebuild) |

Not deployed yet: Container Apps and the registry (Phase 8), the only resources with an hourly price.

## Interview questions you should be able to answer

1. Why Azure AI Search over pgvector (and when would you keep pgvector)? (Managed hybrid + semantic ranker + scale vs. relational metadata, joins, one less service, cost.)
2. How does hybrid search work in Azure AI Search? (BM25 + vector in one request, fused with RRF; optional semantic L2 rerank.)
3. How do you stop query-language injection when an LLM picks the arguments? (Allowlist fields/names, escape values, never concatenate.)
4. Walk me through getting custom logs into Log Analytics today. (Logs Ingestion API → DCR stream + transform → `_CL` table; RBAC on the DCR.)
5. What's ingestion latency and how does it affect an incident assistant? (Minutes; keep a live path, flag stale data.)
6. Managed identity vs service principal with secret vs OIDC federation?
7. System- vs user-assigned managed identity: when each?
8. How would you keep Log Analytics costs under control? (DCR transforms, daily cap, Basic logs plan for verbose tables, retention.)

## Quiz (answer first, then expand)

**Q1. Why does each child document store its parent's full text?**
<details><summary>Model answer</summary>

Search indexes have no joins. Denormalising lets one query return the matched child (for ranking) and the parent (for the LLM),
which keeps small-to-big retrieval to a single round trip. The cost is duplicate storage (fine at 403 chunks; at scale you'd use a
second index or a key-value lookup by `parent_id`).
</details>

**Q2. Hybrid alone hit 1.00 on Azure but needed a reranker locally. Explain.**
<details><summary>Model answer</summary>

`text-embedding-3-small` represents these technical texts better than `nomic-embed-text`, so the fused candidate list is already
correctly ordered. The reranker adds value when first-stage ranking is wrong; on this (saturated) query set there's nothing left to fix.
Conclusion: you need a harder evaluation set before claiming a reranker helps, and you shouldn't pay ~450 ms for nothing.
</details>

**Q3. The backend says `running (stale: last report 7 min ago...)`. Why not just say `running`?**
<details><summary>Model answer</summary>

The snapshot is what the shipper saw 7 minutes ago. If the shipper or the host died, the last snapshot would say "running"
forever. Absence of new data must be visible, otherwise the agent would confidently report a healthy service that is down.
</details>

**Q4. Which role lets the shipper upload logs, and on which resource?**
<details><summary>Model answer</summary>

`Monitoring Metrics Publisher` on the **Data Collection Rule** (not on the workspace). Scope it to the DCR so the identity can only
write the streams that DCR declares.
</details>

**Q5. What exactly does the GitHub federated credential trust?**
<details><summary>Model answer</summary>

Tokens issued by `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`, subject
`repo:harshil1902p/devops-sre-assistant:environment:cloud-eval`. A fork, another branch's job without that environment, or another
repo can't get a token, and the environment has a required reviewer.
</details>

**Q6. Why is `read_env_config` empty in the Azure Monitor backend? Isn't that a regression?**
<details><summary>Model answer</summary>

It's a deliberate data-minimisation choice: configuration and environment variables are the most secret-dense data on a host,
and the cloud path doesn't need them to diagnose most incidents. The tool still works (it reports nothing available). If needed,
you'd export a *redacted config schema* explicitly, not raw env.
</details>

**Q7. Why upload Key Vault secrets from a temp file rather than `--value`?**
<details><summary>Model answer</summary>

Command-line arguments are visible to other users via `ps` and land in shell history. A file with `0600` permissions,
deleted right after, avoids both.
</details>
