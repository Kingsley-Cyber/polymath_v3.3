# Knowledge Bundle Recovery — Progress (Phase 1 partial)

**Status:** adapter live; alias pipeline still disconnected from worker  
**Production mutation:** none

## Shipped

- `models/knowledge_artifact_bundle.py` — KnowledgeArtifactBundleV1
- `services/ingestion/knowledge_bundle.py` — adapter over `ghost_b_extractions` / ExtractionResult
- `assertion_projector` consumes accepted relations via bundle view (not `facts=[]`)
- `mention_normalizer` uses `get_shared_nlp`
- Tests: `tests/test_knowledge_artifact_bundle.py` (3 passed)
- q9 export: `data_eval/knowledge_e2e/knowledge_artifact_bundles.jsonl`  
  MEASURED: 160 bundles, 36 accepted_relation_assertions, 10 docs
- Fixture sources staged (not ingested): `data_eval/knowledge_e2e/fixture_sources/*.md`

## Still open in Phase 1

- Wire `collect_alias_candidates` → `run_alias_gate` → document/corpus clustering on **fixture** ingest only
- Optional Mongo persist of bundles (shadow collection)
- Do not touch 2583 REVIEW_MISSING_EXTRACTION holds

## Next

Phase 1 finish (alias reconnect on fixture) → Phase 2 SummaryInformationRecord → … → Phase 10 STOP
