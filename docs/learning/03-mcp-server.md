# Phase 3: MCP server (live telemetry + knowledge base as tools)

## What we built

| Path | Purpose |
|---|---|
| `src/sre_assistant/mcp_server/server.py` | `build_server()`: 6 read-only **tools**, 1 **resource** template, 1 **prompt**, server instructions. |
| `mcp_server/backends/base.py` | `TelemetryBackend` protocol. Tools never import Docker, K8s or Azure SDKs directly. |
| `mcp_server/backends/docker_backend.py` | Docker implementation: label-scoped discovery, logs, health and stats, events (persisted + live), config files. |
| `mcp_server/logs.py` | Log parsing, **fingerprinting**, error groups, patterns with numeric trends, token budget. |
| `mcp_server/redaction.py` | Rule-based secret redaction applied to every output (Phase 5 adds NER). |
| `mcp_server/config_files.py` | Config-path allowlist: no traversal, config extensions only, 64 KB cap. |
| `mcp_server/audit.py` | Audit record per tool call (redacted args, outcome, output tokens, latency). |
| `mcp_server/models.py` | Pydantic output models, published as each tool's `outputSchema`. |
| `infra/event-collector/` | Streams Docker lifecycle events into Postgres (`container_events`). |
| `infra/compose.yaml` | + `docker-proxy` (read-only Docker API) + `event-collector`. |
| `scripts/mcp_demo.py` | Break the service, then diagnose it using **only** MCP calls over stdio. |
| `.vscode/mcp.json` | Makes the server available to **GitHub Copilot agent mode** in VS Code. |
| `tests/test_mcp_units.py`, `tests/test_mcp_server.py`, `tests/test_mcp_integration.py` | 25 unit tests + 11 contract tests through a real MCP client + stdio end-to-end. |

```bash
uv run python -m sre_assistant.mcp_server                                  # stdio
uv run python -m sre_assistant.mcp_server --transport streamable-http      # http://127.0.0.1:8765/mcp
uv run python scripts/mcp_demo.py pool | oom | none
npx @modelcontextprotocol/inspector uv run python -m sre_assistant.mcp_server
```

### Tools

| Tool | Returns | Why it exists |
|---|---|---|
| `list_services(environment?)` | services, status, health, restart count | discovery; the LLM shouldn't guess names |
| `inspect_live_logs(service, lines_count, since_minutes?, min_level)` | error groups (+ `search_hint`), patterns with numeric trends, newest lines | the main evidence source, compressed for an LLM |
| `check_system_health(service)` | state, probes, restarts, CPU, memory vs limit, ports, last 30 min of events | "is it up, and how close to its limits?" |
| `get_recent_events(service, since_minutes)` | oom / die (exit code + meaning) / start / health changes | evidence that survives restarts |
| `read_env_config(service, filepath?)` | env vars + one allowlisted config file, secrets redacted | verify what a runbook says to change |
| `search_runbooks(query, service?, top_k)` | cited runbook / post-mortem / KB results (Phase 2 retriever) | institutional memory |

Resource `runbook://{doc_id}` returns a full document. Prompt `diagnose_incident(service, symptom)` returns the standard triage workflow.

---

## 1. MCP in one page

**Model Context Protocol** = an open JSON-RPC 2.0 protocol that standardizes how AI applications (hosts/clients) connect to
external capabilities (servers). Write a server once; use it from any client: our agent, GitHub Copilot in VS Code, Claude Desktop, Cursor and others.
Without MCP, every agent framework needs its own glue code for every tool (an N × M integration problem).

| Primitive | Controlled by | Our example | Use when |
|---|---|---|---|
| **Tools** | the **model** decides to call them | `inspect_live_logs` | actions or queries the LLM picks during reasoning |
| **Resources** | the **application** decides to load them | `runbook://RB-AUTH-DB-POOL` | addressable data (files, documents) the host or user attaches |
| **Prompts** | the **user** picks them | `diagnose_incident` | reusable workflows and templates (slash commands) |

Lifecycle: `initialize` (capability negotiation, protocol version) → `tools/list` → `tools/call` … Results contain
`content` (text for the LLM) and, in modern versions, `structuredContent` validated against the tool's `outputSchema`.

**Transports**

| | stdio | Streamable HTTP |
|---|---|---|
| How | client spawns the server as a subprocess; JSON-RPC over stdin/stdout | server listens at `POST /mcp`; can stream (SSE) |
| Use | local tools (IDE, desktop, our agent) | remote or shared servers, cloud deployment (Phase 7) |
| Auth | inherits the OS user | needs real auth (OAuth 2.1 / Entra ID tokens), TLS, rate limits |
| Pitfall | **anything printed to stdout corrupts the protocol**; log to stderr | session management, CORS/DNS-rebinding protection |

**SDK version note:** we're on the **MCP Python SDK v2**: `FastMCP` was renamed `MCPServer`, fields are snake_case,
sync tools run on worker threads, and `httpx` is no longer installed transitively (we declare it ourselves, which is good practice anyway).

---

## 2. Designing tools for an LLM (the most underrated skill)

Tools are **an API whose consumer is a language model**. Good design rules we applied:

1. **Few, purpose-shaped tools** rather than one per SDK call. "check_system_health" bundles state, probes, stats and events because an SRE always wants them together.
2. **Descriptions are prompts.** The docstring tells the model *when* to use the tool and *what to do next* (e.g. "pass `search_hint` to search_runbooks").
3. **Typed, bounded inputs.** Pydantic constraints (`lines_count ≤ 2000`, service name regex) are enforced *before* our code runs, and invalid input comes back with the validation error so the model can retry.
4. **Structured, compact outputs.** `outputSchema` + `structuredContent`; no raw dumps.
5. **Outputs designed for the next call.** `search_hint` is a clean `ExcType: message` ready for retrieval (the first demo searched with a timestamped log line and got a worse query).
6. **Actionable errors.** `ToolError("service 'payments' not found; available services: auth-cache, auth-db, auth-service")` lets the model self-correct. **Unexpected** exceptions are masked by the SDK ("Error executing tool X") so internals don't leak.
7. **Token budgets.** Every output is capped; the most important information survives truncation.
8. **Domain hints in data.** Exit code 137 → "SIGKILL: OOM killer or forced kill (128+9)". Cheap, and it saves the model from having to know.

### Log compression (measured on your machine)
**133,594 raw log tokens → 2,511 tool-output tokens (98% smaller)** for a pool-exhaustion incident.
- **Fingerprinting:** normalise *volatile* values (UUIDs, timestamps, IPs, hex ids, decimals, numbers ≥ 4 digits) so 48 identical errors become 1 group with `count=48`, `first_seen`, `last_seen` and one traceback tail.
  - First version normalised **every** number: `3o7r` → `<n>o<n>r`, and HTTP 200/401/500 would have merged. Keep meaningful small numbers.
- **Patterns with numeric trends:** `POST /login 500 ×48, duration_ms min 5005 max 5022` (= `pool_timeout`!); `runtime stats rss_mb max 276` (above the 256 MB limit).
- **Rare patterns are kept** (startup and shutdown lines): "starting auth-service" at 19:58:29 marks the restart 2 s after the `oom` event.

---

## 3. Security: least privilege for an AI agent

| Control | Implementation | Threat it addresses |
|---|---|---|
| **Read-only tools** | no restart, exec, write or delete tools; `readOnlyHint=True`, `destructiveHint=False` | prompt-injected or confused agent taking destructive action |
| **Label-scoped discovery** | only containers with `sre.service` are visible; the assistant's own DB is invisible (tested) | lateral movement / data exfiltration |
| **Docker socket proxy** | `tecnativa/docker-socket-proxy`, `CONTAINERS=1 EVENTS=1 POST=0`. Verified: GET containers **200**, POST restart **403**, images **403** | the raw Docker socket is root-equivalent |
| **Path allowlist** | `normpath` + must stay under `sre.config_dir`, config extensions only, size cap, NUL bytes rejected | `../../etc/shadow` path traversal |
| **Redaction at the boundary** | URL credentials, JWT, Bearer, Azure keys, AWS keys, private keys, secret-named keys, applied to logs, env, config, probe output **and search queries** | secrets reaching the LLM provider, prompts, traces |
| **Input validation** | pydantic schema per tool | injection, resource exhaustion (`lines_count=10^6`) |
| **Error masking** | SDK hides unexpected exception details; `ToolError` for intentional messages | information disclosure |
| **Audit trail** | JSONL per call: redacted args, ok/error, output tokens, duration | accountability and forensics (regulated environments) |

Lesson from debugging: our terminal **masked** the redacted URL (`******`), which looked like a redaction bug. The fix was to
**assert** on exact output (`demo_password not in out`) rather than trust what's displayed.

---

## 4. Evidence durability: the event collector

What we found: after ~15 minutes, `docker events` no longer showed the OOM kill at all. **The Docker daemon keeps only ~256
events in memory**, and our 10-second healthcheck generates ~18 `exec_*` events per minute, which pushes the evidence out.
Kubernetes events expire too (default TTL 1 hour).

**Principle:** telemetry must be shipped off the host and persisted. Never rely on in-memory buffers for forensics.
- `event-collector` streams lifecycle events (filtered to `sre.service` containers) into `container_events` in Postgres. It
  backfills on start (idempotent through a UNIQUE constraint), reconnects with exponential backoff, and runs as non-root.
- `DockerBackend.get_events` = **persisted history ∪ live daemon events**, de-duplicated on (second, action). If the store is down it degrades to live-only.
- Query by **service**, not container id, so history survives container recreation and redeploys.
- Cloud equivalent: Azure Monitor Container Insights (`KubeEvents`, `ContainerLogV2` tables), queried with KQL in Phase 7.

Also remember (Phase 1): `OOMKilled` in `docker inspect` describes only the **current** run. The tool field is named
`oom_killed_last_exit` and its description says "resets on restart; check events", which documents the trap for the model.

---

## 5. Testing an MCP server

- **Pure units:** redaction (positive and negative cases: `token_count` must *not* be redacted), path allowlist, fingerprinting, budgets, event merge.
- **Contract tests:** a real `mcp.client.Client` connects **in-process** to `build_server(FakeBackend(), FakeRetriever())`.
  Tests the actual protocol surface: schemas, annotations, `is_error`, structured content, resources, prompts. Fast and deterministic.
- **End-to-end:** spawn the server over **stdio** as a subprocess against the live Docker stack, the same way VS Code would.
- **Security tests:** traversal attempts, injection-shaped names, oversized arguments, unlabelled containers invisible.
- The `TelemetryBackend` protocol is what makes the contract tests possible (dependency inversion again, as in Phase 0).

---

## Interview questions you should be able to answer

1. **"What is MCP and why use it instead of plain function calling?"** A standard protocol between AI hosts and capability servers. Function calling is how a model *requests* a call; MCP is how tools are *packaged, discovered, transported and secured*, reusable across any client (Copilot, Claude, our agent) without re-integration.
2. **"Tools vs resources vs prompts?"** Model-controlled actions, application-controlled data, user-controlled templates.
3. **"stdio vs Streamable HTTP?"** Local subprocess vs remote service. HTTP needs auth (OAuth/Entra), TLS, session handling; stdio must keep stdout clean.
4. **"How do you make tools safe for an autonomous agent?"** Read-only by default, least privilege (label scoping, socket proxy, allowlists), bounded I/O, redaction, error masking, audit, and human approval for any future write action (Phase 5).
5. **"How do you avoid flooding the context window with logs?"** Fingerprint and group, counts and first/last seen, numeric trends, keep rare events, token budgets, hand the model a ready-made search query. Measured: 98% reduction.
6. **"An OOM happened 30 minutes ago and `docker inspect` says OOMKilled=false. How does your tool still know?"** Events. But daemon events live in a ~256-entry memory buffer, so we persist them with a collector and merge with live events.
7. **"How do you test it?"** Unit + in-process client contract tests with a fake backend + stdio end-to-end + security cases.
8. **"How would this run in production?"** Streamable HTTP behind auth (Entra ID), backends for Azure Monitor (KQL) and the Kubernetes API with read-only RBAC (`get/list/watch` on pods, pods/log, events), audit to a SIEM.

---

## Quiz (answer first, then expand)

**Q1. Your stdio MCP server works in tests but the client hangs or errors with "invalid JSON" when you add `print("debug")`. Why?**
<details><summary>Model answer</summary>

In the stdio transport, stdout *is* the JSON-RPC channel. Any non-protocol bytes on stdout corrupt the message stream.
Log to stderr (or a file). Our `__main__` configures logging to stderr and the audit logger writes to a file.
</details>

**Q2. Why does `check_system_health` call the field `oom_killed_last_exit` and point to events in its description?**
<details><summary>Model answer</summary>

Docker's `State.OOMKilled` describes the current run; after an automatic restart it's `false` even though the previous
run was OOM-killed. Naming and description warn the model not to trust it alone; the `oom` → `die 137` events are the real evidence.
</details>

**Q3. The OOM evidence vanished from `docker events` after ~15 minutes. Explain, and give the production fix.**
<details><summary>Model answer</summary>

The daemon keeps events in a bounded in-memory buffer (~256); frequent healthcheck exec events evict older ones, and nothing
is persisted. Fix: ship events to durable storage (our collector → Postgres; in Azure, Container Insights `KubeEvents`/Log
Analytics; in K8s, an event exporter), and query history by service so it survives container recreation.
</details>

**Q4. Why fingerprint with "volatile values only" instead of replacing every number?**
<details><summary>Model answer</summary>

Replacing every number merges lines that differ meaningfully (HTTP 200 vs 500, pool size 5 vs 20) and corrupts identifiers
(`3o7r` → `<n>o<n>r`). Normalise only values that vary per occurrence without changing meaning: UUIDs, timestamps, IPs, hex ids,
decimals (durations/timeouts) and long integers (pids, txids).
</details>

**Q5. Name four controls that stop `read_env_config` from leaking `/etc/shadow` or a database password.**
<details><summary>Model answer</summary>

1) Path allowlist: `normpath(join(config_dir, path))` must stay under the labelled config dir, so `../../etc/shadow` is rejected.
2) Extension allowlist (yaml/json/toml/ini/conf). 3) Size cap and NUL-byte rejection. 4) Redaction: secret-named keys masked in
parsed YAML/JSON plus pattern redaction (URL credentials etc.) on everything. Also label-scoped discovery and audit.
</details>

**Q6. What's the difference between a `ToolError` and an unexpected exception in MCP SDK v2, and why does it matter?**
<details><summary>Model answer</summary>

`ToolError` messages are returned to the client (`is_error=true`) verbatim: use them for actionable, safe messages
("service not found; available: …"). Unexpected exceptions are masked ("Error executing tool X") so stack traces or internals
don't leak to the model or user. Good for security; it also means expected failures should be raised deliberately.
</details>

**Q7. Why put the Docker socket behind a proxy even though our tools are read-only in code?**
<details><summary>Model answer</summary>

Defense in depth. The raw socket is root-equivalent: a bug, dependency compromise, or future code change could call
POST endpoints. The proxy enforces read-only at the infrastructure layer (verified: POST restart → 403), independent of application code.
</details>

**Q8. How do the contract tests talk to the server without Docker or a network?**
<details><summary>Model answer</summary>

`mcp.client.Client(server_instance)` connects in-process to the `MCPServer` built with a `FakeBackend` and `FakeRetriever`
injected through the `TelemetryBackend` protocol. It exercises the real protocol (schemas, validation, annotations,
errors, resources, prompts) deterministically and in milliseconds.
</details>
