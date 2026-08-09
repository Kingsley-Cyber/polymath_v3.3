# Coverage Regression Classification — Candidate Adoption

**Date:** 2026-08-05  
**Source:** `data_eval/complex_query/candidate_adoption_comparisons.jsonl`  
**Authority:** dark quality-correction slice (visible CQ still NOT_AUTHORIZED)

## Executive finding

The six reported obligation-coverage regressions were **primarily a measurement
defect**, not six independent ranking failures.

```text
baseline_obligation_coverage = 1.0 if any(baseline_finalist_ids) else 0.0
candidate_obligation_coverage = pre-synthesis claim_support_rate / citation_coverage
```

Baseline was hardcoded to perfect coverage whenever finalists existed.
Candidate used a different quantity (answer-claim heuristics from fixture
verification). Apples-to-oranges guaranteed false “regressions” whenever
candidate claim support was < 1.0.

Measured displaced finalist ids on all six rows: **[]** (empty). The candidate
path was adding or reordering novelty, not stripping baseline ids in those
comparisons.

## Per-row classification (pre-fix)

| query_id (short) | class | base→cand cov | displaced | stage | cause | correction |
|---|---|---|---|---|---|---|
| two_hop_dependency_02 | two_hop | 1.0→0.6667 | [] | synthesis metric / curation order | metric asymmetry; CQ novelty prepended before baseline | real coverage metric + baseline-first augment |
| two_hop_dependency_06 | two_hop | 1.0→0.6667 | [] | same | same on q9 | same |
| two_hop_dependency_08 | two_hop | 1.0→0.6667 | [] | same | same on q9 | same |
| unsupported_transitivity_01 | neg | 1.0→0.6667 | [] | same | CQ winners prepended (`doc_rag`, `doc_negative`, …) without obligation need | admit only path/uncovered fills |
| unsupported_transitivity_02 | neg | 1.0→0.6667 | [] | same | same | same |
| unsupported_transitivity_03 | neg | 1.0→0.6667 | [] | same | q9 CQ winners entered candidate set without displacement | same |

```yaml
CoverageRegressionV1_summary:
  measurement_artifact: 6
  true_baseline_id_displacement: 0
  regression_stage_dominant: metric_asymmetry_plus_cq_prepend_policy
  global_weight_changes: none  # classified before any global reweight
```

## Policy correction applied

```text
Baseline finalists (preserve)
+ qualified graph/CQ fills for uncovered obligations / explicit paths
→ dedupe
→ coverage guard (candidate_cov >= baseline_cov else revert to baseline)
```

Admission requires one of:
`fills_uncovered_obligation` | `provides_explicit_relationship_evidence` (path
kids) | `resolves_contradiction` | `provides_temporal_update`.

## Residual risk after fix

Real regressions can still appear if obligation_results are empty (coverage
falls back to 1.0-if-any-ids) or if path-supported kids crowd the top_k after
baseline. The coverage guard hard-stops shipping lower coverage than baseline.
