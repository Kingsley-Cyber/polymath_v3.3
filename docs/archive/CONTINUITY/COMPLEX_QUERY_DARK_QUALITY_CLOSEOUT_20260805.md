# Complex Query — Dark Quality Qualification Closeout

**Date:** 2026-08-05  
**Owner ruling:** HOLD user-visible CQ; authorize dark quality-correction slice, then enable Sambenja+q9 only when use_gate passes.  
**HARD STOP on visible enablement:** waiting on DeepSeek key rotation (credential was pasted in chat).

## Authority

```yaml
candidate_adoption_integration: PASSED_DARK
dark_quality_qualification:
  status: PASSED_USE_GATE   # 15 real q9 questions
  visibility: still DARK
user_visible_complex_query:
  status: NOT_ENABLED       # ready after key rotation + flag flip
global_subquery_planner: DISABLED
production_ontology_activation: PROHIBITED
production_schema_backfill: PROHIBITED
orphan_reextraction: PROHIBITED
production_graph_migration: PROHIBITED
```

## What was wrong (six “regressions”)

See `CONTINUITY/COMPLEX_QUERY_COVERAGE_REGRESSIONS_20260805.md`.

```yaml
root_cause: measurement_asymmetry
baseline_obligation_coverage: hardcoded_1.0_if_any_finalists
candidate_obligation_coverage: pre_synthesis_claim_rate
true_baseline_id_displacement: 0
```

Policy fix: **baseline-augment** (preserve baseline finalists; admit graph/CQ only for uncovered obligations or explicit path evidence; coverage guard).

## Fixes shipped

1. Real comparable obligation coverage + baseline-first augmenter  
2. Revise → regenerate → citation salvage → pass|block; baseline fallback on failed candidate  
3. Paired DeepSeek Flash synthesis (same prompt/temp/max_tokens)  
4. Wired `COMPLEX_QUERY_RERANK_CANDIDATE_MAX` (override **12**) — rerank dominated wall time  
5. CQ winner hydration injection into the single hydrate batch  
6. Visible-answer flags present but **false** until rotation + enablement

## MEASURED use_gate (15 real q9 questions)

Host: Kings-Mac-Studio · `polymath_v33-backend-1`  
Corpus: `6a766597-29f3-4a3e-8918-5de10f0053b3` · User: Sambenja  
Model: `pool:deepseek-api__deepseek-v4-flash` @ `https://api.deepseek.com/v1`

```yaml
use_gate:
  worse_than_baseline: 0
  unsupported_final_claims: 0
  citation_resolution_ok: true
  obligation_coverage_regressions: 0
  obligation_coverage_improvements: 1
  graph_gain_on_relationship_queries: 1
  graph_paths_executed_on_relationship_queries: 1
  full_retrieval_warm_p95_ms: 9216.92   # ≤10000
  n: 15
  pass: true
judgment: {better: 1, equal: 14, worse: 0}
terminal_candidate_pass: 12
baseline_fallback_used: 3
```

Artifacts:
- `data_eval/complex_query/q9_quality_use_report.json`
- `data_eval/complex_query/q9_quality_use_rows.jsonl`
- `CONTINUITY/COMPLEX_QUERY_COVERAGE_REGRESSIONS_20260805.md`

## Latency diagnosis (MEASURED)

```yaml
dominant_cost: reranker   # previously 4–7s at graph rerank_cap=80
secondary: legacy_graph_lane ~0.8–1.0s
cq_fixture_stage: typically <400ms warm
fix: apply COMPLEX_QUERY_RERANK_CANDIDATE_MAX=12 when CQ executor ran
```

## Visible enablement — READY BUT NOT FLIPPED

```yaml
pending_before_enable:
  - rotate DeepSeek platform key (replace both encrypted settings records)
  - confirm old key fails; smoke new key
  - then set:
      COMPLEX_QUERY_VISIBLE_ANSWER_ENABLED: true
      COMPLEX_QUERY_VISIBLE_CORPUS_ALLOWLIST: 6a766597-29f3-4a3e-8918-5de10f0053b3
      COMPLEX_QUERY_VISIBLE_USER_ALLOWLIST: 6a132beafef900c17f87848e
keep:
  COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED: false
  all_other_users: baseline
  all_other_corpora: baseline
fallback_already_coded:
  verification_fails: baseline
  provider_preflight_fails: baseline
```

## HARD STOP

Do **not** enable user-visible CQ until the rotated key is installed and smoked.  
Do **not** enable global planner / ontology / backfill / orphan reextract / migration.
