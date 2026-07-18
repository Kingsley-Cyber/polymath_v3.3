# Owner Results — Final Acceptance v1

## State of record — sole authorized rerun

**VERIFIED RED / technically unsealable. The pipeline is not COMPLETE.**

The one authorized rerun completed all 23 executions and all five
retrieval-only repeats on the repaired measurement harness and corrected
frozen F5 probe. It returned `EXIT=2`, with `sealed=false` and `seal=null`.
No second rerun was attempted.

The RED rollback is complete and verified. The live backend is healthy on
image `sha256:a82cf85c081b6a8c56b20be9ec172d200df6a61284d4ebe88aa768449b723857`.
Relationship allocation, corpus scope v2, temporal routing, claim anchors,
and chat-cost telemetry remain ON. Corpus scope v3, Librarian planning,
LLM decomposition/refinement, synthesis override, two-lane anchoring, the
four-lane router, and waterfall assembly are OFF. Eval mounts are absent and
the host lock is released.

### Rerun identity

| Field | Verified value |
|---|---|
| Started | `2026-07-18T07:20:01.904593Z` |
| Completed | `2026-07-18T07:24:40.482996Z` |
| Wall clock | `278.578s` |
| Query count | `23` |
| Concurrency / temperature | `3 / 0` |
| Query manifest SHA | `a130175f341596baeca8b53a288fde4890f1e1e31c5f83e43f8c4d20a3d6807b` |
| Exact query-surface SHA | `2a746275f9a224a7b916dbee34dfd44d424c23fb2e69230f82870d1c3861debc` |
| Parent acceptance-spec SHA | `99f2c37bbc22ded15135afa0f113f41e1faa0dc4346f77f74c752cb4d6905c4e` |
| Raw journal SHA | `135d7b0586a978d27aaea574622cc9f1ca751bc61a063cb9c01e99148c67d79d` |
| Raw log SHA | `d37df7ecacd52f125b73d3dd31907d164e125d9aa3586306826816ff2f446ee5` |
| Runner exit | `2` |
| Journal state | `sealed=false`, `seal=null` |

### Authorized claim materialization

The E2E claim surface was deterministically completed before the rerun:

- two provider-free exports were byte-identical at `381,243,197` bytes,
  SHA `c3be59bcccc7c3c67237ffb236198af012719d61352a5ec379abb3ea5170804d`;
- both source-lineage manifests were byte-identical at `12,833,836` bytes,
  SHA `a6106ccc38a20f2eaae828b71c94d48efa3388da972d2ed40c1e9067ea4f4a45`;
- the single import changed the E2E corpus from `0` to `18,790` rows,
  inserted `18,790`, reused `0`, and read back all `18,790` as valid;
- all `152,803` stored claim bodies were preserved, including `2,653`
  typed claims, `147` links, and `79,247` exact evidence sentences;
- global rows are `22,283`; the Mark corpus remains `3,493`; unsafe
  canonical-or-missing rows are `0`; protected Mongo, Qdrant, and Neo4j
  census values are exactly unchanged;
- the empty backup SHA is `e3b0c442…` and the exact planned-ID rollback
  manifest SHA is `be8847e5…`.

A provider-free q7 micro-proof attached and rendered one additive anchor
against a selected Murch child. In the final run, claim attachment worked
more broadly—51 anchors attached and 25 rendered—but q7's actual selected
Murch chunk had no query-overlapping anchor and its `ranked_criteria` lane
remained uncovered. The join is working; q7 still fails at evidence
selection/coverage.

### Acceptance gates

| Gate | Target | Rerun result | Verdict |
|---|---:|---:|---|
| Depth bridge, probes 1–2 | at least 1/2 | 1/2 | PASS |
| Graph hop, probes 3–4 | at least 1/2 | 0/2 | **FAIL** |
| Multi-hop probe 5 | green | red | **FAIL** |
| Claims/chunk/temporal consumption, probes 7–10 | 4/4 | 1/4 | **FAIL** |
| Floors, probes 11–16 | 6/6 | 0/6 | **FAIL** |
| Named positives, probes 17–18 | 2/2 | 0/2 | **FAIL** |
| Negative refusal state, probes 19–23 | 5/5 | 5/5 | PASS |
| Negative named-guard proof | 5/5 | 4/5 | **FAIL** |
| Corpus/citation membership | 100% | 100% | PASS |
| Associative profile consumed | true | true | PASS |
| Technical + journal completeness | 23/23 | 21/23 | **FAIL** |
| Cost ledger | CLOSED, zero unmetered | OPEN, 2 unmetered | **FAIL** |
| Deep latency p50 | at most 15s | 49.066s | **FAIL** |
| Fast latency p50 | at most 5s | 13.386s | **FAIL** |
| Plan-hash determinism, probes 1–5 | 5/5 | 4/5 | **FAIL** |
| Final-seat determinism, probes 1–5 | 5/5 | 3/5 | **FAIL** |
| Bounded refinement improves a D1/D6 gap | at least one | 0 | **FAIL** |
| Zero refinement firings on probes 13–16 | 4/4 | telemetry incomplete | **FAIL** |

### Per-query rerun receipt

`State OK` uses the repaired canonical `classification.refused` contract.
`Proof` is the preregistered schema-consumption proof.

| # | Query ID | State | State OK | Proof | Tech | Seconds |
|---:|---|---|---|---|---|---:|
| 1 | `d1a_anticipation_editing_tension` | ANSWERED | yes | pass | pass | 65.710 |
| 2 | `d1b_guiding_eye_drawing_cinematography` | GATE_BLOCKED | no | fail | fail | 70.407 |
| 3 | `d2a_facs_character_animation` | ANSWERED | yes | fail | pass | 60.553 |
| 4 | `d2b_laban_stage_combat` | ANSWERED | yes | fail | pass | 71.570 |
| 5 | `d6a_story_directing_cinematography_vfx` | GATE_BLOCKED | no | fail | pass | 61.306 |
| 6 | `d6b_manga_editing_pacing` | ANSWERED | yes | pass | pass | 65.562 |
| 7 | `d3a_murch_rule_of_six` | GATE_BLOCKED | no | fail | pass | 37.578 |
| 8 | `d3b_ves_pipeline_stages` | GATE_BLOCKED | no | fail | pass | 31.715 |
| 9 | `d5a_film_digital_transition` | ANSWERED | yes | fail | pass | 32.041 |
| 10 | `temporal_nicole_2006` | GATE_BLOCKED | no | pass | pass | 28.470 |
| 11 | `relationship_fight_camera_direction` | GATE_BLOCKED | no | fail | pass | 27.657 |
| 12 | `relationship_shoot_edit_emotion` | GATE_BLOCKED | no | fail | fail | 24.280 |
| 13 | `direct_facs` | GATE_BLOCKED | no | fail | pass | 13.520 |
| 14 | `direct_anatomy_masses` | GATE_BLOCKED | no | fail | pass | 8.841 |
| 15 | `lay_dynamic_figure` | GATE_BLOCKED | no | fail | pass | 8.754 |
| 16 | `lay_natural_cut` | GATE_BLOCKED | no | fail | pass | 8.790 |
| 17 | `author_named_walter_murch` | GATE_BLOCKED | no | fail | pass | 13.386 |
| 18 | `named_source_animators_survival_kit` | GATE_BLOCKED | no | fail | pass | 8.666 |
| 19 | `negv2_f2_oscar_2026` | GATE_BLOCKED | yes | pass | pass | 8.830 |
| 20 | `negv2_f3_deakins` | GATE_BLOCKED | yes | pass | pass | 30.462 |
| 21 | `negv2_f3_visual_story` | GATE_BLOCKED | yes | pass | pass | 30.391 |
| 22 | `negv2_f5_figure_34_1` | GATE_BLOCKED | yes | pass | pass | 26.111 |
| 23 | `negv2_f6_llc_guess` | GATE_BLOCKED | yes | fail | pass | 14.666 |

### What the rerun proves

1. **VERIFIED — the complete dark stack over-refuses positive questions.**
   Only 5/18 positive probes answered. Every direct, lay, relationship-floor,
   and named-positive probe from 11–18 was blocked by the unchanged strict
   sufficiency layer or missing required plan atoms. No gate was weakened.

2. **VERIFIED — claim materialization and the additive join work, but q7
   still misses at selection.** The run attached 51 anchors and rendered 25
   without changing source identity/evidence. Q7 seated a Murch chunk, but
   not one whose claim surface covered `ranked_criteria`, so it attached zero.

3. **VERIFIED — D1, D2, and the corrected F5 probe now behave correctly.**
   Probes 19–22 refused with the named v3 guards
   `temporal_out_of_range`, `named_source_absent`, and `artifact_absent`.
   Probe 23 also refused and stripped the bait, but v3 did not apply a named
   blocking guard, leaving the five-proof refusal gate RED.

4. **VERIFIED — route identity is fixed, but timeout accounting remains
   open.** Every observed helper uses the registered
   `deepseek/deepseek-v4-flash` identity. Two `complete_sync` calls timed out
   without usage receipts, leaving the total cost unknown. The known subtotal
   is `$0.00623476` for 40,202 input and 2,166 output tokens.

5. **VERIFIED — latency and determinism remain promotion blockers.** Deep
   p50 is 49.066s (3.27× its ceiling) and fast p50 is 13.386s (2.68×).
   Path-identical repeats prove plan hashes 4/5 and final seats 3/5, so the
   previous harness ambiguity is resolved and the product gate is RED.

### Promotion disposition

| Feature | Disposition |
|---|---|
| Relationship allocation | Keep ON — previously frozen-green |
| Corpus scope v2 | Keep ON — previously frozen-green |
| Temporal routing | Keep ON — previously promoted |
| Claim anchors + materialized join surface | Keep ON — additive path verified |
| Chat-cost telemetry | Keep ON — exposes the remaining timeout gap |
| Corpus scope v3 | Keep OFF as part of the failed complete stack |
| Librarian planner/decomposer/refinement | Keep OFF |
| Direct synthesis override | Keep OFF |
| Two-lane T / four-lane router / waterfall | Keep OFF |

No further acceptance run is authorized. The durable rerun, rollback, and
materialization receipts are in
`docs/baselines/final_acceptance_v1_20260718/`.

# Historical first attempt (superseded by the rerun above)

## Original decision

**VERIFIED RED. The pipeline is not COMPLETE and none of the final-review
features are promoted.**

The preregistered 23-query window ran once at concurrency 3 and temperature
0 against the direct DeepSeek V4 Flash candidate. It recorded all 23
executions and all five retrieval-only repeats, but only 13 executions were
journal-complete. The runner therefore returned `EXIT=2`, left
`sealed=false`, and produced no canonical seal.

The atomic rollback is complete. The live backend is healthy on image
`sha256:0b7f61f2f9ae9d85452b99af99a194abc470f7093d50d7b43d4efa4d60898ef1`.
Relationship allocation, corpus scope v2, temporal routing, claim anchors,
and chat-cost telemetry remain ON. Corpus scope v3, Librarian planning and
refinement, synthesis override, and two-lane anchoring are OFF. T remains
OFF after its separately sealed RED verdict.

## Window identity

| Field | Verified value |
|---|---|
| Started | `2026-07-18T05:17:41.829783Z` |
| Completed | `2026-07-18T05:22:27.611703Z` |
| Wall clock | `285.782s` |
| Query count | `23` |
| Concurrency / temperature | `3 / 0` |
| Query manifest SHA | `abdc68bb937c2c47c88eeafe918e19cbb462cf44ca9c2ec56e5ae351a6d8eac5` |
| Exact selection SHA | `ae0e7cedfe3cfb5eb9cc41361962fb7b85113eeb6defffceb51f1b43e085b24d` |
| Raw journal SHA | `6a28dc04d30bcda00d472550736d58fb4e22c846ca8dcc9d51c596b95e94bc46` |
| Raw log SHA | `e8ebb901463dc8744a921acd0dbc5974c79132595ad824484b84c66b64967913` |
| Runner exit | `2` |
| Journal state | `sealed=false`, `seal=null` |

The durable compressed journal, log, deployment receipts, and rollback
receipts are in
`docs/baselines/final_acceptance_v1_20260718/`.

## Acceptance gates

| Gate | Target | Result | Verdict |
|---|---:|---:|---|
| Depth bridge, probes 1–2 | at least 1/2 | 1/2 | PASS |
| Graph hop, probes 3–4 | at least 1/2 | 0/2 | **FAIL** |
| Multi-hop probe 5 | green | red | **FAIL** |
| Claims/chunk/temporal consumption, probes 7–10 | 4/4 | 1/4 | **FAIL** |
| Floors, probes 11–16 | 6/6 | 5/6 schema proofs; 4 OPEN ledgers | **FAIL** |
| Named positives, probes 17–18 | 2/2 | 2/2 state/proof; probe 17 ledger OPEN | PASS quality / **FAIL technical** |
| Negative refusal set, probes 19–23 | 5/5 refused | runner state-match 0/5; actual refused boolean 3/5 | **FAIL** |
| Corpus/citation membership | 100% | 100% | PASS |
| Associative profile consumed | true | true | PASS |
| Technical + journal completeness | 23/23 | 13/23 | **FAIL** |
| Cost ledger | CLOSED, zero unmetered | OPEN, 11 unmetered | **FAIL** |
| Deep latency p50 | at most 15s | 39.152s | **FAIL** |
| Fast latency p50 | at most 5s | 7.933s | **FAIL** |
| Plan-hash determinism, probes 1–5 | 5/5 | 0/5 recorded; repeat path mismatch | **FAIL / UNVERIFIED** |
| Final-seat determinism, probes 1–5 | 5/5 | 0/5 recorded; path-confounded | **FAIL / UNVERIFIED** |
| Bounded refinement improves a D1/D6 gap | at least one | 0 | **FAIL** |
| Zero refinement firings on probes 13–16 | 4/4 | telemetry absent/null | **FAIL** |
| Owner synthesis quality eyeball | owner decision | pending | PENDING |

## Per-query receipt

`Proof` is the preregistered schema-consumption proof, not a subjective
answer score. `Tech` requires a closed call ledger and a complete trace.

| # | Query ID | State | Runner state OK | Proof | Tech | Seconds |
|---:|---|---|---|---|---|---:|
| 1 | `d1a_anticipation_editing_tension` | ANSWERED | yes | pass | pass | 75.051 |
| 2 | `d1b_guiding_eye_drawing_cinematography` | GATE_BLOCKED | no | fail | fail | 67.113 |
| 3 | `d2a_facs_character_animation` | ANSWERED | yes | fail | pass | 76.157 |
| 4 | `d2b_laban_stage_combat` | ANSWERED | yes | fail | pass | 58.924 |
| 5 | `d6a_story_directing_cinematography_vfx` | GATE_BLOCKED | no | fail | pass | 45.646 |
| 6 | `d6b_manga_editing_pacing` | ANSWERED | yes | pass | pass | 55.523 |
| 7 | `d3a_murch_rule_of_six` | GATE_BLOCKED | no | fail | pass | 25.289 |
| 8 | `d3b_ves_pipeline_stages` | ANSWERED | yes | fail | pass | 27.965 |
| 9 | `d5a_film_digital_transition` | ANSWERED | yes | fail | pass | 26.093 |
| 10 | `temporal_nicole_2006` | ANSWERED | yes | pass | pass | 32.658 |
| 11 | `relationship_fight_camera_direction` | ANSWERED | yes | fail | pass | 30.831 |
| 12 | `relationship_shoot_edit_emotion` | ANSWERED | yes | pass | pass | 27.997 |
| 13 | `direct_facs` | ANSWERED | yes | pass | fail | 14.896 |
| 14 | `direct_anatomy_masses` | ANSWERED | yes | pass | fail | 14.316 |
| 15 | `lay_dynamic_figure` | ANSWERED | yes | pass | fail | 12.988 |
| 16 | `lay_natural_cut` | ANSWERED | yes | pass | fail | 13.130 |
| 17 | `author_named_walter_murch` | ANSWERED | yes | pass | fail | 7.933 |
| 18 | `named_source_animators_survival_kit` | ANSWERED | yes | pass | pass | 8.078 |
| 19 | `negv2_f2_oscar_2026` | ANSWERED | no | fail | fail | 7.200 |
| 20 | `negv2_f3_deakins` | GATE_BLOCKED | no | pass | fail | 5.185 |
| 21 | `negv2_f3_visual_story` | GATE_BLOCKED | no | pass | fail | 6.450 |
| 22 | `negv2_f5_figure_9_4` | ANSWERED | no | fail | pass | 3.717 |
| 23 | `negv2_f6_llc_guess` | GATE_BLOCKED | no | fail | fail | 5.650 |

The refusal gate is RED, but its recorded `0/5` is partly a harness defect.
The canonical v2 classifier emits `gate_blocked`, `model_voiced_refusal`, or
`answered` and separately provides a boolean `refused`; it never emits the
literal state `refused`. The final runner nevertheless compares every
negative state to that nonexistent literal. The product evidence is still
RED: probes 20, 21, and 23 have `refused=true`, while probes 19 and 22 were
answered. Probe 23 also stripped the bait but the scorer looked for that
proof in the wrong reason-code field.

Probe 17 used the senior's active post-T amendment: T stayed OFF after its
RED verdict, and the author/title anchor was proven through the Librarian
grounded-shortlist path. It was not evidence that the two-lane T mechanism
had recovered.

## Latency

| Tier | Count | p50 | p95 | Gate | Verdict |
|---|---:|---:|---:|---:|---|
| Deep | 12 | 39.152s | 75.051s | p50 at most 15s | **FAIL** |
| Fast | 11 | 7.933s | 14.316s | p50 at most 5s | **FAIL** |

The retrieval diagnostic's own `total_s` separates core retrieval from the
rest of the chat path:

| Retrieval tier | Core retrieval p50 | Core retrieval max |
|---|---:|---:|
| Fast | 1.815s | 3.375s |
| Deep | 26.481s | 55.516s |

| Recorded retrieval stage | p50 | max |
|---|---:|---:|
| Candidate generation | 0.490s | 1.574s |
| Document routing | 0.015s | 0.281s |
| Embed | 0.278s | 30.009s |
| Graph | 4.535s | 12.005s |
| Hydrate finalists | 0.013s | 0.163s |
| Identity dedupe | 0.004s | 0.532s |
| Librarian refinement | 1.509s | 1.618s |
| Repair | 0.039s | 0.815s |
| Rerank | 0.026s | 20.101s |
| Summary-tree routing | 0.016s | 0.142s |
| Temporal metadata | 0.762s | 0.762s |
| Vocabulary resolution | 1.163s | 32.355s |

The direct DeepSeek synthesis calls were not the only paid-call surface.
Librarian decomposition/refinement made `complete_sync` calls before the
streaming synthesis call on part of the set. Five of those calls timed out,
and the fast probes with this extra call clustered around 13–15 seconds.
Probes 4–11 then recorded the planner as `enabled_degraded`,
`behavior_applied=false`, with a named `TimeoutError` fallback. Probe 12
returned to active planning and passed its relationship-seat proof.

## Cost

The cost meter's CLOSED rows were independently recomputed with `Decimal`
using the journal's registered prices of `$0.14/M` uncached input tokens and
`$0.28/M` output tokens.

| Component | Tokens | Cost |
|---|---:|---:|
| Metered input | 88,329 | `$0.01236606` |
| Metered output | 4,699 | `$0.00131572` |
| Exact known subtotal | — | **`$0.01368178`** |

All 17 metered call rows match the stored arithmetic exactly. The run also
contains 11 unmetered calls:

- Five `ReadTimeout` calls have no usage receipt.
- Six calls contain 8,883 input and 465 output tokens but use the normalized
  identity `openai/deepseek-v4-flash`, which does not exactly match the
  registered price-route identity `deepseek/deepseek-v4-flash`.

Therefore `$0.01368178` is a lower-bound known subtotal, not the total cost.
The total remains **UNKNOWN** by contract. The repository registry cites the
[official DeepSeek pricing page](https://api-docs.deepseek.com/quick_start/pricing?article_id=article_1779470751466_8).

## Five-finding ledger

1. **UNVERIFIED / GATE RED — final-stack determinism was not measured
   path-identically.** The full run enabled the LLM decomposer, but the repeat
   harness forcibly disabled it and returned `repeat_plan_hash=null` for all
   five probes. All five repeat seat sets differed, which is a real
   observation but is confounded by the different path. The harness must be
   fixed before attributing this result to product nondeterminism. T's
   separate path-identical RED remains valid.

2. **VERIFIED — stored metadata is reachable but not reliably consumed.**
   Associative shortlist profiles were consumed, and one temporal proof
   passed. Graph-contributed evidence, entity-bridge seating, claims,
   list/table hydration, the Brown temporal preference, and one historical
   relationship seat proof did not reach the final selected context.

3. **VERIFIED — corpus scope v3 still has product defects, and the final
   scorer also has a taxonomy defect.** Two negatives were answered. Probe 19
   missed its out-of-range case because the corpus maximum year was 2099;
   probe 22 falsely found two Figure 9.4 artifact matches. Probe 2 also
   treated a generic plural phrase as a missing named source. Separately, the
   scorer's nonexistent literal `refused` target mislabels three actual
   refusals.

4. **VERIFIED — the LLM planning call has a route/accounting split.** The
   synthesis stream used the registered `deepseek/deepseek-v4-flash` route,
   while planning calls recorded `openai/deepseek-v4-flash`. That exact-model
   mismatch opened six price rows; five additional planner calls timed out
   without usage.

5. **VERIFIED — the complete stack is materially above both latency
   ceilings.** Deep p50 is 2.61 times its gate and fast p50 is 1.59 times its
   gate. The planner's TimeoutError degradation covered probes 4–11. Failed
   planning calls also left refinement telemetry null on simple probes and
   prevented the required one-gap improvement proof. Refinement fired only on
   negative probes 19 and 23; both ended in a named `ValueError` fallback and
   improved no seating.

## Promotion recommendations

| Feature | Recommendation | Evidence |
|---|---|---|
| Relationship allocation | Keep ON | Previously frozen-green; preserved by rollback |
| Corpus scope v2 | Keep ON | Previously frozen-green; preserved by rollback |
| Temporal routing | Keep ON | Previously promoted; preserved by rollback |
| Claim anchors | Keep ON | Previously promoted; preserved by rollback |
| GPU arbiter Q | Keep deployed | Q1–Q5 green; not implicated by final verdict |
| Two-lane T | Keep OFF | Separate sealed RED: 10% deterministic replay, 80% anchor coverage |
| Corpus scope v3 | Keep OFF | 2/5 negatives answered; temporal/artifact/generic-source defects |
| Librarian planner/refinement | Keep OFF | latency, schema, refinement, and accounting gates failed |
| Direct synthesis override | Keep OFF | total cost OPEN and candidate path was only an acceptance arm |

## One targeted fix round

The evidence supports one bounded round with four ordered corrections:

1. Repair the acceptance measurements first: use the same decomposer flag and
   path for full and repeat retrieval, require a non-null repeat plan hash,
   and score canonical refusal through the classifier's `refused` boolean and
   named guard fields.
2. Normalize every Librarian/decomposer dispatch to the same registered
   provider/model identity as synthesis, meter every attempt, and bound or
   cache the call so a timeout cannot consume the fast tier.
3. Correct only the v3 product failures: generic phrases must not become named
   sources; temporal range must use exact support instead of a poisoned 2099
   maximum; artifact locators must require exact Figure 9.4 evidence; bait
   proof must survive into the named guard trace.
4. Repair only the failed consumption joins: graph-to-final-seat, rendered
   claim anchor, list/table hydration, temporal preference, and relationship
   seat trace.

Only after those build gates are green should the exact frozen acceptance
window be re-authorized. No acceptance threshold, query, prompt, scoring rule,
or corpus data was changed in this run.
