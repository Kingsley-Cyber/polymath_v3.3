# Complex Query / Multi-Hop Graph RAG — Phase 0 Baseline (2026-08-04/05)

**Directive:** Complex Query and Multi-Hop Graph RAG Implementation Plan  
**Rule:** extend `QueryPlanV2` / `QueryIR` / `retrieve_planned` — **no parallel planner**, no unbounded agent loop, no LLM Cypher.  
**Production:** subquery planner / vocabulary ranking / ontology activation / schema backfill / graph migration / orphan re-extract = **false**

Machine companions:

- `data_eval/complex_query/current_state_inventory.json`
- `data_eval/complex_query/retrieval_executor_matrix.json`

## BLUF

Most fusion/curation seams from the cross-domain directive already exist (weighted RRF, protect≤4, MMR budgets, context packet, graph capability gate). The **missing core** is a **bounded subquery DAG** with obligation-scoped graph beam traversal, bridges/contradictions, per-obligation fusion, and post-answer verification — plus Graph Lite/Standard/Deep compile levels for latency.

Prior closeouts that this extends (do not replace):

- `CONTINUITY/CROSS_DOMAIN_RETRIEVAL_CLOSEOUT_20260804.md`
- `CONTINUITY/GRAPH_SEMANTIC_E2E_CLOSEOUT_20260805.md` (fixture `gsem-e2e-20260804a`)

## Query / subquery lineage (current)

```text
User message (+ conversation)
→ build_query_plan_v2 / librarian overlay (optional)
→ QueryPlanV2 (lanes, concepts, complexity)
→ QueryIR.from_plan (obligations ≈ required lanes; domains/bridges additive)
→ retrieve_planned
    Wave-ish concurrent: dense lanes + vocabulary embed batch
    → planned pool fusion (weighted RRF when curation allowlisted)
    → optional graph fact seed + expansion (Graph tier)
    → one rerank
    → protected anchors + MMR (when CROSS_DOMAIN_CURATION_ENABLED + allowlist)
    → context packet diagnostics
→ chat synthesis
✖ no AnswerVerificationV1
✖ no SubQueryPlanV1 DAG
✖ no GraphTraversalPlanV1 / GraphPathResultV1
```

## Retrieval executor matrix (summary)

| Tier | Direct | Vocab | Lexical | Summary | Graph |
|---|---|---|---|---|---|
| Fast (`qdrant_only`) | yes | optional | no | limited | no |
| Hybrid (`qdrant_mongo`) | yes | yes | yes | yes | no |
| Graph (`qdrant_mongo_graph`) | yes | yes | yes | yes | fact seed + expansion; capability-gated |

Silent Graph→Hybrid under Graph label: **forbidden / gated** (`graph_authority.py`).

## Graph traversal audit (current)

| Property | Status |
|---|---|
| Unrestricted BFS | **false** — hop-frontier expansion in `expand_subgraph` |
| LLM Cypher | **false** |
| Max hops | clamped (`GRAPH_REL_HOP2_*`, safe max in `graph_query`) |
| Beam + path score | **absent** |
| Every hop requires child support reject | **absent** as typed path contract |
| Batched multi-plan Neo4j | **absent** |
| Chat uses obligation-scoped traversal | **false** — global fact seed |

## Configuration inventory (MEASURED defaults)

See `current_state_inventory.json`. Notable:

```yaml
CROSS_DOMAIN_CURATION_ENABLED: false          # allowlist canary only
ALIAS_RETRIEVAL_RANKING_ENABLED: false
FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED: false
CROSS_DOMAIN_RRF_K: 60
CROSS_DOMAIN_RRF_WEIGHT_DIRECT: 1.00          # strongest
CROSS_DOMAIN_PROTECTED_ANCHORS_MAX: 4
CROSS_DOMAIN_FINAL_CHILDREN_MAX: 18
GRAPH_ENTITY_LIMIT: 8
QUERY_PLAN_GRAPH_RERANK_CANDIDATES: 80
```

## Latency architecture (binding for implementation)

Do **not** multiply embed/rerank/hydrate by subquery count.

```yaml
performance_acceptance:
  embedding:
    original_query_calls: 1
    duplicate_embedding_calls: 0
  Neo4j:
    traversal_round_trips: {preferred: 1, maximum: 2}
    unrestricted_BFS: false
    N_plus_1_queries: 0
  reranker:
    calls_per_root_query: 1
    candidate_maximum: 30
  hydration:
    batch_fetch: true
    duplicate_fetches: 0
  warm_latency_targets_ms:
    graph_lite_p95: 6000
    graph_standard_p95: 8000
    graph_deep_p95: 10000
  fallback:
    timeout_silent_Hybrid: false
    timeout_returns_partial_status: true
```

Internal Graph compile levels (one frontend Graph mode):

| Level | When | max_hops | graph_subqueries | rerank candidates |
|---|---|---|---|---|
| Lite | 1–2 entities, direct relation | 1 | 1 | ≤20 |
| Standard | dependency / comparison | 2 | ≤2 | ≤24 |
| Deep | cross-domain / contradiction / causal | ≤3 | ≤3 | ≤30 |

## Gap matrix → phase map

| Gap | Phase |
|---|---|
| RootQueryIR + typed contracts + config | 1 |
| Deterministic decomposition templates | 2 |
| Subquery DAG executor (waves, prune) | 3 |
| Wire Wave-1 lanes as shared assets | 4 |
| Traversal compiler → Cypher templates | 5 |
| Beam + path verify + inference classes | 6 |
| Bridges + contradictions | 7 |
| Per-obligation RRF | 8 |
| Global protect + MMR + budget | 9 (mostly exists; wire to obligations) |
| ContextPacketV1 + AnswerVerificationV1 | 10 |
| Isolated E2E on `gsem-e2e-20260804a` | 11 |
| Restart replay | 12 |
| Closeout STOP | 13 |

## Fixture authority

Isolated validation corpus: **`gsem-e2e-20260804a`**  
(`fixture.excluded_from_user_search=true`, `production_visible=false`)

Add to curation allowlist only for eval scripts; do not enable global curation.

## Phase 0 status

**COMPLETE.** Proceed to Phase 1 contracts + configuration (dark / fixture-gated).
