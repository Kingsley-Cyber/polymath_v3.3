# Owner Manual Ingestion Runbook — 2026-07-17

Status: SENIOR DRAFT — section 3's exact command is EXECUTOR-FILL; Codex
completes and live-verifies it in the READY receipt before first owner use.
Everything else below is receipted fact from the certified E2E program.

## 1. What this pathway is

The validated main ingestion pathway (owner-designated): your files →
chunking → RunPod extraction (GLiNER/spaCy, locked custom image by
immutable digest, deterministic runtime) → local MLX embedding → Qdrant +
Mongo + Neo4j writes (corpus-qualified composite identities) → API
summaries. Certified by the 15-book E2E: 15/15 verified, 595/595 RunPod
jobs, five production bugs fixed with permanent invariants.

## 2. Before you start (preconditions)

- No live eval running (evals and ingestion fight over the one Metal GPU).
  Check: `ps aux | grep run_two_lane` (and siblings) — must be empty; the
  channel's last entry should show the eval lock released.
- Backend healthy: `curl -s localhost:8000/api/health` → 200.
- Sidecars healthy: `curl -s localhost:8082/health` (embedder),
  `:8081/health` (reranker), `:8084/health` (Ghost B) — all status ok.
- RunPod green endpoints live (Codex verifies in the READY receipt; if a
  worker probe fails, ingestion still queues — jobs wait, nothing is lost).

## 3. The command (EXECUTOR-FILL — exact invocation + one worked example)

> Codex: fill with the precise driver invocation for "ingest folder X as
> new corpus named Y through runpod_local_extraction.v1", including where
> files may live (any readable folder), supported types (.pdf/.md/.epub as
> certified), and the flags for summaries on/off. Verify by dry-run and
> cite the verification in the READY receipt.

## 4. Cost and time expectations (measured, per book)

- Extraction: ~$0.04–0.07 (RunPod, rate-model), ~2–4 min active at
  batch 32–64 on the green fleet.
- Summaries: ~$0.23 average (API lane; provider-usage-accounted).
- All-in E2E measurement: 15 books = $4.46.

## 5. Watching progress

- Ingest jobs are durable and journaled; re-attach any time.
- Extraction fleet truth = Mongo settings endpoints[].enabled (not env).
- Chat lane must stay usable during ingestion, but expect slower embeds
  while extraction embeds batch (the GPU arbiter fixes this after deploy).

## 6. Safety guarantees (engraved invariants — why re-running is safe)

- Never-write-less resume: a resume can never erase information already
  in the durable store.
- Verified-duplicate skip: only documents with write_state.verified=true
  are skipped as duplicates; incomplete ingests resume, not skip.
- Composite (corpus_id, content_id) identity: a new ingest can never
  steal or overwrite another corpus's documents, chunks, or facts.
- Bounded graph transactions (100-row) — no OOM partial-graph states.
- Fail-closed refusals name their guard; noise is excluded at mention
  granularity, never by failing a whole document.
→ Practical meaning: if anything stops mid-run, RUN THE SAME COMMAND
AGAIN. It resumes exactly where it left off.

## 7. Do NOT do while a batch is running

- No live evals, no backend rebuilds/recreates, no sidecar restarts.
- Don't toggle retrieval flags mid-batch (flag flips happen in their own
  verified windows).

## 8. Current retrieval flag state you're ingesting into

relationship allocation ON (verified) · corpus_scope.v2 refusal ON
(verified) · temporal OFF (exonerated; flips on owner word) · claims OFF
(proven; flips on owner word) · router/waterfall/two-lane dark.
