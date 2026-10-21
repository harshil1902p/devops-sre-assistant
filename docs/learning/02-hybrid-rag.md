# Phase 2: Hybrid RAG (pgvector + full-text + RRF + cross-encoder reranking)

## What we built

| Path | Purpose |
|---|---|
| `infra/compose.yaml` | The assistant's **own** Postgres 16 + pgvector 0.8 on port 5433, kept separate from the demo stack it monitors. |
| `src/sre_assistant/rag/chunking.py` | Structure-aware **parent-child** chunker + **naive** fixed-window baseline. |
| `src/sre_assistant/rag/store.py` | Schema (documents / parents / chunks / ingest_state / lexeme_df), HNSW + GIN indexes, vector search, keyword search, metadata filters. |
| `src/sre_assistant/rag/ingest.py` | **Incremental, idempotent** ingestion using content fingerprints. |
| `src/sre_assistant/rag/fusion.py` | **Reciprocal Rank Fusion**, a pure function. |
| `src/sre_assistant/rag/rerank.py` | **Cross-encoder reranker** (ONNX, `ms-marco-MiniLM-L-6-v2`, CPU). |
| `src/sre_assistant/rag/retriever.py` | Pipeline: vector + keyword → RRF → rerank → group by parent → cited context. |
| `src/sre_assistant/llm/embeddings.py` | Query/document **instruction prefixes** for asymmetric embedding models. |
| `scripts/ingest.py`, `scripts/search.py`, `scripts/retrieval_benchmark.py` | Index, explore (shows vector/keyword rank per hit), benchmark. |
| `data/eval/retrieval_queries.yaml` | 34 hand-written queries (17 log-style, 17 symptom-style), never copied from the corpus. |
| `eval/results/retrieval.json` | Benchmark evidence (commit results, don't just quote them). |

```bash
docker compose -f infra/compose.yaml up -d
uv run python scripts/ingest.py                 # incremental (--reset rebuilds)
uv run python scripts/search.py "FATAL: sorry, too many clients already"
uv run python scripts/search.py "pods restart, logs stop" --mode vector --no-rerank
uv run python scripts/retrieval_benchmark.py
uv run pytest && uv run pytest -m integration
```

### The pipeline
```
query ─┬─▶ embed (nomic, "search_query:" prefix) ─▶ pgvector HNSW cosine top-20 child chunks ─┐
       └─▶ tsvector query (rare terms only, OR'ed) ─▶ GIN full-text top-20 child chunks ───────┴─▶ RRF (k=60)
   ─▶ cross-encoder scores top-30 fused children ─▶ group children by parent ─▶ top-2/3 parents (whole docs)
   ─▶ <source id="[RB-AUTH-DB-POOL] Example Log Output"> … </source>  (citable context for the LLM)
```

---

## 1. Chunking: the decision that matters most

### What we tried first, and why it failed
The project brief said "children of 200–400 tokens". We **measured** the corpus first:

| Unit | Median tokens | p90 | Max |
|---|---|---|---|
| Document | 420 | 615 | 1123 |
| `##` section | 38 | 96 | 266 |
| Block (code fence / bullet / paragraph) | 23 | 53 | 192 |

With 300-token children, each child swallowed its whole section: **391 parents = 391 children**. Parent-child had collapsed into plain section chunking.
**Lesson: chunk sizes come from the data, not from a blog post.** The 200–400 rule suits long enterprise documents.

### The design we kept (adaptive small-to-big)
- **Child** = a block packed to ≤ **128 tokens**, prefixed with `"<doc title> | <section>"`. Small, so a match is precise (one error log, one fix bullet).
- **Parent** = the **whole document** if it is ≤ 1,500 tokens, otherwise one `##` section.
  Why? A query matches *symptoms or error signatures*, but the answer is in *Root Cause / Permanent Fix*. A section-level parent would give the LLM the log it already has and not the fix.
- **Citation** = the section of the best-matching child (`[RB-AUTH-DB-POOL] Example Log Output`), so the answer can point to exactly where it matched. (Originally written with `§`; changed to ASCII in Phase 4 after the model returned it corrupted as `\x00a7`, see 04-agent.md.)
- **Code fences are atomic.** Never split a stack trace or a YAML block in half; a `## ` line inside a code fence is not a header (unit-tested).

### Contextual chunk headers
A lone child like ```` ```yaml resources: limits: memory: 512Mi``` ```` means nothing on its own. Prefixing `Runbook: Container OOMKilled… | Permanent Fix` puts the meaning into both the embedding and the keyword index. It costs about 15 tokens per child and is one of the cheapest quality wins in RAG.

---

## 2. Vector search with pgvector

- `embedding vector(768)` plus an **HNSW** index (`vector_cosine_ops`, `m=16, ef_construction=64`). `<=>` is cosine distance, so similarity = `1 - distance`.
- **HNSW vs IVFFlat:** HNSW gives better recall/latency and needs no training step, but builds more slowly and uses more memory. IVFFlat needs data present before building lists and has to be rebuilt as data drifts.
- **Filtered ANN pitfall:** HNSW returns `ef_search` candidates and **then** the `WHERE` filter is applied, so a selective filter can return fewer than k rows. pgvector 0.8's `hnsw.iterative_scan = strict_order` keeps scanning until enough rows pass. We set it per connection.
- **Asymmetric embeddings:** `nomic-embed-text` was trained with `search_query:` and `search_document:` prefixes. Leaving them out degrades retrieval *silently*. `PrefixedEmbeddings` adds them; OpenAI `text-embedding-3` needs none.
- **Dimension contract:** `check_embedding_dim()` reads `atttypmod` from `pg_attribute` and refuses to run if the model's dimension differs from the column's (the Phase 0 quiz, now enforced in code).

---

## 3. Keyword search: Postgres full-text vs BM25

What we observed in Postgres's text parser:
- `sqlalchemy.exc.TimeoutError` → **one** whole token. That's good for exact matching, but a bare `TimeoutError` wouldn't match it.
  **Fix:** index `content || regexp_replace(content, '[._/:=-]+', ' ')`, i.e. both the compound name and its parts.
- `pg_stat_activity` → `pg`, `stat`, `activ` (stemmed); `OOMKilled` → `oomkil`.

**Postgres `ts_rank_cd` is NOT BM25.** BM25 has IDF (rare terms count more) and document-length normalization; ts_rank has neither.
Without IDF, a JSON log query is dominated by generic terms (`level`, `service`, `msg`, `error`) that appear in every log excerpt.
Our approximation:
1. `lexeme_df` stores the document frequency of every term (`ts_stat`).
2. At query time, keep only terms in ≤ 25% of chunks (the 32 rarest), then **OR** them (`websearch_to_tsquery` ANDs everything, and a 40-term log line would match nothing).

At production scale you'd use real BM25: **Azure AI Search** (Phase 7), Elasticsearch/OpenSearch, or the ParadeDB `pg_search` extension.

---

## 4. Fusion: Reciprocal Rank Fusion

`score(d) = Σ_rankers 1 / (k + rank_r(d))`, with k = 60.
- Uses **ranks only**, so there's no need to normalize cosine (0..1) against ts_rank (unbounded). That's why it's the default in Azure AI Search, Elastic and OpenSearch.
- It **rewards agreement**: ranks (3, 1) beat rank 1 in only one list (unit-tested).
- That's also its **weakness**: if one retriever misses a document entirely, the other's strong vote is halved. Both of our hybrid misses were this case.
- Larger k flattens the advantage of the top ranks; smaller k makes rank 1 dominate.

---

## 5. Reranking: two-stage retrieval

| | Bi-encoder (embeddings) | Cross-encoder (reranker) |
|---|---|---|
| How it works | Query and document embedded **separately**; compare vectors | Reads **(query, document) together** with full attention |
| Speed | Precomputed and indexed; milliseconds over millions | One forward pass per pair, so only the top N |
| Quality | Coarse (a whole document squeezed into one vector) | Precise (sees the exact interplay of words) |
| Role | **Recall** stage | **Precision** stage |

The two misses, before and after reranking:

| Query | Hybrid (RRF) top-1 | After rerank |
|---|---|---|
| `KeyError: 'REDIS_URL' Back-off restarting failed container` | redis-connection-refused (the word *redis* pulled vectors) | **crashloop-missing-env** (vector rank −, keyword rank 2) |
| "a 20 second redis failover restarted every replica" | redis-timeout | **liveness-probe-dependency** |

We rerank **children** (~70 tokens), not whole documents, because MiniLM's input limit is 512 tokens and short passages are what it was trained on.
Platform note: `onnxruntime ≥ 1.20` has no Intel-Mac wheels, so it's pinned `<1.20` for `darwin/x86_64` only (a platform-specific dependency marker).

---

## 6. Ingestion engineering

- **Fingerprint** = sha256(document content + chunker version + embedding model). Unchanged documents are skipped; changing the chunker or the model re-embeds everything automatically.
- **Atomic per-document swap:** delete and insert that document's parents and chunks in one transaction, so readers never see a half-indexed document.
- **Deletes propagate:** documents removed from the corpus are removed from the index (no ghost citations).
- **Collections** keep several chunking strategies side by side for A/B comparison.
- **Real event during the build:** `ruff format` reformatted Python code blocks *inside* 3 generated Markdown documents. The **drift test** failed, and the **incremental-ingest test** saw 3 changed fingerprints and re-embedded exactly those 30 chunks (8 s vs 160 s for a full re-index). Fix: exclude `data/` from the formatter.
  Moral: generated data needs the same protection as generated code.

---

## 7. Results and how to talk about them

| Config | hit@1 | hit@3 | MRR | fix in context | context tokens | p50 |
|---|---|---|---|---|---|---|
| naive windows, vector, top 5 | 0.85 | 1.00 | 0.92 | 0.91 | 1113 | 56 ms |
| naive windows, hybrid, top 5 | 0.97 | 1.00 | 0.98 | 0.94 | 1123 | 67 ms |
| parent-child, vector, top 3 | 0.91 | 0.97 | 0.94 | 0.97 | 1395 | 73 ms |
| parent-child, keyword, top 3 | 0.88 | 0.94 | 0.92 | 0.94 | 1444 | 15 ms |
| parent-child, hybrid, top 3 | 0.94 | 0.94 | 0.95 | 0.94 | 1406 | 77 ms |
| **parent-child, hybrid + rerank, top 2** | **1.00** | **1.00** | **1.00** | **1.00** | **1003** | 995 ms |

What you can honestly say:
- "Hybrid beat either retriever alone (vector 0.91 / keyword 0.88 → 0.94 hit@1). Adding a cross-encoder reranker fixed the remaining failures, which were entity-word traps."
- "Parent-child with reranking put the correct fix in context 100% of the time using **11% fewer tokens** than naive top-5 windows (1003 vs 1123), which only reached 94%."
- "On short documents naive chunking was surprisingly competitive; parent-child pays off on long documents where fixed windows separate symptoms from fixes."
- "**The perfect score means the query set is too easy**: 34 queries I wrote over a corpus I wrote. I don't trust it; Phase 6 builds a harder golden set."
- Trade-off: reranking costs about **1 s per query on CPU** (a GPU or Azure's semantic ranker is much faster). Worth it for an incident assistant (accuracy over latency); not for autocomplete.

### Metric definitions
- **hit@k:** the correct scenario is among the first k results. **MRR:** mean of 1/rank of the first correct result (rewards ranking it higher).
- **fix_in_context:** the context actually contains the scenario's fix text. A hit on the log section is worthless if the LLM never sees the fix. Retrieval metrics should be tied to what the generator needs.
- Report **log** and **symptom** queries separately: vector search is weak on symptom queries (0.77 for naive vector), while keyword search shines on exact error codes.

---

## Interview questions you should be able to answer

1. **"Walk me through your retrieval pipeline."** The diagram above, plus *why* each stage exists: recall (vector for meaning, keyword for exact error codes) → fusion (RRF, rank-based) → precision (cross-encoder) → context sufficiency (small-to-big parents) → citations.
2. **"Why hybrid search for logs?"** Error codes such as `40P01`, `ECONNREFUSED` or `QueuePool` are rare exact tokens; embeddings blur them (all timeouts look alike), while keyword search nails them but misses paraphrases. Hybrid gets both.
3. **"How do you choose chunk size?"** Measure the token distribution of documents, sections and blocks, keep structural units (headers, code fences), make children small for matching and parents big enough to contain the answer, then validate with retrieval and fix-coverage metrics.
4. **"Why RRF instead of a weighted score sum?"** Scores aren't on comparable scales, and RRF needs no tuning or normalization. Its known weakness is single-retriever hits, which reranking covers.
5. **"pgvector or a dedicated vector DB (Pinecone, Azure AI Search)?"** pgvector: one database for metadata, ACID, SQL joins and filters, cheap; good up to roughly millions of vectors. A dedicated or managed service gives real BM25, semantic ranking, scale-out and managed operations. We use pgvector locally and Azure AI Search in the cloud behind the same retriever interface.
6. **"How do you keep the index fresh without re-embedding everything?"** Content fingerprints, per-document atomic swaps, deletes propagated, and chunker and model versions in the fingerprint.
7. **"How do you know retrieval is good?"** A labelled query set (log and symptom), hit@k/MRR, fix-in-context, token cost and latency, reported per segment and committed as evidence. A saturated score means you need a harder set.
8. **"How do you prevent SQL injection with dynamic filters?"** Allowlist the column names and turn them into `sql.Identifier`; values are always bound parameters (unit-tested with an injection string).

---

## Quiz (answer first, then expand)

**Q1. Your 300-token child chunker produced exactly one child per section. Why is that a problem, and what did we change?**
<details><summary>Model answer</summary>

Sections were ~38 tokens (median), so each child = its whole section and parent == child: no precision gain and no
context gain. We measured the corpus, made children block-level (≤128 tokens) and made the parent the whole document
(≤1,500 tokens) so the LLM sees symptoms *and* fix, with section-level parents as the fallback for long documents.
</details>

**Q2. Compute RRF (k=60) for doc A (vector rank 1, missing from keyword) and doc B (vector rank 3, keyword rank 2). Which wins?**
<details><summary>Model answer</summary>

A = 1/61 ≈ 0.01639. B = 1/63 + 1/62 ≈ 0.01587 + 0.01613 = 0.03200. **B wins**: RRF rewards agreement between retrievers.
</details>

**Q3. Why isn't Postgres `ts_rank_cd` the same as BM25, and how did we work around it?**
<details><summary>Model answer</summary>

ts_rank has no IDF and no BM25-style length normalization, so generic terms count as much as rare error codes. We
store document frequencies (`ts_stat` → `lexeme_df`), drop query terms in more than 25% of chunks, keep the 32 rarest and OR them. In production,
use real BM25 (Azure AI Search, OpenSearch, or ParadeDB pg_search).
</details>

**Q4. A filtered vector query (`service = 'payments-api'`) sometimes returns only 3 results when you asked for 20. Why, and how do you fix it?**
<details><summary>Model answer</summary>

HNSW returns its `ef_search` nearest candidates, then the WHERE filter discards most of them (post-filtering). Fix with
pgvector ≥ 0.8 `hnsw.iterative_scan` (keep scanning), a larger `ef_search`, partial indexes per hot filter value, or
partitioning by tenant or service.
</details>

**Q5. Bi-encoder vs cross-encoder: why not just use the cross-encoder for everything?**
<details><summary>Model answer</summary>

A cross-encoder must run once per (query, document) pair at query time; nothing can be precomputed or indexed, so scoring a
whole corpus is far too slow. Use bi-encoder and keyword retrieval for recall over everything, then the cross-encoder on the top ~30 for precision.
</details>

**Q6. What does `fix_in_context` capture that hit@1 doesn't?**
<details><summary>Model answer</summary>

hit@1 only says the right *document or scenario* ranked first. fix_in_context checks that the text the LLM needs to answer
(the fix) is actually in the context window. Naive windows can hit the log excerpt while cutting off the fix, a retrieval
"success" that still produces a bad answer.
</details>

**Q7. You switch embeddings from nomic (768) to text-embedding-3-small (1536). What does our code do, and what's the safe rollout?**
<details><summary>Model answer</summary>

`check_embedding_dim()` raises (vector(768) ≠ 1536), and the fingerprint includes the model name, so every document will
re-embed. Safe rollout: ingest into a new collection or index (blue/green), run the retrieval benchmark on both, switch
config, keep the old one for rollback.
</details>

**Q8. Our benchmark shows 1.00 everywhere for hybrid + rerank. What do you say if an interviewer asks "so retrieval is solved?"**
<details><summary>Model answer</summary>

No. The query set is small (34), self-written, over a small self-written corpus, so it's saturated and possibly biased.
Next steps: a larger golden set with real noisy logs (multi-error, truncated), unanswerable or out-of-scope queries, queries
written by someone else, and measuring end-to-end answer quality (faithfulness), not just retrieval. That's Phase 6.
</details>

**Q9. What does the `search_query: ` prefix do, and what happens if you forget it?**
<details><summary>Model answer</summary>

nomic-embed-text is an asymmetric, instruction-tuned embedding model: queries and documents were embedded with different
task prefixes during training. Without them, query and document vectors land in slightly mismatched regions and
retrieval quality drops **silently**: no error, just worse rankings. Only an evaluation would catch it.
</details>
