# Complex Query — Candidate Adoption Closeout (DARK / Allowlist)

**Date:** 2026-08-05  
**Owner ruling:** Option 2 — production-integration work AUTHORIZED behind candidate-adoption flags.  
**User-visible CQ:** **NOT_AUTHORIZED**  
**HARD STOP:** after this closeout.

## Authority state

```yaml
expanded_dark_shadow_canary: PASSED
candidate_adoption_integration:
  status: COMPLETED_DARK
  visibility: DARK
  scope: ALLOWLIST_ONLY
  acceptance.pass: true
user_visible_complex_query_canary:
  status: NOT_AUTHORIZED
global_subquery_planner:
  status: DISABLED
production_ontology_activation: PROHIBITED
production_schema_backfill: PROHIBITED
orphan_reextraction: PROHIBITED
production_graph_migration: PROHIBITED
```

## Scope

```yaml
enabled_globally: false
fixture_or_candidate_allowlist_only: true
planner_global_enable: false
production_default_ranking: unchanged   # user-visible chunks = baseline
user_visible_answer_replacement: false
authorized_corpora:
  - gsem-e2e-20260804a
  - 6a766597-29f3-4a3e-8918-5de10f0053b3   # q9_10_file_inspection
authorized_users:
  - Sambenja   # 6a132beafef900c17f87848e
flags:
  COMPLEX_QUERY_RUNTIME_ENABLED: true_on_allowlist
  COMPLEX_QUERY_RANKING_ADOPTION_ENABLED: true_on_allowlist
  COMPLEX_QUERY_SYNTHESIS_PACKET_ENABLED: true_on_allowlist
  COMPLEX_QUERY_FINAL_VERIFICATION_ENABLED: true_on_allowlist
  COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED: false
```

## What shipped (control path)

```text
shared retrieval lanes
→ obligation-level CQ fusion (allowlist)
→ graph-supported child candidates
→ unified candidate pool
→ dedupe → ≤1 rerank → protect ≤4 → obligation/domain MMR
→ candidate finalists (DARK)
→ ContextPacketV1
→ dark synthesis (not returned to user)
→ AnswerVerificationV1 on generated answer
```

Baseline finalists + baseline answer remain user-visible. Candidate path mutates
ranking **only inside allowlist scope** and stores comparison / packet / verify
ledgers.

## Synthesis provider (MEASURED)

Dark synthesis originally failed on LongCat-1 (402 quota). Owner supplied a
DeepSeek platform key; stored encrypted in `settings.api_keys.deepseek` for
credential owner + Sambenja.

```yaml
synthesis_route:
  pool_entry: deepseek-api__deepseek-v4-flash
  model: deepseek/deepseek-v4-flash
  api_base: https://api.deepseek.com/v1
  smoke: LLM_OK ("OK")
  dark_synthesis_success_count: 25
  returned_to_user: false
```

Suite default: `CQ_SYNTHESIS_POOL_ENTRY=deepseek-api__deepseek-v4-flash`.

## MEASURED acceptance (n=50 retrieve + ≥25 dark synth)

Host: Kings-Mac-Studio.local · container `polymath_v33-backend-1`

```yaml
acceptance.pass: true
cq_winners_reach_finalists_rate: 0.98
graph_added_final_evidence_rate_on_relationship_queries: 1.0
context_packet_consumed_by_synthesis: true
final_generated_answer_verified: true   # status in {pass,revise} on ≥40% rows
ranking_mutated_inside_candidate_scope: true   # 48/50
ranking_mutated_outside_candidate_scope: false
user_visible_answer_mutations: 0
global_planner_enabled: false
false_transitive_inferences: 0
ambiguous_identity_merges: 0
unsupported_selected_paths: 0
unsupported_final_claims: 0   # gate counts verification_status==block only
silent_graph_to_hybrid_downgrades: 0
unexplained_empty_graph_paths: 0
duplicate_root_embeddings: 0
duplicate_wave1_searches: 0
hydration_batches_max: 1
reranker_calls_max: 1
neo4j_round_trips_max: 2
OOM_events: 0
candidate_retrieval_p95_ms: 1188.15   # CQ-stage; ≤10000
candidate_retrieval_wall_p95_ms: 15477.1   # full retrieve_planned wall (reported separately)
rollback_count: 0
```

Dark synthesis class quota (exact):
`direct_one_hop:5`, `two_hop_dependency:5`, `cross_domain_translation:5`,
`contradiction:3`, `temporal_latest:3`, `ambiguous_identity:2`,
`unsupported_transitivity:2` → **25/25**.

Verification status mix (DeepSeek Flash answers):
`pass:5`, `revise:20`, `block:0`.  
Claim rates on generated answers: supported_claim_rate **0.7451**
(152/204); residual unsupported sentence claims exist under `revise`
(not counted as `unsupported_final_claims` by the block-only gate).

## Quality delta (for visible-canary decision — not a pass claim)

```yaml
quality_delta:
  obligation_coverage_improved_count: 0
  obligation_coverage_regressed_count: 6
  candidate_added_relevant_evidence_count: 1
  candidate_added_irrelevant_evidence_count: 0
  baseline_supported_claim_rate: null   # baseline answer not dark-synthesized
  candidate_supported_claim_rate: 0.7451
  baseline_citation_resolution_rate: 1.0
  candidate_citation_resolution_rate: 1.0
  contradiction_disclosure_gain: 0
  temporal_correctness_gain: null
```

**Verdict for next owner decision:** integration wiring is proven dark and
allowlist-isolated. Quality evidence does **not** yet show material improvement
on relationship-heavy questions (0 coverage improvements, 6 regressions,
1 graph-added finalist on relationship set). Do **not** authorize user-visible
CQ from this closeout alone.

## Graphify gap closure (vs PRODUCTION_GAP report)

| Gap | Status after candidate adoption |
|---|---|
| G02 CQ winners → finalists | **Closed dark** — candidate finalists; user chunks stay baseline |
| G03 ContextPacket / verify → synthesis | **Closed dark** — packet → dark synth → post-answer verify |
| G04 silent Graph→Hybrid | Explicit `graph_execution_status` on comparisons (no silent hybrid labels) |
| G10 dual packet shapes | Compatibility adapter → `ContextPacketV1` for candidate path |
| User-visible answer replacement | **Still prohibited** |

## Artifacts

```text
CONTINUITY/COMPLEX_QUERY_CANDIDATE_ADOPTION_CLOSEOUT_20260805.md
data_eval/complex_query/candidate_adoption_report.json
data_eval/complex_query/candidate_adoption_comparisons.jsonl
data_eval/complex_query/candidate_context_packets.jsonl
data_eval/complex_query/candidate_answer_verification.jsonl
data_eval/complex_query/candidate_restart_replay.json
data_eval/complex_query/candidate_performance.json
data_eval/complex_query/candidate_acceptance_matrix.json
```

## HARD STOP

```yaml
stop: true
authorized_next: owner_review_only
not_authorized:
  - user_visible_complex_query_canary
  - COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=true
  - production_migration
  - ontology_activation
  - schema_backfill
  - orphan_reextraction
```

Next decision should weigh whether CQ-selected evidence improves real finalists
and synthesized answers enough to justify a **still-bounded** visible canary —
not whether the shadow graph path can find good chunks.
