# Knowledge Pipeline Lineage Map — Phase 0 (2026-08-04/05)

**Directive:** `polymath_graph_semantic_e2e_directive` v1.0  
**Rule:** recovery / remapping — do not rebuild extraction, graph, ontology, summary, or retrieval architecture unless a component is absent.  
**Production:** migration / schema backfill / ontology activation / re-extraction = **false**

Machine-readable companions:

- `data_eval/knowledge_e2e/producer_consumer_lineage.json`
- `data_eval/knowledge_e2e/control_plane_drift_matrix.json`

## BLUF

Most of the deterministic stack **exists and works**: `relex_local`, shared spaCy via `get_shared_nlp`, FrameExtractor / DependencyMatcher / SVO, corroboration gate, `ghost_b_extractions`, graph projection jobs, Neo4j assertion plane, `deterministic_summary.v1`, QueryIR / retrieve_planned, cross-domain modules (default off).

The critical drift is **caller disconnection after the gate**: alias identity, CandidateExtractionArtifact, a unified KnowledgeArtifactBundle, schema→entity joins, and a Mongo ontology proposal SM are not on the live ingest call graph. Neo4j still has a **duplicate write entry** (worker/backfill → `write_document_graph` direct).

## Intended call graph

```
Document
→ parse/chunk
→ get_shared_nlp (1 Doc/chunk)
→ relex_local (1 encode/chunk)
→ FrameExtractor + DependencyMatcher + native SVO (same Doc)
→ corroboration_gate (= arbitration)
→ ExtractionResult / CandidateExtractionArtifact
→ KnowledgeArtifactBundleV1 (post-gate family)
→ alias_candidates → alias_gate → document_identity → corpus_identity
→ SummaryInformationRecord → parent/section/document summaries
→ graph_projection_jobs (structural | entity | assertion | fact | schema_entity_links)
→ neo4j_writer / assertion_projector (materializers only)
→ lexicon/schemas + SchemaEntityJoin
→ retrieve_planned: direct vector ∥ vocabulary vector ∥ graph ∥ summary routing
→ weighted RRF → protect ≤4 → cross-domain MMR (10–18)
→ grounded chat / artifacts
```

## Drifted call graph (live today)

```
Document
→ parse/chunk
→ relex_local ✓
→ stash ghost_b_extractions ✓
→ worker.write_document_graph (direct) ⚠
→ plan/advance graph_projection_jobs (ledger after write) ✓ partial
→ assertion_projector from ghost relations ✓
→ deterministic_summary from parent text (weak post-gate) ⚠
→ lexicon/schemas without durable alias clusters ⚠
→ retrieve_planned + graph_assertion capability ✓
→ alias_retrieval_shadow only (ranking off) ✓ shadow
✖ alias_candidates/gate/clustering not called from worker
✖ CandidateExtractionArtifact not persisted on live path
✖ claim_assembler absent (facts=[] honest)
✖ SchemaEntityJoin absent
✖ ontology_proposals Mongo SM absent (file release_registry only)
```

## Name remaps (not missing)

| Directive name | Existing equivalent |
|---|---|
| `arbitration_gate` | `corroboration_gate.evaluate_relation` + `relation_acceptance.yaml` |
| `native_SVO` | `svo_candidates` via `syntax_lane` |
| `hierarchical_aggregator` | `summary_tree` + `deterministic_summary` |
| `summary_planner` | `summary_jobs` / `plan_summary_jobs` |
| `ghost_b_local` | retired; live engine `relex_local` |
| post-gate bundle | closest: `CandidateExtractionArtifact` + `ExtractionResult` |

## Post-gate artifact authority (finding)

**Authority today:** Mongo `ghost_b_extractions` rows shaped like `ExtractionResult`, with `local_extraction` stamps (engine, hashes, gate counts).  
**Observation-only contract:** `CandidateExtractionArtifact` — adapted in tests, not on worker.  
**Phase 1 decision:** implement `KnowledgeArtifactBundleV1` as a **compatibility view/adapter** over those rows (+ optional alias decisions), not a second extractor.

## Drift classes (counts)

| Class | Count (major seams) |
|---|---:|
| still_correct | 11 components |
| caller_disconnected | 5 (alias*, CandidateArtifact, …) |
| wrong_input_artifact | 2 (summary, lexicon) |
| duplicate_authority | 2 (neo4j entry, graph_promotion) |
| contract_version_drift | 3 (bundle, ontology CP, readiness flags) |
| dead_code | 3 (ghost_b_local, claim_assembler, schema join) |

## q9 holds (do not re-extract)

- 2583 `REVIEW_MISSING_EXTRACTION`, `bounded_reextract_limit=0`
- Dry-run reconciliation only in later phases; **fresh fixture** proves e2e
- 2 structural `BLOCKED_INPUT_MISSING` on q9 (Document 8/10) — leave; fixture must fully certify

## Phase order (binding)

0. ✅ Lineage (this document)  
1. ✅ Knowledge bundle recovery + alias reconnect (fixture)  
2. ✅ Summary reconnection  
3. ✅ Ontology proposal CP (fixture pin)  
4. ✅ Schema–entity join  
5. ✅ Graph projection extension / writer convergence  
6. ✅ Capability certification  
7. ✅ Retrieval integration (fixture allowlist)  
8. ✅ Chat + HTML artifact  
9. ✅ Restart replay  
10. ✅ Closeout → **HARD STOP** (`CONTINUITY/GRAPH_SEMANTIC_E2E_CLOSEOUT_20260805.md`)

## Prohibitions honored

No production corpus mutation, schema backfill, ontology activation, orphan re-extract, global ranking enable, or collection migration in Phase 0.
