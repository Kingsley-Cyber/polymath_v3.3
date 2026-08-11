# extraction modules

Module pages — subsystem index. Back to [INDEX](../INDEX.md).

39 pages:

| Page | Source | Lines | Purpose (first docstring line) |
|---|---|---|---|
| [ablation](../services_extraction_ablation.md) | `backend/services/extraction/ablation.py` | 68 | Ablation profile configuration for the unified shadow pipeline. |
| [appos_enrichment](../services_extraction_appos_enrichment.md) | `backend/services/extraction/appos_enrichment.py` | 481 | spaCy appositional classification for the deterministic alias pipeline. |
| [canonical](../services_extraction_canonical.md) | `backend/services/extraction/canonical.py` | 697 | Canonical entity and predicate representations — single source of truth. |
| [config_locator](../services_extraction_config_locator.md) | `backend/services/extraction/config_locator.py` | 24 | Locate the versioned config/ directory from any deployment layout. |
| [corpus_coordinator](../services_extraction_corpus_coordinator.md) | `backend/services/extraction/corpus_coordinator.py` | 147 | CorpusCoordinator — factory execution plane, station B. |
| [corroboration_gate](../services_extraction_corroboration_gate.md) | `backend/services/extraction/corroboration_gate.py` | 999 | Deterministic corroboration gate between Relex raw scores and syntax evidence. |
| [coverage_checkpoint](../services_extraction_coverage_checkpoint.md) | `backend/services/extraction/coverage_checkpoint.py` | 175 | Extraction coverage checkpoints — make a dead organ impossible to miss. |
| [credit_patterns](../services_extraction_credit_patterns.md) | `backend/services/extraction/credit_patterns.py` | 363 | Credit and metadata fragment patterns (P2B construction family). |
| [dep_path_extractor](../services_extraction_dep_path_extractor.md) | `backend/services/extraction/dep_path_extractor.py` | 1545 | Core dependency-path relation extractor. |
| [engine_routing](../services_extraction_engine_routing.md) | `backend/services/extraction/engine_routing.py` | 220 | Runtime routing for the extraction engine (owner-ordered, 2026-08-09). |
| [entity_encoder](../services_extraction_entity_encoder.md) | `backend/services/extraction/entity_encoder.py` | 465 | Provider-neutral entity-encoder boundary (owner-ratified 2026-08-08). |
| [entity_quality](../services_extraction_entity_quality.md) | `backend/services/extraction/entity_quality.py` | 574 | Deterministic entity quality gate. |
| [frame_extractor](../services_extraction_frame_extractor.md) | `backend/services/extraction/frame_extractor.py` | 1292 | Frame-licensed relation extraction — the rebuilt pairing model. |
| [gliner2_cpu_provider](../services_extraction_gliner2_cpu_provider.md) | `backend/services/extraction/gliner2_cpu_provider.py` | 350 | Sole warm entity provider for the canonical Graphify extraction path. |
| [graphify_argument_adapter](../services_extraction_graphify_argument_adapter.md) | `backend/services/extraction/graphify_argument_adapter.py` | 424 | Deterministic entity-linking ladder for raw OpenIE arguments. |
| [graphify_assertion_assembler](../services_extraction_graphify_assertion_assembler.md) | `backend/services/extraction/graphify_assertion_assembler.py` | 162 | Assemble evidence-scoped OpenIE candidates into explicit authority lanes. |
| [graphify_assertion_semantics](../services_extraction_graphify_assertion_semantics.md) | `backend/services/extraction/graphify_assertion_semantics.py` | 102 | Clause-local deterministic assertion-status rules shared by extraction lanes. |
| [graphify_census](../services_extraction_graphify_census.md) | `backend/services/extraction/graphify_census.py` | 619 | Structure-aware, corpus-batched entity census with mention conservation. |
| [graphify_completion](../services_extraction_graphify_completion.md) | `backend/services/extraction/graphify_completion.py` | 184 | Document-local exact mention completion with ambiguity guards. |
| [graphify_normalization](../services_extraction_graphify_normalization.md) | `backend/services/extraction/graphify_normalization.py` | 70 | Loss-aware source normalization with reversible character boundaries. |
| [graphify_openie](../services_extraction_graphify_openie.md) | `backend/services/extraction/graphify_openie.py` | 493 | Balanced CPU OpenIE proposal source for canonical Graphify. |
| [graphify_pipeline](../services_extraction_graphify_pipeline.md) | `backend/services/extraction/graphify_pipeline.py` | 969 | Resumable, fail-closed orchestration for the canonical Graphify path. |
| [graphify_predicate_compiler](../services_extraction_graphify_predicate_compiler.md) | `backend/services/extraction/graphify_predicate_compiler.py` | 220 | Compile reduced OpenIE surface relations without inventing ontology edges. |
| [graphify_proposition_reducer](../services_extraction_graphify_proposition_reducer.md) | `backend/services/extraction/graphify_proposition_reducer.py` | 210 | Reduce entailed OpenIE renderings without losing provenance variants. |
| [graphify_provider_registry](../services_extraction_graphify_provider_registry.md) | `backend/services/extraction/graphify_provider_registry.py` | 13 | Fail-closed registry for the canonical Graphify entity provider. |
| [graphify_reducer](../services_extraction_graphify_reducer.md) | `backend/services/extraction/graphify_reducer.py` | 714 | Document-local entity clustering and conservative quality adjudication. |
| [graphify_relations](../services_extraction_graphify_relations.md) | `backend/services/extraction/graphify_relations.py` | 2559 | Selective parse-once relation fast path and predicate compiler. |
| [graphify_survey](../services_extraction_graphify_survey.md) | `backend/services/extraction/graphify_survey.py` | 263 | Deterministic, zero-model survey for Graphify source documents. |
| [graphify_unit_kind](../services_extraction_graphify_unit_kind.md) | `backend/services/extraction/graphify_unit_kind.py` | 197 | unit.kind representation router — the factory's first station. |
| [graphify_value_ir](../services_extraction_graphify_value_ir.md) | `backend/services/extraction/graphify_value_ir.py` | 98 | Typed value + temporal qualifier IR (#7, owner-ratified 2026-08-07). |
| [mention_normalizer](../services_extraction_mention_normalizer.md) | `backend/services/extraction/mention_normalizer.py` | 155 | Deterministic mention normalization for entity boundary equivalence. |
| [openie_farm](../services_extraction_openie_farm.md) | `backend/services/extraction/openie_farm.py` | 137 | N-process warm triplet-extract farm — factory execution plane, station A. |
| [relation_evidence](../services_extraction_relation_evidence.md) | `backend/services/extraction/relation_evidence.py` | 384 | Shared evidence contract for the relation corroboration gate. |
| [relex_adapter](../services_extraction_relex_adapter.md) | `backend/services/extraction/relex_adapter.py` | 686 | Adapter: converts Relex raw predictions + syntax candidates → RelationEvidence. |
| [relex_gate](../services_extraction_relex_gate.md) | `backend/services/extraction/relex_gate.py` | 329 | Precision gate for GLiNER-Relex output. |
| [relex_sidecar_client](../services_extraction_relex_sidecar_client.md) | `backend/services/extraction/relex_sidecar_client.py` | 249 | Client for the host-MPS GLiNER-Relex sidecar (contract relex-infer-v1). |
| [spacy_relation_adapter](../services_extraction_spacy_relation_adapter.md) | `backend/services/extraction/spacy_relation_adapter.py` | 437 | spaCy dependency-path relation adapter for the Ghost B pipeline. |
| [svo_candidates](../services_extraction_svo_candidates.md) | `backend/services/extraction/svo_candidates.py` | 148 | Verb-centric SVO candidate generation — textacy's algorithm, natively. |
| [syntax_lane](../services_extraction_syntax_lane.md) | `backend/services/extraction/syntax_lane.py` | 493 | Production syntax lane for the relex_local extraction engine. |