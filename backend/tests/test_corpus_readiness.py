from datetime import datetime, timedelta, timezone

from services.ingestion.readiness import (
    READINESS_SCHEMA_VERSION,
    _demote_stale_graph_job_counts,
    build_corpus_readiness_record,
    build_corpus_readiness_snapshot,
    neo4j_pressure_from_graph_promotion_jobs,
)
from services.ingestion.pressure import build_ingestion_pressure_snapshot
from services.ingestion.storage_pressure import (
    parse_memory_limit_bytes,
    qdrant_pressure_from_prometheus,
)


def test_readiness_reports_registered_and_excluded_document_counts():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-duplicates",
        document_counts={
            "total": 72,
            "registered_total": 75,
            "excluded_total": 3,
            "queryable": 72,
            "fully_enriched": 72,
            "verified": 72,
        },
        stage_counts={"fully_enriched": 72, "skipped_duplicate": 3},
        summary_counts={
            "retrieval_parent_total": 10,
            "retrieval_parent_done": 10,
            "document_done": 72,
            "document_synced_done": 72,
        },
        graph_counts={"required": True, "promoted": 72},
    )

    assert snapshot["status"] == "fully_enriched"
    assert snapshot["documents"]["total"] == 72
    assert snapshot["documents"]["registered_total"] == 75
    assert snapshot["documents"]["excluded_total"] == 3


def test_readiness_uses_retrieval_parent_summaries_as_primary_summary_gate():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 10, "queryable": 10, "fully_enriched": 10, "verified": 10},
        stage_counts={"fully_enriched": 10},
        chunk_counts={"total": 100, "docs_with_chunks": 10},
        summary_counts={
            "parent_total": 200,
            "parent_done": 50,
            "retrieval_parent_total": 80,
            "retrieval_parent_done": 80,
            # Legacy aliases should not override the explicit retrieval contract.
            "body_parent_total": 999,
            "body_parent_done": 0,
            "document_profile_done": 10,
            "document_tree_done": 10,
        },
        graph_counts={"promoted": 10},
        repair_counts={
            "provider_lane_health": {
                "status": "degraded",
                "cooldown_keys": ["longcat|openai/longcat-2.0"],
                "lanes": [],
            }
        },
    )

    assert snapshot["status"] == "fully_enriched"
    assert snapshot["summaries"]["retrieval_parent_total"] == 80
    assert snapshot["summaries"]["retrieval_parent_done"] == 80
    assert snapshot["summaries"]["retrieval_parent_missing"] == 0
    assert snapshot["summaries"]["body_parent_total"] == 999
    assert snapshot["summaries"]["body_parent_done"] == 0
    assert snapshot["summaries"]["body_parent_missing"] == 999
    assert snapshot["summaries"]["parent_total"] == 80
    assert snapshot["summaries"]["parent_done"] == 80
    assert snapshot["summaries"]["parent_missing"] == 0
    assert snapshot["summaries"]["all_parent_total"] == 200
    assert snapshot["summaries"]["all_parent_done"] == 50
    assert snapshot["summaries"]["all_parent_missing"] == 150
    assert snapshot["summaries"]["summary_excluded_parent_total"] == 120
    assert snapshot["summaries"]["summary_excluded_parent_done"] == 0
    assert snapshot["summaries"]["retrieval_parent_coverage"] == 1.0
    assert snapshot["summaries"]["body_parent_coverage"] == 0.0
    assert snapshot["summaries"]["parent_coverage"] == 1.0
    assert snapshot["summaries"]["all_parent_coverage"] == 0.25
    assert snapshot["pressure"]["queues"]["summary_missing"] == 0
    assert snapshot["repair"]["provider_lane_health"]["status"] == "degraded"
    assert snapshot["repair"]["provider_lane_health"]["cooldown_keys"] == [
        "longcat|openai/longcat-2.0"
    ]
    assert snapshot["summaries"]["scopes"]["primary"] == "retrieval_parent"
    assert snapshot["summaries"]["primary_parent_scope"] == "retrieval_parent"
    assert snapshot["summaries"]["parent_alias_scope"] == "retrieval_parent"
    assert snapshot["summaries"]["primary_parent_label"] == "Retrieval summaries"
    assert snapshot["summaries"]["scopes"]["retrieval_parent"]["readiness_gate"] is True
    assert snapshot["summaries"]["scopes"]["retrieval_parent"]["includes_chunk_kinds"] == [
        "body",
        "table",
    ]
    assert snapshot["summaries"]["scopes"]["body_parent"]["readiness_gate"] is False
    assert "diagnostic" in snapshot["summaries"]["scopes"]["body_parent"]["label"].lower()
    assert snapshot["summaries"]["scopes"]["all_parent"]["readiness_gate"] is False
    assert "diagnostic" in snapshot["summaries"]["scopes"]["all_parent"]["label"].lower()


def test_readiness_record_wraps_snapshot_for_materialized_view():
    record = build_corpus_readiness_record(
        {"corpus_id": "corpus-1", "status": "summaries_pending"},
        computed_at="2026-07-09T00:00:00Z",
    )

    assert record["schema_version"] == READINESS_SCHEMA_VERSION
    assert record["computed_at"] == "2026-07-09T00:00:00Z"
    assert record["source"] == "durable_artifacts"
    assert record["stale"] is False
    assert record["corpus_id"] == "corpus-1"
    assert record["status"] == "summaries_pending"
    assert "refresh_error" not in record


def test_readiness_record_marks_stale_refresh_failures():
    record = build_corpus_readiness_record(
        {"corpus_id": "corpus-1", "status": "graph_pending"},
        computed_at="2026-07-09T00:00:00Z",
        stale=True,
        refresh_error="mongo timeout",
    )

    assert record["schema_version"] == READINESS_SCHEMA_VERSION
    assert record["stale"] is True
    assert record["refresh_error"] == "mongo timeout"


def test_readiness_flags_stale_failure_metadata_ahead_of_failed_chunks():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 3, "queryable": 3, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2, "queryable_with_pending_graph": 1},
        summary_counts={
            "body_parent_total": 12,
            "body_parent_done": 12,
            "document_profile_done": 3,
            "document_tree_done": 3,
        },
        graph_counts={
            "promoted": 2,
            "pending": 1,
            "failed_chunks": 4,
            "stale_failure_docs": 1,
            "stale_failure_rows": 4,
            "stale_failure_reason_counts": {
                "stale_chunk_hash_mismatch": 3,
                "stale_extraction_contract_mismatch": 1,
            },
            "stale_failure_scan_limit": 5000,
            "stale_failure_scan_limited": False,
        },
    )

    assert snapshot["status"] == "needs_reconciliation"
    assert "stale_failure_metadata" in snapshot["blocking"]
    assert snapshot["graph"]["stale_failure_rows"] == 4
    assert snapshot["graph"]["stale_failure_reason_counts"] == {
        "stale_chunk_hash_mismatch": 3,
        "stale_extraction_contract_mismatch": 1,
    }
    assert snapshot["graph"]["stale_failure_scan_limit"] == 5000
    assert snapshot["graph"]["stale_failure_scan_limited"] is False
    assert snapshot["next_actions"][0]["id"] == "reconcile_stale_failures"
    assert snapshot["next_actions"][0]["severity"] == "critical"
    assert snapshot["next_actions"][0]["count"] == 4


def test_readiness_distinguishes_summary_pending_from_queryability():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 4, "queryable": 4, "fully_enriched": 2, "verified": 2},
        stage_counts={"queryable_with_pending_summary": 2, "fully_enriched": 2},
        summary_counts={
            "parent_total": 40,
            "parent_done": 20,
            "body_parent_total": 24,
            "body_parent_done": 12,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 4},
    )

    assert snapshot["documents"]["queryable"] == 4
    assert snapshot["status"] == "summaries_pending"
    assert "retrieval_parent_summaries_pending" in snapshot["blocking"]
    assert "document_summaries_pending" in snapshot["blocking"]
    assert snapshot["next_actions"][0]["id"] == "run_summary_jobs"
    assert snapshot["next_actions"][0]["lane"] == "summary"
    assert snapshot["next_actions"][0]["count"] == 14


def test_readiness_document_summaries_use_union_and_surface_drift():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 4, "queryable": 4, "fully_enriched": 3, "verified": 3},
        stage_counts={"queryable_with_pending_summary": 1, "fully_enriched": 3},
        summary_counts={
            "retrieval_parent_total": 16,
            "retrieval_parent_done": 16,
            "body_parent_total": 16,
            "body_parent_done": 16,
            "document_done": 3,
            "document_profile_done": 2,
            "document_tree_done": 2,
            "document_both_done": 1,
            "document_profile_only": 1,
            "document_tree_only": 1,
            "document_mismatch": 2,
        },
        graph_counts={"promoted": 4},
    )

    assert snapshot["status"] == "summaries_pending"
    assert snapshot["summaries"]["document_done"] == 3
    assert snapshot["summaries"]["document_missing"] == 1
    assert snapshot["summaries"]["document_synced_done"] == 1
    assert snapshot["summaries"]["document_sync_missing"] == 3
    assert snapshot["summaries"]["document_profile_done"] == 2
    assert snapshot["summaries"]["document_tree_done"] == 2
    assert snapshot["summaries"]["document_both_done"] == 1
    assert snapshot["summaries"]["document_profile_only"] == 1
    assert snapshot["summaries"]["document_tree_only"] == 1
    assert snapshot["summaries"]["document_mismatch"] == 2
    assert "document_summaries_pending" in snapshot["blocking"]
    assert snapshot["next_actions"][0]["id"] == "run_summary_jobs"
    assert snapshot["next_actions"][0]["count"] == 3


def test_readiness_does_not_mark_document_summary_drift_complete():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "retrieval_parent_total": 8,
            "retrieval_parent_done": 8,
            "document_done": 2,
            "document_profile_done": 1,
            "document_tree_done": 1,
            "document_both_done": 0,
            "document_profile_only": 1,
            "document_tree_only": 1,
            "document_mismatch": 2,
        },
        graph_counts={"promoted": 2, "pending": 0},
    )

    assert snapshot["summaries"]["document_missing"] == 0
    assert snapshot["summaries"]["document_sync_missing"] == 2
    assert snapshot["status"] == "summaries_pending"
    assert "document_summaries_pending" in snapshot["blocking"]


def test_readiness_blocks_on_pending_extraction_jobs():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 2, "pending": 0},
        repair_counts={"extraction_jobs": {"queued": 3, "running": 1}},
    )

    assert snapshot["status"] == "extraction_pending"
    assert "extraction_jobs_pending" in snapshot["blocking"]
    assert snapshot["repair"]["extraction_jobs_pending"] == 4
    assert snapshot["repair"]["extraction_jobs_failed"] == 0
    assert snapshot["next_actions"][0]["id"] == "run_extraction_jobs"
    assert snapshot["next_actions"][0]["count"] == 4


def test_readiness_surfaces_document_pipeline_job_queue():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 1, "fully_enriched": 1, "verified": 1},
        stage_counts={"queryable": 1, "chunks_saved": 1},
        summary_counts={
            "body_parent_total": 4,
            "body_parent_done": 4,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 1, "pending": 0},
        repair_counts={
            "document_pipeline_jobs": {
                "queued": 2,
                "blocked_no_source": 1,
                "blocked_mongo_state": 1,
            }
        },
    )

    assert snapshot["status"] == "needs_repair"
    assert "document_pipeline_jobs_pending" in snapshot["blocking"]
    assert "document_pipeline_jobs_blocked" in snapshot["blocking"]
    assert snapshot["repair"]["document_pipeline_jobs_pending"] == 2
    assert snapshot["repair"]["document_pipeline_jobs_failed"] == 2


def test_readiness_surfaces_source_parse_job_queue_before_documents_exist():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 0},
        repair_counts={
            "source_parse_jobs": {
                "queued": 3,
                "blocked_source_missing": 1,
            }
        },
    )

    assert snapshot["status"] == "needs_repair"
    assert "source_parse_jobs_pending" in snapshot["blocking"]
    assert "source_parse_jobs_blocked" in snapshot["blocking"]
    assert snapshot["repair"]["source_parse_jobs_pending"] == 3
    assert snapshot["repair"]["source_parse_jobs_failed"] == 1


def test_readiness_blocks_on_failed_extraction_jobs():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 2, "pending": 0},
        repair_counts={
            "extraction_jobs": {
                "provider_failed": 2,
                "validation_failed": 1,
                "failed": 1,
                "blocked_provider_contract": 3,
            }
        },
    )

    assert snapshot["status"] == "needs_repair"
    assert "extraction_jobs_need_retry" in snapshot["blocking"]
    assert "extraction_jobs_blocked_provider_contract" in snapshot["blocking"]
    assert snapshot["repair"]["extraction_jobs_pending"] == 0
    assert snapshot["repair"]["extraction_jobs_failed"] == 4
    assert snapshot["repair"]["extraction_jobs_blocked"] == 3
    assert any(
        action["id"] == "fix_extraction_provider_contract"
        for action in snapshot["next_actions"]
    )


def test_readiness_provider_contract_blocks_are_not_retry_actions():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 1, "queryable": 1, "fully_enriched": 1, "verified": 1},
        stage_counts={"fully_enriched": 1},
        summary_counts={
            "body_parent_total": 4,
            "body_parent_done": 4,
            "document_profile_done": 1,
            "document_tree_done": 1,
        },
        graph_counts={"promoted": 1, "pending": 0},
        repair_counts={"extraction_jobs": {"blocked_provider_contract": 2}},
    )

    assert snapshot["status"] == "needs_repair"
    assert "extraction_jobs_need_retry" not in snapshot["blocking"]
    assert "extraction_jobs_blocked_provider_contract" in snapshot["blocking"]
    assert snapshot["repair"]["extraction_jobs_failed"] == 0
    assert snapshot["repair"]["extraction_jobs_blocked"] == 2
    assert [action["id"] for action in snapshot["next_actions"]] == [
        "fix_extraction_provider_contract"
    ]


def test_readiness_surfaces_summary_job_queue_and_blocks():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 6,
            "document_profile_done": 1,
            "document_tree_done": 1,
        },
        graph_counts={"promoted": 2, "pending": 0},
        repair_counts={
            "summary_jobs": {
                "queued": 3,
                "blocked_parent_summaries_incomplete": 1,
                "blocked_no_parent_summaries": 1,
            }
        },
    )

    assert snapshot["status"] == "summaries_pending"
    assert "summary_jobs_pending" in snapshot["blocking"]
    assert "summary_jobs_waiting_dependencies" in snapshot["blocking"]
    assert "summary_jobs_blocked" not in snapshot["blocking"]
    assert snapshot["repair"]["summary_jobs_pending"] == 5
    assert snapshot["repair"]["summary_jobs_waiting_dependencies"] == 2
    assert snapshot["repair"]["summary_jobs_failed"] == 0


def test_readiness_treats_true_summary_job_failures_as_repair_blocking():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 2, "pending": 0},
        repair_counts={
            "summary_jobs": {
                "blocked_empty_source": 1,
                "failed": 1,
                "blocked_parent_summaries_incomplete": 3,
            }
        },
    )

    assert snapshot["status"] == "needs_repair"
    assert "summary_jobs_blocked" in snapshot["blocking"]
    assert "summary_jobs_waiting_dependencies" in snapshot["blocking"]
    assert snapshot["repair"]["summary_jobs_pending"] == 3
    assert snapshot["repair"]["summary_jobs_waiting_dependencies"] == 3
    assert snapshot["repair"]["summary_jobs_failed"] == 2


def test_readiness_surfaces_duplicate_source_identity_for_review():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 4, "queryable": 4, "fully_enriched": 4, "verified": 4},
        stage_counts={"fully_enriched": 4},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 4,
            "document_tree_done": 4,
        },
        graph_counts={"promoted": 4, "pending": 0},
        idempotency_counts={
            "source_keyed_documents": 3,
            "content_hash_documents": 2,
            "duplicate_source_key_groups": 1,
            "duplicate_source_key_docs": 2,
            "duplicate_content_hash_groups": 1,
            "duplicate_content_hash_docs": 2,
        },
    )

    assert snapshot["status"] == "needs_review"
    assert "source_identity_missing" in snapshot["blocking"]
    assert "duplicate_source_identity" in snapshot["blocking"]
    assert snapshot["idempotency"]["missing_source_identity"] == 1
    assert snapshot["idempotency"]["duplicate_source_key_groups"] == 1
    assert snapshot["idempotency"]["duplicate_content_hash_docs"] == 2


def test_readiness_separates_source_key_collisions_from_exact_duplicates():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 4, "queryable": 4, "fully_enriched": 4, "verified": 4},
        stage_counts={"fully_enriched": 4},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 4,
            "document_tree_done": 4,
        },
        graph_counts={"promoted": 4, "pending": 0},
        idempotency_counts={
            "source_keyed_documents": 4,
            "content_hash_documents": 4,
            "duplicate_source_key_groups": 2,
            "duplicate_source_key_docs": 6,
            "source_key_collision_groups": 2,
            "source_key_collision_docs": 6,
            "duplicate_content_hash_groups": 0,
            "duplicate_content_hash_docs": 0,
        },
    )

    assert snapshot["status"] == "needs_review"
    assert "source_identity_collision" in snapshot["blocking"]
    assert "duplicate_source_identity" not in snapshot["blocking"]
    assert snapshot["idempotency"]["source_key_collision_groups"] == 2
    actions = {row["id"]: row for row in snapshot["next_actions"]}
    assert "repair_source_identity_collisions" in actions
    assert "audit_duplicate_sources" not in actions


def test_readiness_flags_missing_stage_identity_for_retryable_jobs():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 2, "pending": 0},
        idempotency_counts={
            "source_keyed_documents": 2,
            "content_hash_documents": 2,
            "source_parse_jobs_missing_stage_identity": 6,
            "document_pipeline_jobs_missing_stage_identity": 1,
            "extraction_jobs_missing_stage_identity": 2,
            "summary_jobs_missing_stage_identity": 3,
            "graph_promotion_jobs_missing_stage_identity": 4,
            "ghost_b_extractions_missing_stage_identity": 5,
            "stage_identity_missing_total": 21,
        },
    )

    assert snapshot["status"] == "needs_review"
    assert "stage_identity_missing" in snapshot["blocking"]
    assert snapshot["idempotency"]["stage_identity_missing_total"] == 21
    assert snapshot["idempotency"]["source_parse_jobs_missing_stage_identity"] == 6
    assert snapshot["idempotency"]["document_pipeline_jobs_missing_stage_identity"] == 1
    assert snapshot["idempotency"]["extraction_jobs_missing_stage_identity"] == 2
    assert snapshot["idempotency"]["summary_jobs_missing_stage_identity"] == 3
    assert snapshot["idempotency"]["graph_promotion_jobs_missing_stage_identity"] == 4
    assert snapshot["idempotency"]["ghost_b_extractions_missing_stage_identity"] == 5


def test_readiness_treats_legacy_ok_extraction_identity_as_diagnostic_debt():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 2, "pending": 0},
        idempotency_counts={
            "source_keyed_documents": 2,
            "content_hash_documents": 2,
            "ghost_b_extractions_missing_stage_identity": 100,
            "ghost_b_extractions_missing_stage_identity_blocking": 0,
            "ghost_b_extractions_missing_stage_identity_legacy_ok": 100,
            "stage_identity_missing_total": 100,
            "stage_identity_blocking_total": 0,
        },
    )

    assert snapshot["status"] == "fully_enriched"
    assert "stage_identity_missing" not in snapshot["blocking"]
    assert snapshot["idempotency"]["stage_identity_missing_total"] == 100
    assert snapshot["idempotency"]["stage_identity_blocking_total"] == 0
    actions = {row["id"]: row for row in snapshot["next_actions"]}
    assert "backfill_legacy_extraction_artifact_identity" in actions
    assert "repair_stage_identity" not in actions


def test_readiness_treats_nonactionable_extraction_job_identity_as_diagnostic_debt():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"promoted": 2, "pending": 0},
        idempotency_counts={
            "source_keyed_documents": 2,
            "content_hash_documents": 2,
            "extraction_jobs_missing_stage_identity": 22,
            "extraction_jobs_missing_stage_identity_blocking": 0,
            "extraction_jobs_missing_stage_identity_nonblocking": 22,
            "stage_identity_missing_total": 22,
            "stage_identity_blocking_total": 0,
        },
    )

    assert snapshot["status"] == "fully_enriched"
    assert "stage_identity_missing" not in snapshot["blocking"]
    assert snapshot["idempotency"]["extraction_jobs_missing_stage_identity"] == 22
    assert snapshot["idempotency"]["extraction_jobs_missing_stage_identity_blocking"] == 0
    assert snapshot["idempotency"]["extraction_jobs_missing_stage_identity_nonblocking"] == 22
    actions = {row["id"]: row for row in snapshot["next_actions"]}
    assert "backfill_legacy_extraction_job_identity" in actions
    assert "repair_stage_identity" not in actions


def test_readiness_does_not_block_on_graph_when_graph_is_not_required():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2, "fully_enriched": 2, "verified": 2},
        stage_counts={"fully_enriched": 2},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 2,
            "document_tree_done": 2,
        },
        graph_counts={"required": False, "promoted": 0, "pending": 2},
    )

    assert snapshot["status"] == "fully_enriched"
    assert snapshot["graph"]["required"] is False
    assert snapshot["graph"]["pending"] == 0
    assert "graph_promotion_pending" not in snapshot["blocking"]


def test_readiness_treats_unmarked_promoted_extractions_as_metadata_drift():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 4, "queryable": 4, "fully_enriched": 4, "verified": 4},
        stage_counts={"fully_enriched": 4},
        summary_counts={
            "body_parent_total": 8,
            "body_parent_done": 8,
            "document_profile_done": 4,
            "document_tree_done": 4,
        },
        graph_counts={
            "promoted": 4,
            "pending": 0,
            "unmarked_promoted_extraction_docs": 2,
            "unmarked_promoted_extraction_rows": 9,
        },
    )

    assert snapshot["status"] == "fully_enriched"
    assert "graph_promotion_pending" not in snapshot["blocking"]
    assert snapshot["graph"]["pending"] == 0
    assert snapshot["graph"]["unmarked_promoted_extraction_docs"] == 2
    assert snapshot["graph"]["unmarked_promoted_extraction_rows"] == 9


def test_readiness_includes_pressure_snapshot_from_repair_backlog():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 2, "queryable": 2},
        summary_counts={"body_parent_total": 8, "body_parent_done": 4},
        repair_counts={
            "active_runs": 1,
            "graph_promotion_jobs": {"queued": 3},
            "extraction_jobs": {"provider_failed": 5},
        },
    )

    assert snapshot["pressure"]["status"] == "elevated"
    assert snapshot["pressure"]["queues"]["active_repairs"] == 1
    assert snapshot["pressure"]["queues"]["graph_pending"] == 3
    assert snapshot["pressure"]["queues"]["extraction_pending"] == 5
    assert snapshot["pressure"]["queues"]["summary_missing"] == 6


def test_stale_graph_jobs_do_not_count_as_actionable_pressure():
    graph_jobs = _demote_stale_graph_job_counts(
        {"queued": 5, "running": 2, "done": 10},
        [{"_id": "queued", "count": 5}, {"_id": "running", "count": 1}],
    )

    assert graph_jobs["queued"] == 0
    assert graph_jobs["queued_stale"] == 5
    assert graph_jobs["running"] == 1
    assert graph_jobs["running_stale"] == 1
    assert graph_jobs["done"] == 10

    pressure = build_ingestion_pressure_snapshot(graph_jobs=graph_jobs)
    assert pressure["queues"]["graph_pending"] == 1


def test_fully_stale_graph_jobs_stay_inspectable_without_repair_pressure():
    graph_jobs = _demote_stale_graph_job_counts(
        {"queued": 45, "noop": 55},
        [{"_id": "queued", "count": 45}],
    )

    assert graph_jobs == {"queued": 0, "noop": 55, "queued_stale": 45}
    pressure = build_ingestion_pressure_snapshot(graph_jobs=graph_jobs)
    assert pressure["queues"]["graph_pending"] == 0
    assert pressure["status"] == "normal"
    assert "continue_incremental_repair" not in pressure["recommendations"]


def test_readiness_next_actions_mark_pressure_blocked_lanes():
    snapshot = build_corpus_readiness_snapshot(
        corpus_id="corpus-1",
        document_counts={"total": 1, "queryable": 1, "fully_enriched": 0},
        graph_counts={"promoted": 0, "pending": 1, "failed_chunks": 3},
        repair_counts={"extraction_jobs": {"provider_failed": 2}},
        pressure={
            "status": "high",
            "backpressure": {
                "extraction_backfill_allowed": False,
                "graph_promotion_allowed": False,
            },
        },
    )

    extraction_action = next(
        action for action in snapshot["next_actions"] if action["id"] == "run_extraction_jobs"
    )
    graph_action = next(
        action for action in snapshot["next_actions"] if action["id"] == "run_graph_jobs"
    )
    assert extraction_action["blocked_by_pressure"] is True
    assert graph_action["blocked_by_pressure"] is True


def test_pressure_snapshot_blocks_backfills_when_rss_is_high():
    pressure = build_ingestion_pressure_snapshot(
        backend_rss_mb=900,
        ram_cap_mb=1000,
        rss_soft_limit_mb=850,
        summary_missing=10,
    )

    assert pressure["status"] == "high"
    assert "backend_rss_over_soft_limit" in pressure["reasons"]
    assert pressure["backpressure"]["source_parse_allowed"] is False
    assert pressure["backpressure"]["summary_backfill_allowed"] is False
    assert pressure["backpressure"]["extraction_backfill_allowed"] is False
    assert pressure["backpressure"]["graph_promotion_allowed"] is False


def test_pressure_snapshot_elevates_when_mongo_storage_nears_stop_limit():
    pressure = build_ingestion_pressure_snapshot(
        backend_rss_mb=200,
        ram_cap_mb=1000,
        rss_soft_limit_mb=850,
        mongo_stats={"fsUsedSize": 86, "fsTotalSize": 100},
        mongo_storage_warn_ratio=0.85,
        mongo_storage_stop_ratio=0.90,
    )

    assert pressure["status"] == "elevated"
    assert "mongo_storage_near_stop_limit" in pressure["reasons"]
    assert "run_bounded_repairs_only" in pressure["recommendations"]
    assert pressure["storage"]["mongo_fs_pressure"] == 0.86
    assert pressure["backpressure"]["source_parse_allowed"] is True
    assert pressure["backpressure"]["summary_backfill_allowed"] is True
    assert pressure["backpressure"]["extraction_backfill_allowed"] is True
    assert pressure["backpressure"]["graph_promotion_allowed"] is True


def test_pressure_snapshot_blocks_backfills_when_mongo_storage_is_high():
    pressure = build_ingestion_pressure_snapshot(
        backend_rss_mb=200,
        ram_cap_mb=1000,
        rss_soft_limit_mb=850,
        mongo_stats={"fsUsedSize": 91, "fsTotalSize": 100},
        mongo_storage_warn_ratio=0.85,
        mongo_storage_stop_ratio=0.90,
        summary_missing=10,
    )

    assert pressure["status"] == "high"
    assert "mongo_storage_over_stop_limit" in pressure["reasons"]
    assert "pause_nonessential_backfills" in pressure["recommendations"]
    assert "free_mongo_storage_or_expand_volume" in pressure["recommendations"]
    assert pressure["storage"]["mongo_fs_pressure"] == 0.91
    assert pressure["backpressure"]["source_parse_allowed"] is False
    assert pressure["backpressure"]["document_pipeline_allowed"] is False
    assert pressure["backpressure"]["summary_backfill_allowed"] is False
    assert pressure["backpressure"]["extraction_backfill_allowed"] is False
    assert pressure["backpressure"]["graph_promotion_allowed"] is False


def test_pressure_snapshot_blocks_qdrant_write_lanes_without_pausing_extraction():
    pressure = build_ingestion_pressure_snapshot(
        backend_rss_mb=200,
        ram_cap_mb=1000,
        rss_soft_limit_mb=850,
        qdrant_pressure={"queue_depth": 5_001},
    )

    assert pressure["status"] == "high"
    assert "qdrant_write_pressure_high" in pressure["reasons"]
    assert "pause_qdrant_indexing" in pressure["recommendations"]
    assert pressure["writers"]["qdrant"]["status"] == "high"
    assert pressure["writers"]["qdrant"]["queue_depth"] == 5_001
    assert pressure["backpressure"]["source_parse_allowed"] is True
    assert pressure["backpressure"]["document_pipeline_allowed"] is False
    assert pressure["backpressure"]["summary_generation_allowed"] is True
    assert pressure["backpressure"]["summary_indexing_allowed"] is False
    assert pressure["backpressure"]["summary_backfill_allowed"] is True
    assert pressure["backpressure"]["extraction_backfill_allowed"] is True
    assert pressure["backpressure"]["graph_promotion_allowed"] is True


def test_qdrant_prometheus_metrics_feed_writer_pressure():
    payload = qdrant_pressure_from_prometheus(
        """
        # HELP collections_total number of collections
        collections_total 12
        collections_vector_total 123456
        collection_update_queue_length{id="corpus_a_naive"} 4
        collection_update_queue_length{id="corpus_a_hrag"} 7
        collection_update_queue_deferred_points{id="corpus_a_naive"} 3
        rest_responses_avg_duration_seconds{method="PUT",endpoint="/collections/{collection_name}/points",status="200"} 6.25
        rest_responses_avg_duration_seconds{method="GET",endpoint="/collections/{collection_name}",status="200"} 0.01
        """
    )

    assert payload["source"] == "qdrant_metrics"
    assert payload["queue_depth"] == 14
    assert payload["max_queue_depth"] == 7
    assert payload["deferred_points"] == 3
    assert payload["collections_total"] == 12
    assert payload["vectors_total"] == 123456
    assert payload["write_latency_ms"] == 6250.0

    pressure = build_ingestion_pressure_snapshot(qdrant_pressure=payload)
    assert pressure["status"] == "high"
    assert pressure["writers"]["qdrant"]["source"] == "qdrant_metrics"
    assert pressure["writers"]["qdrant"]["vectors_total"] == 123456
    assert pressure["backpressure"]["document_pipeline_allowed"] is False
    assert pressure["backpressure"]["summary_generation_allowed"] is True
    assert pressure["backpressure"]["summary_indexing_allowed"] is False
    assert pressure["backpressure"]["summary_backfill_allowed"] is True


def test_qdrant_memory_metrics_pause_vector_write_lanes():
    payload = qdrant_pressure_from_prometheus(
        """
        memory_resident_bytes 950
        memory_allocated_bytes 900
        memory_active_bytes 875
        memory_retained_bytes 1200
        collection_update_queue_length{id="corpus_a_naive"} 0
        """,
        memory_limit_bytes=1000,
        memory_warn_ratio=0.85,
        memory_stop_ratio=0.90,
    )

    assert payload["status"] == "high"
    assert payload["reasons"] == ["qdrant_memory_over_stop_limit"]
    assert payload["memory_pressure"] == 0.95
    assert payload["memory_limit_bytes"] == 1000

    pressure = build_ingestion_pressure_snapshot(qdrant_pressure=payload)
    assert pressure["status"] == "high"
    assert "qdrant_write_pressure_high" in pressure["reasons"]
    assert "qdrant_memory_over_stop_limit" in pressure["writers"]["qdrant"]["reasons"]
    assert pressure["writers"]["qdrant"]["memory_pressure"] == 0.95
    assert pressure["backpressure"]["document_pipeline_allowed"] is False
    assert pressure["backpressure"]["summary_generation_allowed"] is True
    assert pressure["backpressure"]["summary_indexing_allowed"] is False
    assert pressure["backpressure"]["summary_backfill_allowed"] is True
    assert pressure["backpressure"]["extraction_backfill_allowed"] is True


def test_parse_qdrant_memory_limit_strings():
    assert parse_memory_limit_bytes("5g") == 5 * 1024**3
    assert parse_memory_limit_bytes("512MiB") == 512 * 1024**2
    assert parse_memory_limit_bytes("") is None


def test_pressure_snapshot_blocks_neo4j_promotion_without_pausing_summaries():
    pressure = build_ingestion_pressure_snapshot(
        backend_rss_mb=200,
        ram_cap_mb=1000,
        rss_soft_limit_mb=850,
        neo4j_pressure={"write_latency_ms": 12_000},
    )

    assert pressure["status"] == "high"
    assert "neo4j_write_pressure_high" in pressure["reasons"]
    assert "pause_neo4j_promotion" in pressure["recommendations"]
    assert pressure["writers"]["neo4j"]["status"] == "high"
    assert pressure["writers"]["neo4j"]["write_latency_ms"] == 12_000
    assert pressure["backpressure"]["source_parse_allowed"] is True
    assert pressure["backpressure"]["document_pipeline_allowed"] is True
    assert pressure["backpressure"]["summary_backfill_allowed"] is True
    assert pressure["backpressure"]["extraction_backfill_allowed"] is True
    assert pressure["backpressure"]["graph_promotion_allowed"] is False


def test_graph_promotion_latency_feeds_neo4j_pressure_signal():
    payload = neo4j_pressure_from_graph_promotion_jobs(
        {"queued": 2, "running": 1, "done": 10},
        [
            {"neo4j_write_latency_ms": 12_500},
            {"neo4j_write_latency_ms": 2_000},
        ],
    )

    assert payload["source"] == "graph_promotion_jobs"
    assert payload["queue_depth"] == 3
    assert payload["sample_size"] == 2
    assert payload["write_latency_ms"] == 12_500
    assert payload["latest_write_latency_ms"] == 12_500
    assert payload["avg_write_latency_ms"] == 7_250

    pressure = build_ingestion_pressure_snapshot(neo4j_pressure=payload)
    assert pressure["status"] == "high"
    assert "neo4j_write_pressure_high" in pressure["reasons"]
    assert pressure["writers"]["neo4j"]["source"] == "graph_promotion_jobs"
    assert pressure["writers"]["neo4j"]["sample_size"] == 2
    assert pressure["writers"]["neo4j"]["avg_write_latency_ms"] == 7_250
    assert pressure["backpressure"]["graph_promotion_allowed"] is False


def test_graph_promotion_pressure_expires_stale_latency_samples():
    now = datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc)
    payload = neo4j_pressure_from_graph_promotion_jobs(
        {"queued": 2, "done": 10},
        [
            {
                "neo4j_write_latency_ms": 87_000,
                "updated_at": now - timedelta(minutes=10),
            },
            {
                "neo4j_write_latency_ms": 250,
                "updated_at": now - timedelta(seconds=30),
            },
        ],
        now=now,
    )

    assert payload["sample_size"] == 1
    assert payload["write_latency_ms"] == 250
    pressure = build_ingestion_pressure_snapshot(neo4j_pressure=payload)
    assert pressure["writers"]["neo4j"]["status"] == "normal"
    assert pressure["backpressure"]["graph_promotion_allowed"] is True
