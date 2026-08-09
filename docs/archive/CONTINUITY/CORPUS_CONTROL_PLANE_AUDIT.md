# CORPUS CONTROL PLANE — ARCHITECTURE AUDIT & BUILD SPEC
**Author:** Claude (ops/supervision) · 2026-07-21 · graphify-guided (14,089 nodes / 36,627 edges) + live Mongo probes
**Executor:** Codex · **Owner sign-off:** King
**Verdict up front: the control plane is ~90% already built as request-scoped API endpoints. Nobody wired a resident loop around them. This is a WIRING project (~3–5 days), not a rebuild.**

---

## 1. What e2e must mean (the contract this spec targets)

`ingest(files) → [deterministic: parse → chunk → embed → Qdrant] → [RunPod extraction, fail-closed] →
[summaries via provider lanes, schema-gated] → [promote() → Neo4j facts/edges] →
readiness: queryable_partial (chunks searchable) → fully_ready (summary lane + graph lane live)`
— with **no human or agent ever probing Mongo to learn state**. One status call answers: what stage, what's blocking, why, and what will fix it.

---

## 2. State surfaces that ALREADY exist (graphify + live-verified)

| Surface | Where | What it records | Health |
|---|---|---|---|
| Doc state machine | `documents.ingest_stage` + `write_state.{mongo_written,qdrant_written,verified}` | per-doc stage incl. `queryable_with_pending_summary(_and_graph)`, FULLY_ENRICHED_STAGES | EXISTS, trustworthy |
| Corpus readiness | `services/ingestion/readiness.py:1105 compute_corpus_readiness` (+ `_materialize_corpus_readiness_safely`) | totals: queryable / fully_enriched / verified; graph_required via use_neo4j | EXISTS |
| Summary queue | `summary_jobs` (kinds: retrieval_parent_summary, document_summary; statuses incl. blocked_no_parent_summaries, superseded; dead-letter + reconcile) | durable jobs w/ attempts, leases | EXISTS |
| Promote queue | `graph_promotion_jobs` (+ `services/ingestion/promote.py` = B2 promote(), pure/deterministic/idempotent, asserting tests) | claim→graph jobs w/ receipts | EXISTS |
| Provider call receipts | `ingest_provider_call_metrics` (44k docs): failure_class, accepted/rejected counts, tokens, cost to nano-USD, run_id | ground truth for every LLM call | EXISTS — the receipts that solved tonight's mystery |
| Leases | `ingest_lane_leases` + `services/ingestion/job_leases.py` (atomic acquire, expiry reclaim) | one-worker-owns-one-unit | EXISTS (King's rule #2 — already law) |
| Cost control | `SummaryCostController` (run_id, authority_usd, reservations/settlement) | spend authority per run | EXISTS |
| Backpressure | `_backpressure_pause_result` (`summary_generation_allowed`, `paused_pressure`) | pause lanes under pressure | EXISTS |
| Schema gate | `summary_semantics.py:585 _validate_summary_fields` (hard flags → quarantined) + strict Pydantic (ClaimRecordV1 etc.) | LLM output never writes raw (rule #3) | EXISTS |

**Conclusion:** every noun in King's CCP sketch exists. The missing thing is a VERB — a loop.

## 3. Actors today (the split-brain, receipts from 2026-07-20/21)

- **Planner endpoints** (`ingestion_service.plan_summary_jobs` :2750, `plan_graph_promotion_jobs` :2347; HTTP at `routers/ingestion.py:2096/:1953`): request-scoped, `limit=500` per call, **supports `doc_ids=[...]` pinning**. Called manually → planned one 500-wave at 20:44Z, never re-called → 9 tail docs (f8–ff) never planned → deadlock-by-omission (their document_summary jobs blocked_no_parent_summaries).
- **Runner endpoint** (`run_summary_jobs` :2796): consumes the queue with cost + backpressure + readiness — but the LIVE drains were `codex_deepseek_summary_*` / `codex_deepseek_assist_*` direct drivers writing AROUND the queue (queue stale 500/corpus while metrics show 42k accepted items). Two truths, no commander.
- **Auto-repair** (worker): logs `summary={'status':'failed'}` with NO reason — the component that told King "DeepSeek is broken" when the receipts said `length_truncated` at exactly 4096 (microbatch 4 × min(1024,·) clamp at `summary_tree_llm.py:63`), 87% acceptance overall.
- **~124 backfill scripts/functions** (graphify actor-inventory): each a one-off actor with its own universe. They exist BECAUSE the loop doesn't.

## 4. The three splits, named precisely

1. **Universe split** — readiness counts required-missing parents (clause-based); planner only knows rows from its last bounded call; direct drains snapshot their own lists. Three different "missing" numbers for the same corpus.
2. **Actor split** — queue exists; work happens outside it. Any reader of either surface gets a false story.
3. **Reason split** — failure REASONS live only in `ingest_provider_call_metrics.failure_class`; status surfaces say only "failed". Misdiagnosis is the default outcome for humans AND models.

## 5. Build spec (ordered by blast radius; each item has an acceptance probe)

### F1 — Corpus Commander loop (the missing verb) — ~2 days
Resident per-corpus loop (ingest-worker container):
`while not readiness.fully_ready: plan_all_lanes(full universe, no limit-orphans) → run via queue → reconcile → materialize readiness → sleep(backpressure-aware)`.
Rules: ALL summary/promote work flows through `summary_jobs`/`graph_promotion_jobs` (direct-driver mode becomes a queue feeder, never a bypass). Planner must page the FULL keyspace each cycle (limit=500 per call is fine — re-call until planned==0).
**Accept:** ingest a fresh 20-doc corpus, zero manual calls → `fully_ready` with receipts; the 9 pinned docs (COORDINATION 04:20Z flag) drain to 0 required-missing.

### F2 — One status surface with reasons — ~1 day
`GET /api/corpora/{id}/readiness` extended: per-lane `{summaries: {done, missing, blocked: [{doc_id, reason}], top_failure_classes_24h}}` — joined FROM `ingest_provider_call_metrics` + job statuses. Auto-repair log lines must carry `reason=` from the same source.
**Accept:** tonight's misdiagnosis is impossible: status shows `length_truncated 8,614 items (top)`, not `summary=failed`.

### F3 — Provider registry with quirk presets — ~1 day
One collection `provider_registry`: `{provider_family, base_url, defaults: {schema_mode, disable_thinking, microbatch_size, max_tokens_per_item, supports_json_object}}`. A lane = registry ref + api_key + overrides. Kills: hand-set extra_params drift, the hardcoded `min(1024,…)` clamp (becomes `max_tokens_per_item`), and re-discovering quirks in production. King's "any cloud LLM + any key just works."
**Accept:** add a brand-new OpenAI-compatible provider by INSERTING ONE DOC + key → summaries flow, zero code.

### F4 — Promote-job scaling — ~0.5 day
Per-doc claim batching/checkpointing so `claims_in=20,374` docs cannot die at a 300s wall (video doc 4bc34dfa… failed reason=claims_unpromoted at exactly 03:54:59→03:59:59).
**Accept:** that pinned job re-runs to completion with batch receipts.

### F5 — Interim unblocks (no code, DONE/pending GO)
`microbatch_size:1` on deepseek-assist lanes (King GO pending); targeted 9-doc drain pass (pinned ids in COORDINATION 04:20Z flag); drain-liveness check (both runs silent since 04:01:47Z).

## 6. What NOT to do
- No re-extraction (extraction layer healthy; claims inline, promote() live and landing — ecom 31k/video 35k v2 facts).
- No loosening of read filters (`r.corpus_ids`, `f.corpus_id`) — citation-integrity law.
- No new queue tech — `summary_jobs`/`graph_promotion_jobs` + leases already satisfy durable-queue requirements.
- Rebuild freeze respected: F1–F4 are Codex; config/ops are Claude; King signs the order.

## Executor addendum — 2026-07-21 — ID-join reconcile invariant

Tonight's parent-summary vector repair is now the F1 reconcile acceptance test, not a one-off operator trick.

**Required invariant per commander cycle:** for each retrieval summary lane and target vector collection, compute the ID-level set difference:

`required_mongo_parent_ids - qdrant_indexed_parent_ids == empty`

This is not a count check. Counts are only a final sanity check after the ID join passes.

Implementation contract:

1. Build `required_mongo_parent_ids` from `parent_chunks` using `parent_summary_required_clause()` plus non-empty validated `summary` text.
2. Build `qdrant_indexed_parent_ids` by scrolling Qdrant summary payloads and reading `parent_id` for the same corpus and collection.
3. Requeue or directly repair only the missing parent IDs, idempotently, through the queue-owned summary indexing path.
4. Mark readiness only after the set difference is empty for every required target collection.
5. Emit a receipt with required IDs, indexed IDs before, missing IDs repaired, collections written, and final sanity counts.

Manual repair receipt promoted to invariant:

| Corpus | Required Mongo parent IDs | Qdrant indexed IDs before | Missing IDs repaired | Final HRAG | Final naive |
|---|---:|---:|---:|---:|---:|
| `ecom` | `22,800` | `20,995` | `1,805` | `22,800` | `22,800` |
| `video` | `20,343` | `18,807` | `1,536` | `20,343` | `20,343` |

Document-rollup warning: `document_summary` vectors are not currently in the main retrieval path, but Tier-0/doc-summary routing will inherit the same dual-write stranding risk. When document-rollup summary vectors become query-visible, F1/F4 must apply the same ID-join invariant to the rollup lane: required Mongo document-summary IDs minus indexed Qdrant document-summary IDs must be empty before doc-summary readiness is true. Stale queued rollup rows are a janitor/reconcile item, not proof of missing parent-summary retrieval readiness.
