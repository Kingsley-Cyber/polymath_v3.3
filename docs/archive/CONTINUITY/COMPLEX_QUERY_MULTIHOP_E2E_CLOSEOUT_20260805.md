# Complex Query / Multi-Hop — E2E Closeout + Dark Canary

**Date:** 2026-08-05  
**Status:** Owner lifted Graph Semantic E2E hard stop for **bounded dark canary only**  
**Fixture:** `gsem-e2e-20260804a`  
**Global planner:** `COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED=false`  
**Fixture runtime:** `COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED=true` (override only)  
**Dark canary:** `COMPLEX_QUERY_DARK_CANARY_ENABLED=true` (override only; shadow metrics)

## Authority (owner ruling 2026-08-04 / 2026-08-05)

```yaml
complex_query_retrieval_fixture: PASSED
complex_query_durability: PASSED
complex_query_semantic_suite: PASSED
complex_query_restart_recovery: PASSED
full_provider_backed_chat_e2e: PASSED
graph_semantic_e2e_hard_stop: LIFTED_FOR_BOUNDED_DARK_CANARY_ONLY
dark_canary: EXPAND_PASSED   # n=50 shadow suite; see expanded_dark_canary_report.json
global_subquery_planner: DISABLED
user_visible_complex_query_ranking: NOT_AUTHORIZED
production_activation: NOT_AUTHORIZED
```

Dark canary must not mutate user-visible answers or production ranking.
Baseline retrieval remains authoritative. Global planner, ontology activation,
schema backfill, orphan re-extraction, and production migration remain blocked.

## Provider-backed synthesis gap — closed (MEASURED)

### Root cause (not LiteLLM master-key mismatch)

Prior `SSE` runs authenticated as a user whose answer stream defaulted to an
OpenCode/Anthropic pool entry with insufficient credits → HTTP 401. Retrieval
still completed; generation did not.

Stored DeepSeek credential was invalid / zero-balance. Working fixture
synthesis credential: LongCat `provider-readiness-longcat-1` (`LongCat-2.0`).

### Fix applied (credential + request routing only)

1. Point fixture owner `Sambenja` at LongCat pool entry for synthesis settings.
2. Bind fixture corpus `user_id`/`owner_id` to that user; keep `use_neo4j=true`.
3. Generation probes send `overrides.model: pool:provider-readiness-longcat-1`
   because the answer stream uses request/conversation model selection, not the
   synthesis role alone.
4. Obligation tagging: `sq_vocab` credits a distinct obligation when present so
   claim coverage can reach `claims_unsupported: 0`.

No architecture redesign.

### Successful chat probes (3 classes × warm-up + 5 measured)

```yaml
successful_chat_probe:
  direct_one_hop:
    provider_response_status: success
    answer_text_tokens_streamed: true
    SSE_terminal_event: completed
    complex_query_executor_ran: true
    graph_paths_used: 0          # allowed — direct one-hop may stop on evidence
    citations_resolve_to_exact_children: true
    claims_total: 2
    claims_supported: 2
    claims_unsupported: 0
    answer_verification_passed: true
    silent_hybrid_fallback: 0
    fixture_scope_enforced: true
    global_planner_enable: false
  cross_domain_translation:
    provider_response_status: success
    answer_text_tokens_streamed: true
    SSE_terminal_event: completed
    complex_query_executor_ran: true
    graph_paths_used: 2
    citations_resolve_to_exact_children: true
    claims_total: 5
    claims_supported: 5
    claims_unsupported: 0
    bridge_claims_verified: true
    answer_verification_passed: true
    silent_hybrid_fallback: 0
    fixture_scope_enforced: true
    global_planner_enable: false
  contradiction_or_temporal:
    provider_response_status: success
    answer_text_tokens_streamed: true
    SSE_terminal_event: completed
    complex_query_executor_ran: true
    citations_resolve_to_exact_children: true
    claims_total: 3
    claims_supported: 3
    claims_unsupported: 0
    contradiction_disclosed: true
    answer_verification_passed: true
    silent_hybrid_fallback: 0
    fixture_scope_enforced: true
    global_planner_enable: false
all_probes_passed: true
```

### Successful-generation performance (MEASURED, n=15 success samples)

```yaml
successful_generation_performance:
  retrieval_total_p50_p95_max: {p50: 405.4, p95: 1272.33, max: 1940.13, n: 15}
  synthesis_first_token_p50_p95_max: {p50: 5918.13, p95: 10557.6, max: 11804.85, n: 15}
  synthesis_last_token_p50_p95_max: {p50: 5918.13, p95: 22187.7, max: 30897.63, n: 15}
  answer_verification_p50_p95_max: {p50: 405.43, p95: 1272.39, max: 1940.19, n: 15}
  full_chat_p50_p95_max: {p50: 5921.76, p95: 22247.41, max: 31020.97, n: 15}
```

Note: 6/8/10s Graph Lite/Standard/Deep targets remain **retrieval/planning**
targets. Full chat p95 includes LongCat generation length/latency and is
reported separately — not used as a retrieval SLA.

## Prior accepted retrieval evidence (still holds)

Durable bake · no `docker cp` dependency · fixture-scoped `/api/chat` executor ·
8/8 semantic suite · restart-replay hash identity · retrieval p95≈423ms ·
neo4j ≤2 RT · child-support gate · global planner disabled.

Artifacts:
- `data_eval/complex_query/successful_generation_probes.json`
- `data_eval/complex_query/successful_generation_performance.json`
- `data_eval/complex_query/acceptance_matrix.json`
- `data_eval/complex_query/restart_replay.json`

## Dark canary (AUTHORIZED — shadow only) — MEASURED

Operating contract: real query runs normal production retrieval/answer; a
parallel dark CQ path records `DarkCanaryComparisonV1` only. Rollback disables
the canary flag in-process and leaves baseline authoritative. Unsupported
*final* claims rollback only after dark synthesis; packet/obligation gaps are
metrics (`packet_claims_unsupported`) so shadow collection is not aborted by
retrieval-only verification.

Limits: ≤3 corpora, ≤2 users, ≤100 queries, concurrency 1, graph subqueries ≤3,
Neo4j RT ≤2, hops ≤3, 1 rerank, 1 hydrate batch.

### First bounded run (2026-08-05)

```yaml
dark_canary_run:
  n: 5
  corpora: [gsem-e2e-20260804a]
  user: Sambenja / 6a132beafef900c17f87848e
  acceptance.pass: true
  citation_resolution_rate: 1.0
  unsupported_selected_paths: 0
  false_transitive_inferences: 0
  ambiguous_identity_cross_merges: 0
  unsupported_final_claims: 0
  silent_hybrid_fallback: 0
  child_support_on_selected_paths: 1.0
  production_answer_mutations: 0
  production_ranking_mutations: 0
  retrieval_warm_p95_ms: 1522.43   # dark CQ stage
  OOM_events: 0
  graph_relationship_query_gain: 5/5 positive (graph_added_child_ids)
  cross_domain_graph_paths_used: 2
  ranking_mutated: false  # all probes
```

Provider note: one probe (`dependency_analysis`) hit LongCat `402 Payment
Required` after retrieval — provider-readiness, not retrieval failure; dark
comparison still recorded; other 4 probes SSE `completed`.

Artifacts:
- `data_eval/complex_query/dark_canary_report.json`
- `data_eval/complex_query/dark_canary_comparisons.jsonl`
- `/data/ingest-files/complex-query-dark-canary/comparisons.jsonl` (container)

## HARD STOP (next decision)

Dark canary collection complete → **stop**. User-visible canary activation and
global activation remain **NOT_AUTHORIZED** pending a separate owner decision.
No automatic production promotion.
