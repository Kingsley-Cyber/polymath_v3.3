# Graph Semantic E2E Closeout — 2026-08-05

**Directive:** `polymath_graph_semantic_e2e_directive` v1.0  
**Fixture corpus:** `gsem-e2e-20260804a`  
**HARD STOP.** Production migration / ontology activation / orphan re-extract / global ranking remain **prohibited**.

## Acceptance matrix (MEASURED)

| Phase | Result | Receipt |
|---|---|---|
| 0 Lineage | accepted (prior) | `CONTINUITY/KNOWLEDGE_PIPELINE_LINEAGE_MAP_20260804.md` |
| 1 Bundle + alias | **PASS** | `data_eval/knowledge_e2e/phase1_fixture_proof.json` (`phase_1_ok: true`) |
| 2 Summaries | **PASS** | `data_eval/knowledge_e2e/phases_2_6_fixture_report.json` — 8 child / 8 parent / 8 section / 8 document |
| 3 Ontology CP (fixture pin) | **PASS** | `ontology-fixture-gsem-e2e-20260804a-v1` pinned; `production_activation: false` |
| 4 Schema→entity joins | **PASS** | 16 CERTIFIED joins; ambiguous_cross_joins=0 |
| 5 Writer convergence | **PASS** | worker + graph_backfill → `project_document_via_control_plane`; pytest `test_neo4j_write_via_projection_cp.py` green |
| 6 Capability certificate | **PASS** | `advertised_mode=graph_assertion`, `schema_entity_join_ready=true`, Fact=0 honest |
| 7 Retrieval | **PASS** | qdrant_only=7, qdrant_mongo=8, qdrant_mongo_graph=6 chunks |
| 8 Chat/SSE + HTML | **PASS** | SSE answer_len=355, sources=2 from fixture only; HTML 3482 bytes |
| 9 Force-recreate replay | **PASS** | job_id set stable; CERTIFIED=19 / NOOP=13 both runs |
| 10 Closeout | **STOP** | this document |

Machine report: `data_eval/knowledge_e2e/phases_7_10_fixture_report.json` (`all_ok: true`).

## Phase 1 gate (binding)

```yaml
phase_1:
  shared_spacy_parse_per_chunk: 1
  relex_pass_per_chunk: 1
  bundles:
    created_for_every_fixture_chunk: true
    accepted_assertions_preserved: true
    source_offsets_preserved: true
    evidence_text_preserved: true
  aliases:
    candidate_generation_reachable: true
    gate_reachable: true
    document_clustering_reachable: true
    corpus_clustering_shadow_reachable: true
    ambiguous_acronym_cross_merges: 0
    descriptions_as_aliases: 0
    semantic_similarity_identity_merges: 0
  production:
    production_alias_writes: 0
    production_schema_writes: 0
    global_ranking_activation: false
```

## Architecture locked

```
worker / graph_backfill
→ project_document_via_control_plane
→ plan graph_projection_jobs
→ write_document_graph (materializer only)
→ run_projection_jobs (authorize/apply/verify/certify)
```

Direct `Call write_document_graph` outside the control-plane adapter is forbidden in ingestion services (static pytest).

## Summary authority (frozen)

```yaml
summary_rules:
  may_route_retrieval: true
  may_orient_llm_context: true
  may_create_alias_identity: false
  may_create_graph_facts: false
  may_be_answer_citation: false
  must_link_to_source_children: true
```

## Fixture isolation

- `fixture.purpose=graph_semantic_e2e_fixture`
- `production_visible=false`
- `excluded_from_user_search=true`
- Shadow collections only for alias/schema/joins; no production schema overwrite
- q9 orphans (`REVIEW_MISSING_EXTRACTION`, `bounded_reextract_limit=0`) untouched

## Remaining measured debt (not blockers for this STOP)

- Structural Neo4j capability flags can lag assertion-ready certificate depending on `inspect_graph_capabilities` corpus_id join path; assertion lane CERTIFIED with Fact=0 is the honest graph mode.
- Fixture retrieval materialization is a separate pass (`materialize_gsem_fixture_retrieval.py`) after Phase 1 extract/alias — bake into Phase 1 on next fixture refresh if desired.
- Backend image still needs a durable `--build` to bake worker/backfill CP routing (docker cp used for live proof).

## Prohibitions still in force

```yaml
production_migration: prohibited
ontology_production_activation: prohibited
schema_backfill: prohibited
orphan_reextraction: prohibited
global_ranking_enable: prohibited
```

**HARD STOP.** Do not start production migration or ontology activation without a new owner directive.
