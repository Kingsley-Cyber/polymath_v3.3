# Owner Results — Final Acceptance v1

## Decision

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
