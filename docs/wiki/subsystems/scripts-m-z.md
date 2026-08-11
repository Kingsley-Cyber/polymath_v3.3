# Scripts M Z

Module pages — subsystem index. Back to [INDEX](../INDEX.md).

106 pages:

| Page | Source | Lines | Purpose (first docstring line) |
|---|---|---|---|
| [mark_permanent_integration_fixture](../scripts_mark_permanent_integration_fixture.md) | `backend/scripts/mark_permanent_integration_fixture.py` | 120 | Mark a corpus as the permanent integration fixture (owner directive 2026-08-03). |
| [materialize_gsem_fixture_retrieval](../scripts_materialize_gsem_fixture_retrieval.md) | `backend/scripts/materialize_gsem_fixture_retrieval.py` | 262 | Materialize Qdrant + Neo4j for the isolated gsem fixture (fixture-only). |
| [materialize_semantic_digest_claim_inputs](../scripts_materialize_semantic_digest_claim_inputs.md) | `backend/scripts/materialize_semantic_digest_claim_inputs.py` | 1730 | Materialize and audit B2 atomic-claim inputs for mark digest packets. |
| [materialize_t91_document_profiles](../scripts_materialize_t91_document_profiles.md) | `backend/scripts/materialize_t91_document_profiles.py` | 256 | Dry-run-first materializer for additive T9.1 document profiles. |
| [mine_dependency_patterns](../scripts_mine_dependency_patterns.md) | `backend/scripts/mine_dependency_patterns.py` | 329 | Dependency-path pattern mining script. |
| [normalize_extraction_provenance](../scripts_normalize_extraction_provenance.md) | `backend/scripts/normalize_extraction_provenance.py` | 213 | Normalize extraction provenance in ghost_b_extractions (P0.8). |
| [orphan_ownership_manifest](../scripts_orphan_ownership_manifest.md) | `backend/scripts/orphan_ownership_manifest.py` | 213 | Dry-run ownership manifest for historical artifacts (checklist P0.6). |
| [p0_1_summary_integrity](../scripts_p0_1_summary_integrity.md) | `backend/scripts/p0_1_summary_integrity.py` | 568 | P0.1 summary-integrity repair driver (checklist: P0 - Summary Integrity). |
| [p0_5_facet_decontamination](../scripts_p0_5_facet_decontamination.md) | `backend/scripts/p0_5_facet_decontamination.py` | 411 | P0.5 facet decontamination: measure and strip corpus-lens-inherited facet ids that lack pe… |
| [p1b_precision_audit](../scripts_p1b_precision_audit.md) | `backend/scripts/p1b_precision_audit.py` | 424 | P1B: Acceptance-precision audit and dual trace system. |
| [polymath_failed_chunk_backfill](../scripts_polymath_failed_chunk_backfill.md) | `backend/scripts/polymath_failed_chunk_backfill.py` | 482 | Plan or run bounded Ghost B failed-chunk retries for a corpus. |
| [polymath_graph_replay_backlog](../scripts_polymath_graph_replay_backlog.md) | `backend/scripts/polymath_graph_replay_backlog.py` | 554 | Plan or run bounded Ghost B full-replay graph repair for a corpus. |
| [polymath_summary_backfill_scoped](../scripts_polymath_summary_backfill_scoped.md) | `backend/scripts/polymath_summary_backfill_scoped.py` | 376 | Plan or run bounded parent-summary backfill for an existing corpus. |
| [predicate_confusion_matrix](../scripts_predicate_confusion_matrix.md) | `backend/scripts/predicate_confusion_matrix.py` | 283 | P3 Predicate Confusion Matrix — diagnostic tool for wrong-predicate traces. |
| [probe_shelf_roles](../scripts_probe_shelf_roles.md) | `backend/scripts/probe_shelf_roles.py` | 155 | READ-ONLY dry-run probe for the P1.5 shelf-role engine. |
| [probe_structured_output_capabilities](../scripts_probe_structured_output_capabilities.md) | `backend/scripts/probe_structured_output_capabilities.py` | 381 | Probe owner-configured routes for real native JSON-Schema support. |
| [probe_tier3_tool_capability](../scripts_probe_tier3_tool_capability.md) | `backend/scripts/probe_tier3_tool_capability.py` | 205 | Probe one configured route for a tiny forced Tier-3 tool contract. |
| [probe_tier_latency](../scripts_probe_tier_latency.md) | `backend/scripts/probe_tier_latency.py` | 238 | Three-tier live latency probe against the deployed backend (read-only). |
| [promote_backfilled_relations](../scripts_promote_backfilled_relations.md) | `backend/scripts/promote_backfilled_relations.py` | 206 | Promote backfilled relations into Neo4j via the SANCTIONED writer. |
| [promote_relations_targeted](../scripts_promote_relations_targeted.md) | `backend/scripts/promote_relations_targeted.py` | 234 | Targeted relation-only promotion into Neo4j. |
| [prune_ineligible_graph_edges](../scripts_prune_ineligible_graph_edges.md) | `backend/scripts/prune_ineligible_graph_edges.py` | 160 | Promotion-boundary filter: the graph keeps only edges between real nodes. |
| [pt9_smoke_test](../scripts_pt9_smoke_test.md) | `backend/scripts/pt9_smoke_test.py` | 452 | Pt9 end-to-end smoke test. |
| [purge_orphan_graph_edges](../scripts_purge_orphan_graph_edges.md) | `backend/scripts/purge_orphan_graph_edges.py` | 139 | Purge RELATES_TO edges belonging only to corpora that no longer exist. |
| [q9_phase0_snapshot](../scripts_q9_phase0_snapshot.md) | `backend/scripts/q9_phase0_snapshot.py` | 164 | Phase 0 snapshot for q9 final E2E — source hashes + store counts. |
| [qualify_relex_batching](../scripts_qualify_relex_batching.md) | `backend/scripts/qualify_relex_batching.py` | 309 | Qualify batched Relex MPS inference against the frozen gold set. |
| [recall_score](../scripts_recall_score.md) | `backend/scripts/recall_score.py` | 58 | Score RECALL against blind-authored gold. Gold was committed first. |
| [reclassify_citation_chunks](../scripts_reclassify_citation_chunks.md) | `backend/scripts/reclassify_citation_chunks.py` | 148 | Corrective reclassification of body-misclassified citation/reference chunks. |
| [reconcile_qdrant_mongo](../scripts_reconcile_qdrant_mongo.md) | `backend/scripts/reconcile_qdrant_mongo.py` | 47 | Mongo <-> Qdrant reconciliation: per-corpus counts. Read-only. |
| [reconcile_tier0_profiles](../scripts_reconcile_tier0_profiles.md) | `backend/scripts/reconcile_tier0_profiles.py` | 94 | Reconcile and optionally backfill shared Tier-0 document routing cards. |
| [reconcile_verified_documents](../scripts_reconcile_verified_documents.md) | `backend/scripts/reconcile_verified_documents.py` | 212 | Reverify completed document artifacts without re-embedding or model calls. |
| [regen_relex_benchmark](../scripts_regen_relex_benchmark.md) | `backend/scripts/regen_relex_benchmark.py` | 608 | Regenerate the frozen Relex Large benchmark predictions with preserved entity types. |
| [regen_relex_benchmark_batched](../scripts_regen_relex_benchmark_batched.md) | `backend/scripts/regen_relex_benchmark_batched.py` | 374 | Regenerate Relex Large predictions with BATCHED MPS inference. |
| [register_runpod_account](../scripts_register_runpod_account.md) | `backend/scripts/register_runpod_account.py` | 203 | Register or update one Runpod account for multi-account burst routing. |
| [relation_stage_trace](../scripts_relation_stage_trace.md) | `backend/scripts/relation_stage_trace.py` | 412 | Where does a CORRECT relation die? Per-stage attribution against blind gold. |
| [relation_waterfall](../scripts_relation_waterfall.md) | `backend/scripts/relation_waterfall.py` | 192 | R0→R7 relation-loss waterfall (owner-demanded, 2026-08-08). |
| [relex_gate_run](../scripts_relex_gate_run.md) | `backend/scripts/relex_gate_run.py` | 43 | — |
| [relex_generate](../scripts_relex_generate.md) | `backend/scripts/relex_generate.py` | 58 | Phase A: run GLiNER-Relex on host (has torch+gliner), dump raw relations. Phase B applies … |
| [relex_pr_curve](../scripts_relex_pr_curve.md) | `backend/scripts/relex_pr_curve.py` | 60 | Precision/recall trade-off for the relex->gate pipeline, at every threshold. |
| [relex_sidecar_server](../scripts_relex_sidecar_server.md) | `backend/scripts/relex_sidecar_server.py` | 165 | GLiNER-Relex host-MPS sidecar (release relex-large-mps-sidecar-v1). |
| [repair_incidental_source_identities](../scripts_repair_incidental_source_identities.md) | `backend/scripts/repair_incidental_source_identities.py` | 169 | Repair colliding source identities derived from incidental YouTube links. |
| [repair_nonsemantic_source_shells](../scripts_repair_nonsemantic_source_shells.md) | `backend/scripts/repair_nonsemantic_source_shells.py` | 201 | Exclude zero-chunk cover/navigation shells from corpus readiness. |
| [replay_downstream](../scripts_replay_downstream.md) | `backend/scripts/replay_downstream.py` | 441 | Downstream replay harness — freeze upstream observations, replay the compiler. |
| [report_graphify_s04](../scripts_report_graphify_s04.md) | `backend/scripts/report_graphify_s04.py` | 71 | Emit release and fixture evidence for Graphify stage S04. |
| [report_ingest_walltime](../scripts_report_ingest_walltime.md) | `backend/scripts/report_ingest_walltime.py` | 176 | Corpus ingest wall-time report — instrument, never narrate. |
| [rerun_candidate_synthesis](../scripts_rerun_candidate_synthesis.md) | `backend/scripts/rerun_candidate_synthesis.py` | 156 | Re-run dark candidate synthesis for quota classes using DeepSeek Flash. |
| [rescore_heldout_v4](../scripts_rescore_heldout_v4.md) | `backend/scripts/rescore_heldout_v4.py` | 92 | Scorer v4: lead-anchored refusal detection, applied SYMMETRICALLY offline. |
| [rpre_export_sample](../scripts_rpre_export_sample.md) | `backend/scripts/rpre_export_sample.py` | 114 | R-pre — export a deterministic stratified chunk sample for guard measurement. |
| [rpre_measure](../scripts_rpre_measure.md) | `backend/scripts/rpre_measure.py` | 233 | R-pre — measure what the deterministic relation lane actually discards. |
| [rpre_sample_relations](../scripts_rpre_sample_relations.md) | `backend/scripts/rpre_sample_relations.py` | 66 | Dump extracted relations with evidence for hand precision judging. |
| [run_candidate_adoption_suite](../scripts_run_candidate_adoption_suite.md) | `backend/scripts/run_candidate_adoption_suite.py` | 396 | Candidate-adoption validation suite (dark, allowlist-only) → HARD STOP. |
| [run_canonical_heldout_negative_eval](../scripts_run_canonical_heldout_negative_eval.md) | `backend/scripts/run_canonical_heldout_negative_eval.py` | 1120 | Run the immutable 28-probe held-out refusal measurement. |
| [run_chat_cost_live_gate](../scripts_run_chat_cost_live_gate.md) | `backend/scripts/run_chat_cost_live_gate.py` | 211 | Run the single preregistered live gate for the P7 chat cost ledger. |
| [run_claim_anchor_additivity_replay](../scripts_run_claim_anchor_additivity_replay.md) | `backend/scripts/run_claim_anchor_additivity_replay.py` | 352 | Replay atomic anchors over one sealed OFF final-evidence packet. |
| [run_claim_anchor_micro_ab](../scripts_run_claim_anchor_micro_ab.md) | `backend/scripts/run_claim_anchor_micro_ab.py` | 564 | Run one read-only arm of the preregistered claim-anchor micro A/B. |
| [run_complex_query_dark_canary](../scripts_run_complex_query_dark_canary.md) | `backend/scripts/run_complex_query_dark_canary.py` | 301 | Bounded dark-canary canary run — shadow metrics via /api/chat. |
| [run_complex_query_fixture_e2e](../scripts_run_complex_query_fixture_e2e.md) | `backend/scripts/run_complex_query_fixture_e2e.py` | 214 | Complex-query Phases 4–12 fixture E2E (gsem-e2e-20260804a only) → STOP. |
| [run_complex_query_generation_probes](../scripts_run_complex_query_generation_probes.md) | `backend/scripts/run_complex_query_generation_probes.py` | 437 | Provider-backed synthesis probes for complex-query fixture (3 queries). |
| [run_complex_query_validation_delta](../scripts_run_complex_query_validation_delta.md) | `backend/scripts/run_complex_query_validation_delta.py` | 740 | Complex-query closeout validation delta (fixture-only). |
| [run_corpus_factory](../scripts_run_corpus_factory.md) | `backend/scripts/run_corpus_factory.py` | 98 | Corpus factory gate — serial corpus digest == overlapped corpus digest. |
| [run_deterministic_summary_materialization](../scripts_run_deterministic_summary_materialization.md) | `backend/scripts/run_deterministic_summary_materialization.py` | 232 | P3 — materialize the deterministic summary chain on a fixture corpus. |
| [run_eval_with_embedder_preflight](../scripts_run_eval_with_embedder_preflight.md) | `backend/scripts/run_eval_with_embedder_preflight.py` | 80 | Run an eval command only after the backend warms its MLX client pool. |
| [run_expanded_dark_canary](../scripts_run_expanded_dark_canary.md) | `backend/scripts/run_expanded_dark_canary.py` | 480 | Expanded dark canary — 50-query shadow suite across certified corpora. |
| [run_factory_e2e](../scripts_run_factory_e2e.md) | `backend/scripts/run_factory_e2e.py` | 263 | Factory E2E — production-shaped corpus through the REAL worker path. |
| [run_final_acceptance_v1](../scripts_run_final_acceptance_v1.md) | `backend/scripts/run_final_acceptance_v1.py` | 1525 | Run the single preregistered COMPLETE-pipeline final acceptance window. |
| [run_gold_entity_syntax_ceiling](../scripts_run_gold_entity_syntax_ceiling.md) | `backend/scripts/run_gold_entity_syntax_ceiling.py` | 304 | Measure the deterministic relation ceiling with exact gold entity spans. |
| [run_graph_semantic_e2e_phase2_plus](../scripts_run_graph_semantic_e2e_phase2_plus.md) | `backend/scripts/run_graph_semantic_e2e_phase2_plus.py` | 342 | Phases 2–6 on isolated graph_semantic_e2e fixture (no production mutation). |
| [run_graph_semantic_e2e_phase7_10](../scripts_run_graph_semantic_e2e_phase7_10.md) | `backend/scripts/run_graph_semantic_e2e_phase7_10.py` | 466 | Phases 7–10: retrieval, chat/SSE HTML probe, force-recreate replay, closeout. |
| [run_graphify_argument_adapter](../scripts_run_graphify_argument_adapter.md) | `backend/scripts/run_graphify_argument_adapter.py` | 54 | Classify persisted OpenIE arguments against completed entity mentions. |
| [run_graphify_assertion_assembler](../scripts_run_graphify_assertion_assembler.md) | `backend/scripts/run_graphify_assertion_assembler.py` | 46 | Assemble persisted OpenIE predicate candidates into authority lanes. |
| [run_graphify_census](../scripts_run_graphify_census.md) | `backend/scripts/run_graphify_census.py` | 66 | Run the isolated Graphify entity census over one or more Markdown files. |
| [run_graphify_completion](../scripts_run_graphify_completion.md) | `backend/scripts/run_graphify_completion.py` | 164 | Complete document mentions and evaluate the committed quality fixture. |
| [run_graphify_fixture_e2e](../scripts_run_graphify_fixture_e2e.md) | `backend/scripts/run_graphify_fixture_e2e.py` | 1137 | Run the committed Graphify fixtures through isolated live stores. |
| [run_graphify_openie](../scripts_run_graphify_openie.md) | `backend/scripts/run_graphify_openie.py` | 65 | Run Balanced CPU triplet-extract on persisted eligible units. |
| [run_graphify_predicate_compiler](../scripts_run_graphify_predicate_compiler.md) | `backend/scripts/run_graphify_predicate_compiler.py` | 50 | Compile persisted OpenIE proposition families into bounded predicates. |
| [run_graphify_proposition_reducer](../scripts_run_graphify_proposition_reducer.md) | `backend/scripts/run_graphify_proposition_reducer.py` | 48 | Collapse equivalent OpenIE renderings into provenance-preserving families. |
| [run_graphify_reducer](../scripts_run_graphify_reducer.md) | `backend/scripts/run_graphify_reducer.py` | 82 | Reduce persisted raw mentions into document-local entity clusters. |
| [run_graphify_relations](../scripts_run_graphify_relations.md) | `backend/scripts/run_graphify_relations.py` | 180 | Run the parse-once relation lane and score the committed quality fixture. |
| [run_graphify_stress_answer_key](../scripts_run_graphify_stress_answer_key.md) | `backend/scripts/run_graphify_stress_answer_key.py` | 292 | Run one Markdown stress fixture through Graphify and score its Neo4j projection. |
| [run_heldout_eval](../scripts_run_heldout_eval.md) | `backend/scripts/run_heldout_eval.py` | 385 | Run the held-out evaluation suite against the deployed backend (P1.1). |
| [run_openie_relation_kill_switch](../scripts_run_openie_relation_kill_switch.md) | `backend/scripts/run_openie_relation_kill_switch.py` | 605 | Compare syntax, triplet-extract, and their precision-gated union. |
| [run_oracle_adapter_qual](../scripts_run_oracle_adapter_qual.md) | `backend/scripts/run_oracle_adapter_qual.py` | 172 | Oracle-adapter qualification run (owner-authorized 2026-08-08). |
| [run_q8_parity_harness](../scripts_run_q8_parity_harness.md) | `backend/scripts/run_q8_parity_harness.py` | 473 | q8 (owner directive 2026-08-04) — canary parity harness. |
| [run_q9_quality_use_suite](../scripts_run_q9_quality_use_suite.md) | `backend/scripts/run_q9_quality_use_suite.py` | 389 | 15 real q9 questions — paired baseline vs candidate (DeepSeek Flash). |
| [run_relation_eligibility](../scripts_run_relation_eligibility.md) | `backend/scripts/run_relation_eligibility.py` | 92 | Persist relation-eligibility decisions for canonical Graphify documents. |
| [run_temporal_canonical_window](../scripts_run_temporal_canonical_window.md) | `backend/scripts/run_temporal_canonical_window.py` | 599 | Run the owner-bounded canonical temporal activation window. |
| [run_tier0_bridge_diagnostic](../scripts_run_tier0_bridge_diagnostic.md) | `backend/scripts/run_tier0_bridge_diagnostic.py` | 499 | Run the immutable six-query Tier-0 bridge diagnostic. |
| [run_two_lane_anchoring_ab](../scripts_run_two_lane_anchoring_ab.md) | `backend/scripts/run_two_lane_anchoring_ab.py` | 497 | Run one preregistered Agent-T arm against an already deployed backend. |
| [run_two_lane_canonical_window](../scripts_run_two_lane_canonical_window.md) | `backend/scripts/run_two_lane_canonical_window.py` | 819 | Run the owner-bounded canonical Agent-T verification window. |
| [run_two_lane_zero_provider_diagnosis](../scripts_run_two_lane_zero_provider_diagnosis.md) | `backend/scripts/run_two_lane_zero_provider_diagnosis.py` | 560 | Diagnose Agent-T determinism without reaching a provider call. |
| [run_waterfall_pressure_diagnostic](../scripts_run_waterfall_pressure_diagnostic.md) | `backend/scripts/run_waterfall_pressure_diagnostic.py` | 564 | Run the preregistered deterministic hydration-pressure diagnostic. |
| [s4_quarantine_stale](../scripts_s4_quarantine_stale.md) | `backend/scripts/s4_quarantine_stale.py` | 64 | S4 driver: quarantine capture-stale summaries (valid summary, no latent_concepts) so the s… |
| [semantic_gateway_mark_atomic_b4](../scripts_semantic_gateway_mark_atomic_b4.md) | `backend/scripts/semantic_gateway_mark_atomic_b4.py` | 607 | Execute the senior-authorized, noncanonical atomic-claims B4 canary. |
| [semantic_gateway_mark_atomic_preflight](../scripts_semantic_gateway_mark_atomic_preflight.md) | `backend/scripts/semantic_gateway_mark_atomic_preflight.py` | 699 | Build the zero-provider, read-only T9.3 B4 atomic-packet preflight. |
| [semantic_gateway_mark_paid_pass](../scripts_semantic_gateway_mark_paid_pass.md) | `backend/scripts/semantic_gateway_mark_paid_pass.py` | 2330 | Run the certified, noncanonical T9.3 semantic-digest paid pass. |
| [semantic_gateway_mark_prose_phase2](../scripts_semantic_gateway_mark_prose_phase2.md) | `backend/scripts/semantic_gateway_mark_prose_phase2.py` | 2357 | Run the owner-authorized B1-scoped mark Phase-2 prose purchase. |
| [semantic_gateway_mark_sentence_hybrid_canary](../scripts_semantic_gateway_mark_sentence_hybrid_canary.md) | `backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py` | 486 | Execute the exact senior-authorized sentence-hybrid v3 canary. |
| [semantic_gateway_mark_sentence_hybrid_preflight](../scripts_semantic_gateway_mark_sentence_hybrid_preflight.md) | `backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py` | 873 | Build the credential-blind, zero-provider sentence-hybrid v3 preflight. |
| [semantic_gateway_ugo_canary](../scripts_semantic_gateway_ugo_canary.md) | `backend/scripts/semantic_gateway_ugo_canary.py` | 1516 | Run the T4.4 structured-gateway canary on accepted UGO evidence. |
| [shadow_a_graph_release_gate_probe](../scripts_shadow_a_graph_release_gate_probe.md) | `backend/scripts/shadow_a_graph_release_gate_probe.py` | 609 | Shadow A probe for the canonical graph-write release gate. |
| [shadow_b_graph_release_gate_probe](../scripts_shadow_b_graph_release_gate_probe.md) | `backend/scripts/shadow_b_graph_release_gate_probe.py` | 524 | Shadow B probe for the canonical graph-write release gate. |
| [shadow_mode_corroboration](../scripts_shadow_mode_corroboration.md) | `backend/scripts/shadow_mode_corroboration.py` | 640 | Shadow-mode evaluation of the corroboration gate on 18 gold relations. |
| [soak_local_embedder](../scripts_soak_local_embedder.md) | `backend/scripts/soak_local_embedder.py` | 110 | Sustained, read-only query-embedding soak using the production client. |
| [unified_shadow_pipeline](../scripts_unified_shadow_pipeline.md) | `backend/scripts/unified_shadow_pipeline.py` | 1642 | Unified shadow pipeline: Relex Large + FrameExtractor union evaluation. |
| [verify_factory_equality](../scripts_verify_factory_equality.md) | `backend/scripts/verify_factory_equality.py` | 91 | Factory equality gate (owner-ratified 2026-08-08). |
| [verify_gliner2_cpu_provider](../scripts_verify_gliner2_cpu_provider.md) | `backend/scripts/verify_gliner2_cpu_provider.py` | 87 | Load the canonical entity provider twice and emit a runtime proof. |
| [verify_graphify_semantic_safety](../scripts_verify_graphify_semantic_safety.md) | `backend/scripts/verify_graphify_semantic_safety.py` | 199 | Verify strict frozen semantics without modifying the frozen scorer or policy. |