# ingestion modules

Module pages — subsystem index. Back to [INDEX](../INDEX.md).

86 pages:

| Page | Source | Lines | Purpose (first docstring line) |
|---|---|---|---|
| [admission](../services_ingestion_admission.md) | `backend/services/ingestion/admission.py` | 104 | Process-local ingest admission control. |
| [alias_candidates](../services_ingestion_alias_candidates.md) | `backend/services/ingestion/alias_candidates.py` | 785 | Alias candidate unification around live miners (Phases 2–3). |
| [alias_corpus_clustering](../services_ingestion_alias_corpus_clustering.md) | `backend/services/ingestion/alias_corpus_clustering.py` | 1014 | Deterministic corpus identity clustering (Phase 6). |
| [alias_document_clustering](../services_ingestion_alias_document_clustering.md) | `backend/services/ingestion/alias_document_clustering.py` | 442 | Document-scoped entity clustering from gated + parent-aggregated alias evidence. |
| [alias_gate](../services_ingestion_alias_gate.md) | `backend/services/ingestion/alias_gate.py` | 536 | Deterministic alias gate (Alias Pipeline Phase 4). |
| [alias_parent_aggregation](../services_ingestion_alias_parent_aggregation.md) | `backend/services/ingestion/alias_parent_aggregation.py` | 463 | Parent-level alias evidence aggregation (Phase 5). |
| [alias_retrieval_shadow](../services_ingestion_alias_retrieval_shadow.md) | `backend/services/ingestion/alias_retrieval_shadow.py` | 475 | Phase-8 live wiring for alias schema retrieval (shadow / fixture-canary). |
| [alias_schema_projection](../services_ingestion_alias_schema_projection.md) | `backend/services/ingestion/alias_schema_projection.py` | 249 | Project CorpusEntityV1 into separated shadow schema trust classes (Phase 7). |
| [alias_schema_retrieval](../services_ingestion_alias_schema_retrieval.md) | `backend/services/ingestion/alias_schema_retrieval.py` | 489 | Phase-7 dual-lane schema-assisted retrieval (shadow index only). |
| [alias_shadow_build](../services_ingestion_alias_shadow_build.md) | `backend/services/ingestion/alias_shadow_build.py` | 173 | Production ingest → alias SHADOW builder (owner-ordered wiring, 2026-08-09). |
| [b_plus_normalizer](../services_ingestion_b_plus_normalizer.md) | `backend/services/ingestion/b_plus_normalizer.py` | 172 | B+ tier synthetic header injection. |
| [batches](../services_ingestion_batches.md) | `backend/services/ingestion/batches.py` | 3674 | Durable ingestion batch helpers. |
| [bibliographic](../services_ingestion_bibliographic.md) | `backend/services/ingestion/bibliographic.py` | 711 | Bibliographic + date identity for documents (T-HOOK-3 / P2.1). |
| [chunk_subprocess](../services_ingestion_chunk_subprocess.md) | `backend/services/ingestion/chunk_subprocess.py` | 80 | Lean chunk-stage subprocess entrypoint. |
| [claim_assessment](../services_ingestion_claim_assessment.md) | `backend/services/ingestion/claim_assessment.py` | 423 | Deterministic negation and typed-signature assessment for compiled claims. |
| [claim_compiler](../services_ingestion_claim_compiler.md) | `backend/services/ingestion/claim_compiler.py` | 768 | Deterministic ObservationBundle + LocalExtractionV1 claim compiler. |
| [code_splitter](../services_ingestion_code_splitter.md) | `backend/services/ingestion/code_splitter.py` | 416 | Embedder-safe AST packing + metadata extraction for code chunks. |
| [corpus_commander](../services_ingestion_corpus_commander.md) | `backend/services/ingestion/corpus_commander.py` | 470 | Resident-style corpus commander cycle. |
| [corpus_lexicon](../services_ingestion_corpus_lexicon.md) | `backend/services/ingestion/corpus_lexicon.py` | 3435 | Durable corpus vocabulary materialization from existing extraction artifacts. |
| [corpus_repair](../services_ingestion_corpus_repair.md) | `backend/services/ingestion/corpus_repair.py` | 1008 | Bounded corpus repair cycle. |
| [dedup](../services_ingestion_dedup.md) | `backend/services/ingestion/dedup.py` | 653 | Deterministic document-deduplication pipeline: DETECT / PREVENT / CORRECT. |
| [deterministic_summary](../services_ingestion_deterministic_summary.md) | `backend/services/ingestion/deterministic_summary.py` | 519 | Deterministic ingestion summaries — ``deterministic_summary.v1``. |
| [doc_artifact](../services_ingestion_doc_artifact.md) | `backend/services/ingestion/doc_artifact.py` | 373 | Passive document artifact compiler for source-role synthesis headers. |
| [docling_adapter](../services_ingestion_docling_adapter.md) | `backend/services/ingestion/docling_adapter.py` | 2525 | Deterministic parse adapter (historically the "docling adapter"). |
| [document_pipeline_executors](../services_ingestion_document_pipeline_executors.md) | `backend/services/ingestion/document_pipeline_executors.py` | 658 | Executors for durable document-stage repair jobs. |
| [document_pipeline_jobs](../services_ingestion_document_pipeline_jobs.md) | `backend/services/ingestion/document_pipeline_jobs.py` | 984 | Durable document-stage ingestion job planner. |
| [document_semantic_profile](../services_ingestion_document_semantic_profile.md) | `backend/services/ingestion/document_semantic_profile.py` | 672 | Deterministic T9.1 document-profile compiler. |
| [document_summaries](../services_ingestion_document_summaries.md) | `backend/services/ingestion/document_summaries.py` | 407 | Bounded document-level summary repair. |
| [enrich](../services_ingestion_enrich.md) | `backend/services/ingestion/enrich.py` | 717 | enrich.py — Pass-1 deterministic enrichment (no model, bit-for-bit reproducible). |
| [enrichment_executor](../services_ingestion_enrichment_executor.md) | `backend/services/ingestion/enrichment_executor.py` | 135 | THE enrichment executor — single owner of enrichment execution. |
| [enrichment_gate](../services_ingestion_enrichment_gate.md) | `backend/services/ingestion/enrichment_gate.py` | 160 | E1 — quality-gated RTX enrichment decision (§13-H, owner-ratified). |
| [extraction_artifacts](../services_ingestion_extraction_artifacts.md) | `backend/services/ingestion/extraction_artifacts.py` | 498 | Additive adapters into the shared P2.6 candidate artifact contract. |
| [extraction_burst](../services_ingestion_extraction_burst.md) | `backend/services/ingestion/extraction_burst.py` | 403 | Pure P2.7b burst manifest, barrier, metrics, and retry-safety contracts. |
| [extraction_contract](../services_ingestion_extraction_contract.md) | `backend/services/ingestion/extraction_contract.py` | 66 | Fail-closed extraction contract for qualified extraction paths. |
| [extraction_jobs](../services_ingestion_extraction_jobs.md) | `backend/services/ingestion/extraction_jobs.py` | 1604 | Durable chunk-level extraction job planner. |
| [extraction_parity](../services_ingestion_extraction_parity.md) | `backend/services/ingestion/extraction_parity.py` | 323 | Measurement-only engine parity harness for P2.6/P2.7. |
| [failure_reconciliation](../services_ingestion_failure_reconciliation.md) | `backend/services/ingestion/failure_reconciliation.py` | 531 | Ghost B failure-metadata reconciliation. |
| [fixture_knowledge_pipeline](../services_ingestion_fixture_knowledge_pipeline.md) | `backend/services/ingestion/fixture_knowledge_pipeline.py` | 605 | Historical isolated fixture pipeline for alias identity lineage. |
| [fleet_status](../services_ingestion_fleet_status.md) | `backend/services/ingestion/fleet_status.py` | 203 | Deterministic worker-fleet observability (owner-ordered, 2026-08-09). |
| [format_router](../services_ingestion_format_router.md) | `backend/services/ingestion/format_router.py` | 199 | Format router — MIME detection and document decoding (Phase 1). |
| [frame_motif](../services_ingestion_frame_motif.md) | `backend/services/ingestion/frame_motif.py` | 504 | Side-effect-free T9.2 frame binding and strict motif matching. |
| [graph_backfill](../services_ingestion_graph_backfill.md) | `backend/services/ingestion/graph_backfill.py` | 890 | Retry failed Ghost B graph extraction chunks after a document lands. |
| [graph_promotion_jobs](../services_ingestion_graph_promotion_jobs.md) | `backend/services/ingestion/graph_promotion_jobs.py` | 1384 | Durable graph-promotion job queue. |
| [idempotency_audit](../services_ingestion_idempotency_audit.md) | `backend/services/ingestion/idempotency_audit.py` | 352 | Exact source identity audit for corpus ingestion. |
| [job_control](../services_ingestion_job_control.md) | `backend/services/ingestion/job_control.py` | 155 | Inspectable operator controls for durable ingestion jobs. |
| [job_leases](../services_ingestion_job_leases.md) | `backend/services/ingestion/job_leases.py` | 524 | Lease and exhaustion helpers for durable ingestion repair queues. |
| [knowledge_bundle](../services_ingestion_knowledge_bundle.md) | `backend/services/ingestion/knowledge_bundle.py` | 293 | Adapt live ExtractionResult / ghost_b rows → KnowledgeArtifactBundleV1. |
| [model_lifecycle](../services_ingestion_model_lifecycle.md) | `backend/services/ingestion/model_lifecycle.py` | 513 | Managed remote model lifecycle helpers. |
| [organ_repair_jobs](../services_ingestion_organ_repair_jobs.md) | `backend/services/ingestion/organ_repair_jobs.py` | 280 | Organ repair as a first-class, reconciler-owned lane. |
| [paid_cost_reservation](../services_ingestion_paid_cost_reservation.md) | `backend/services/ingestion/paid_cost_reservation.py` | 108 | Fail-closed pre-claim cost reservation for every paid provider lane. |
| [parse_policy](../services_ingestion_parse_policy.md) | `backend/services/ingestion/parse_policy.py` | 104 | q9 deterministic parsing contract (owner directive v1.0, 2026-08-05). |
| [pressure](../services_ingestion_pressure.md) | `backend/services/ingestion/pressure.py` | 278 | Ingestion pressure/readiness signals. |
| [promote](../services_ingestion_promote.md) | `backend/services/ingestion/promote.py` | 925 | Deterministic claim-to-graph promotion. |
| [provider_call_telemetry](../services_ingestion_provider_call_telemetry.md) | `backend/services/ingestion/provider_call_telemetry.py` | 202 | Secret-free provider call accounting for ingestion phases. |
| [provider_canary_cache](../services_ingestion_provider_canary_cache.md) | `backend/services/ingestion/provider_canary_cache.py` | 101 | Secret-safe provider canary cache shared across corpora and pool roles. |
| [provider_lane_health](../services_ingestion_provider_lane_health.md) | `backend/services/ingestion/provider_lane_health.py` | 394 | Durable provider-lane health for Ghost B extraction pools. |
| [readiness](../services_ingestion_readiness.md) | `backend/services/ingestion/readiness.py` | 2291 | Computed corpus-readiness contract. |
| [repair_scheduler](../services_ingestion_repair_scheduler.md) | `backend/services/ingestion/repair_scheduler.py` | 197 | Cheap, readiness-driven scheduling state for corpus repair. |
| [resource_planner](../services_ingestion_resource_planner.md) | `backend/services/ingestion/resource_planner.py` | 356 | Environment-aware ingestion resource planning. |
| [route_readiness](../services_ingestion_route_readiness.md) | `backend/services/ingestion/route_readiness.py` | 348 | Route-aware readiness bridge: artifact census -> ReadinessDecision. |
| [schema_lens](../services_ingestion_schema_lens.md) | `backend/services/ingestion/schema_lens.py` | 870 | Auto schema lens generation for Ghost B ingestion. |
| [section_classifier](../services_ingestion_section_classifier.md) | `backend/services/ingestion/section_classifier.py` | 553 | Section classifier — tag each parent chunk with a `ChunkKind` based on its heading path, s… |
| [semantic_digest_claim_inputs](../services_ingestion_semantic_digest_claim_inputs.md) | `backend/services/ingestion/semantic_digest_claim_inputs.py` | 1400 | Compile, validate, materialize, and packetize deterministic atomic claims. |
| [semantic_observations](../services_ingestion_semantic_observations.md) | `backend/services/ingestion/semantic_observations.py` | 808 | Deterministic spaCy observation capture and claim-candidate compilation. |
| [semantic_parent_eligibility](../services_ingestion_semantic_parent_eligibility.md) | `backend/services/ingestion/semantic_parent_eligibility.py` | 155 | Versioned, content-neutral semantic-parent eligibility. |
| [semantic_resolution](../services_ingestion_semantic_resolution.md) | `backend/services/ingestion/semantic_resolution.py` | 491 | Local deterministic T9.1 domain and predicate→superframe resolution. |
| [slm_enrich](../services_ingestion_slm_enrich.md) | `backend/services/ingestion/slm_enrich.py` | 301 | Pass-1 deterministic + Pass-2 SLM-residual enrichment for the local lane. |
| [source_classifier](../services_ingestion_source_classifier.md) | `backend/services/ingestion/source_classifier.py` | 36 | DEPRECATED — Phase 7.6. |
| [source_identity](../services_ingestion_source_identity.md) | `backend/services/ingestion/source_identity.py` | 387 | Deterministic source identity helpers for ingestion guardrails. |
| [source_parse_jobs](../services_ingestion_source_parse_jobs.md) | `backend/services/ingestion/source_parse_jobs.py` | 815 | Durable source/parse job read model. |
| [stage_identity](../services_ingestion_stage_identity.md) | `backend/services/ingestion/stage_identity.py` | 162 | Stage-level idempotency keys for ingestion jobs. |
| [stage_identity_repair](../services_ingestion_stage_identity_repair.md) | `backend/services/ingestion/stage_identity_repair.py` | 248 | Bounded stage-identity repair helpers. |
| [storage_pressure](../services_ingestion_storage_pressure.md) | `backend/services/ingestion/storage_pressure.py` | 235 | Storage service pressure samplers for ingestion readiness. |
| [summary_backfill](../services_ingestion_summary_backfill.md) | `backend/services/ingestion/summary_backfill.py` | 827 | Summary-tier backfill + readiness guardrail. |
| [summary_cost_control](../services_ingestion_summary_cost_control.md) | `backend/services/ingestion/summary_cost_control.py` | 622 | Durable, fail-closed list-price ceilings for paid summary calls. |
| [summary_from_bundle](../services_ingestion_summary_from_bundle.md) | `backend/services/ingestion/summary_from_bundle.py` | 194 | Build SummaryInformationRecordV1 from KnowledgeArtifactBundleV1. |
| [summary_jobs](../services_ingestion_summary_jobs.md) | `backend/services/ingestion/summary_jobs.py` | 1516 | Durable summary job planner. |
| [summary_provider_pool](../services_ingestion_summary_provider_pool.md) | `backend/services/ingestion/summary_provider_pool.py` | 321 | Resolve the production summary-provider pool without exposing secrets. |
| [summary_semantics](../services_ingestion_summary_semantics.md) | `backend/services/ingestion/summary_semantics.py` | 928 | §10.1 — the semantic parent-summary contract (POLYMATH_ARCHITECTURE §10.1). |
| [summary_tree](../services_ingestion_summary_tree.md) | `backend/services/ingestion/summary_tree.py` | 1119 | B3 — the owner summary tree (OWNER_SUMMARY_TREE_DESIGN.md). |
| [summary_tree_llm](../services_ingestion_summary_tree_llm.md) | `backend/services/ingestion/summary_tree_llm.py` | 147 | Corpus-scoped LLM hook for document summary trees. |
| [summary_vector_reconcile](../services_ingestion_summary_vector_reconcile.md) | `backend/services/ingestion/summary_vector_reconcile.py` | 358 | ID-level reconciliation for summary text vs summary vectors. |
| [tier0](../services_ingestion_tier0.md) | `backend/services/ingestion/tier0.py` | 321 | W1 Tier-0 — auto-embed the document ROUTING CARD at ingest. |
| [tier_chunker](../services_ingestion_tier_chunker.md) | `backend/services/ingestion/tier_chunker.py` | 1796 | Tier chunker — hierarchical parent/child splitting. |
| [verify](../services_ingestion_verify.md) | `backend/services/ingestion/verify.py` | 627 | Phase E — post-write verification. |
| [worker](../services_ingestion_worker.md) | `backend/services/ingestion/worker.py` | 5103 | Ingestion pipeline worker — locked pipeline order: |