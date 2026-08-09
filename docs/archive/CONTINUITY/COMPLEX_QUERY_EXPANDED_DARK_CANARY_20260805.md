# Expanded Dark Canary — Comparison Ledger + Production-Gap Delta

**Date:** 2026-08-05  
**Owner ruling:** Expand dark (option 2). User-visible CQ **NOT_AUTHORIZED**.

## Categorical state

```yaml
dark_shadow_fixture_run: PASSED
expanded_dark_canary: PASSED   # shadow retrieval suite n=50
user_visible_canary: NOT_AUTHORIZED
global_planner: DISABLED
ontology_activation: PROHIBITED
schema_backfill: PROHIBITED
orphan_reextract: PROHIBITED
graph_migration: PROHIBITED
```

## Scope certified

| Dimension | Authorized max | Used | Notes |
|---|---|---|---|
| Corpora | 3 | **2** | Only two corpora had certified `graph_assertion` + `entity_ready` + `assertion_ready` |
| Users | 3 | **1** | Only Sambenja exists in this deploy; synthesis preflighted LongCat OK |
| Queries | 50 target / 100 hard max | **50** | Exact distribution executed |
| Concurrency | 1 | 1 | Sequential |

Corpora:
- `gsem-e2e-20260804a` — fixture; `production_visible=false`, `excluded_from_user_search=true`
- `6a766597-29f3-4a3e-8918-5de10f0053b3` (`q9_10_file_inspection`) — certified graph_assertion

## MEASURED acceptance

```yaml
n: 50
acceptance.pass: true
citation_resolution_rate: 1.0
child_support_rate: 1.0
unsupported_selected_paths: 0
false_transitive_inferences: 0
ambiguous_identity_merges: 0
silent_hybrid_fallbacks: 0
unexplained_empty_graph_paths: 0
production_answer_mutations: 0
ranking_mutations: 0
OOM_events: 0
graph_relationship_gain_positive_rate: 1.0   # 36/36 relationship-class rows
nonrelationship_irrelevant_graph_addition_rate: 0.0  # 0/3 (paths=0, neo4j_rt=0)
retrieval_p95_ms: 9760.13   # ≤10000
neo4j_round_trips_breaches: 0
hydration_batches_breaches: 0
reranker_calls_breaches: 0
rollback_count: 0
synthesis_preflight: ok  # pool:provider-readiness-longcat-1
```

Distribution executed (exact):
`direct_one_hop:8`, `two_hop_dependency:8`, `cross_domain_translation:8`,
`contradiction:6`, `temporal_latest:6`, `ambiguous_identity:4`,
`unsupported_transitivity:4`, `graph_fact_block:3`,
`nonrelationship_negative_control:3`.

Contract held: `ranking_mutated=false` on all probes; baseline path authoritative;
dark path shadow-only.

## Production-gap delta (still blocks user-visible CQ)

Shadow success does **not** prove visible behavior. Still false / unmet:

```yaml
visible_canary_prerequisites:
  CQ_winners_reach_finalists: false
  graph_added_chunks_reach_finalists: false
  ranking_mutated_inside_candidate_scope: false
  ranking_mutated_outside_candidate_scope: false   # also false — never mutates
  canonical_context_packet_count: not_1_on_synthesis_path
  context_packet_consumed_by_synthesis: false
  verification_controls_final_answer: false
  graph_downgrade_states_are_explicit: partial_via_dark_ledger
  synthesis_provider_preflight: true
  duplicate_wave1_searches: not_reproven_this_run
```

Open Graphify gaps unchanged for promotion: **G02, G03, G04, G10**.

Required wire before any visible canary:

```text
CQ obligation winners → candidate pool → one reranker → protect/MMR
→ finalists → canonical ContextPacket → synthesis → answer verification
```

## Artifacts

- `data_eval/complex_query/expanded_dark_canary_report.json`
- `data_eval/complex_query/expanded_dark_canary_comparisons.jsonl`
- `data_eval/complex_query/expanded_dark_production_gap_delta.json`

## HARD STOP

Awaiting separate owner decision on **user-visible canary**. Do not auto-promote.
Global planner, ontology, schema backfill, orphan re-extract, and graph migration
remain prohibited.
