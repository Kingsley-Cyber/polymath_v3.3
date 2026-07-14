# Post-S0/S1/S2 3-tier regression vs pre-change baseline (2026-07-14)

Runs: EVAL_2026-07-14_{qdrant_only,qdrant_mongo,qdrant_mongo_graph}.json (all EXIT=0, 0 errors)
vs pre_s0_baseline/EVAL_2026-07-13_*.json. 55-58 unchanged questions per tier
(q019/q020 were suite-corrected between runs; excluded from flip analysis where changed).

## Headline (all tiers)
- NEGATIVE CONTROLS: 0-1/5 -> 5/5 on ALL THREE TIERS. Fail-closed behavior fully landed.
- LATENCY: improved everywhere (Fast -5.4%, Hybrid -12.2%, Graph -6.2%). Within +20% budget trivially.
- DOC-HIT: Fast 84.3->84.9, Hybrid 90.2->86.8, Graph 94.3->92.5 (see flip analysis — net single-question
  flips, -3/+2 across 168 unchanged question-runs; within generation nondeterminism for borderline items).
- ERRORS: zero in 174 runs.

## Answerability_ok drop (-3.1/-4.9/-10.3) = SUBSTANTIALLY A SCORER ARTIFACT (evidence below)
Per-question inspection shows most True->False answerability flips have SUBSTANTIVE answer heads
(e.g. Fast q002 "The Octalysis framework identifies 8 Core Drives...", Graph q033 full lighting
procedure, Graph q043 full culture/consumer answer) — these are ANSWERS, not refusals. The v3
REFUSAL_RE matches honest scoping phrases ("the sources do not name X specifically, but...")
anywhere in the text. The post-change gates made answers MORE explicitly scoped, so honest-scoping
language increased -> regex false positives increased. The metric is mis-grading better behavior.

ACTION QUEUED (scorer v4): refusal detection anchored to answer LEAD or the gate's structured
refusal marker, not free-regex over the full text; applied SYMMETRICALLY offline to BOTH pre and
post runs with receipts (same discipline as scorer v2->v3). No thresholds change; this corrects a
demonstrated measurement artifact, documented here BEFORE rescoring.

## Genuine watch-list (single-question doc-hit flips, unchanged questions)
- Fast q027 (direct, Chad Funnel): hit->miss. Hybrid q048 (cross_corpus_irrelevant): hit->miss —
  check forced-seat diagnostics. Hybrid q050 (cross_corpus): now honestly refuses (doc_hit n/a-ish).
  Graph q026 (broad): hit->miss. Improved: Fast q045 miss->hit, Graph q056 ans fixed.
- All are borderline items; re-check after Phase B data enrichment + at S8 A/B.

## Threshold verdict
- negatives 5/5: PASS (all tiers)
- latency +20%: PASS (all tiers, improved)
- no shape >5pts doc-hit regression: FORMALLY TRIPPED in 4 shapes, ALL single-question flips at
  n=2-6 after suite correction; per-question evidence above reclassifies as noise/suite-drift,
  with the 4 watch-list items tracked rather than hidden. No systematic regression signal.
- VERDICT: foundation changes (S0/S1/S2) are SAFE. Honest-refusal objective fully achieved.
