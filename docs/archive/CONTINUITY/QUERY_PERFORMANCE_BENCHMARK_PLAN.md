# Query Performance Routes and Benchmark Plan

**Audit date:** 2026-08-02 · Stage 8 of the repository-grounded audit.
**Rule:** no unverified latency claims. Every number below that is not a
proposed LIMIT is a quantity to be MEASURED by the benchmark defined at the end.

---

## Route 1 — FAST (exact facts, entities, relations, timelines)

Characteristics: no planning LLM, no broad corpus fan-out, no reranker
escalation beyond bounded graph lookups.

Current functions that execute in this route:

```
polymath_mcp/tools.py::polymath_search_entities        entity lookup (lexicon)
polymath_mcp/tools.py::polymath_get_entity_relations   relation lookup (Neo4j)
polymath_mcp/tools.py::polymath_graph_query            parameterized graph reads
services/retriever/fact_retrieval.py                   exact fact retrieval
services/retriever/temporal.py::detect_temporal_intent exact-window queries
services/retriever/tier0_router.py (single-corpus, single-lane mode)
services/retriever/vocabulary.py (exact_terms path only, no ANN fan-out)
```

Proposed hard limits (FAST):

```
corpora:              1        (entity/fact lookups are corpus-scoped)
query lanes:          1        (no decomposition)
document candidates:  8        (routing cards only)
child chunks:         16
graph hops:           1        (neighbor facts only)
reranker inputs:      0–16     (skip rerank below 4 candidates)
second passes:        0
model calls:          0        (no planner LLM, no synthesis LLM in pure FAST)
tool calls:           2        (lookup + optional hydrate)
```

---

## Route 2 — CURATED (selected cross-domain corpora, one synthesis stage)

Characteristics: parallel summary-first retrieval, evidence obligations,
single synthesis. This is the default `retrieve_planned` path.

Current functions that execute in this route:

```
chat_orchestrator.process_chat_request
  _build_chat_query_plan → query_plan.build_query_plan_v2 (QueryPlanV2)
  _build_librarian_plan_trace → librarian_planner (rule path; LLM decomposer
                                 only under configured escalation)
RetrieverOrchestrator.retrieve_planned:
  temporal.detect_temporal_intent
  CorpusVocabularyResolver.resolve (+ vocabulary_cache)
  grounded_vocabulary_lanes (optional translation lanes)
  tier0_router.route_lanes → four_lane_router.route_lanes
  summary_tree_navigator.navigate
  funnel_a (summaries) ‖ funnel_b (children) ‖ lexical (Mongo BM25) ‖
  document_anchor (title anchors) ‖ _retrieve_graph_seed_facts (Neo4j, gated)
  evidence_plan / evidence_allocation (two-lane seats)
  planned_fusion.fuse_planned_pools (+ reservation_policy)
  reranker_service (cross-encoder, ONCE on full pool)
  [shadow] curation.py when RETRIEVAL_CURATION_V4_ENABLED
  cross_domain levers · query_grounding guard
  hydrate.py → assembly.py → RetrievalResult
chat_orchestrator:
  _build_retrieval_answerability_gate
  synthesis prompt contract → llm_service (ONE synthesis call)
```

Proposed hard limits (CURATED):

```
corpora:              existing request cap (ChatRequest.corpus_ids, currently 32)
                      — propose ROUTE cap 8 for curated latency budget
query lanes:          7        (query_plan max_lanes=7 today; keep)
                      + 3 optional vocabulary translation lanes (existing caps)
document candidates:  20 per corpus round-robin (existing funnel contract)
child chunks:         20 per corpus (existing FUNNEL B contract)
graph hops:           1        (Mode A expansion only when graph tier enabled)
reranker inputs:      rerank_cap as computed by _planned_rerank_candidate_limit
                      (existing; propose absolute ceiling 128, already coded)
second passes:        1        (_repair_lane / _missing_concept_support_query
                                exist today; keep at exactly 1)
model calls:          2 max    (1 optional decomposer escalation + 1 synthesis)
tool calls:           n/a      (route executes in-process)
```

---

## Route 3 — RESEARCH (multi-hop, temporal, contradiction, external)

Characteristics: second-pass gap recovery, stronger synthesis, optional web
evidence. Material exists but the route is not yet a first-class classifier.

Current functions that execute (or seed) this route:

```
services/research/orchestrator.py + planner.py + worker.py + evidence.py
                                   bounded lane orchestration for research
chat_orchestrator web evidence:    web_query_builder / web_cache /
                                   _score_web_evidence_chunk /
                                   _classify_web_evidence_sufficiency
RetrieverOrchestrator second pass: _repair_lane (_repair_dense /
                                   _repair_summary / _repair_lexical)
                                   _repair_cross_corpus_missing_concepts
gap analysis:                      gap_profile.py (graph synthesis frame)
                                   _missing_concept_support_query
multi-hop:                         mode_a.py / mode_b.py graph expansion
```

Proposed hard limits (RESEARCH):

```
corpora:              same request cap; propose ROUTE cap 16
query lanes:          7 core + 3 vocabulary + 2 research lanes (bounded)
document candidates:  24 per corpus
child chunks:         24 per corpus
graph hops:           2        (multi-hop ceiling; measured fan-out explosion
                                is the known risk — cap + timeout, not hopes)
reranker inputs:      192 ceiling (2 passes)
second passes:        2 max    (gap recovery + one contradiction re-check)
model calls:          4 max    (decomposer + synthesis + stronger synthesis
                                + claim audit if evidence_verification on)
tool calls:           web evidence bounded by existing _cap_web_sources_for_turn
```

Route classification itself must be deterministic (intent_policy extension),
never an LLM decision — consistent with POLYMATH_ARCHITECTURE §0.2.

---

## Benchmark required to measure (no latency claims until it runs)

### Environment
- Docker backend stack at release-pinned versions (Slice 1 release_pins).
- Representative corpus set: at least 3 corpora spanning different domains,
  including the 360k-row lexicon workload the vocabulary cache was measured
  against (that 1.8–18.1s figure is the ONLY recorded baseline and is
  pre-retrieval only).

### Workload
1. **Route classifier probe set:** 60 queries, 20 per route, deterministic
   labels (exact-entity / curated-cross-domain / multi-hop-temporal).
2. **Frozen ablation gold queries:** reuse `data_eval/query_plan_v2_*`
   acceptance sets for regression comparability.
3. Each query executed 5× after warm-up; report p50/p95/max.

### Metrics recorded per query (route-stamped)
```
total wall time                     e2e from entrypoint to final token
pre-retrieval time                  plan + vocabulary + routing (timings dict
                                    already emits these stages)
funnel gather time                  existing per-funnel timings
rerank time                         existing rerank timing block
hydration time                      existing timings["hydrate"]
synthesis first-token / last-token  SSE event deltas
candidate counts                    pool size, rerank inputs, finalists
store usage                         qdrant/mongo/neo4j flags + call counts
cache behavior                      vocabulary_cache hit/miss + epoch
second-pass occurrence rate         repair lane activation frequency
per-route budget violation count    any hard limit exceeded → FAIL the run
```

### Acceptance for the benchmark itself
- Script lives in `backend/scripts/` beside existing probe scripts
  (`scripts_probe_*.py` convention) and writes a JSON artifact to `data_eval/`.
- Deterministic: same pins + same corpus ⇒ same route assignment and same
  candidate counts (timing may vary).
- Produces the per-route latency table that this document refuses to invent.

### Open questions the benchmark must answer before route caps are finalized
1. Does vocabulary resolution dominate CURATED wall time on cache miss?
   (determines whether Slice 2 IR gate overhead is measurable at all)
2. Does graph hop=2 in RESEARCH blow the candidate budget on dense corpora?
3. Is the reranker or the funnel gather the p95 bottleneck per route?
4. Does the shadow curation v4 path change finalist counts enough to need
   its own per-route limits?
