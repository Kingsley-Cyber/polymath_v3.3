# Ingestion → Retrieval Rebuild — Implementation Plan

**Date:** 2026-07-02 · **Scope (owner-set):** the ingestion→retrieval FLOW only — extraction,
summarization, parent/child chunking, filtering, indexing, schema usage, and the 3 retrieval
tiers. Visual/graph view comes AFTER. **Priority = REBUILD, not re-ingestion** (test on ~5 docs).
**North star:** NotebookLM-grade breadth + depth + precision, matched to retrieval type, at a
fast/reasonable query. Consolidates: RETRIEVAL_LAYER_SPEC · METADATA_LAYER_AUDIT ·
BRIDGE_RETRIEVAL_DESIGN · EXTRACTION_VS_METADATA · repo-health inventory (wcmd175qo).

---

## 0. The governing principle — the Stage Contract
> **Every stage declares `(input → process → output → CONSUMER → end goal)`. Any produced field
> with no named downstream consumer is cut. Any consumer that reads a field never produced is a
> bug. Nothing is "extracted but unused"; nothing is "used but unvalidated."**

This is the whole rebuild in one rule. "Cut hard, with reasoning" = apply this rule to every
field and stage; a field is removed only when we can name that it has no synthesis, retrieval,
index/filter, or context consumer — never by blind deletion.

---

## 1. Current flow — where the contract is BROKEN (grounded)
| Stage | Produces | Real consumer today | Verdict |
|---|---|---|---|
| Chunk (tier_chunker) | parent/child, `resolved_child_strategy` | retriever | **CORRECTED (red-team): semantic_split is the DEFAULT and ACTIVE** — `_build_policy` sets requested/resolved='semantic_split' (tier_chunker.py:24-25); body chunks split via `_split_by_paragraph_idea` (:735). It is paragraph/idea-based, NOT embedding-similarity. Open question = whether to UPGRADE to embedding-similarity, not "unstub" |
| Ghost A | summary, domain, topics | summary vector + domain(soft) | **PARTIAL: `topics` has NO consumer; summary schema is prose, not token-minimal** |
| Ghost B (GLiNER/GLiREL) | entities, relations, aliases, facts | Neo4j + Mode A live only | **DEAD-END: never promoted to retrieval metadata** (EXTRACTION_VS_METADATA) |
| Facets | facet_ids/text | coverage pass | works, but coarse topic slugs |
| Write | Mongo/Qdrant/Neo4j payloads | retriever | **NO `promote()` — per-field ad-hoc; domain/mechanisms are POST-ingest backfills** |
| Filter/index | corpus_id, chunk_type, chunk_kind, domain, facet_ids | Qdrant `must`/`must_not` | thin — entities/concepts/mechanisms/semantic-type/temporal absent |
| Retrieve (3 tiers) | candidates | rerank → packet | works but tiers lack crisp metadata contracts; bridges flattened |

**The rot, named:** semantic chunking stubbed; `topics` orphaned; Ghost-B extraction dead-ends;
no `promote()` layer (backfills instead); metadata too thin to filter on; no mechanism/concept/
temporal layer. Every one is a Stage-Contract violation.

---

## 2. Rebuilt pipeline — stage contracts (the target)

**S1 Parse** (docling_adapter) → *out:* clean text + `{title, author, source_type, document_date}`
extracted at parse (currently only filename). *Consumer:* ChunkMetadata + packet prefix + temporal
filter. *End goal:* citation + as-of retrieval.

**S2 Chunk — SEMANTIC 100%** (tier_chunker) → *out:* parent (≈1200 tok) + child (≈128 tok) on
**real semantic boundaries** (embedding-similarity split, not sentence-merge fallback). *Consumer:*
embed + retrieve. *End goal:* a child is one coherent idea (precision) under a coherent parent
(context). **Rebuild: implement the semantic splitter; delete the sentence_merge stub path or keep
ONLY as an explicit fallback with a logged reason.**

**S3 Summarize — token-minimal elite schema** (Ghost A) → replace prose summary with a structured,
LLM-optimized minimal object:
```
{ "gist": "<=40-tok dense claim of what this parent establishes",
  "domain": "<taxonomy>", "mechanisms": ["<=5 transferable snake_case"],
  "key_terms": ["proper nouns / defined terms"], "chunk_kind_semantic": "definition|claim|procedure|principle|framework|example|comparison|warning" }
```
*Consumers:* `gist` → summary vector (Hybrid tier) + packet context; `mechanisms` → bridge lane +
diversity; `domain` → soft boost; `key_terms` → concept recall; `chunk_kind_semantic` → operator
match + diversity. *End goal:* every field feeds retrieval OR synthesis — no orphan `topics`.
*(`topics` is cut and subsumed by `key_terms` + `mechanisms`, each with a consumer.)*

**S4 Extract** (Ghost B / GLiNER/GLiREL) → *out:* entities (canonical_name + aliases + type),
relations (typed). *Consumers (NEW):* graph write **AND** `promote()`. *End goal:* extraction now
has TWO consumers, closing the dead-end.

**S5 promote()** — the missing single mapping step (services/ingestion/promote.py), runs after
S3+S4, before writes. Pure, deterministic, normalizes once. Emits the 5 schemas
(EXTRACTION_VS_METADATA): ChunkMetadata, RetrievalPayload (domain, mechanisms, concepts[]←entity
canonical+aliases, semantic chunk_type, temporal), GraphWriteModel, SummaryPayload. *End goal:*
extraction→metadata is one auditable place; backfill scripts become the *forward path*.

**S6 Write + index** — atomic-as-possible across Mongo/Qdrant/Neo4j with a write-state barrier so a
partial failure is detectable and replayable (fixes the ghost_b_staging-orphan fragility). Payload
indexes for every field a tier filters on.

---

## 3. The three retrieval tiers — crisp contracts (owner-defined)
| Tier | Stores | Consumes (metadata) | Contract / end goal | Budget |
|---|---|---|---|---|
| **Fast Vector** `qdrant_only` | Qdrant child dense+sparse | corpus_id, chunk_kind, (domain soft) | fastest DIRECT evidence; lookup/definition | fetch 50 → rerank 32 → 8 |
| **Hybrid** `qdrant_mongo` (default) | + Qdrant summaries (`gist`), Mongo lexical | + concepts[], key_terms, mechanisms | best default: breadth (summaries) + precision (children) + literal (BM25) | fetch 70 → rerank 40 → 12–14 |
| **Graph** `qdrant_mongo_graph` | + Neo4j Mode A | + relations, bridge cache | slow deep synthesis / relationships / cross-domain bridges | fetch 120 → rerank 40 → 14–16 + facts |

Cross-cutting (from v4 spec, already shipped): cross-encoder is the SOLE ranking authority (rank-
only lanes); domain is a SOFT boost NEVER a gate; mechanisms/concepts feed recall+diversity, never
score multipliers; deterministic packet with per-domain diversity for synthesis queries.

---

## 4. Cut-hard-with-reasoning (kill / fix list — each justified)
**CUT (no consumer / dead):**
- `resolved_child_strategy = sentence_merge` stub → replaced by real semantic split (S2).
- Ghost A `topics` field → subsumed by `key_terms`+`mechanisms` (each with a consumer).
- Disabled browser-ingest endpoint (410 Gone) — dead surface; formally remove.
- `_orchestrator_legacy.pyc` path — missing; keep only the tracked fallback, delete the loader.
**FIX (fragility, from inventory — bake into the rebuild):**
- Semantic chunking stub (S2). · promote() replacing 3 backfill scripts (S5).
- Partial cross-store write / ghost_b_staging orphan → write barrier (S6).
- Reranker circuit-breaker + embedder-URL revert invisible to health → expose in /health
  (real-work probe; ends green-while-dead). · reranker split-recovery recursion needs a depth cap.
- Config/env drift (RERANKER_TIMEOUT, RERANKER_SCORE_SCALE, embedder URL) → one validated source.
**KEEP (works, on the critical path):** 3 tiers, coverage + evidence-plan passes, LLM decomposer,
HyDE, reranker sidecar (fp16 CE), answerability gate, facets, multi-corpus fair mode. Graph
view/MCP/web/agentic KEEP but OFF the critical path for this phase.

---

## 5. Rebuild sequence (rebuild-first; re-ingest is validation, not the work)
- **R0 Contracts:** define the 5 schemas + Stage-Contract table as typed models; write the
  golden + habits-NN + seducer probes as the acceptance harness. *(no data change)*
- **R1 Semantic chunking:** implement the real semantic splitter; unit-test boundary quality on a
  handful of docs. *Gate:* children are single-idea coherent.
- **R2 Elite summary + promote():** new Ghost A minimal schema + `promote()` mapping. *Gate:*
  every emitted field has a named consumer; promote() unit-tested deterministic.
- **R3 Retrieval wiring:** tiers consume the new metadata (concepts recall, mechanisms diversity,
  semantic-type operator match); domain stays soft. *Gate:* golden battery ≥ baseline, habits-NN
  bridges CE-scored (not 0.5), per-domain diversity present.
- **R4 5-doc re-ingest:** ingest ~5 diverse books through the rebuilt pipeline; verify every
  payload field is populated + filterable + consumed; run the full probe battery.
- **R5 Hardening:** write barrier, health-exposed circuit state, config single-source, then the
  full re-ingest when R4 is green.

---

## 6. Stress test (failure modes → mitigation, must survive)
- **Semantic splitter is slow/OOM on a huge doc** → token-budget cap + timeout → explicit
  sentence-merge fallback WITH a logged reason (not a silent default).
- **promote() drops a field** → unit test asserts every RetrievalPayload field is non-null for a
  fixture with full extraction; CI gate.
- **Partial write (Qdrant ok, Neo4j down)** → write barrier marks doc incomplete; retriever must
  never serve a half-indexed doc; replayable.
- **New metadata unindexed → slow filter** → payload index created in the SAME migration; CI
  asserts index presence before enabling the filter.
- **Determinism** → same doc re-ingested twice → identical payloads (promote() is pure); same
  query → identical packet_hash.
- **Elite summary too terse → loses synthesis signal** → A/B the `gist` schema vs current prose on
  the answer-quality probes before locking; keep the richer variant if quality drops.
- **Cross-domain recall regresses while chasing precision** → habits-NN probe (≥3 domains, ≥4
  books, bridges CE-scored) is a MERGE GATE, not an afterthought.

## 7. Acceptance (definition of "just works" for this phase)
1. Every ingest stage's every output field has a named consumer (Stage-Contract table, enforced).
2. 100% semantic chunking (no silent sentence-merge).
3. One `promote()` layer; extraction (incl. Ghost B) reaches retrieval metadata; 0 dead-end fields.
4. 3 tiers each honor their contract + consume their metadata; cross-encoder sole authority.
5. Probes green: golden battery (precision), seducer (0 off-topic), habits-NN (cross-domain recall
   + diversity), determinism (packet_hash stable), latency (fast/reasonable per tier).
6. Fragility fixed: no green-while-dead; no config drift; partial writes detectable.

---

## 8. RED-TEAM HARDENING (workflow wb5ehn7oo — supersedes naive parts above)
The adversarial pass found 3 real blockers + corrected 1 factual error. Amendments:

**CORRECTION — semantic chunking is NOT stubbed** (verified tier_chunker.py:24-25, 731-741).
It defaults to `semantic_split` → `_split_by_paragraph_idea` (paragraph/idea granularity, no
embeddings). So S2's real choice: **(a)** accept paragraph-idea splitting as "semantic" (works
today, zero ingest cost), or **(b)** build an embedding-similarity splitter — which hits a
**chicken-egg**: embeddings are computed at Phase 5, AFTER chunking (Phase 2). (b) needs a
pre-chunk embed pass (extra ingest cost) and must be justified by a measured precision gain.
**Decision required from owner before R1.** R1 also must add: a token-budget cap in
`_split_by_paragraph_idea`, a timeout→`sentence_merge` fallback WITH a logged reason (not silent),
and stop silently reverting typo/None strategies (tier_chunker.py:25).

**BLOCKER-1/2 — promote() timing.** Ghost A + Ghost B run in PARALLEL (`_run_ghosts_parallel`,
worker.py:2541) and **children are written to Mongo at `_checkpoint_child_chunks` (2522) BEFORE
the ghosts finish**; both ghosts resume independently from staging. So promote() CANNOT be "one
pure step before writes." **Decision:** make `promote(ghost_a|None, ghost_b|None, chunk_ids)` a
pure function whose *output* is applied as an **idempotent post-ghost promotion write** (like the
domain backfill, but in-pipeline, deterministic, re-runnable on resume) — OR add a barrier that
delays child writes until after both ghosts + promote(). Idempotent-post-write is lower risk and
matches the resume model. Codify ONE invariant: *promoted metadata is written by promote() only,
never ad-hoc per field.*

**BLOCKER-3 — R3 consumers are vapor.** grep of services/retriever/* reads ZERO of mechanisms /
concepts[] / gist / key_terms / chunk_kind_semantic. R2 would write fields nothing reads.
**Amend sequence:** R3 (retrieval wiring) ships WITH unit consumer-tests using MOCKED Qdrant
payloads (fields pre-set), so R3 is validated without waiting on data; and **R4 (5-doc re-ingest)
becomes a PREREQUISITE of the R3 golden-battery gate** (real data before the quality claim).

**BLOCKER-4 — write barrier.** Sequential independent Mongo/Qdrant/Neo4j writes let a partial
Qdrant batch leave a doc served half-indexed. R5→**promote to R2-blocking**: document-level
`write_state` (writing|qdrant_failed|complete); retriever must skip non-`complete` docs; on
exception do NOT flip `*_written`.

**BLOCKER-5 — `topics` is LIVE, not orphaned.** Written at ingestion_service.py:1679,
worker.py:1203; asserted in test_ghost_a_summary_fallback.py:68; parsed in ghost_a.py. **Do NOT
cut** — coordinated migration only (stop writing → null/drop on existing → remove field+parser →
update tests), or keep it as a deprecated no-consumer field. The naive "cut topics" in §2.S3/§4
is WITHDRAWN pending that migration.

**GIST RISK (survived as risk, not blocker).** ≤40-token `gist` may embed poorly as a summary
vector (short-text degradation). **Hard merge-gate, not A/B afterthought:** run the habits-NN +
golden probes on prose-summary vectors vs gist vectors; REJECT gist if recall/NDCG drops >5% on
any domain; if rejected, keep prose for the *vector* and use the minimal schema for *metadata* only.

**Downgraded (not blockers):** reranker fp16 determinism (re-verify services/reranker.py, treat
packet_hash as post-R1 hardening), `_orchestrator_legacy` (guarded), browser-ingest 410 (minor).

**Revised R0 (must precede R1):** walk worker.py's ACTUAL sequence, rewrite §1's dependency table
with real line refs, and lock the promote() placement + write-barrier decisions above. Nothing in
R1+ starts until R0 + the two owner decisions (semantic (a)/(b); promote placement) are settled.

**Red-team verdict:** the Stage-Contract PRINCIPLE survived intact; the EXECUTION as first
sequenced did not. With the amendments above the plan is executable. Owner approval + the two
decisions gate R1.
