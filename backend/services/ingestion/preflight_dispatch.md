# Pre-flight Mistral Batch Dispatcher — Integration Plan

**Status:** orchestrator module shipped (`preflight_batcher.py`); queue
wiring + frontend toggle deferred for review.

**Goal:** flip cross-document Ghost B extraction from N small per-doc
Mistral batches (each paying the 5-15 min queue floor) into ONE fat
batch covering every chunk in a multi-file ingest.

---

## What's already shipped

`backend/services/ingestion/preflight_batcher.py` exposes one public
function:

```python
async def run_preflight_extraction_batch(
    *, db, qdrant_client, docs, corpus_config, pool, schema_ctx, model,
) -> dict[str, list[ExtractionResult]]
```

It does the parse → chunk → single-batch dispatch → per-doc routing.
It does NOT write to Mongo — that's the caller's job, on purpose, so the
existing `_write_mongo_all` stays the single source of truth for the
mongo phase.

---

## What needs to be wired in

### 1. `batch_queue.py` — opt-in dispatch path

Add a new field to the batch document (Mongo collection
`ingestion_batches`):

```python
extraction_dispatch: Literal["per_doc", "preflight"] = "per_doc"
```

In `_run_batch`, branch on this field BEFORE spawning workers:

```python
if batch.extraction_dispatch == "preflight":
    await self._run_preflight_phase(batch_id)
# Then proceed to existing _run_batch_worker pool — workers will hit
# the resume gates and skip Ghost B.
```

`_run_preflight_phase` would:

1. Update batch status: `current_phase = "preflight_extracting"`
2. Load all doc records for this batch (with file_ids, content_bytes
   from spool)
3. Call `run_preflight_extraction_batch(...)` with the aggregated docs
4. For each doc that returned results:
   - Call `_write_mongo_all` with the staged ghost_b output
   - Flip `mongo_written = True` on the doc's WriteState
5. Update batch status: `current_phase = "running"` (handoff to worker
   pool)

**Failure mode:** if Mistral batch fails, mark batch as
`failed` with `error="Mistral batch <job_id> failed: <reason>"`. Docs
that were parsed but not staged stay queued for retry — next ingest
runs the standard per-doc path.

### 2. `IngestionConfig` — already has the fields

`extraction_batch_mode` and `summary_batch_mode` already exist (Phase
25 foundation). No schema changes needed for the corpus side.

### 3. Frontend — batch upload toggle

Currently the UI toggles batch mode at the **corpus** level. For
pre-flight we need a toggle at the **batch** level (the user might
want preflight for some uploads but not others).

Option A — auto-detect:
- If `extraction_batch_mode == "mistral"` AND batch has ≥ 50 files →
  pre-flight automatically
- < 50 files → per-doc (avoids the queue-floor-vs-aggregation tradeoff)

Option B — explicit checkbox on the upload modal:
- "Use Mistral pre-flight (one batch for all files, ~50% off, ~30 min
  wall clock)"

I'd recommend Option A — fewer knobs, sensible default, the threshold
encodes the actual economics. Let the user override via Option B if
they want.

### 4. SSE progress

`GET /api/ingestion/jobs/{doc_id}/stream` infers stage from
`write_state` flags. With pre-flight, individual docs sit in
`ingesting` until the whole batch's pre-flight completes. We need a
batch-level SSE stream:

`GET /api/ingestion/batches/{batch_id}/stream`

Events: `preflight_parsing` (with X/Y), `preflight_submitted` (job_id),
`preflight_polling` (X/Y rows complete), `preflight_done`,
`workers_running`. The existing per-doc stream still works once docs
hit the worker phase.

### 5. Tests

- Unit: `test_preflight_batcher.py` — mock parse/chunk/Mistral, verify
  task collection and per-doc routing
- Integration: end-to-end test with 3-5 small fixture files + a stubbed
  Mistral client returning canned extraction
- Failure path: Mistral returns partial → docs with results stage,
  others retry on next run

---

## Risks & open questions

1. **Memory pressure during PHASE 1A.** Parsing 500 PDFs in a row
   keeps all chunks in RAM until the batch submits. Worst case:
   500 files × ~50KB text × 30 chunks = ~750 MB. Manageable but worth
   capping. Suggest streaming chunks to a temp Mongo collection if
   total task count > N.

2. **Schema lens drops.** Per-doc schema lens (per-corpus retrieval
   guidance for Ghost B) is meaningless across docs in pre-flight. The
   shipped orchestrator passes `schema_lens=None`. This is a quality
   regression on extraction guidance for corpora that rely on lens —
   but only for batch ingest. Per-doc dispatch keeps the lens.

3. **Resume after backend restart.** If the backend dies mid-pre-flight,
   the Mistral batch is still running on their side. We'd want to
   persist the `job_id` in the batch document so a restarted backend
   can poll and resume rather than re-submitting.

4. **Cost of failed batches.** Mistral charges for completed rows even
   in a failed batch. Worth surfacing the partial cost in the failure
   message.

---

## Decision points for review

Before wiring this in:

- [ ] Approve auto-detect (≥ 50 files) vs explicit checkbox
- [ ] Approve adding `job_id` persistence on the batch doc (for restart
  resume)
- [ ] Approve dropping schema_lens for pre-flight ingests (or implement
  per-doc lens carry-through)
- [ ] Approve the chunk-streaming-to-Mongo escape hatch above N tasks,
  and what N should be (default proposal: 5000)

Once these are confirmed, full wiring is ~400 lines:
- batch_queue: ~150 lines (`_run_preflight_phase` + state machine)
- router: ~50 lines (batch SSE stream)
- frontend: ~100 lines (auto-detect logic + progress UI)
- tests: ~100 lines

Estimated implementation: one focused session.
