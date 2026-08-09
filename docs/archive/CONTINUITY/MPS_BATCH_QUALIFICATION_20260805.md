# MPS Batch Qualification — Relex sidecar (2026-08-05)

**Verdict: DO NOT RAISE `RELEX_SIDECAR_BATCH_SIZE` above 1.** Batched MPS inference is
**not output-deterministic** against the frozen batch-1 contract, and the measured
throughput gain is **zero**. Both findings are MEASURED on the live model + gold set,
not projected.

## What was built (and kept)

- `scripts/apple_ml_services/relex_extract_svc/main.py`
  - `_row_from_batch(prepared, batch, model_output, decoded_entities, decoded_relations, i)`
    — the per-item decode extracted from the old `_predict_one`, batch-position `i`
    parametrized. Single source of truth for decode; batched and unbatched cannot drift.
  - `_predict_many(texts)` — ONE prepare/collate/run/decode forward per window,
    per-item decode via `_row_from_batch`, input-order zip, empty-text short-circuit.
  - `_predict_one(text)` = `_predict_many([text])[0]` (unchanged contract; still the
    startup readiness probe).
  - `_extract_locked` chunks `tasks` into windows of `RELEX_SIDECAR_BATCH_SIZE`
    (new env, **default 1**), window-granular failure isolation, `RT.lock` kept.
  - Offline import smoke: OK. Real-model smoke: batched decisions == unbatched decisions
    on ad-hoc texts (entity/relation sets equal); only sub-threshold confidence differs.

- `backend/scripts/regen_relex_benchmark_batched.py` — gold-scoring mirror of
  `_predict_many`; writes `relex_large_v3_mps_fp32.batch{N}.predictions.jsonl`.
- `backend/scripts/qualify_relex_batching.py` — regen each size, diff vs frozen batch-1,
  score via `goldscore.run_all --apply-type-constraints`, enforce parity gates.

## Measured throughput (MPS, gliner-relex-large, 21 gold chunks, median 520 chars)

| batch | total_s | s/sample | parity gates |
|-------|---------|----------|--------------|
| 1 (frozen) | 15.23 | 0.725 | reference |
| 4  | 16.40 | 0.781 | span/ep/pred/lane = 1.0 |
| 8  | 15.02 | 0.715 | span/ep/pred/lane = 1.0 |
| 16 | 15.36 | 0.731 | span/ep/pred/lane = 1.0 |

**No speedup.** GLiNER encode on MPS is per-token bound, not launch-bound, on long-form
chunks. Production child chunks match the gold shape (median 544 chars, 94% ≥300 chars,
measured on corpus 0189427c), so no gain is projected at any size. Batch-8 was nominally
fastest but within noise of batch-1.

## The blocker: raw-confidence non-determinism

Diffing batch-N raw `scores` (sigmoid over all 28 labels per pair) vs frozen batch-1:

- max confidence delta = **8.56e-01** (NOT float jitter)
- deltas > 1e-4: **23,934** label-scores
- **threshold flips across the 0.30 decision boundary: 99** (identical for 4/8/16)
- examples (batch-1 → batch-8): `synonym of` 0.581→0.027; `part of` 0.134→0.950;
  `part of` 0.093→0.543; `detects` 0.267→0.783

These are large-magnitude sign changes across the relation-acceptance threshold caused by
**padding-length interaction in the batched MPS forward** (window max-length padding
shifts logits for shorter members). This is a known MPS batched-inference behavior, not
a bug in `_row_from_batch` — the decode path is per-item and correct.

## Why the parity gates "passed" anyway

The goldscore **gold-standard decision metrics are identical** batch-1 vs batch-8
(strict AND canonical): `correct_predictions`, `gold_match_f1`, `endpoint_coverage`,
`complete_triple_recall`, `accepted lane` all byte-equal. The frozen gold set's accepted
relations sit comfortably above 0.30, so the 99 flips land on sub-threshold / rejected
pairs and do not move gold precision/recall. My harness's coarse span/endpoint/predicate
gates (which ignore sub-threshold confidence) therefore returned 1.0.

**But the frozen-generation contract is not "gold F1 parity" — it is output
determinism.** `raw_pair_scores` feeds downstream confidence-ranked consumers
(`relex_adapter.build_relation_evidence`, acceptance lanes, the frozen artifact
`relex_large_v3_mps_fp32.predictions.jsonl`). 99 cross-threshold flips = re-running the
same corpus with batch>1 yields a *different* extraction than the frozen batch-1 lane on
real data near the threshold. That is exactly what the sidecar header forbids:
> "Generation parameters are FROZEN … batch size 1 … Device: MPS FP32 (frozen
> determinism contract)."

## Decision

- `RELEX_SIDECAR_BATCH_SIZE` stays **1** (default). The env knob and `_predict_many`
  remain in code (default-off) so a future owner can re-qualify if (a) chunk lengths
  become short enough for batching to actually help, or (b) a padding-invariant forward
  (e.g. sort-by-length bucketing with per-length windows, or CPU-fp64 verification)
  restores determinism.
- Not deployed. No plist change, no launchctl kickstart, runtime copy untouched.
- Extraction speed must come from elsewhere: Phase 2 (Neo4j ~78s/doc fixed overhead,
  the dominant non-extraction cost) and Phase 3 (pass overlap) — both larger measured
  wins than the zero measured here.

## Receipts

- `data_eval/ingestion_speed/relex_batch_comparison.json` — per-size gates + rationale
- `data_eval/ingestion_speed/gold_decision_parity.json` — lane parity per size
- `data/deterministic_gold_score/data/relex_large_v3_mps_fp32.batch{4,8,16}.predictions.jsonl`
  (qualification artifacts, NOT the frozen lane)

Quality gates held: no production corpus touched, frozen batch-1 artifact untouched,
no threshold reduction, no fabricated parity — the 99-flip regression is reported, not
smoothed over.
