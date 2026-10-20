# Phase 4: The agent (LangGraph, workflow + ReAct, Azure OpenAI)

## What we built

| Path | Purpose |
|---|---|
| `src/sre_assistant/agent/graph.py` | The LangGraph state machine: intake → triage → agent ⇄ tools → remind/respond, plus follow-ups. |
| `agent/mcp_tools.py` | Our own **MCP → LangChain tool adapter** (content for the LLM, artifact for code). |
| `agent/timeline.py` | Deterministic **timeline** + **incident-window segmentation** + coverage check. |
| `agent/context.py` | **Context compaction** (stub old tool outputs) and internal-note messages. |
| `agent/diagnosis.py` | `Diagnosis` schema (structured output) + **grounding / citation verification** + Markdown rendering. |
| `agent/prompts.py` | System prompt (ReAct method + rules), final-answer and follow-up instructions. |
| `agent/__main__.py` | CLI: spawns the MCP server over stdio, Postgres checkpointer, streaming progress, usage/cost. |
| `llm/providers.py` | Chat and embeddings **can use different providers**; Azure OpenAI with **keyless Entra ID**. |
| `llm/spend.py` | **Hard spend cap**: ledger + LangChain callback that refuses calls over the limit. |
| `infra/azure/` | **Bicep** (subscription scope): resource group, budget alert, Azure OpenAI (keys disabled), model deployments, RBAC. |
| `tests/fakes.py`, `tests/test_agent.py`, `tests/test_spend.py` | Scripted fake LLM + in-process MCP server: every routing decision tested with no network and no cost. |
| `docs/examples/` | Real transcripts from the demo stack. |

```bash
uv run python -m sre_assistant.agent "Users report login failures on auth-service. What is going on?"
uv run python -m sre_assistant.agent "What exact config change should I make?" --thread <same thread id>
uv run python -m sre_assistant.agent "..." --json --memory     # JSON output, in-memory checkpointer
```

---

## 1. Choosing the model: measure first

| Option | What happened |
|---|---|
| Local `qwen2.5:7b` on the Intel i7 | Measured **23 tok/s prefill, 3.6 tok/s decode** → a 6–8k-token agent run is **5–10 minutes**. Fine offline, too slow to iterate and evaluate. |
| GitHub Models (free) | **Retired 30 Jul 2026**, found by checking the docs *before* asking you for a token. Always verify a service is still live. |
| Gemini free tier | Live and OpenAI-compatible, but free-tier data may be used by the provider (acceptable only for synthetic data). |
| **Azure OpenAI `gpt-4.1-mini`** (chosen) | **~18–26 s per diagnosis, ~$0.008–0.015**, keyless, Canadian region, and what a bank would use. |

Why `gpt-4.1-mini` and not the newer GA `gpt-5.x-mini`: the 5.x models are **reasoning models**. They don't support
`temperature`, they add paid hidden "thinking" tokens, and they're slower. For a tool-calling agent that should be deterministic
and cheap, a non-reasoning model is the better fit. A reasoning model is a good candidate for the **judge** (Phase 6).
Pricing came from the **Azure Retail Prices API** (the pricing web page renders prices client-side): $0.40/1M input,
**$0.10/1M cached input**, $1.60/1M output.

**Chat and embeddings are separate providers.** The pgvector index was built with `nomic-embed-text`, so embeddings stay local:
mixing embedding models breaks the vector space (Phase 0 quiz). Moving chat to Azure is just `SRE_CHAT_PROVIDER=azure_openai`.

---

## 2. Architecture: workflow + agent

```
START → start_turn ─┬─ new investigation, service unknown → intake (list_services + LLM pick, or exact name match)
                    ├─ new investigation, service known   → triage
                    └─ follow-up in same thread           → agent
triage (deterministic, parallel): check_system_health + inspect_live_logs + get_recent_events → TIMELINE note
agent (LLM with tools, ReAct) ─┬─ tool calls  → tools (budget, dedupe) → agent
                               ├─ "READY" but a significant incident window was not searched → remind → agent
                               ├─ "READY" → respond (structured Diagnosis + grounding check) → END
                               └─ follow-up prose answer → finish_followup → END
```

**Why not let the LLM do everything (pure ReAct)?** Anthropic's *Building effective agents* distinction:
- **Workflows**: predefined code paths. Use them when the steps are always the same. Every incident needs health, logs and events, so we fetch them **deterministically, in parallel** (3 tool calls, 0 LLM calls, ~1 s).
- **Agents**: the LLM decides the next step. Use them only where judgement is needed: *what to search, what to verify, when to stop*.
- Result: fewer LLM calls (3–5 per diagnosis), lower cost and latency, and behaviour that's far easier to test.

**ReAct** (Reason + Act): the model alternates reasoning ("the error is QueuePool…") with tool calls (`search_runbooks`) and
observations (tool results) until it can answer. LangGraph models this as a cycle `agent ⇄ tools` with conditional edges.

**Prefetched results are injected as a synthetic tool-call transcript** (an `AIMessage` with `tool_calls` + matching
`ToolMessage`s), so the conversation stays a valid tool-calling transcript for any provider. OpenAI rejects a
ToolMessage without a matching tool call.

---

## 3. LangGraph concepts you used

| Concept | In our code |
|---|---|
| **State** (`TypedDict`) | `AgentState`: messages, service, tool counters, retrieved doc ids, windows, diagnosis, llm_calls |
| **Reducers** | `messages: add_messages` (append/replace by id), `retrieved_doc_ids: _union`, `llm_calls: operator.add`. Others overwrite. |
| **Nodes** | async functions returning *partial* state updates |
| **Conditional edges** | `route_start`, `route_agent`, `route_tools`: pure functions of state, so they're easy to test |
| **Checkpointer** | `AsyncPostgresSaver` (or `InMemorySaver` in tests): state saved after every node, keyed by `thread_id` |
| **Threads / memory** | Same `--thread` → the follow-up turn sees the previous diagnosis; works across processes (Postgres) |
| **Message replacement** | `finish_followup` returns an `AIMessage` with the **same id**, so `add_messages` *replaces* it (stray "READY" stripped) |
| **recursion_limit** | Safety net against infinite loops (40), on top of our own tool budget |

**Memory vs context.** The checkpointer keeps *everything* (full tool outputs as artifacts). The *prompt* is compacted:
tool outputs from previous turns become one-line stubs, and the oldest outputs of the current turn are stubbed if over budget.
Measured: the follow-up turn's LLM call used **2,463 input tokens** vs 7,140 for the first turn's final call.

---

## 4. Guards: making a probabilistic model behave like a product

| Guard | Mechanism | Real evidence it's needed |
|---|---|---|
| Tool budget | max 8 executed calls; extra parallel calls get "budget exhausted"; reaching the budget forces `respond` | loop prevention, cost control |
| Duplicate calls | identical (name, args) → "already made, reuse the earlier result", including duplicates of the triage prefetch | tested |
| Mandatory search | READY without any `search_runbooks` → `remind` | **gpt-4.1-mini tried to answer without searching** on a real run |
| **Coverage guard** | every significant **incident window** (OOM / error burst / memory growth) must be mentioned by some search query | fixed the overlapping-incidents failure (below) |
| Structured output | `with_structured_output(Diagnosis, method="json_schema")`, schema enforced by the API | parseable, renderable, evaluable |
| Citation verification | every cited doc id must have been **returned by `search_runbooks` in this thread**; otherwise removed and reported | hallucinated citations are caught deterministically |
| Sanitising | control characters stripped from all Diagnosis fields; ASCII-only citation format | **`§` came back as `\x00a7`** |
| Spend cap | `SpendGuard` callback blocks calls when the ledger ≥ cap | Azure budgets only alert |

### The overlapping-incidents story (an interview gem)
A real scenario: pool-exhaustion errors at 22:37:54–22:38:10, then an **unrelated** OOM kill at 22:39:17.

| Version | Agent behaviour |
|---|---|
| v1 (prompt: "correlate timestamps") | **Hallucinated causality**: "pool exhaustion… led to increased memory usage and eventual OOM". Searched only the pool error. |
| v2 + deterministic **incident windows** in the timeline + "never assume causality" rule | Said "**Separately**, the container was OOM killed". Still searched only the pool error; OOM cause left open. |
| v3 + **coverage guard** (code) | Guard fired ("window #2 OOM kill not investigated") → 2 parallel OOM searches → identified the **unbounded in-memory cache**, cited 5 real documents across both incidents. 26 s, $0.015. |

Lessons:
1. **Push reasoning the model is bad at (timestamp arithmetic, clustering) into deterministic code** and hand it the result.
2. **Enforce critical behaviours in the graph, not just the prompt.** Prompts are suggestions; edges are rules.
3. You only find these failures by **running real scenarios**. Phase 6 turns them into regression tests.

---

## 5. Keyless Azure OpenAI and infrastructure as code

- **`disableLocalAuth: true`**: the resource has *no API keys*. Access requires an Entra ID token and the RBAC role
  **Cognitive Services OpenAI User** (assigned to you in Bicep). Locally the token comes from `az login`; in Azure it'll come from a
  **Managed Identity** (Phase 7). `DefaultAzureCredential` handles both with the same code.
- **Bicep at subscription scope** creates the resource group, **budget alert ($1)**, OpenAI account, deployments and role.
  `what-if` previewed exactly 6 creates before deploying. Model versions are pinned (`NoAutoUpgrade`); upgrades should go through evals.
- Deployments on one account must be created **sequentially** (`dependsOn`).
- **Free Trial check before any spend:** `quotaId: FreeTrial_2014-09-01`, `spendingLimit: On`, so Azure blocks real charges.
- **Spend ledger:** `.audit/spend.jsonl` (file-locked, shared by all processes). Usage is priced with cached-input rates.
  Total project spend at the end of Phase 4: **≈ $0.044**.

---

## 6. Engineering incidents during this phase (good behavioural-interview stories)

1. **Dependency conflict:** `langchain-mcp-adapters` silently **downgraded `mcp` 2.3 → 1.30**, which would have broken the server.
   Caught by reading the resolver output; reverted and wrote a 40-line adapter instead.
2. **Dependency drift:** a Phase 2 `git checkout pyproject.toml` (to undo a failed install) had also removed `psycopg`,
   `pgvector` and `tiktoken`. Tests kept passing for two phases because the packages stayed installed locally. Found when `uv sync`
   pruned them. Fix: declare them and add **CI on a clean runner** (`uv sync --locked`).
3. **Slow tooling:** Homebrew was compiling `azure-cli` from source on Intel macOS; `uv tool install azure-cli` used wheels (< 1 min).

## 7. Publishing safely to GitHub

- Every local credential moved to **gitignored** files (`.env`, `demo/.env`, `infra/.env`, rendered `app.yaml`) generated by
  `scripts/bootstrap_local_secrets.py`. Compose uses `${VAR:?message}` to **fail fast** if a value is missing.
- **Git history rewritten** with `git filter-repo --replace-text`: the demo passwords had been committed in Phases 1–3.
  Removing them in a new commit is not enough; anyone can read old commits.
- Fake test secrets are **assembled at runtime** (`"AKIA" + "FAKE" * 4`) so scanners and GitHub push protection never see literals.
- **gitleaks** scanned the working tree and the **full history** (0 leaks) before the first push, and runs in CI on every push.
- Verified after pushing: credential files return **404** on raw.githubusercontent.com; only `*.example` templates are public.
- YAML gotcha: `${VAR:?missing - run: ...}` broke compose parsing, because `": "` inside an unquoted value starts a mapping.

---

## Interview questions you should be able to answer

1. **"Walk me through your agent."** Workflow + ReAct diagram; why triage is deterministic; what the LLM decides; guards; structured output; grounding.
2. **"How do you stop an agent from looping or overspending?"** Tool budget, duplicate detection, recursion limit, forced final answer, a spend cap enforced *before* each call, prompt caching.
3. **"How do you handle hallucinated citations?"** The agent may only cite doc ids that `search_runbooks` returned in this thread; anything else is removed and reported as a warning.
4. **"What is the difference between memory and context?"** The checkpointer persists full state per thread; the prompt sees a compacted view (stubs for old tool outputs).
5. **"Why LangGraph instead of a plain loop?"** Explicit state and reducers, conditional edges, checkpointing/resume, human-in-the-loop interrupts (Phase 5), streaming, testability.
6. **"How do you test an LLM agent deterministically?"** Scripted fake model (the exact tool calls and answers) + real in-process MCP server with a fake backend; assert on routing, prompts and state. Non-determinism is handled separately by evals (Phase 6).
7. **"Why gpt-4.1-mini?"** Measured latency/cost needs, temperature control, tool-calling reliability, price; reasoning models reserved for judging.
8. **"How do you authenticate to Azure OpenAI?"** Keyless: keys disabled, Entra ID token, RBAC role, DefaultAzureCredential (az login locally, Managed Identity in Azure).
9. **"Tell me about a time the model did something wrong."** The overlapping-incidents story (v1 → v3).

---

## Quiz (answer first, then expand)

**Q1. Why are health, logs and events fetched by code instead of letting the LLM call them?**
<details><summary>Model answer</summary>

They're needed in every investigation, so an LLM decision adds latency, cost and failure modes without adding value. A
deterministic parallel prefetch takes ~1 s with 0 LLM calls; the LLM's reasoning is reserved for decisions that need judgement
(what to search, what to verify, when to stop). That's the workflow-vs-agent distinction.
</details>

**Q2. How is the prefetched data given to the model so that every provider accepts it?**
<details><summary>Model answer</summary>

As a synthetic `AIMessage` containing `tool_calls` with ids, followed by `ToolMessage`s carrying the same `tool_call_id`s. Tool results
must correspond to a prior tool call in the transcript, or providers like OpenAI reject the request.
</details>

**Q3. The model replied "READY" without calling search_runbooks. What happens, and why is it in code rather than only in the prompt?**
<details><summary>Model answer</summary>

`route_agent` sends it to `remind`, which injects an internal note requiring a search (once). Prompts are suggestions the
model sometimes ignores (observed with gpt-4.1-mini); graph edges are enforced rules.
</details>

**Q4. Explain the coverage guard and the bug it fixed.**
<details><summary>Model answer</summary>

Triage builds incident windows (signals separated by quiet gaps) with typed signals and keywords. Before answering,
every significant window (OOM, error burst, memory growth) must be covered by at least one search query containing its
keywords. Without it, the agent searched only the frequent pool error, missed the later OOM, and once even invented a causal link
between them.
</details>

**Q5. A citation `[RB-FAKE-123] Fix` appears in the model's answer. What does the system do?**
<details><summary>Model answer</summary>

`check_grounding` extracts doc ids and keeps only those in `retrieved_doc_ids` (the ids returned by `search_runbooks` in this
thread). `RB-FAKE-123` is removed from the citations and reported in `invalid_citations` and the grounding warnings rendered at the end of the answer.
</details>

**Q6. Why did we switch the citation format from `RB-X § Section` to `[RB-X] Section`?**
<details><summary>Model answer</summary>

The model returned `§` as `\x00a7` (a NUL byte + "a7"), corrupting the citation. Strings the model must copy
verbatim should be plain ASCII; we also strip control characters from structured output as a defence.
</details>

**Q7. Azure budgets are set to $1. Why do we still need SpendGuard?**
<details><summary>Model answer</summary>

Budgets only send alerts, with hours of delay; they never stop consumption. SpendGuard checks the ledger **before** each call and raises
`SpendLimitExceeded`, so a runaway loop or eval job can't keep spending. It also prices cached input tokens correctly.
</details>

**Q8. What does `disableLocalAuth: true` change, and how does the app authenticate then?**
<details><summary>Model answer</summary>

API keys are disabled entirely. Clients must present an Entra ID token for the scope `https://cognitiveservices.azure.com/.default`,
and the identity needs the *Cognitive Services OpenAI User* role. Locally the token comes from `az login`, in Azure from a
Managed Identity, via `DefaultAzureCredential`. No secret to leak or rotate.
</details>

**Q9. Why rewrite git history before making the repo public, instead of just deleting the passwords in a new commit?**
<details><summary>Model answer</summary>

Old commits remain readable by anyone who clones the repo, so a later deletion doesn't remove the secret. `git filter-repo --replace-text` rewrites
every commit; then scan the full history (gitleaks) before the first push. If a real secret had ever been pushed, it would also
have to be **rotated**, because it should be considered compromised.
</details>

**Q10. Memory vs context: the follow-up turn used 2.4k input tokens while the thread's state is much larger. How?**
<details><summary>Model answer</summary>

The checkpointer stores the full state (all messages and artifacts), but `compact()` replaces tool outputs from previous turns
with one-line stubs before each LLM call. The model keeps the conclusions (previous diagnosis) and can re-call a tool if it
needs fresh raw data.
</details>
