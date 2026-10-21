# Phase 8: Production (API, auth, observability, containers, Kubernetes)

> From "works on my laptop" to a service other people can call safely: authenticated, authorised, rate-limited,
> observable, deployed from an immutable image with no secrets, and cheap to run or tear down.

## What we built

| Path | Purpose |
|---|---|
| `src/sre_assistant/api/app.py` | FastAPI: `/healthz`, `/readyz`, `POST /v1/diagnose` (JSON or **SSE**), `GET /v1/threads/{id}`, `POST /v1/threads/{id}/approval`. Input validation, security headers, request ids. |
| `api/auth.py` | **Entra ID JWT validation** (JWKS, RS256 only, issuer, audience, expiry) + **app roles** `SRE.Diagnose` / `SRE.Approve`. |
| `api/service.py` | Long-lived agent service: one MCP session + one compiled graph per process; **thread ownership**; **four-eyes approvals**; concurrency cap. |
| `api/ratelimit.py` | Per-principal **token bucket** (429 + `Retry-After`). |
| `src/sre_assistant/telemetry.py` | **OpenTelemetry** traces + metrics (tokens, cost, duration, tool calls) → Application Insights (keyless). |
| `mcp_server/backends/kubernetes_backend.py` | Read-only **Kubernetes backend** (pods, logs incl. *previous* container, events, OOMKilled in `lastState`). |
| `Dockerfile`, `.dockerignore` | Multi-stage image from `uv.lock`, non-root (uid 10001), read-only-root compatible, allowlisted build context. |
| `infra/azure/modules/platform.bicep` | Registry (Basic, AcrPull via managed identity), App Insights (local auth off), Container Apps env + app (scale to zero, startup/liveness/readiness probes). |
| `scripts/setup_entra_app.py` | Idempotent Entra app registration: app roles, scope, v2 tokens, Azure CLI pre-authorised, assignment required. |
| `k8s/` | Namespace (Pod Security `restricted`), read-only RBAC, hardened Deployment, Service, NetworkPolicy, PDB, Kustomize. |
| `scripts/teardown.sh` | `apps` (stop hourly costs) or `all` (RG + purge soft-deleted + Entra app). |
| Tests | `test_api.py` (17: real JWTs signed with generated keys, RBAC, four-eyes, rate limit, SSE, env forwarding), `test_k8s_backend.py` (5), `test_k8s_manifests.py` (3 policy checks). |

```
client ──(Entra token)──▶ Container Apps ingress (HTTPS) ──▶ FastAPI ── auth (JWKS) ── roles ── rate limit
                                                              │
                                                              ├── LangGraph agent ──▶ Azure OpenAI          (managed identity)
                                                              └── MCP server (stdio, same container)
                                                                    ├── AzureMonitorBackend ──▶ Log Analytics (KQL)
                                                                    └── AzureSearchRetriever ──▶ AI Search (semantic ranker)
                       OpenTelemetry ──▶ Application Insights (same workspace)      secrets ◀── Key Vault
```

## 1. Authentication and authorisation

- **AuthN**: clients get an access token from Entra ID for *our API* (audience) and send `Authorization: Bearer`. The API
  validates it **offline** against Entra's published keys (JWKS, cached): signature, `iss` (our tenant only), `aud` (our API),
  `exp`/`nbf`. Only RS256 is accepted, which blocks *algorithm confusion* (`alg: none`, or HS256 signed with the public key).
- **AuthZ**: **app roles** in the token's `roles` claim, assigned to users/groups in Entra. Diagnose ≠ approve.
  "Assignment required" on the enterprise app means a user without a role can't even obtain a token.
- **Identity for authorisation is `oid`**, the stable object id, never the display name or email (those change and can collide).
- **Ownership**: threads belong to their requester; others get **404, not 403** (don't confirm existence). Thread ids are
  server-generated random ids, never user-chosen.
- **Four-eyes**: the person who requested a diagnosis can't approve the action it proposes (they *can* reject it). A classic
  bank control (segregation of duties) implemented in ~3 lines, and tested.
- Verified for real: a token from `az account get-access-token --resource api://<app id>` validated with both roles; no token → 401.

## 2. Abuse and cost protection

| Layer | Protects against |
|---|---|
| Pydantic limits (question ≤ 2000 chars, service/thread regex, since ≤ 24 h) | Oversized prompts (cost), path/selector injection |
| Token bucket per principal (6/min, 4/min in the cloud) | One user burning the LLM budget |
| Semaphore (`api_max_concurrent`) | Overloading the replica / the model's TPM quota |
| Spend guard (Phase 4) → **503** when exceeded | Runaway cost; the API degrades instead of erroring randomly |
| Azure budget + deployment TPM quota | Global limits outside our code |

Honest limitations (good interview material): the rate limiter, thread ownership map, in-memory checkpointer and spend
ledger are **per replica**. That's why `maxReplicas: 1`. To scale out: Postgres checkpointer (already supported:
`SRE_CHECKPOINTER=postgres`), ownership in a table, rate limiting in Redis or at the gateway (Azure API Management), spend in a shared store.

## 3. Streaming (SSE)

`Accept: text/event-stream` returns `started` → `progress` (one per graph node, with tool names) → `result`.
- SSE over plain HTTP: works through proxies, auto-reconnect in browsers, one direction is all we need (vs WebSockets).
- The first event is pulled **before** the response starts, so ownership/validation errors still become proper HTTP status codes.
- `x-accel-buffering: no` and `cache-control: no-store` stop proxies from buffering or caching operational data.
- Measured in the cloud: 1 `started`, 7 `progress`, 1 `result`; 16 s end-to-end.

## 4. Observability with OpenTelemetry

- **Vendor-neutral API in code** (`opentelemetry.trace` / `metrics`), Azure exporter configured once at startup
  (`azure-monitor-opentelemetry`). Swapping to Grafana/Datadog = exporter change.
- **Auto-instrumentation**: FastAPI requests and outgoing HTTP (Azure OpenAI, Entra JWKS, managed-identity token calls).
  The MCP SDK emits its own spans too (`MCP send server/discover`).
- **Custom spans**: `agent.diagnose`, `agent.approval`, `mcp.tool <name>` (name + outcome only: **no log content in telemetry**,
  that's privacy-by-design; the redacted details live in the MCP audit log).
- **Custom metrics**: `sre.llm.tokens` (by model and direction), `sre.llm.cost`, `sre.diagnose.duration`, `sre.diagnoses` (by outcome),
  `sre.mcp.tool_calls` (by tool and outcome). These answer "what does a diagnosis cost?" and "which tool fails?" with a KQL query.
- Keyless: App Insights has local auth disabled; the exporter uses the managed identity (`Monitoring Metrics Publisher`).

Measured after one cloud diagnosis (KQL over the workspace): `AppRequests` 401 + 200 (p50 16.3 s), Azure OpenAI calls avg 2.4 s × 3,
managed-identity token calls 224 ms, metrics: 20,149 tokens, $0.00911, 5 tool calls.

## 5. The container

- Multi-stage build from **`uv.lock`** (`--locked`: the image can't silently resolve different versions than CI tested).
  Dependencies layer first → code changes rebuild in seconds and push only a small layer.
- **Allowlist `.dockerignore`** (`*` then `!src/`...): `.env`, `.audit/`, `.git` can't end up in an image by accident.
- **Non-root** (uid 10001), works with a **read-only root filesystem** (`--read-only --tmpfs /tmp` tested); `HOME`, caches, ledger in `/tmp`.
- No secrets in the image or env: endpoints only; secrets come from Key Vault at startup via managed identity.
- 1.35 GB (spaCy, ONNX runtime, Azure SDKs). Smaller: drop DeepEval/docker SDK from the runtime extra, distroless base.
- Built with the classic builder (no BuildKit-only features) so it builds the same on Colima and in ACR Tasks.
  Pushed from the laptop instead of `az acr build` (ACR Tasks bills per second; pushing is free).

## 6. Azure Container Apps

- **Scale to zero** (`minReplicas: 0`): no traffic, no compute bill; the monthly free grant covers this demo.
  Trade-off: **cold start**. Measured: ~25 s to ready (container start + spaCy + MCP subprocess + tool discovery). Fine for an
  incident tool used a few times a day; for an always-on SLO set `minReplicas: 1`.
- **Probes**: *startup* probe on `/readyz` with a long budget (cold start), *liveness* on `/healthz` (process alive, cheap),
  *readiness* on `/readyz` (don't route traffic before the MCP tools are loaded). Liveness must never depend on downstreams,
  or an Azure OpenAI outage would make the platform restart healthy containers in a loop.
- Registry pull via **managed identity** (`AcrPull`), admin user disabled. HTTPS only (HTTP → 301).

### The production bug: "works locally, 401s in the cloud"

First deployment: the app crashed at startup with `DefaultAzureCredential failed to retrieve a token` **inside the MCP
subprocess**. Locally, `az login` worked because the subprocess inherits `PATH`/`HOME`. In Container Apps, managed identity
is exposed to the process as env vars (`IDENTITY_ENDPOINT`, `IDENTITY_HEADER`), and we (correctly) pass only an **allowlist**
of env vars to the stdio MCP server, which didn't include them. AKS Workload Identity would fail the same way
(`AZURE_FEDERATED_TOKEN_FILE`). Fix: one shared `mcp_subprocess_env()` that forwards `SRE_*`, `AZURE_*`, `IDENTITY_*`, `MSI_*`,
OTel vars, and still **not** arbitrary secrets (a test asserts `GITHUB_TOKEN` isn't forwarded).
Lesson: least-privilege environments are right, but each platform's identity plumbing is part of the contract. Test the image
in an environment that looks like production before calling it done.

## 7. Kubernetes (AKS-ready, not deployed: AKS costs money even idle)

- `KubernetesBackend` reads pods, logs, events and metrics through the API server with a **read-only Role** in one namespace
  (`get/list/watch` on `pods`, `pods/log`, `events`). It **can't read Secrets, exec, or write**, enforced by the cluster.
- K8s-specific diagnosis knowledge baked into the tools:
  - a Running pod can hide `CrashLoopBackOff` (status comes from the container's *waiting reason*, not the phase);
  - the evidence of a crash is in the **previous** container's logs (`previous=True`);
  - `OOMKilled` / exit 137 is in `lastState.terminated`;
  - label values from the LLM never become label selectors unless they're plain names (selector injection).
- Manifests: Pod Security **restricted**, non-root, `readOnlyRootFilesystem`, `drop: [ALL]`, memory limit (no CPU limit,
  avoids CFS throttling), probes, PDB, NetworkPolicy, **Workload Identity** (service account annotated with the managed identity).
- `tests/test_k8s_manifests.py` enforces the policy in CI (read-only verbs, no secrets/exec, hardened pod).

## 8. Cost and teardown

| Resource | Cost | Status |
|---|---|---|
| Container Apps (consumption, scale to zero) | free grant: 180k vCPU-s + 360k GiB-s / month | deployed |
| Container registry Basic | ~$0.17/day | deployed: **delete within ~2 days** (`scripts/teardown.sh apps`) |
| App Insights | per GB, same workspace, 0.1 GB/day cap | deployed |
| AKS | ~$70+/month for the smallest node | **not deployed** (manifests + tests only) |

`scripts/teardown.sh apps` removes everything hourly-priced; `all` deletes the resource group and **purges** soft-deleted
Key Vault / Azure OpenAI (otherwise names and quota stay reserved) and the Entra app.

## Final measured numbers for your resume

Every number below comes from a run in this repo (`eval/results/*.json`, App Insights). Don't round them up.

| Claim | Measured | Source |
|---|---|---|
| Retrieval accuracy (34 queries) | hit@1 **1.00**: pgvector hybrid + cross-encoder; Azure AI Search hybrid alone | Phases 2, 7 |
| Hybrid vs single retriever | vector 0.94 / BM25 0.97 / hybrid 1.00 (Azure) | Phase 7 |
| Context reduction | log summarisation 133,594 → 2,511 tokens (**98%**); parent-child top-2 uses **11%** fewer tokens than naive at higher fix coverage | Phases 2-3 |
| Answer quality (21-case golden set incl. adversarial) | correctness **0.94**, groundedness **1.00**, citation precision **1.00**, 0 hallucinated citations, injection resisted 100% | Phase 6 |
| Redaction | secrets + structured identifiers **100%**, person names **88%**, 0 false positives | Phase 5 |
| Speed and cost per diagnosis | median **12 s** (eval), 16 s via the cloud API; ≈ **$0.008-0.009** | Phases 6, 8 |
| Whole project LLM spend | < $0.50 | spend ledger |

**MTTR**: still needs *your* manual baseline (time yourself on ~10 golden cases without the assistant). Until then, say
"diagnoses in ~12 s" rather than "reduced MTTR by X%".

Suggested bullets (replace the earlier draft ones):
- *Built an agentic SRE assistant (LangGraph, Python) that diagnoses service incidents by combining live telemetry from a custom
  **MCP server** (Docker / Kubernetes / Azure Monitor KQL backends) with **hybrid RAG** over runbooks and post-mortems
  (pgvector and Azure AI Search, parent-child chunking, RRF, semantic/cross-encoder reranking; 1.00 hit@1).*
- *Designed an evaluation harness (21-case golden set with adversarial injection/overlap/novel cases, DeepEval G-Eval
  LLM-as-a-Judge, deterministic RAG metrics) enforced as a CI quality gate: 0.94 correctness, 1.00 groundedness, 0 hallucinated citations.*
- *Implemented Responsible-AI guardrails: secret redaction + PII pseudonymisation (regex, Luhn, spaCy NER), layered prompt-injection
  defence, and human-in-the-loop remediation with signed, single-use approval tokens and four-eyes control.*
- *Deployed keyless on Azure (Bicep): Container Apps behind Entra ID app-role auth, managed identity to Azure OpenAI / AI Search /
  Key Vault / Log Analytics, GitHub OIDC for cloud evals, and OpenTelemetry tracing + token/cost metrics in Application Insights;
  ~$0.009 and ~16 s per diagnosis.*

## Interview questions you should be able to answer

1. How does your API validate an Entra ID token? What's algorithm confusion?
2. Roles vs scopes in Entra ID; why app roles here?
3. Why 404 instead of 403 for someone else's thread?
4. What would you change to run 5 replicas? (State, rate limit, spend, approvals.)
5. Liveness vs readiness vs startup probes; what goes wrong if liveness checks the database?
6. Why SSE and not WebSockets?
7. What do you put in traces, and what do you deliberately leave out?
8. Scale-to-zero trade-offs for an incident tool.
9. Walk through the "works locally, fails in the cloud" managed-identity bug.
10. How do you give an AI agent access to a cluster safely?

## Quiz (answer first, then expand)

**Q1. A token is signed correctly by Entra for another app in your tenant. What stops it?**
<details><summary>Model answer</summary>

The audience check: `aud` must be our API (app id URI or client id). Signature and issuer alone only prove *Entra* issued it,
not that it was issued *for us*. Without `aud` validation, any token from the tenant would work (a confused-deputy problem).
</details>

**Q2. Why must the first SSE event be pulled before returning `StreamingResponse`?**
<details><summary>Model answer</summary>

Once streaming starts, the status code (200) and headers are already sent. Errors like "thread not found" would arrive as an
in-stream event instead of a 404. Pulling the first item forces those checks to run while we can still return a proper status.
</details>

**Q3. The liveness probe calls Azure OpenAI to "make sure the app really works". What happens during an Azure OpenAI incident?**
<details><summary>Model answer</summary>

Liveness fails on every replica, the platform restarts them in a loop, and you turn a dependency outage into a full outage
(and lose in-memory state). Liveness should only answer "is this process stuck?"; dependency checks belong in readiness
(or nowhere, if every replica shares the dependency).
</details>

**Q4. Why doesn't the image contain `.env` even though it's in the repo folder?**
<details><summary>Model answer</summary>

`.dockerignore` is an allowlist: `*` excludes everything, then only `pyproject.toml`, `uv.lock`, `README.md`, `src/`, `data/corpus/`
are added back. A denylist (`.env`) would miss `.env.bak`, `.audit/`, new files. Plus nothing secret is needed: secrets come
from Key Vault at runtime via managed identity.
</details>

**Q5. In Kubernetes, the pod is `Running` but users get errors and the restart count is 6. Where do you look?**
<details><summary>Model answer</summary>

The container's state reason (often `CrashLoopBackOff` while the pod phase still says Running), `lastState.terminated`
(reason `OOMKilled`, exit code 137 or 1), the **previous** container's logs (`kubectl logs --previous`), and events (`BackOff`,
`Unhealthy`). The backend does all four automatically.
</details>

**Q6. What does the four-eyes rule prevent here, and why can the requester still reject?**
<details><summary>Model answer</summary>

One person (or one compromised account) can't both trigger a diagnosis that proposes a restart and approve it: segregation of
duties. Rejecting is always safe (it results in no action), so blocking it would only add friction.
</details>

**Q7. You need 5 replicas tomorrow. List what breaks.**
<details><summary>Model answer</summary>

Threads/approvals in memory (a follow-up or approval may hit another replica) → Postgres checkpointer + ownership table;
rate limits per replica (5× the allowed rate) → Redis/APIM; spend ledger per replica → shared store or rely on Azure budget/TPM;
MCP subprocess per replica is fine. Sticky sessions are a band-aid, not a fix.
</details>
