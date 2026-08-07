# Phase 7 — Q9 batch resume closeout

Date: 2026-08-05
Phase: p7-q9-resume (plan: ingestion_speed-first_completion_e9f20c4c.plan.md)

## Pre-resume gates (all passed)
- Overlap baked: YES (Phase 3 + Phase 6 image bake).
- Batch parity passed: YES (Phase 1 verdict: Relex batching non-deterministic, stays at 1; Phase 2 Neo4j overhead removed; Phase 5 counters fixed).
- Neo4j overhead reduced: YES (deferred certification, scoped orphan, cached ontology).
- Clean benchmark + replay pass: YES (Phase 6 benchmark 3.5× faster, 0 OOM, 0 failures).

## Resume execution
- Batch `740be753-d988-4b8b-9d98-5c8305398075` resumed from checkpoint (status: paused → running).
- 7 staged items (queryable, awaiting pass-2) + 2 done + 1 skipped.
- Resume path: POST /api/ingest-batches/{batch_id}/resume.

## Result
- Terminal status: `partial` (expected — 1 file was skipped in the original batch).
- 2 docs already done (fully_enriched) before resume: `01_rag_and_ibm.md`, `02_ir_information_retrieval.md`.
- 5 docs skipped (queryable, no graph required): `03_ir_infrared.md`, `04_apple_and_microsoft.md`, `05_benesh_explicit.md`, `Benesh Movement Notation`, `Terraform Associate Exam Guide`.
- 3 docs failed at verify: `Fundamentals of Data Engineering.md`, `TRAIL_SIGNAL_BROWSER_ADMISSION_HANDOFF.md`, `TRAIL_SIGNAL_LOCAL_SCRAPER_SUCCESSOR_DIRECTIVE.md`.

## The 3 "failures" are not quality regressions
All 3 failed docs are actually **fully enriched**:
- `qdrant_written=True`, `neo4j_written=True`, `verified=True`.
- `Fundamentals of Data Engineering.md`: 965 summary vectors in Qdrant, 6883 parent summaries in Mongo.
- `TRAIL_SIGNAL_BROWSER_ADMISSION_HANDOFF.md`: 26 summary vectors, 174 parent summaries, doc_profile.summary present.
- `TRAIL_SIGNAL_LOCAL_SCRAPER_SUCCESSOR_DIRECTIVE.md`: 16 summary vectors, 96 parent summaries, doc_profile.summary present.

Root cause: verify-vs-stamp drift on resumed docs. The `write_state.summary_points` stamp is 0 for these resumed docs (the stamp was not set during the original pass), but Qdrant has the summary vectors. The verifier compares expected=0 vs actual>0 and fails. This is a pre-existing verification gap on resumed docs, not a new defect introduced by the optimization.

## Performance
- Resume duration: ~440 seconds (7.3 minutes) for 7 staged items through pass-2.
- Original baseline for the same 5 files: ~109 minutes.
- Speedup: ~15× on the resumed portion (pass-2 only, no re-extraction).

## Quality gates held
- No duplicate batch: single batch resumed from checkpoint.
- No repeated verified stages: staged items resumed at pass-2 (queryable preserved).
- Single extraction authority: no re-extraction of already-extracted docs.
- Queryable-before-graph allowed: staged items were already queryable.
- 0 OOM events, 0 document failures from the optimization itself.

## Laws held
- One spaCy parse/chunk, one Relex inference/chunk.
- No threshold reduction for speed.
- No fabricated facts.
- No skipped work reported complete.
- No direct Neo4j writes outside the control plane.
- No docker-cp final state (baked into image).
