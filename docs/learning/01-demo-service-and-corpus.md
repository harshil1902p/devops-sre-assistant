# Phase 1: Demo service that really breaks, plus the knowledge base

## What we built

| Path | Purpose |
|---|---|
| `demo/auth-service/` | FastAPI + SQLAlchemy (Postgres) + Redis login service. JSON logs, `/healthz` + `/readyz`, non-root container. |
| `demo/auth-service/app/chaos.py` | Silent fault injection: slow queries (pool exhaustion), `CLIENT PAUSE` (Redis stall), memory leak (OOM). |
| `demo/auth-service/config/app.yaml` | Runtime config with **fake** secrets, used to test safe config reading in Phases 3 and 5. |
| `demo/compose.yaml` | The "staging" environment: auth-service + Postgres + Redis, 256 MB memory limit, `sre.*` labels. |
| `scripts/demo_chaos.py` | Load generator + scenario driver: `traffic`, `pool`, `redis`, `oom`, `reset`. |
| `data/scenarios/*.yaml` | **Single source of truth**: 17 hand-written incident scenarios. |
| `src/sre_assistant/corpus/` | `schema.py` (metadata contract), `loader.py` (front-matter parser), `generate.py` (renderer). |
| `data/corpus/` | Generated knowledge base: 17 runbooks + 17 post-mortems + 17 KB threads = **51 docs, ~11.4k words**, plus `manifest.json`. |
| `tests/test_corpus.py` | Structure, cross-references, exact error strings, determinism, drift check. |
| `tests/test_demo_service.py` | Integration tests (`uv run pytest -m integration`) against the running stack. |

### Commands
```bash
colima start && open -a Ollama
docker compose -f demo/compose.yaml up -d --build
uv run python scripts/demo_chaos.py traffic --seconds 10
uv run python scripts/demo_chaos.py pool      # or: redis | oom | reset
docker logs sre-demo-auth-service-1 --since 2m
uv run python scripts/generate_corpus.py      # after editing data/scenarios/*.yaml
uv run pytest                                 # unit;  add -m integration for live checks
```

---

## Part A: The demo service

### Design choices and why

| Choice | Why it matters for an SRE assistant |
|---|---|
| **Structured JSON logs**, one object per line, with `exc_type` / `exc_message` / `exc` fields | Machines (Docker/K8s tools, Azure Monitor KQL) can filter on fields, and the LLM gets the exact exception class without multi-line traceback noise. |
| **`/healthz` (liveness) vs `/readyz` (readiness)** | Liveness checks only that the process is alive, while readiness also checks DB and Redis. Mixing them up causes cascading restarts; that's scenario `liveness-probe-dependency` in the corpus. |
| **Silent chaos endpoints** | The assistant must diagnose from *symptoms*, as in a real incident. No "chaos started" log line gives the answer away. |
| **Small pool on purpose** (`pool_size=5, max_overflow=2, pool_timeout=5`) | Makes exhaustion reproducible with modest load. |
| **Runtime stats log every 5 s** (pool status, RSS, threads) | Leading indicators: pool saturation and memory growth show up before errors. |
| **Docker labels** `sre.service`, `sre.environment` | The MCP server (Phase 3) finds containers by label, like Kubernetes label selectors. Nothing is hard-coded. |
| **Non-root user, memory limit, restart policy** | Production-like behaviour. The limit is what makes OOMKilled real. |

### The three failure modes and the evidence they produce (observed on your machine)

| Scenario | Error | Fingerprint |
|---|---|---|
| `pool` | `sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 2 reached, connection timed out, timeout 5.00` | Failed requests take **~5,000 ms** (= `pool_timeout`); pool stats show 7 connections checked out and 0 in the pool; Postgres itself is idle. |
| `redis` | `redis.exceptions.TimeoutError: Timeout reading from socket` | Failures take **~1,000 ms** (= `socket_timeout`). It's a timeout, not a refusal: Redis is reachable but not answering. |
| `oom` | No application error at all | RSS climbs 116 → 176 → 224 MB, then the process restarts. `docker events` shows `oom` then `die` with `exitCode=137`. After the restart, `docker inspect` shows `OOMKilled=false` because the state reset. |

**Interview gold: the duration of a failure tells you which timeout fired.** 5 s matches `pool_timeout` and 1 s matches `socket_timeout`.

### Three real bugs we hit and fixed (use these as stories in behavioural and technical rounds)

1. **Lost 500s became dropped connections.** With `@app.exception_handler(Exception)`, half the failures showed up client-side as `RemoteProtocolError` instead of HTTP 500, and every error was logged twice. Starlette's `ServerErrorMiddleware` sends the 500 and then **re-raises**, so the access-log middleware's view of the response broke.
   *Fix:* catch exceptions in our own HTTP middleware and build the 500 response there.
   *Lesson:* check your observability layer. A wrong status code in logs means wrong SLO numbers.
2. **The load test measured the rate limiter, not the pool.** The second run returned **239 × HTTP 429**: we reuse 4 usernames and the limit was 100 per minute, so the rate limiter hid the failure we were trying to inject.
   *Fix:* make the limit configurable (1000 in staging) and flush Redis between runs.
   *Lesson:* a test can measure a different bottleneck than the one you intended. Check the status-code distribution, not just "it failed".
3. **OOM evidence disappears.** After the auto-restart, `docker inspect` shows a fresh State, so `OOMKilled` reads false.
   *Lesson:* evidence is short-lived. Use the event stream (`docker events`, Kubernetes events), `kubectl describe pod` → `Last State`, and `kubectl logs --previous`. This is exactly why the MCP server must expose **events and restart history**, not just current state.

---

## Part B: The knowledge base (RAG corpus)

### Why a synthetic corpus, and why generate it from a catalogue?
- Real post-mortems are confidential. Public ones (e.g. the danluu/post-mortems collection) are inconsistent in format and have no labels.
- **Catalogue → templates** gives:
  - **One source of truth.** Fix a fact once in YAML and all three documents update.
  - **Determinism.** Identical output every run, reviewable in git, so evaluation results are reproducible.
  - **Free ground truth.** Every document carries `scenario`, and `manifest.json` maps scenario → correct document IDs. Phase 6 uses this as the "correct answers" for retrieval metrics.
- Be honest in interviews: *"The corpus is synthetic, modelled on common real-world incident patterns. In production I'd ingest from Confluence, ServiceNow or Git and keep the same metadata schema."*

### Three document types: different shapes of the same knowledge
| Type | Shape | Why the assistant needs it |
|---|---|---|
| Runbook (`RB-…`) | Procedural: signatures → symptoms → diagnosis commands → mitigation → permanent fix | *What to do now* |
| Post-mortem (`PM-INC-…`) | Narrative: timeline, root-cause analysis, action items, lessons | *What happened last time* and team-specific context (e.g. "pool_size to 20 in database.py") |
| KB thread (`KB-…`) | Q&A: question with stack trace, accepted answer, comment | Community knowledge, phrased the way engineers *ask* |

The same fact phrased three ways increases the chance that at least one phrasing matches the query.

### Metadata schema (`DocMetadata`, strict, `extra="forbid"`)
`doc_id, doc_type, title, scenario, service, environment, language, infrastructure, error_type, error_codes[], severity, created, tags[]`

- **Retrieval filters:** "only auth-service docs", "only production post-mortems".
- **Ranking boosts:** recent documents and matching `error_type`.
- **Evaluation:** `scenario` is the label.
- **Exact strings for keyword search:** `error_codes` holds strings like `ERR_CONNECTION_REFUSED` or `40P01`. Embeddings blur these, so BM25 needs them verbatim (Phase 2).
- **Metadata filters can hurt recall.** Production `auth-service` runs on AKS (`infrastructure: kubernetes`), but our demo staging runs on Docker. A hard filter `infrastructure=docker` would hide the correct runbook. Rule: **hard-filter only on high-confidence fields** such as `service`, and use the rest as soft boosts.

### Look-alike scenarios (hard negatives): making retrieval honestly difficult
| Query evidence | Correct doc | Look-alike that a naive system confuses it with |
|---|---|---|
| `QueuePool limit … reached` (client side) | RB-AUTH-DB-POOL | RB-PAY-PG-MAXCONN `too many clients already` (server side) |
| `TimeoutError: Timeout reading from socket` | RB-AUTH-REDIS-TIMEOUT | RB-NOTIF-REDIS-REFUSED `ECONNREFUSED` |
| exit 137 with no app error | RB-AUTH-OOMKILLED | RB-RPT-JVM-HEAP `java.lang.OutOfMemoryError` |
| `upstream timed out` → 504 | RB-GW-504-UPSTREAM-TIMEOUT | RB-GW-502-UPSTREAM-REFUSED |

Each pair is semantically close, so embeddings alone score them similarly, but each needs a **different fix**. That's why the next phase uses hybrid search.

### Structure is a contract for chunking
Every document type has fixed `##` headers (tested). In Phase 2, each `##` section becomes a **parent chunk**, and smaller pieces inside it (code blocks, bullet groups) become **child chunks**. Because the section names are consistent, a retrieved chunk always carries meaningful context such as "this is the *Permanent Fix* of RB-AUTH-DB-POOL".

### Corpus tests (data quality is code quality)
- Every scenario produces exactly 3 document types.
- **Drift check:** the regenerated corpus must equal the committed one. This catches "edited YAML, forgot to regenerate".
- Determinism, required headers, cross-references resolve (`RB-…` mentioned in text must exist).
- Every runbook and post-mortem contains at least one exact error signature (protects keyword retrieval).
- The schema rejects unknown fields (protects the metadata contract).

---

## Interview questions you should be able to answer

1. **"How do you tell client-side connection-pool exhaustion apart from the database's connection limit?"** Client side: `QueuePool limit … reached`, requests wait exactly `pool_timeout`, DB is idle, and `pg_stat_activity` shows few connections. Server side: `FATAL: too many clients already`, fails immediately, total connections = replicas × (pool_size + max_overflow) ≥ `max_connections`. The fixes are different: the pool and its leaks vs PgBouncer and the connection budget.
2. **"A pod restarts every 20 minutes with nothing in the logs. What do you check?"** Exit code (137 = SIGKILL → OOMKilled or a liveness kill), `kubectl describe pod` → Last State / events, `kubectl logs --previous`, memory trend (saw-tooth = leak). Mitigate by raising the limit; fix with a bounded cache or profiling.
3. **"Liveness vs readiness?"** Liveness: should the kubelet restart me? Process-only checks. Readiness: should I get traffic? Checks dependencies. Dependency checks in liveness cause cascading restarts.
4. **"Why structured logs for an AI assistant?"** They're filterable, fields like `exc_type` are cheaper (fewer tokens) and less ambiguous to pass to the LLM, they work with KQL in Azure, and they make redaction easier (Phase 5).
5. **"Where would you get documents in a real bank, and what's the data-quality risk?"** Confluence, ServiceNow, Git, Teams/Slack. Risks: stale runbooks, inconsistent formats, duplicates, secrets pasted into tickets. Mitigations: a metadata schema, ingestion validation, freshness (`created`) and redaction at ingestion.
6. **"Why generate test data deterministically?"** Reproducible evaluations, reviewable diffs, and labels for free.

---

## Quiz (answer first, then expand to check)

**Q1. Login requests fail after almost exactly 5.0 s with HTTP 500, while Postgres CPU is 10% and only 12 of its 200 connections are used. What's the most likely cause, and what's your first query?**
<details><summary>Model answer</summary>

Client-side SQLAlchemy pool exhaustion: 5 s equals `pool_timeout`. Connections are checked out by slow or leaked sessions.
First query: `SELECT pid, state, now()-query_start AS age, left(query,80) FROM pg_stat_activity WHERE usename='auth' ORDER BY age DESC;`
to find long-running or `idle in transaction` sessions holding the pooled connections.
</details>

**Q2. Why did our first exception handler produce `RemoteProtocolError` on the client instead of HTTP 500?**
<details><summary>Model answer</summary>

Starlette's `ServerErrorMiddleware` calls the `Exception` handler to send the 500 and then **re-raises** the exception (so servers and tests can see it).
That re-raise propagated through our middleware stack, so the connection was torn down and the error double-logged. Catching it in our own HTTP
middleware returns a normal 500 response and keeps the access log accurate.
</details>

**Q3. After an OOM, `docker inspect` shows `OOMKilled=false`. Is the OOM theory wrong?**
<details><summary>Model answer</summary>

No. The container auto-restarted and `State` describes the **current** run. Evidence of the previous run lives in
`docker events` (`oom`, `die` with `exitCode=137`). In Kubernetes it's `Last State: Terminated / Reason: OOMKilled` and
`kubectl logs --previous`. The MCP tools must therefore expose events and restart history.
</details>

**Q4. Why include look-alike (hard-negative) documents such as "too many clients" next to "QueuePool limit"?**
<details><summary>Model answer</summary>

They're semantically close, so pure vector search can rank the wrong one first, but they need different fixes.
Including them makes evaluation honest (an easy corpus inflates scores) and justifies hybrid retrieval: exact error
strings via BM25 separate them where embeddings can't.
</details>

**Q5. You filter retrieval with `infrastructure = "docker"` because the demo runs on Docker, and recall drops. Why?**
<details><summary>Model answer</summary>

The correct runbook is tagged `kubernetes` (production auth-service runs on AKS), so the hard filter removed it.
Hard-filter only on reliable, high-confidence fields (e.g. `service`); use fields like infrastructure, environment or recency
as soft ranking boosts, and measure the effect with retrieval evaluations.
</details>

**Q6. What does `test_committed_corpus_is_up_to_date` protect against, and what's the general pattern called?**
<details><summary>Model answer</summary>

It catches generated artifacts drifting from their source: someone edits `data/scenarios/*.yaml` but forgets to regenerate,
so the index and evaluations would run on stale documents. The general pattern is a **golden-file / snapshot drift check** for generated code or data in CI.
</details>

**Q7. Why does a SIGKILL'd process log nothing, and what's exit code 143?**
<details><summary>Model answer</summary>

SIGKILL (9) can't be caught, so the process gets no chance to run handlers or flush logs: 128 + 9 = **137**.
SIGTERM (15) can be caught for graceful shutdown: 128 + 15 = **143**. Kubernetes sends SIGTERM first, then SIGKILL after
`terminationGracePeriodSeconds`.
</details>
