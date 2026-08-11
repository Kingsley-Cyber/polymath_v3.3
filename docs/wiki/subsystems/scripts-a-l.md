# Scripts A L

Module pages — subsystem index. Back to [INDEX](../INDEX.md).

55 pages:

| Page | Source | Lines | Purpose (first docstring line) |
|---|---|---|---|
| [_probe_cq_retrieve](../scripts__probe_cq_retrieve.md) | `backend/scripts/_probe_cq_retrieve.py` | 57 | — |
| [ab_gliner2_device](../scripts_ab_gliner2_device.md) | `backend/scripts/ab_gliner2_device.py` | 205 | GLiNER2 execution-placement A/B (owner-ratified 2026-08-08). |
| [adjudicate_d_e_candidates](../scripts_adjudicate_d_e_candidates.md) | `backend/scripts/adjudicate_d_e_candidates.py` | 731 | Adjudicate every production-eligible candidate introduced by D and E. |
| [aggregate_chat_cost_ledger](../scripts_aggregate_chat_cost_ledger.md) | `backend/scripts/aggregate_chat_cost_ledger.py` | 86 | Aggregate additive ``/api/chat`` cost traces from eval JSON or raw SSE. |
| [annotate_entity_quality](../scripts_annotate_entity_quality.md) | `backend/scripts/annotate_entity_quality.py` | 92 | Annotate stored entity mentions with a graph-eligibility verdict. |
| [apply_mongo_validators](../scripts_apply_mongo_validators.md) | `backend/scripts/apply_mongo_validators.py` | 170 | Apply (or dry-run) the P0.8 warn-first Mongo JSON-schema validators. |
| [assess_speed_bench_test](../scripts_assess_speed_bench_test.md) | `backend/scripts/assess_speed_bench_test.py` | 364 | Assess speed_bench_test_20260805: extraction speed + ontology + summary quality. |
| [audit_claim_assessment_ugo](../scripts_audit_claim_assessment_ugo.md) | `backend/scripts/audit_claim_assessment_ugo.py` | 541 | Read-only, count-only T8.4 UGO claim-assessment census. |
| [audit_claim_compiler_ugo](../scripts_audit_claim_compiler_ugo.md) | `backend/scripts/audit_claim_compiler_ugo.py` | 295 | Read-only, count-only UGO audit of deterministic ClaimRecordV1 compilation. |
| [audit_graphify_stress_frozen](../scripts_audit_graphify_stress_frozen.md) | `backend/scripts/audit_graphify_stress_frozen.py` | 531 | Audit a frozen Graphify stress namespace without rerunning extraction. |
| [audit_local_extraction_ugo](../scripts_audit_local_extraction_ugo.md) | `backend/scripts/audit_local_extraction_ugo.py` | 208 | Read-only trained-spaCy audit of LocalExtractionV1 on UGO child text. |
| [audit_semantic_parent_eligibility_mark](../scripts_audit_semantic_parent_eligibility_mark.md) | `backend/scripts/audit_semantic_parent_eligibility_mark.py` | 154 | Read-only B1 census for mark semantic-parent eligibility v2. |
| [audit_t83_negation_signatures](../scripts_audit_t83_negation_signatures.md) | `backend/scripts/audit_t83_negation_signatures.py` | 397 | Read-only aggregate census for T8.3 negation and legacy signatures. |
| [backfill_child_domain](../scripts_backfill_child_domain.md) | `backend/scripts/backfill_child_domain.py` | 176 | Backfill child-chunk `domain` from Ghost-A parent domains (M1, 2026-07-02). |
| [backfill_corpus_lexicon](../scripts_backfill_corpus_lexicon.md) | `backend/scripts/backfill_corpus_lexicon.py` | 590 | Backfill the corpus vocabulary bridge from durable extraction artifacts. |
| [backfill_doc_anchors](../scripts_backfill_doc_anchors.md) | `backend/scripts/backfill_doc_anchors.py` | 245 | Backfill :Document anchor properties on Neo4j from MongoDB. |
| [backfill_doc_artifacts](../scripts_backfill_doc_artifacts.md) | `backend/scripts/backfill_doc_artifacts.py` | 182 | Backfill passive doc_profile.doc_artifact records for existing documents. |
| [backfill_mechanisms](../scripts_backfill_mechanisms.md) | `backend/scripts/backfill_mechanisms.py` | 173 | Backfill bridge `mechanisms` onto parent chunks (bridge retrieval B1). |
| [backfill_parent_domains](../scripts_backfill_parent_domains.md) | `backend/scripts/backfill_parent_domains.py` | 62 | Backfill `domain` onto parent_chunks from the graph_domain_cache. |
| [backfill_parent_domains_llm](../scripts_backfill_parent_domains_llm.md) | `backend/scripts/backfill_parent_domains_llm.py` | 150 | LLM backfill of `domain` + `topics` onto parent_chunks (Ghost-A-style tags). |
| [backfill_passthrough_sections](../scripts_backfill_passthrough_sections.md) | `backend/scripts/backfill_passthrough_sections.py` | 419 | Backfill passthrough payloads for EXISTING one-child section rows (P0.2). |
| [backfill_q8_evidence_collection](../scripts_backfill_q8_evidence_collection.md) | `backend/scripts/backfill_q8_evidence_collection.py` | 230 | q8 (owner directive 2026-08-04) — backfill the one-point-per-child candidate evidence coll… |
| [backfill_qdrant_graph_defaults](../scripts_backfill_qdrant_graph_defaults.md) | `backend/scripts/backfill_qdrant_graph_defaults.py` | 181 | Backfill missing graph-default payload keys on Qdrant graph points. |
| [backfill_qdrant_payload_text](../scripts_backfill_qdrant_payload_text.md) | `backend/scripts/backfill_qdrant_payload_text.py` | 200 | Backfill Qdrant text payloads from Mongo canonical text. |
| [backfill_related_to_mitigation](../scripts_backfill_related_to_mitigation.md) | `backend/scripts/backfill_related_to_mitigation.py` | 219 | Backfill bounded related_to mitigation metadata on existing Neo4j edges. |
| [backfill_relates_to_support_metadata](../scripts_backfill_relates_to_support_metadata.md) | `backend/scripts/backfill_relates_to_support_metadata.py` | 202 | Backfill materialized RELATES_TO support metadata in Neo4j. |
| [backfill_relation_support_records](../scripts_backfill_relation_support_records.md) | `backend/scripts/backfill_relation_support_records.py` | 271 | Backfill Mongo relation_support_records from staged Ghost B extractions. |
| [backfill_semantic_facets](../scripts_backfill_semantic_facets.md) | `backend/scripts/backfill_semantic_facets.py` | 323 | Backfill semantic facet metadata for already-ingested corpora. |
| [backfill_summary_tree_index](../scripts_backfill_summary_tree_index.md) | `backend/scripts/backfill_summary_tree_index.py` | 373 | Backfill pre-embedded RAPTOR section/rollup routing points. |
| [backfill_summary_tree_metadata](../scripts_backfill_summary_tree_metadata.md) | `backend/scripts/backfill_summary_tree_metadata.py` | 575 | Backfill deterministic routing metadata onto existing summary-tree rows. |
| [backfill_tree_concepts](../scripts_backfill_tree_concepts.md) | `backend/scripts/backfill_tree_concepts.py` | 302 | Backfill deterministic `concepts` onto existing summary_tree rows (P0.2/P2.1). |
| [bench_entity_encoders](../scripts_bench_entity_encoders.md) | `backend/scripts/bench_entity_encoders.py` | 138 | Entity-encoder bake-off at the sidecar boundary (owner, 2026-08-08). |
| [bench_gliner_relex](../scripts_bench_gliner_relex.md) | `backend/scripts/bench_gliner_relex.py` | 165 | Head-to-head: GLiNER-Relex vs the in-repo frame extractor. |
| [benchmark_book_ingestion_pressure](../scripts_benchmark_book_ingestion_pressure.md) | `backend/scripts/benchmark_book_ingestion_pressure.py` | 226 | Book-ingestion pressure baseline (Part 7/8 audit deliverable). |
| [bibliographic_backfill](../scripts_bibliographic_backfill.md) | `backend/scripts/bibliographic_backfill.py` | 681 | T-HOOK-3 / P2.1 deterministic bibliographic backfill (documents only). |
| [build_librarian_cards](../scripts_build_librarian_cards.md) | `backend/scripts/build_librarian_cards.py` | 222 | Build deterministic librarian_card.v0 cards for one or more corpora. |
| [canary_graph_release_gate_probe](../scripts_canary_graph_release_gate_probe.md) | `backend/scripts/canary_graph_release_gate_probe.py` | 698 | 1C canary enforcement probe for the canonical graph-write release gate. |
| [capture_raptor_baseline](../scripts_capture_raptor_baseline.md) | `backend/scripts/capture_raptor_baseline.py` | 302 | Reproducible RAPTOR baseline census (read-only). |
| [check_markdown_links](../scripts_check_markdown_links.md) | `backend/scripts/check_markdown_links.py` | 77 | Relative-link check for git-tracked Markdown files (checklist P0.7). |
| [cleanup_orphan_catalog](../scripts_cleanup_orphan_catalog.md) | `backend/scripts/cleanup_orphan_catalog.py` | 409 | One-shot orphan catalog sweeper for the intended 3+1 live corpus set. |
| [compare_ablation_runs](../scripts_compare_ablation_runs.md) | `backend/scripts/compare_ablation_runs.py` | 247 | Compare ablation runs and produce a unified comparison artifact. |
| [compare_embedding_instruction_ab](../scripts_compare_embedding_instruction_ab.md) | `backend/scripts/compare_embedding_instruction_ab.py` | 194 | Assert the preregistered T5.6 query-instruction promotion gates. |
| [diagnose_atomic_claim_anchor_coverage](../scripts_diagnose_atomic_claim_anchor_coverage.md) | `backend/scripts/diagnose_atomic_claim_anchor_coverage.py` | 381 | Offline replay of claim-anchor eligibility over persisted selected sources. |
| [entity_gate_sample](../scripts_entity_gate_sample.md) | `backend/scripts/entity_gate_sample.py` | 180 | Entity gate v1 — sample by MENTION, judge on four dimensions, score. |
| [entity_sidecar_server](../scripts_entity_sidecar_server.md) | `backend/scripts/entity_sidecar_server.py` | 142 | Entity-encoder host-MPS sidecar (release entity-sidecar-mps-v1). |
| [export_final_heldout_v2](../scripts_export_final_heldout_v2.md) | `backend/scripts/export_final_heldout_v2.py` | 305 | Final held-out v2 submission exporter (public export_contract.json only). |
| [export_final_qual_predictions](../scripts_export_final_qual_predictions.md) | `backend/scripts/export_final_qual_predictions.py` | 164 | Export locked extraction as GLiNER/GLiREL-style predictions for scoring. |
| [export_heldout_submission](../scripts_export_heldout_submission.md) | `backend/scripts/export_heldout_submission.py` | 302 | Export held-out submission artifacts from the factory Mongo per contract. |
| [extraction_coverage_check](../scripts_extraction_coverage_check.md) | `backend/scripts/extraction_coverage_check.py` | 104 | Run extraction coverage checkpoints and persist receipts. |
| [freeze_heldout_eval](../scripts_freeze_heldout_eval.md) | `backend/scripts/freeze_heldout_eval.py` | 151 | Validate and freeze the held-out evaluation suite (checklist P1.1). |
| [gate_diagnostic](../scripts_gate_diagnostic.md) | `backend/scripts/gate_diagnostic.py` | 275 | Gate-reason diagnostic for every gold relation. |
| [gate_v2_sample](../scripts_gate_v2_sample.md) | `backend/scripts/gate_v2_sample.py` | 182 | Gate v2 — build the judging worksheet, then score it. |
| [graph_projection_control_plane_close](../scripts_graph_projection_control_plane_close.md) | `backend/scripts/graph_projection_control_plane_close.py` | 201 | q9 Graph Projection Control-Plane closure — orphan classify + project + certify. |
| [graphify_e2e_client](../scripts_graphify_e2e_client.md) | `backend/scripts/graphify_e2e_client.py` | 44 | Small stdlib client for the warm Graphify E2E benchmark worker. |
| [label_structural_shadow_projections](../scripts_label_structural_shadow_projections.md) | `backend/scripts/label_structural_shadow_projections.py` | 169 | Label existing structural graph projections as noncanonical shadow. |