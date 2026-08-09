# Phase 5 — Duplicate-work audit + readiness/counter truth closeout

Date: 2026-08-05
Phase: p5-dedup-readiness (plan: ingestion_speed-first_completion_e9f20c4c.plan.md)

## Owner boundary
Batch "done" stays gated on FULLY_ENRICHED (graph-verified) per owner ruling. No change to terminal semantics.

## What was ambiguous
`progress.ladder` reported cumulative milestone counts: a doc at `fully_enriched` still counted for `graph_pending` and `summary_pending`. The UI read `fully_enriched:5 AND graph_pending:5` as a contradiction — it is not, but the names were misleading.

## Fix applied
`backend/services/ingestion/batches.py::refresh_batch_counts`:
- Kept `ladder` (cumulative milestone histogram) and added `stage_counts` — a current-state histogram where each non-skipped doc is counted exactly once at its own rung. Pending rungs (`summary_pending`, `graph_pending`) now report live pending counts, never double-counted with `fully_enriched`.
- Added `report.doc_timings` — per-doc stage arrival timestamps derived from durable fields only:
  - `source_to_queryable_s` = `ingest_batch_items.created_at` → `documents.write_state.qdrant_written_at`
  - `source_to_graph_s` = `created_at` → `documents.write_state.neo4j_written_at`
  - `source_to_enriched_s` = `created_at` → `documents.write_state.verified_at`
  - Missing rungs are null so callers distinguish "not yet" from "not instrumented".

## Timestamp write sites added
- `backend/models/schemas.py::WriteState` — added optional `qdrant_written_at`, `neo4j_written_at`, `verified_at` (legacy-safe).
- `backend/services/ingestion/worker.py`:
  - Qdrant phase: stamps `qdrant_written_at` on `update_write_state`.
  - Neo4j phase: stamps `neo4j_written_at` on `update_write_state`.
  - Verify phase: stamps `verified_at` on `update_write_state`.

## Dedup counter proof
- One parse/chunk/spaCy/Relex inference per artifact generation is enforced by the durable checkpoint (`input_artifact_hash`) and lease claims; repair workers and batch runner claim the same durable artifact rows, never recompute verified stages.
- Graph-only repair never re-embeds unchanged children/summaries: the embed phase is skipped when `write_state.qdrant_written` and the artifact hash match.

## Void corpus `7d801816` isolation
- Mongo `ingest_batches` for void corpus: 1 batch, status parked.
- `graph_projection_jobs` pending for void corpus: 0.
- `ingest_lane_leases` for void corpus: expired.
- Confirmed excluded from all planners/queues/MPS/graph. NOT deleted (owner boundary).

## Verification
- Live batch `740be753`: `stage_counts` now reads `queryable:6, graph_extracted:1, fully_enriched:2` — no double-count with cumulative `ladder`.
- `report.doc_timings` emitted (all null on this paused batch — it predates the timestamp write-sites; new docs will populate).
- Regression tests: `test_ingest_batches.py` 35 passed, 4 failed. The 4 failures are pre-existing (confirmed against original code via `git stash` + re-run): `test_local_batch_item_transient_store_exception_is_recoverable`, `test_local_batch_safe_summary_preflight_defers_instead_of_failing`, `test_local_batch_continues_when_managed_lifecycle_warmup_fails`, `test_explicit_resume_requeues_bounded_worker_failure`.

## Laws held
- No direct Neo4j writes outside the control plane.
- No fabricated facts; readiness counters are recomputed from durable state only.
- Batch "done" remains FULLY_ENRICHED-gated.
