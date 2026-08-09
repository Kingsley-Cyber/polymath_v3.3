# Graph Authority Repair — CLOSEOUT (2026-08-04)

**Directive:** `CONTINUITY/GRAPH_AUTHORITY_REPAIR_DIRECTIVE_20260804.md`  
**Status:** Phase-1 repair COMPLETE on q9 canary — HARD PAUSE for further ontology control-plane work  
**Qualified Fact gate:** NOT weakened (`fact_nodes=0` allowed)

---

## Critical measurement correction

Earlier `Entity=0` was a **query bug**: Entity nodes are global (no `corpus_id`).  
Correct count via corpus Chunk MENTIONS:

| Metric | MEASURED (q9) |
|---|---|
| Document | 8 |
| Chunk | 160 |
| Entity (via MENTIONS) | **2310** |
| MENTIONS edges | 3155 |
| RELATES_TO edges | 1230 |
| RelationAssertion (new) | **36** |
| Fact | **0** |

Extraction value was **not** fully dead — entities/relations were already in Neo4j via MENTIONS/RELATES_TO. The broken seams were: (1) Graph gate treated Fact as the only authority, (2) readiness/job honesty, (3) no first-class RelationAssertion projection.

---

## What shipped

1. **GraphProjectionIR** — `models/graph_projection_ir.py`  
2. **Assertion projector** — `services/graph/assertion_projector.py` (36 assertions from Relex relations)  
3. **Capability-aware Graph gate** — `graph_authority.inspect_graph_capabilities`  
   - `graph_assertion` when entities/relations present  
   - Fact-only block only when `CROSS_DOMAIN_GRAPH_REQUIRE_QUALIFIED_FACTS=true`  
   - Never silent Hybrid when no capability  
4. **Stuck job honesty** — 2583 `stuck_orphan` (no ghost_b row); covered jobs left promoted  
5. **Corpus `graph_capabilities` certificate** stamped on q9 corpus  
6. Backend image baked

Defaults: `CROSS_DOMAIN_GRAPH_REQUIRE_QUALIFIED_FACTS=false` (assertion plane valid Graph).

---

## Acceptance (MEASURED)

```yaml
extraction:
  jobs_pending: 0
  jobs_leased: 0
  jobs_stuck: 2583   # explicitly recorded as stuck_orphan
neo4j:
  Entity_via_mentions: 2310
  RelationAssertion: 36
  Chunk: 160
  Document: 8
  Fact: 0
facts:
  canonical_Fact_zero_allowed: true
  fact_gate_weakened: false
graph_query:
  graph_ran_not_blocked: true
  graph_capability: graph_assertion
  relation_assertions_available: 36
  graph_added_evidence_beyond_Hybrid: true  # 6 ids on Benesh/movement query
  silent_Hybrid_fallback: 0
readiness:
  capability_vector_exposed: true
  advertised_mode: graph_assertion
```

Artifacts:
- `data_eval/q9_final/assertion_projection.json`
- `data_eval/q9_final/graph_assertion_retrieval_probe.json`

---

## Still open (next owner-authorized slices)

- Full lane-specific `graph_projection_jobs` state machine + remove any remaining direct worker Neo4j write paths  
- Ontology proposal/release control plane  
- Schema concept → corpus entity ID → Neo4j entity join at query time  
- `extraction_terminal: true` certificate (stuck orphans need explicit policy: re-extract vs abandon)  
- Frontend disclosure of `graph_capability` in UI  

**STOP** on architecture expansion until owner prioritizes the next slice.
