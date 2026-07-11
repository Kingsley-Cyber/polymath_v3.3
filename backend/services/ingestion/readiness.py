"""Computed corpus-readiness contract.

Batch rows are run history. This module derives the current corpus truth from
durable Mongo artifacts so the API/UI can answer what is queryable, what still
needs repair, and which summary layer is actually retrieval-critical.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import get_settings
from services.ingestion.failure_reconciliation import classify_stale_failure_rows
from services.ingestion.pressure import build_ingestion_pressure_snapshot
from services.ingestion.provider_lane_health import load_recent_provider_lane_health
from services.ingestion.resource_planner import current_process_rss_mb, memory_soft_limit_mb
from services.ingestion.section_classifier import (
    PARENT_SUMMARY_KINDS,
    parent_summary_required_clause,
)
from services.ingestion.storage_pressure import sample_qdrant_pressure
from services.ingestion.storage_pressure import parse_memory_limit_bytes
from services.storage.record_status import with_active_records

READINESS_SCHEMA_VERSION = "corpus_readiness.v1"
READINESS_COLLECTION = "corpus_readiness"
RETRIEVAL_PARENT_SUMMARY_CLAUSE: dict[str, Any] = parent_summary_required_clause()
# Pure body-prose diagnostic. Retrieval readiness is broader: body prose,
# tables, and legacy rows with missing chunk_kind.
BODY_PARENT_CLAUSE: dict[str, Any] = {"chunk_kind": "body"}
SUMMARY_SCOPES: dict[str, Any] = {
    "primary": "retrieval_parent",
    "retrieval_parent": {
        "label": "Retrieval summaries",
        "description": (
            "Canonical parent-summary rows that count toward corpus readiness: "
            "body/table parents plus legacy parent rows with no chunk_kind. "
            "This is the retrieval-readiness metric, not a body-only count."
        ),
        "readiness_gate": True,
        "includes_chunk_kinds": list(PARENT_SUMMARY_KINDS),
        "includes_missing_chunk_kind": True,
    },
    "body_parent": {
        "label": "Body-only summaries (diagnostic)",
        "description": (
            "Diagnostic subset where chunk_kind is exactly body. Retrieval "
            "readiness also includes eligible table and legacy parent rows."
        ),
        "readiness_gate": False,
        "includes_chunk_kinds": ["body"],
        "includes_missing_chunk_kind": False,
    },
    "all_parent": {
        "label": "All parent rows (diagnostic)",
        "description": (
            "Diagnostic count across every structural parent row. This includes "
            "navigation, bibliography, index, appendix, links, and code rows "
            "that are not part of the retrieval-summary readiness gate."
        ),
        "readiness_gate": False,
    },
    "document": {
        "label": "Document summaries",
        "description": (
            "Whole-document summaries built after retrieval parent summaries "
            "are available."
        ),
        "readiness_gate": True,
    },
}
STALE_FAILURE_READINESS_SCAN_LIMIT = 5000
NEO4J_PRESSURE_SAMPLE_MAX_AGE_SECONDS = 300
SUMMARY_TEXT_CLAUSE: dict[str, Any] = {
    "summary": {"$exists": True, "$nin": [None, ""]}
}
DOCUMENT_SUMMARY_CLAUSE: dict[str, Any] = {
    "doc_profile.summary": {"$exists": True, "$nin": [None, ""]}
}
STAGE_IDENTITY_MISSING_CLAUSE: dict[str, Any] = {
    "$or": [
        {"stage_identity": {"$exists": False}},
        {"stage_identity": None},
        {"stage_identity.identity_version": {"$exists": False}},
        {"stage_identity.identity_version": None},
        {"stage_identity.identity_version": ""},
    ]
}
SOURCE_KEY_IDENTITY_FIELDS: tuple[str, ...] = ("source_key", "source_identity.source_key")
CONTENT_HASH_IDENTITY_FIELDS: tuple[str, ...] = (
    "source_identity.content_sha256",
    "content_sha256",
    "source_file_hash",
)
FAILED_STAGES = {
    "failed",
    "setup_failed",
    "chunk_failed",
}
QUERYABLE_STAGES = {
    "complete",
    "fully_enriched",
    "queryable_with_pending_graph",
    "queryable_with_pending_summary",
    "queryable_with_pending_summary_and_graph",
}
FULLY_ENRICHED_STAGES = {"complete", "fully_enriched"}
EXCLUDED_DOCUMENT_STAGES = {"skipped_duplicate"}
EXTRACTION_PENDING_JOB_STATUSES = ("queued", "running")
EXTRACTION_FAILED_JOB_STATUSES = (
    "provider_failed",
    "validation_failed",
    "failed",
    "dead_letter",
)
EXTRACTION_BLOCKED_JOB_STATUSES = ("blocked_provider_contract",)
EXTRACTION_IDENTITY_BLOCKING_STATUSES = (
    *EXTRACTION_PENDING_JOB_STATUSES,
    *EXTRACTION_FAILED_JOB_STATUSES,
    *EXTRACTION_BLOCKED_JOB_STATUSES,
)
SUMMARY_PENDING_JOB_STATUSES = ("queued", "running")
SUMMARY_WAITING_JOB_STATUSES = (
    "blocked_no_parent_summaries",
    "blocked_parent_summaries_incomplete",
)
SUMMARY_FAILED_JOB_STATUSES = ("failed", "blocked_empty_source", "dead_letter")
DOCUMENT_PIPELINE_PENDING_JOB_STATUSES = ("queued", "running")
DOCUMENT_PIPELINE_FAILED_JOB_STATUSES = (
    "failed",
    "blocked_no_source",
    "blocked_missing_chunks",
    "blocked_mongo_state",
    "dead_letter",
)
SOURCE_PARSE_PENDING_JOB_STATUSES = ("queued", "running")
SOURCE_PARSE_FAILED_JOB_STATUSES = (
    "failed",
    "failed_recoverable",
    "blocked_source_missing",
    "dead_letter",
)


def _coverage(done: int, total: int) -> float:
    if total <= 0:
        return 1.0
    return round(max(0, min(done, total)) / total, 4)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_any_identity_field_query(fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        "$or": [
            {field: {"$exists": True, "$nin": [None, ""]}}
            for field in fields
        ]
    }


def _first_non_empty_identity_expr(fields: tuple[str, ...]) -> Any:
    expr: Any = None
    for field in reversed(fields):
        expr = {
            "$cond": [
                {
                    "$and": [
                        {"$ne": [f"${field}", None]},
                        {"$ne": [f"${field}", ""]},
                    ]
                },
                f"${field}",
                expr,
            ]
        }
    return expr


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def neo4j_pressure_from_graph_promotion_jobs(
    graph_jobs: dict[str, int] | None,
    recent_jobs: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    max_sample_age_seconds: int = NEO4J_PRESSURE_SAMPLE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Derive Neo4j writer pressure from recent durable promotion attempts.

    Direct Neo4j metrics are not always available to the backend. Promotion
    jobs already execute the real graph flush path, so their observed latency is
    a useful control-plane signal for bounded repair/backpressure decisions.
    """

    queue_depth = sum(
        _int((graph_jobs or {}).get(status))
        for status in ("queued", "running")
    )
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    max_age = max(int(max_sample_age_seconds or 0), 0)
    latencies: list[float] = []
    for row in recent_jobs or []:
        sampled_at = row.get("updated_at")
        if isinstance(sampled_at, datetime) and max_age > 0:
            if sampled_at.tzinfo is None:
                sampled_at = sampled_at.replace(tzinfo=timezone.utc)
            age_seconds = (current_time - sampled_at).total_seconds()
            if age_seconds > max_age:
                continue
        try:
            value = float(row.get("neo4j_write_latency_ms"))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            latencies.append(value)

    if not queue_depth and not latencies:
        return {}

    payload: dict[str, Any] = {
        "source": "graph_promotion_jobs",
        "queue_depth": queue_depth,
        "sample_size": len(latencies),
    }
    if latencies:
        payload.update(
            {
                "write_latency_ms": max(latencies),
                "latest_write_latency_ms": latencies[0],
                "max_write_latency_ms": max(latencies),
                "avg_write_latency_ms": round(sum(latencies) / len(latencies), 2),
            }
        )
    return payload


def _demote_stale_graph_job_counts(
    graph_jobs: dict[str, int] | None,
    stale_rows: list[dict[str, Any]] | None,
) -> dict[str, int]:
    """Move stale queued/running graph jobs out of actionable queue counts.

    Durable job rows are audit history. Corpus readiness is the current artifact
    truth. If a queued graph-promotion job points at a document that is already
    Neo4j-written, it must not inflate UI badges, pressure queues, or repair
    prompts. Preserve the count under ``queued_stale``/``running_stale`` so the
    stale work remains inspectable without looking actionable.
    """

    counts = {str(key): _int(value) for key, value in (graph_jobs or {}).items()}
    for row in stale_rows or []:
        status = str(row.get("_id") or row.get("status") or "")
        if status not in {"queued", "running"}:
            continue
        stale_count = _int(row.get("count"))
        if stale_count <= 0:
            continue
        raw_count = _int(counts.get(status))
        demoted = min(raw_count, stale_count)
        if demoted <= 0:
            continue
        counts[status] = max(raw_count - demoted, 0)
        stale_key = f"{status}_stale"
        counts[stale_key] = _int(counts.get(stale_key)) + demoted
    return counts


def _readiness_action(
    *,
    action_id: str,
    label: str,
    lane: str,
    severity: str,
    reason: str,
    count: int = 0,
    blocked_by_pressure: bool = False,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "lane": lane,
        "severity": severity,
        "reason": reason,
        "count": max(_int(count), 0),
        "blocked_by_pressure": bool(blocked_by_pressure),
    }


def build_corpus_readiness_record(
    snapshot: dict[str, Any],
    *,
    computed_at: str | None = None,
    stale: bool = False,
    refresh_error: str | None = None,
) -> dict[str, Any]:
    """Wrap a readiness snapshot as the durable materialized view record."""

    record = dict(snapshot or {})
    record["schema_version"] = READINESS_SCHEMA_VERSION
    record["computed_at"] = computed_at or _utcnow_iso()
    record["source"] = "durable_artifacts"
    record["stale"] = bool(stale)
    if refresh_error:
        record["refresh_error"] = str(refresh_error)[:500]
    else:
        record.pop("refresh_error", None)
    return record


def build_corpus_readiness_snapshot(
    *,
    corpus_id: str,
    document_counts: dict[str, Any] | None = None,
    stage_counts: dict[str, int] | None = None,
    chunk_counts: dict[str, int] | None = None,
    summary_counts: dict[str, int] | None = None,
    graph_counts: dict[str, Any] | None = None,
    idempotency_counts: dict[str, int] | None = None,
    repair_counts: dict[str, Any] | None = None,
    pressure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize raw counts into the public readiness contract.

    Kept pure so tests can pin labels/status without a Mongo fixture.
    """

    document_counts = document_counts or {}
    stage_counts = stage_counts or {}
    chunk_counts = chunk_counts or {}
    summary_counts = summary_counts or {}
    graph_counts = graph_counts or {}
    idempotency_counts = idempotency_counts or {}
    repair_counts = repair_counts or {}
    graph_required = bool(graph_counts.get("required", True))

    doc_total = _int(document_counts.get("total"))
    registered_doc_total = _int(document_counts.get("registered_total", doc_total))
    excluded_doc_total = _int(
        document_counts.get(
            "excluded_total",
            max(registered_doc_total - doc_total, 0),
        )
    )
    queryable = max(
        _int(document_counts.get("queryable")),
        sum(_int(stage_counts.get(stage)) for stage in QUERYABLE_STAGES),
    )
    fully_enriched = max(
        _int(document_counts.get("fully_enriched")),
        sum(_int(stage_counts.get(stage)) for stage in FULLY_ENRICHED_STAGES),
    )
    verified = _int(document_counts.get("verified"))
    failed_docs = max(
        _int(document_counts.get("failed")),
        sum(_int(stage_counts.get(stage)) for stage in FAILED_STAGES),
    )

    all_parent_total = _int(
        summary_counts.get("all_parent_total", summary_counts.get("parent_total"))
    )
    all_parent_done = _int(
        summary_counts.get("all_parent_done", summary_counts.get("parent_done"))
    )
    retrieval_parent_total = _int(
        summary_counts.get("retrieval_parent_total", summary_counts.get("body_parent_total"))
    )
    retrieval_parent_done = _int(
        summary_counts.get("retrieval_parent_done", summary_counts.get("body_parent_done"))
    )
    body_parent_total = _int(summary_counts.get("body_parent_total", retrieval_parent_total))
    body_parent_done = _int(summary_counts.get("body_parent_done", retrieval_parent_done))
    document_profile_done = _int(summary_counts.get("document_profile_done"))
    document_tree_done = _int(summary_counts.get("document_tree_done"))
    document_done = _int(
        summary_counts.get(
            "document_done",
            max(document_profile_done, document_tree_done),
        )
    )
    document_profile_only = _int(summary_counts.get("document_profile_only"))
    document_tree_only = _int(summary_counts.get("document_tree_only"))
    document_both_done = _int(
        summary_counts.get(
            "document_both_done",
            max(document_profile_done, document_tree_done),
        )
    )
    document_synced_done = _int(
        summary_counts.get(
            "document_synced_done",
            document_both_done,
        )
    )
    document_mismatch = _int(
        summary_counts.get(
            "document_mismatch",
            document_profile_only + document_tree_only,
        )
    )

    unpromoted_extraction_docs = _int(graph_counts.get("unpromoted_extraction_docs"))
    unpromoted_extraction_rows = _int(graph_counts.get("unpromoted_extraction_rows"))
    unmarked_promoted_extraction_docs = _int(
        graph_counts.get("unmarked_promoted_extraction_docs")
    )
    unmarked_promoted_extraction_rows = _int(
        graph_counts.get("unmarked_promoted_extraction_rows")
    )
    graph_pending = _int(graph_counts.get("pending")) if graph_required else 0
    graph_failed_docs = _int(graph_counts.get("failed_docs"))
    failed_chunks = _int(graph_counts.get("failed_chunks"))
    stale_failure_docs = _int(graph_counts.get("stale_failure_docs"))
    stale_failure_rows = _int(graph_counts.get("stale_failure_rows"))
    stale_failure_reason_counts = {
        str(key): _int(value)
        for key, value in (graph_counts.get("stale_failure_reason_counts") or {}).items()
    }
    reconciled_stale_failure_docs = _int(graph_counts.get("reconciled_stale_failure_docs"))
    reconciled_stale_failure_rows = _int(graph_counts.get("reconciled_stale_failure_rows"))
    orphaned_failure_docs = _int(graph_counts.get("orphaned_failure_docs"))
    active_repairs = _int(repair_counts.get("active_runs"))
    graph_jobs = repair_counts.get("graph_promotion_jobs") or {}
    extraction_jobs = repair_counts.get("extraction_jobs") or {}
    provider_lane_health = repair_counts.get("provider_lane_health") or {}
    summary_jobs = repair_counts.get("summary_jobs") or {}
    document_pipeline_jobs = repair_counts.get("document_pipeline_jobs") or {}
    source_parse_jobs = repair_counts.get("source_parse_jobs") or {}
    provider_efficiency = repair_counts.get("provider_efficiency") or {}
    scheduler_state = repair_counts.get("scheduler_state") or {}
    queue_telemetry = repair_counts.get("queue_telemetry") or {}
    source_parse_pending_jobs = sum(
        _int(source_parse_jobs.get(status)) for status in SOURCE_PARSE_PENDING_JOB_STATUSES
    )
    source_parse_failed_jobs = sum(
        _int(source_parse_jobs.get(status)) for status in SOURCE_PARSE_FAILED_JOB_STATUSES
    )
    extraction_pending_jobs = sum(_int(extraction_jobs.get(status)) for status in EXTRACTION_PENDING_JOB_STATUSES)
    extraction_failed_jobs = sum(_int(extraction_jobs.get(status)) for status in EXTRACTION_FAILED_JOB_STATUSES)
    extraction_blocked_jobs = sum(
        _int(extraction_jobs.get(status)) for status in EXTRACTION_BLOCKED_JOB_STATUSES
    )
    summary_waiting_jobs = sum(_int(summary_jobs.get(status)) for status in SUMMARY_WAITING_JOB_STATUSES)
    summary_pending_jobs = (
        sum(_int(summary_jobs.get(status)) for status in SUMMARY_PENDING_JOB_STATUSES)
        + summary_waiting_jobs
    )
    summary_failed_jobs = sum(_int(summary_jobs.get(status)) for status in SUMMARY_FAILED_JOB_STATUSES)
    document_pipeline_pending_jobs = sum(
        _int(document_pipeline_jobs.get(status))
        for status in DOCUMENT_PIPELINE_PENDING_JOB_STATUSES
    )
    document_pipeline_failed_jobs = sum(
        _int(document_pipeline_jobs.get(status))
        for status in DOCUMENT_PIPELINE_FAILED_JOB_STATUSES
    )
    duplicate_source_key_groups = _int(idempotency_counts.get("duplicate_source_key_groups"))
    duplicate_source_key_docs = _int(idempotency_counts.get("duplicate_source_key_docs"))
    source_key_collision_groups = _int(idempotency_counts.get("source_key_collision_groups"))
    source_key_collision_docs = _int(idempotency_counts.get("source_key_collision_docs"))
    duplicate_content_hash_groups = _int(idempotency_counts.get("duplicate_content_hash_groups"))
    duplicate_content_hash_docs = _int(idempotency_counts.get("duplicate_content_hash_docs"))
    missing_source_identity = (
        max(doc_total - _int(idempotency_counts.get("source_keyed_documents")), 0)
        if "source_keyed_documents" in idempotency_counts
        else 0
    )
    missing_stage_identity = _int(idempotency_counts.get("stage_identity_missing_total"))
    blocking_stage_identity = _int(
        idempotency_counts.get("stage_identity_blocking_total", missing_stage_identity)
    )
    legacy_extraction_artifact_identity = _int(
        idempotency_counts.get("ghost_b_extractions_missing_stage_identity_legacy_ok")
    )
    legacy_extraction_job_identity = _int(
        idempotency_counts.get("extraction_jobs_missing_stage_identity_nonblocking")
    )

    retrieval_missing = max(retrieval_parent_total - retrieval_parent_done, 0)
    body_missing = max(body_parent_total - body_parent_done, 0)
    parent_total = retrieval_parent_total
    parent_done = retrieval_parent_done
    parent_missing = retrieval_missing
    all_parent_missing = max(all_parent_total - all_parent_done, 0)
    excluded_parent_total = max(all_parent_total - retrieval_parent_total, 0)
    excluded_parent_done = max(all_parent_done - retrieval_parent_done, 0)
    document_missing = max(doc_total - document_done, 0)
    document_sync_missing = max(doc_total - document_synced_done, 0)
    exact_duplicate_source_key_groups = max(
        duplicate_source_key_groups - source_key_collision_groups,
        0,
    )

    blocking: list[str] = []
    if stale_failure_docs or stale_failure_rows or orphaned_failure_docs:
        blocking.append("stale_failure_metadata")
    if source_parse_failed_jobs:
        blocking.append("source_parse_jobs_blocked")
    if source_parse_pending_jobs:
        blocking.append("source_parse_jobs_pending")
    if document_pipeline_failed_jobs:
        blocking.append("document_pipeline_jobs_blocked")
    if document_pipeline_pending_jobs:
        blocking.append("document_pipeline_jobs_pending")
    if failed_docs or graph_failed_docs or failed_chunks:
        blocking.append("failed_extractions")
    if extraction_failed_jobs:
        blocking.append("extraction_jobs_need_retry")
    if extraction_blocked_jobs:
        blocking.append("extraction_jobs_blocked_provider_contract")
    if extraction_pending_jobs:
        blocking.append("extraction_jobs_pending")
    if summary_failed_jobs:
        blocking.append("summary_jobs_blocked")
    if summary_pending_jobs:
        blocking.append("summary_jobs_pending")
    if summary_waiting_jobs:
        blocking.append("summary_jobs_waiting_dependencies")
    if missing_source_identity:
        blocking.append("source_identity_missing")
    if blocking_stage_identity:
        blocking.append("stage_identity_missing")
    if exact_duplicate_source_key_groups or duplicate_content_hash_groups:
        blocking.append("duplicate_source_identity")
    if source_key_collision_groups:
        blocking.append("source_identity_collision")
    if graph_pending:
        blocking.append("graph_promotion_pending")
    if retrieval_missing:
        blocking.append("retrieval_parent_summaries_pending")
    if document_sync_missing:
        blocking.append("document_summaries_pending")

    if doc_total == 0:
        if source_parse_failed_jobs:
            status = "needs_repair"
        elif source_parse_pending_jobs:
            status = "ingestion_pending"
        else:
            status = "empty"
    elif stale_failure_docs or stale_failure_rows or orphaned_failure_docs:
        status = "needs_reconciliation"
    elif source_parse_failed_jobs:
        status = "needs_repair"
    elif source_parse_pending_jobs:
        status = "ingestion_pending"
    elif document_pipeline_failed_jobs:
        status = "needs_repair"
    elif document_pipeline_pending_jobs:
        status = "ingestion_pending"
    elif failed_docs or graph_failed_docs or failed_chunks:
        status = "needs_repair"
    elif extraction_failed_jobs:
        status = "needs_repair"
    elif extraction_blocked_jobs:
        status = "needs_repair"
    elif (
        extraction_pending_jobs
        and not (failed_docs or graph_failed_docs or failed_chunks or extraction_failed_jobs)
    ):
        status = "extraction_pending"
    elif summary_failed_jobs:
        status = "needs_repair"
    elif (
        duplicate_source_key_groups
        or duplicate_content_hash_groups
        or source_key_collision_groups
        or missing_source_identity
        or blocking_stage_identity
    ):
        status = "needs_review"
    elif graph_pending:
        status = "graph_pending"
    elif retrieval_missing or document_sync_missing:
        status = "summaries_pending"
    elif fully_enriched >= doc_total:
        status = "fully_enriched"
    elif queryable > 0:
        status = "queryable_partial"
    else:
        status = "not_ready"

    pressure_snapshot = pressure or build_ingestion_pressure_snapshot(
        active_repairs=active_repairs,
        graph_jobs=graph_jobs,
        extraction_jobs=extraction_jobs,
        summary_missing=retrieval_missing + document_sync_missing,
    )
    backpressure = pressure_snapshot.get("backpressure") or {}
    next_actions: list[dict[str, Any]] = []
    if stale_failure_docs or stale_failure_rows or orphaned_failure_docs:
        next_actions.append(
            _readiness_action(
                action_id="reconcile_stale_failures",
                label="Reconcile stale failures",
                lane="repair",
                severity="critical",
                reason="Failure metadata no longer matches live chunks.",
                count=stale_failure_rows + orphaned_failure_docs,
            )
        )
    if source_parse_failed_jobs:
        next_actions.append(
            _readiness_action(
                action_id="repair_source_parse_jobs",
                label="Repair source jobs",
                lane="source_parse",
                severity="critical",
                reason="Source/parse jobs are blocked or failed.",
                count=source_parse_failed_jobs,
            )
        )
    if source_parse_pending_jobs:
        next_actions.append(
            _readiness_action(
                action_id="run_source_parse_jobs",
                label="Run source jobs",
                lane="source_parse",
                severity="warning",
                reason="Documents still need parse/chunk work.",
                count=source_parse_pending_jobs,
            )
        )
    if document_pipeline_failed_jobs:
        next_actions.append(
            _readiness_action(
                action_id="repair_document_pipeline_jobs",
                label="Repair pipeline jobs",
                lane="document_pipeline",
                severity="critical",
                reason="Document-stage jobs are blocked or failed.",
                count=document_pipeline_failed_jobs,
                blocked_by_pressure=backpressure.get("document_pipeline_allowed") is False,
            )
        )
    if document_pipeline_pending_jobs:
        next_actions.append(
            _readiness_action(
                action_id="run_document_pipeline_jobs",
                label="Run pipeline jobs",
                lane="document_pipeline",
                severity="warning",
                reason="Chunk/persist/embed document-stage jobs are queued.",
                count=document_pipeline_pending_jobs,
                blocked_by_pressure=backpressure.get("document_pipeline_allowed") is False,
            )
        )
    if failed_docs or graph_failed_docs or failed_chunks or extraction_failed_jobs:
        next_actions.append(
            _readiness_action(
                action_id="run_extraction_jobs",
                label="Retry extraction jobs",
                lane="extraction",
                severity="critical",
                reason="Failed extraction chunks need bounded retry.",
                count=failed_chunks + extraction_failed_jobs,
                blocked_by_pressure=backpressure.get("extraction_backfill_allowed") is False,
            )
        )
    if extraction_blocked_jobs:
        next_actions.append(
            _readiness_action(
                action_id="fix_extraction_provider_contract",
                label="Fix extraction provider contract",
                lane="extraction",
                severity="critical",
                reason=(
                    "One or more extraction lanes rejected the selected structured-output "
                    "contract; update the provider route or schema mode before retrying."
                ),
                count=extraction_blocked_jobs,
            )
        )
    elif extraction_pending_jobs:
        next_actions.append(
            _readiness_action(
                action_id="run_extraction_jobs",
                label="Run extraction jobs",
                lane="extraction",
                severity="warning",
                reason="Chunk extraction jobs are queued or running.",
                count=extraction_pending_jobs,
                blocked_by_pressure=backpressure.get("extraction_backfill_allowed") is False,
            )
        )
    if summary_failed_jobs:
        next_actions.append(
            _readiness_action(
                action_id="repair_summary_jobs",
                label="Repair summary jobs",
                lane="summary",
                severity="critical",
                reason="Summary jobs are blocked or failed.",
                count=summary_failed_jobs,
                blocked_by_pressure=backpressure.get("summary_backfill_allowed") is False,
            )
        )
    if retrieval_missing or document_sync_missing or summary_pending_jobs:
        next_actions.append(
            _readiness_action(
                action_id="run_summary_jobs",
                label="Run summary jobs",
                lane="summary",
                severity="warning",
                reason=(
                    "Retrieval-parent summaries or synchronized document-summary "
                    "artifacts are incomplete."
                ),
                count=retrieval_missing + document_sync_missing + summary_pending_jobs,
                blocked_by_pressure=backpressure.get("summary_backfill_allowed") is False,
            )
        )
    if graph_pending:
        next_actions.append(
            _readiness_action(
                action_id="run_graph_jobs",
                label="Run graph jobs",
                lane="graph",
                severity="warning",
                reason="Queryable documents still need Neo4j graph promotion.",
                count=graph_pending,
                blocked_by_pressure=backpressure.get("graph_promotion_allowed") is False,
            )
        )
    if exact_duplicate_source_key_groups or duplicate_content_hash_groups:
        next_actions.append(
            _readiness_action(
                action_id="audit_duplicate_sources",
                label="Audit duplicate sources",
                lane="idempotency",
                severity="review",
                reason="Duplicate source identity groups need review.",
                count=exact_duplicate_source_key_groups + duplicate_content_hash_groups,
            )
        )
    if source_key_collision_groups:
        next_actions.append(
            _readiness_action(
                action_id="repair_source_identity_collisions",
                label="Repair source identity collisions",
                lane="idempotency",
                severity="review",
                reason=(
                    "Some documents share a source_key but have different content "
                    "hashes; do not reuse artifacts until source identity is repaired."
                ),
                count=source_key_collision_groups,
            )
        )
    if missing_source_identity or blocking_stage_identity:
        next_actions.append(
            _readiness_action(
                action_id="repair_stage_identity",
                label="Repair identity metadata",
                lane="idempotency",
                severity="review",
                reason="Documents or active stage jobs are missing deterministic identity.",
                count=missing_source_identity + blocking_stage_identity,
            )
        )
    if legacy_extraction_artifact_identity:
        next_actions.append(
            _readiness_action(
                action_id="backfill_legacy_extraction_artifact_identity",
                label="Backfill legacy extraction artifact identity",
                lane="idempotency",
                severity="info",
                reason=(
                    "Successful legacy Ghost B artifacts are missing stage_identity; "
                    "they are diagnostic debt, not an active retry blocker."
                ),
                count=legacy_extraction_artifact_identity,
            )
        )
    if legacy_extraction_job_identity:
        next_actions.append(
            _readiness_action(
                action_id="backfill_legacy_extraction_job_identity",
                label="Backfill legacy extraction job identity",
                lane="idempotency",
                severity="info",
                reason=(
                    "Non-actionable extraction job history is missing stage_identity; "
                    "it is diagnostic debt, not an active retry blocker."
                ),
                count=legacy_extraction_job_identity,
            )
        )

    return {
        "corpus_id": corpus_id,
        "status": status,
        "blocking": blocking,
        "next_actions": next_actions,
        "documents": {
            "total": doc_total,
            "registered_total": registered_doc_total,
            "excluded_total": excluded_doc_total,
            "queryable": queryable,
            "fully_enriched": fully_enriched,
            "verified": verified,
            "failed": failed_docs,
            "coverage": _coverage(queryable, doc_total),
            "fully_enriched_coverage": _coverage(fully_enriched, doc_total),
            "stage_counts": dict(sorted(stage_counts.items())),
        },
        "chunks": {
            "total": _int(chunk_counts.get("total")),
            "docs_with_chunks": _int(chunk_counts.get("docs_with_chunks")),
        },
        "summaries": {
            "scopes": SUMMARY_SCOPES,
            "primary_parent_scope": "retrieval_parent",
            "primary_parent_label": SUMMARY_SCOPES["retrieval_parent"]["label"],
            "parent_alias_scope": "retrieval_parent",
            # Canonical parent-summary readiness is the retrieval-required set,
            # not every structural parent row. Use all_parent_* for diagnostics.
            "parent_total": parent_total,
            "parent_done": parent_done,
            "parent_missing": parent_missing,
            "parent_coverage": _coverage(parent_done, parent_total),
            "all_parent_total": all_parent_total,
            "all_parent_done": all_parent_done,
            "all_parent_missing": all_parent_missing,
            "all_parent_coverage": _coverage(all_parent_done, all_parent_total),
            "summary_excluded_parent_total": excluded_parent_total,
            "summary_excluded_parent_done": excluded_parent_done,
            "retrieval_parent_total": retrieval_parent_total,
            "retrieval_parent_done": retrieval_parent_done,
            "retrieval_parent_missing": retrieval_missing,
            "retrieval_parent_coverage": _coverage(
                retrieval_parent_done, retrieval_parent_total
            ),
            "body_parent_total": body_parent_total,
            "body_parent_done": body_parent_done,
            "body_parent_missing": body_missing,
            "body_parent_coverage": _coverage(body_parent_done, body_parent_total),
            "document_total": doc_total,
            # Usable means either document-summary artifact exists. Synced means
            # the writer's intended pair exists in both summary_tree and
            # documents.doc_profile, and this strict count gates readiness.
            "document_done": document_done,
            "document_missing": document_missing,
            "document_coverage": _coverage(document_done, doc_total),
            "document_synced_done": document_synced_done,
            "document_sync_missing": document_sync_missing,
            "document_sync_coverage": _coverage(document_synced_done, doc_total),
            "document_profile_done": document_profile_done,
            "document_tree_done": document_tree_done,
            "document_both_done": document_both_done,
            "document_profile_only": document_profile_only,
            "document_tree_only": document_tree_only,
            "document_mismatch": document_mismatch,
        },
        "graph": {
            "required": graph_required,
            "promoted": _int(graph_counts.get("promoted")),
            "pending": graph_pending,
            "unpromoted_extraction_docs": unpromoted_extraction_docs,
            "unpromoted_extraction_rows": unpromoted_extraction_rows,
            "unmarked_promoted_extraction_docs": unmarked_promoted_extraction_docs,
            "unmarked_promoted_extraction_rows": unmarked_promoted_extraction_rows,
            "failed_docs": graph_failed_docs,
            "failed_chunks": failed_chunks,
            "failure_docs": _int(graph_counts.get("failure_docs")),
            "failure_rows": _int(graph_counts.get("failure_rows")),
            "stale_failure_docs": stale_failure_docs,
            "stale_failure_rows": stale_failure_rows,
            "stale_failure_reason_counts": dict(sorted(stale_failure_reason_counts.items())),
            "stale_failure_scan_limit": _int(graph_counts.get("stale_failure_scan_limit")),
            "stale_failure_scan_limited": bool(graph_counts.get("stale_failure_scan_limited")),
            "reconciled_stale_failure_docs": reconciled_stale_failure_docs,
            "reconciled_stale_failure_rows": reconciled_stale_failure_rows,
            "orphaned_failure_docs": orphaned_failure_docs,
        },
        "idempotency": {
            "source_keyed_documents": _int(idempotency_counts.get("source_keyed_documents")),
            "content_hash_documents": _int(idempotency_counts.get("content_hash_documents")),
            "missing_source_identity": missing_source_identity,
            "stage_identity_missing_total": missing_stage_identity,
            "stage_identity_blocking_total": blocking_stage_identity,
            "source_parse_jobs_missing_stage_identity": _int(
                idempotency_counts.get("source_parse_jobs_missing_stage_identity")
            ),
            "document_pipeline_jobs_missing_stage_identity": _int(
                idempotency_counts.get("document_pipeline_jobs_missing_stage_identity")
            ),
            "extraction_jobs_missing_stage_identity": _int(
                idempotency_counts.get("extraction_jobs_missing_stage_identity")
            ),
            "extraction_jobs_missing_stage_identity_blocking": _int(
                idempotency_counts.get("extraction_jobs_missing_stage_identity_blocking")
            ),
            "extraction_jobs_missing_stage_identity_nonblocking": legacy_extraction_job_identity,
            "summary_jobs_missing_stage_identity": _int(
                idempotency_counts.get("summary_jobs_missing_stage_identity")
            ),
            "graph_promotion_jobs_missing_stage_identity": _int(
                idempotency_counts.get("graph_promotion_jobs_missing_stage_identity")
            ),
            "ghost_b_extractions_missing_stage_identity": _int(
                idempotency_counts.get("ghost_b_extractions_missing_stage_identity")
            ),
            "ghost_b_extractions_missing_stage_identity_blocking": _int(
                idempotency_counts.get("ghost_b_extractions_missing_stage_identity_blocking")
            ),
            "ghost_b_extractions_missing_stage_identity_legacy_ok": legacy_extraction_artifact_identity,
            "duplicate_source_key_groups": duplicate_source_key_groups,
            "duplicate_source_key_docs": duplicate_source_key_docs,
            "source_key_collision_groups": source_key_collision_groups,
            "source_key_collision_docs": source_key_collision_docs,
            "duplicate_content_hash_groups": duplicate_content_hash_groups,
            "duplicate_content_hash_docs": duplicate_content_hash_docs,
        },
        "repair": {
            "active_runs": active_repairs,
            "source_parse_jobs": source_parse_jobs,
            "source_parse_jobs_pending": source_parse_pending_jobs,
            "source_parse_jobs_failed": source_parse_failed_jobs,
            "graph_promotion_jobs": graph_jobs,
            "extraction_jobs": extraction_jobs,
            "extraction_jobs_pending": extraction_pending_jobs,
            "extraction_jobs_failed": extraction_failed_jobs,
            "extraction_jobs_blocked": extraction_blocked_jobs,
            "provider_lane_health": provider_lane_health,
            "summary_jobs": summary_jobs,
            "summary_jobs_pending": summary_pending_jobs,
            "summary_jobs_waiting_dependencies": summary_waiting_jobs,
            "summary_jobs_failed": summary_failed_jobs,
            "document_pipeline_jobs": document_pipeline_jobs,
            "document_pipeline_jobs_pending": document_pipeline_pending_jobs,
            "document_pipeline_jobs_failed": document_pipeline_failed_jobs,
            "provider_efficiency": provider_efficiency,
            "scheduler": scheduler_state,
            "queue_telemetry": queue_telemetry,
            "latest_runs": repair_counts.get("latest_runs") or [],
        },
        "pressure": pressure
        or pressure_snapshot,
    }


async def compute_corpus_readiness(db: Any, corpus_id: str) -> dict[str, Any]:
    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "default_ingestion_config.use_neo4j": 1},
    )
    graph_required = bool(
        ((corpus or {}).get("default_ingestion_config") or {}).get("use_neo4j", True)
    )
    registered_doc_match = with_active_records({"corpus_id": corpus_id})
    doc_match = with_active_records(
        {
            "corpus_id": corpus_id,
            "ingest_stage": {"$nin": sorted(EXCLUDED_DOCUMENT_STAGES)},
        }
    )
    docs_summary = await db["documents"].aggregate([
        {"$match": doc_match},
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "queryable": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$write_state.mongo_written", True]},
                                    {"$eq": ["$write_state.qdrant_written", True]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "fully_enriched": {
                    "$sum": {
                        "$cond": [
                            {
                                "$or": [
                                    {"$eq": ["$write_state.verified", True]},
                                    {"$in": ["$ingest_stage", list(FULLY_ENRICHED_STAGES)]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "verified": {
                    "$sum": {
                        "$cond": [{"$eq": ["$write_state.verified", True]}, 1, 0]
                    }
                },
                "failed": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$ingest_stage", list(FAILED_STAGES)]},
                            1,
                            0,
                        ]
                    }
                },
                "graph_promoted": {
                    "$sum": {
                        "$cond": [{"$eq": ["$write_state.neo4j_written", True]}, 1, 0]
                    }
                },
                "graph_pending": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$write_state.qdrant_written", True]},
                                    {"$ne": ["$write_state.neo4j_written", True]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "summary_indexed": {
                    "$sum": {
                        "$cond": [{"$eq": ["$write_state.summaries_indexed", True]}, 1, 0]
                    }
                },
                "failed_chunks": {"$sum": {"$ifNull": ["$ghost_b_failure_count", 0]}},
                "failure_docs": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$ghost_b_failure_count", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "document_profile_done": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$doc_profile.summary", None]},
                                    {"$ne": ["$doc_profile.summary", ""]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]).to_list(length=1)
    doc_counts = docs_summary[0] if docs_summary else {"total": 0}
    registered_doc_total = await db["documents"].count_documents(registered_doc_match)
    doc_counts["registered_total"] = registered_doc_total
    doc_counts["excluded_total"] = max(
        registered_doc_total - _int(doc_counts.get("total")),
        0,
    )

    stage_rows = await db["documents"].aggregate([
        {"$match": registered_doc_match},
        {"$group": {"_id": {"$ifNull": ["$ingest_stage", "unknown"]}, "count": {"$sum": 1}}},
    ]).to_list(length=None)
    stage_counts = {str(row["_id"]): _int(row.get("count")) for row in stage_rows}

    chunk_total = await db["chunks"].count_documents(with_active_records({"corpus_id": corpus_id}))
    chunk_doc_rows = await db["chunks"].aggregate([
        {"$match": with_active_records({"corpus_id": corpus_id})},
        {"$group": {"_id": "$doc_id"}},
        {"$group": {"_id": None, "count": {"$sum": 1}}},
    ]).to_list(length=1)
    chunk_counts = {
        "total": chunk_total,
        "docs_with_chunks": _int(chunk_doc_rows[0].get("count") if chunk_doc_rows else 0),
    }

    parent_query = with_active_records({"corpus_id": corpus_id})
    retrieval_parent_query = with_active_records(
        {"corpus_id": corpus_id, "$and": [RETRIEVAL_PARENT_SUMMARY_CLAUSE]}
    )
    body_parent_query = with_active_records({"corpus_id": corpus_id, "$and": [BODY_PARENT_CLAUSE]})
    parent_done_query = with_active_records({"corpus_id": corpus_id, **SUMMARY_TEXT_CLAUSE})
    retrieval_parent_done_query = with_active_records(
        {
            "corpus_id": corpus_id,
            "$and": [RETRIEVAL_PARENT_SUMMARY_CLAUSE, SUMMARY_TEXT_CLAUSE],
        }
    )
    body_parent_done_query = with_active_records(
        {"corpus_id": corpus_id, "$and": [BODY_PARENT_CLAUSE, SUMMARY_TEXT_CLAUSE]}
    )
    document_summary_rows = await db["documents"].aggregate([
        {"$match": doc_match},
        {
            "$lookup": {
                "from": "summary_tree",
                "let": {"doc_id": "$doc_id", "corpus_id": "$corpus_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$doc_id", "$$doc_id"]},
                                    {"$eq": ["$corpus_id", "$$corpus_id"]},
                                    {"$eq": ["$node_type", "document"]},
                                ]
                            },
                            "summary": {"$exists": True, "$nin": [None, ""]},
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "document_summary_tree",
            }
        },
        {
            "$project": {
                "profile_done": {
                    "$and": [
                        {"$ne": ["$doc_profile.summary", None]},
                        {"$ne": ["$doc_profile.summary", ""]},
                    ]
                },
                "tree_done": {
                    "$gt": [{"$size": "$document_summary_tree"}, 0]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "document_done": {
                    "$sum": {
                        "$cond": [
                            {"$or": ["$profile_done", "$tree_done"]},
                            1,
                            0,
                        ]
                    }
                },
                "document_profile_done": {
                    "$sum": {"$cond": ["$profile_done", 1, 0]}
                },
                "document_tree_done": {
                    "$sum": {"$cond": ["$tree_done", 1, 0]}
                },
                "document_both_done": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$profile_done", "$tree_done"]},
                            1,
                            0,
                        ]
                    }
                },
                "document_profile_only": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$profile_done", {"$not": ["$tree_done"]}]},
                            1,
                            0,
                        ]
                    }
                },
                "document_tree_only": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$not": ["$profile_done"]}, "$tree_done"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]).to_list(length=1)
    document_summary_counts = document_summary_rows[0] if document_summary_rows else {}
    all_parent_total = await db["parent_chunks"].count_documents(parent_query)
    all_parent_done = await db["parent_chunks"].count_documents(parent_done_query)
    retrieval_parent_total = await db["parent_chunks"].count_documents(retrieval_parent_query)
    retrieval_parent_done = await db["parent_chunks"].count_documents(
        retrieval_parent_done_query
    )
    summary_counts = {
        "parent_total": retrieval_parent_total,
        "parent_done": retrieval_parent_done,
        "all_parent_total": all_parent_total,
        "all_parent_done": all_parent_done,
        "retrieval_parent_total": retrieval_parent_total,
        "retrieval_parent_done": retrieval_parent_done,
        "body_parent_total": await db["parent_chunks"].count_documents(body_parent_query),
        "body_parent_done": await db["parent_chunks"].count_documents(body_parent_done_query),
        "document_done": _int(document_summary_counts.get("document_done")),
        "document_profile_done": _int(document_summary_counts.get("document_profile_done")),
        "document_tree_done": _int(document_summary_counts.get("document_tree_done")),
        "document_both_done": _int(document_summary_counts.get("document_both_done")),
        "document_profile_only": _int(document_summary_counts.get("document_profile_only")),
        "document_tree_only": _int(document_summary_counts.get("document_tree_only")),
        "document_mismatch": _int(document_summary_counts.get("document_profile_only"))
        + _int(document_summary_counts.get("document_tree_only")),
    }

    failure_rows = await db["ghost_b_extractions"].count_documents(
        {"corpus_id": corpus_id, "status": "error"}
    )
    stale_scan = await classify_stale_failure_rows(
        db,
        corpus_id=corpus_id,
        limit=STALE_FAILURE_READINESS_SCAN_LIMIT,
    )
    stale_by_doc = stale_scan["stale_by_doc"]
    stale_reason_counts = stale_scan["stale_reason_counts"]
    reconciled_stale_rows = await db["ghost_b_extractions"].aggregate([
        {"$match": {"corpus_id": corpus_id, "status": "stale_chunk_reference"}},
        {"$group": {"_id": "$doc_id", "rows": {"$sum": 1}}},
        {"$group": {"_id": None, "docs": {"$sum": 1}, "rows": {"$sum": "$rows"}}},
    ]).to_list(length=1)
    orphaned_failure_rows = await db["documents"].aggregate([
        {
            "$match": with_active_records(
                {"corpus_id": corpus_id, "ghost_b_failure_count": {"$gt": 0}}
            )
        },
        {
            "$lookup": {
                "from": "ghost_b_extractions",
                "let": {"doc_id": "$doc_id", "corpus_id": "$corpus_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$doc_id", "$$doc_id"]},
                                    {"$eq": ["$corpus_id", "$$corpus_id"]},
                                    {"$eq": ["$status", "error"]},
                                ]
                            }
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "failure_rows",
            }
        },
        {"$match": {"failure_rows": {"$eq": []}}},
        {"$count": "count"},
    ]).to_list(length=1)
    unmarked_promoted_extraction_rows = await db["ghost_b_extractions"].aggregate([
        {
            "$match": {
                "corpus_id": corpus_id,
                "status": "ok",
                "$or": [
                    {"promoted_at": {"$exists": False}},
                    {"promoted_at": None},
                ],
            }
        },
        {"$group": {"_id": "$doc_id", "rows": {"$sum": 1}}},
        {
            "$lookup": {
                "from": "documents",
                "let": {"doc_id": "$_id"},
                "pipeline": [
                    {
                        "$match": with_active_records(
                            {
                                "corpus_id": corpus_id,
                                "$expr": {"$eq": ["$doc_id", "$$doc_id"]},
                                "write_state.qdrant_written": True,
                                "write_state.neo4j_written": True,
                            }
                        )
                    },
                    {"$limit": 1},
                ],
                "as": "doc",
            }
        },
        {"$match": {"doc.0": {"$exists": True}}},
        {"$group": {"_id": None, "docs": {"$sum": 1}, "rows": {"$sum": "$rows"}}},
    ]).to_list(length=1)

    graph_counts = {
        "required": graph_required,
        "promoted": _int(doc_counts.get("graph_promoted")),
        "pending": _int(doc_counts.get("graph_pending")),
        "unpromoted_extraction_docs": 0,
        "unpromoted_extraction_rows": 0,
        "unmarked_promoted_extraction_docs": _int(
            unmarked_promoted_extraction_rows[0].get("docs")
            if unmarked_promoted_extraction_rows
            else 0
        ),
        "unmarked_promoted_extraction_rows": _int(
            unmarked_promoted_extraction_rows[0].get("rows")
            if unmarked_promoted_extraction_rows
            else 0
        ),
        "failed_docs": _int(doc_counts.get("failed")),
        "failed_chunks": _int(doc_counts.get("failed_chunks")),
        "failure_docs": _int(doc_counts.get("failure_docs")),
        "failure_rows": failure_rows,
        "stale_failure_docs": len(stale_by_doc),
        "stale_failure_rows": sum(int(count) for count in stale_by_doc.values()),
        "stale_failure_reason_counts": dict(sorted(stale_reason_counts.items())),
        "stale_failure_scan_limit": int(stale_scan["limit"]),
        "stale_failure_scan_limited": bool(stale_scan["scan_limit_reached"]),
        "reconciled_stale_failure_docs": _int(
            reconciled_stale_rows[0].get("docs") if reconciled_stale_rows else 0
        ),
        "reconciled_stale_failure_rows": _int(
            reconciled_stale_rows[0].get("rows") if reconciled_stale_rows else 0
        ),
        "orphaned_failure_docs": _int(
            orphaned_failure_rows[0].get("count") if orphaned_failure_rows else 0
        ),
    }

    source_key_query = with_active_records(
        {"corpus_id": corpus_id, **_has_any_identity_field_query(SOURCE_KEY_IDENTITY_FIELDS)}
    )
    content_hash_query = with_active_records(
        {"corpus_id": corpus_id, **_has_any_identity_field_query(CONTENT_HASH_IDENTITY_FIELDS)}
    )

    async def _duplicate_identity_rows(
        fields: tuple[str, ...],
        *,
        classify_content_collisions: bool = False,
    ) -> list[dict[str, Any]]:
        content_hash_expr = _first_non_empty_identity_expr(CONTENT_HASH_IDENTITY_FIELDS)
        group_stage: dict[str, Any] = {
            "_id": "$_identity_group_key",
            "docs": {"$sum": 1},
        }
        if classify_content_collisions:
            group_stage["content_hashes"] = {"$addToSet": "$_identity_content_hash"}
        pipeline: list[dict[str, Any]] = [
            {
                "$match": with_active_records(
                    {"corpus_id": corpus_id, **_has_any_identity_field_query(fields)}
                )
            },
            {"$addFields": {"_identity_group_key": _first_non_empty_identity_expr(fields)}},
        ]
        if classify_content_collisions:
            pipeline.append({"$addFields": {"_identity_content_hash": content_hash_expr}})
        pipeline.extend([
            {"$match": {"_identity_group_key": {"$exists": True, "$nin": [None, ""]}}},
            {"$group": group_stage},
            {"$match": {"docs": {"$gt": 1}}},
        ])
        if classify_content_collisions:
            pipeline.extend([
                {
                    "$addFields": {
                        "_content_hashes": {
                            "$filter": {
                                "input": "$content_hashes",
                                "as": "hash",
                                "cond": {
                                    "$and": [
                                        {"$ne": ["$$hash", None]},
                                        {"$ne": ["$$hash", ""]},
                                    ]
                                },
                            }
                        }
                    }
                },
                {"$addFields": {"_content_hash_count": {"$size": "$_content_hashes"}}},
                {
                    "$addFields": {
                        "_source_key_collision": {"$gt": ["$_content_hash_count", 1]}
                    }
                },
            ])
        final_group: dict[str, Any] = {
            "_id": None,
            "groups": {"$sum": 1},
            "docs": {"$sum": "$docs"},
        }
        if classify_content_collisions:
            final_group.update(
                {
                    "collision_groups": {
                        "$sum": {"$cond": ["$_source_key_collision", 1, 0]}
                    },
                    "collision_docs": {
                        "$sum": {"$cond": ["$_source_key_collision", "$docs", 0]}
                    },
                }
            )
        pipeline.append({"$group": final_group})
        return await db["documents"].aggregate(pipeline).to_list(length=1)

    duplicate_source_key_rows = await _duplicate_identity_rows(
        SOURCE_KEY_IDENTITY_FIELDS,
        classify_content_collisions=True,
    )
    duplicate_content_hash_rows = await _duplicate_identity_rows(CONTENT_HASH_IDENTITY_FIELDS)
    async def _stage_identity_missing(
        collection: str,
        extra_filter: dict[str, Any] | None = None,
    ) -> int:
        return await db[collection].count_documents(
            {
                "corpus_id": corpus_id,
                **(extra_filter or {}),
                **STAGE_IDENTITY_MISSING_CLAUSE,
            }
        )

    document_pipeline_jobs_missing_stage_identity = await _stage_identity_missing(
        "document_pipeline_jobs"
    )
    source_parse_jobs_missing_stage_identity = await _stage_identity_missing(
        "source_parse_jobs"
    )
    extraction_jobs_missing_stage_identity = await _stage_identity_missing("extraction_jobs")
    extraction_jobs_missing_stage_identity_blocking = await _stage_identity_missing(
        "extraction_jobs",
        {"status": {"$in": list(EXTRACTION_IDENTITY_BLOCKING_STATUSES)}},
    )
    extraction_jobs_missing_stage_identity_nonblocking = max(
        extraction_jobs_missing_stage_identity
        - extraction_jobs_missing_stage_identity_blocking,
        0,
    )
    summary_jobs_missing_stage_identity = await _stage_identity_missing("summary_jobs")
    graph_promotion_jobs_missing_stage_identity = await _stage_identity_missing(
        "graph_promotion_jobs"
    )
    ghost_b_extractions_missing_stage_identity = await _stage_identity_missing(
        "ghost_b_extractions"
    )
    ghost_b_extractions_missing_stage_identity_blocking = await _stage_identity_missing(
        "ghost_b_extractions",
        {"status": {"$ne": "ok"}},
    )
    ghost_b_extractions_missing_stage_identity_legacy_ok = max(
        ghost_b_extractions_missing_stage_identity
        - ghost_b_extractions_missing_stage_identity_blocking,
        0,
    )
    stage_identity_blocking_total = (
        source_parse_jobs_missing_stage_identity
        + document_pipeline_jobs_missing_stage_identity
        + extraction_jobs_missing_stage_identity_blocking
        + summary_jobs_missing_stage_identity
        + graph_promotion_jobs_missing_stage_identity
        + ghost_b_extractions_missing_stage_identity_blocking
    )
    idempotency_counts = {
        "source_keyed_documents": await db["documents"].count_documents(source_key_query),
        "content_hash_documents": await db["documents"].count_documents(content_hash_query),
        "source_parse_jobs_missing_stage_identity": source_parse_jobs_missing_stage_identity,
        "document_pipeline_jobs_missing_stage_identity": document_pipeline_jobs_missing_stage_identity,
        "extraction_jobs_missing_stage_identity": extraction_jobs_missing_stage_identity,
        "extraction_jobs_missing_stage_identity_blocking": (
            extraction_jobs_missing_stage_identity_blocking
        ),
        "extraction_jobs_missing_stage_identity_nonblocking": (
            extraction_jobs_missing_stage_identity_nonblocking
        ),
        "summary_jobs_missing_stage_identity": summary_jobs_missing_stage_identity,
        "graph_promotion_jobs_missing_stage_identity": graph_promotion_jobs_missing_stage_identity,
        "ghost_b_extractions_missing_stage_identity": ghost_b_extractions_missing_stage_identity,
        "ghost_b_extractions_missing_stage_identity_blocking": (
            ghost_b_extractions_missing_stage_identity_blocking
        ),
        "ghost_b_extractions_missing_stage_identity_legacy_ok": (
            ghost_b_extractions_missing_stage_identity_legacy_ok
        ),
        "stage_identity_missing_total": (
            source_parse_jobs_missing_stage_identity
            + document_pipeline_jobs_missing_stage_identity
            + extraction_jobs_missing_stage_identity
            + summary_jobs_missing_stage_identity
            + graph_promotion_jobs_missing_stage_identity
            + ghost_b_extractions_missing_stage_identity
        ),
        "stage_identity_blocking_total": stage_identity_blocking_total,
        "duplicate_source_key_groups": _int(
            duplicate_source_key_rows[0].get("groups") if duplicate_source_key_rows else 0
        ),
        "duplicate_source_key_docs": _int(
            duplicate_source_key_rows[0].get("docs") if duplicate_source_key_rows else 0
        ),
        "source_key_collision_groups": _int(
            duplicate_source_key_rows[0].get("collision_groups")
            if duplicate_source_key_rows
            else 0
        ),
        "source_key_collision_docs": _int(
            duplicate_source_key_rows[0].get("collision_docs")
            if duplicate_source_key_rows
            else 0
        ),
        "duplicate_content_hash_groups": _int(
            duplicate_content_hash_rows[0].get("groups") if duplicate_content_hash_rows else 0
        ),
        "duplicate_content_hash_docs": _int(
            duplicate_content_hash_rows[0].get("docs") if duplicate_content_hash_rows else 0
        ),
    }

    active_runs = await db["ingest_repair_runs"].count_documents(
        {"corpus_id": corpus_id, "status": {"$in": ["queued", "running"]}}
    )
    latest_runs = await db["ingest_repair_runs"].find(
        {"corpus_id": corpus_id},
        {"_id": 0, "run_id": 1, "kind": 1, "status": 1, "counts": 1, "updated_at": 1},
    ).sort("updated_at", -1).limit(3).to_list(length=3)
    graph_job_rows = await db["graph_promotion_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    graph_jobs = {str(row["_id"]): int(row["count"]) for row in graph_job_rows}
    stale_graph_job_rows = await db["graph_promotion_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id, "status": {"$in": ["queued", "running"]}}},
        {
            "$lookup": {
                "from": "documents",
                "let": {"doc_id": "$doc_id", "corpus_id": "$corpus_id"},
                "pipeline": [
                    {
                        "$match": with_active_records(
                            {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$doc_id", "$$doc_id"]},
                                        {"$eq": ["$corpus_id", "$$corpus_id"]},
                                    ]
                                }
                            }
                        )
                    },
                    {"$project": {"write_state.neo4j_written": 1}},
                    {"$limit": 1},
                ],
                "as": "readiness_doc",
            }
        },
        {"$addFields": {"_readiness_doc": {"$arrayElemAt": ["$readiness_doc", 0]}}},
        {
            "$match": {
                "$or": [
                    {"_readiness_doc": None},
                    {"_readiness_doc.write_state.neo4j_written": True},
                ]
            }
        },
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    graph_jobs = _demote_stale_graph_job_counts(graph_jobs, stale_graph_job_rows)
    try:
        recent_graph_latency_jobs = await db["graph_promotion_jobs"].find(
            {
                "corpus_id": corpus_id,
                "neo4j_write_latency_ms": {"$exists": True},
            },
            {
                "_id": 0,
                "neo4j_write_latency_ms": 1,
                "updated_at": 1,
            },
        ).sort("updated_at", -1).limit(20).to_list(length=20)
    except Exception:
        recent_graph_latency_jobs = []
    neo4j_pressure = neo4j_pressure_from_graph_promotion_jobs(
        graph_jobs,
        recent_graph_latency_jobs,
    )
    source_parse_job_rows = await db["source_parse_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    source_parse_jobs = {
        str(row["_id"]): int(row["count"]) for row in source_parse_job_rows
    }
    extraction_job_rows = await db["extraction_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    extraction_jobs = {str(row["_id"]): int(row["count"]) for row in extraction_job_rows}
    try:
        provider_lane_health = await load_recent_provider_lane_health(
            db,
            corpus_id=corpus_id,
        )
    except Exception as exc:  # noqa: BLE001
        provider_lane_health = {
            "status": "unknown",
            "error": str(exc)[:300],
            "cooldown_keys": [],
            "lanes": [],
        }
    summary_job_rows = await db["summary_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    summary_jobs = {str(row["_id"]): int(row["count"]) for row in summary_job_rows}
    document_pipeline_job_rows = await db["document_pipeline_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    document_pipeline_jobs = {
        str(row["_id"]): int(row["count"]) for row in document_pipeline_job_rows
    }
    try:
        mongo_stats = await db.command("dbStats")
    except Exception:
        mongo_stats = {}
    qdrant_url = ""
    qdrant_memory_limit_bytes = None
    qdrant_memory_warn_ratio = 0.85
    qdrant_memory_stop_ratio = 0.90
    try:
        settings = get_settings()
        ram_cap_mb, rss_soft_limit_mb = memory_soft_limit_mb(settings)
        qdrant_write_concurrency = int(getattr(settings, "QDRANT_INGEST_WRITE_CONCURRENCY", 2))
        neo4j_write_concurrency = int(getattr(settings, "NEO4J_INGEST_WRITE_CONCURRENCY", 1))
        qdrant_url = str(getattr(settings, "QDRANT_URL", "") or "")
        qdrant_memory_limit_bytes = parse_memory_limit_bytes(
            getattr(settings, "QDRANT_MEM_LIMIT", None)
        )
        qdrant_memory_warn_ratio = float(
            getattr(settings, "QDRANT_MEMORY_WARN_RATIO", 0.85)
        )
        qdrant_memory_stop_ratio = float(
            getattr(settings, "QDRANT_MEMORY_STOP_RATIO", 0.90)
        )
        mongo_storage_warn_ratio = float(
            getattr(settings, "INGEST_MONGO_STORAGE_WARN_RATIO", 0.85)
        )
        mongo_storage_stop_ratio = float(
            getattr(settings, "INGEST_MONGO_STORAGE_STOP_RATIO", 0.90)
        )
    except Exception:
        ram_cap_mb = None
        rss_soft_limit_mb = None
        qdrant_write_concurrency = None
        neo4j_write_concurrency = None
        mongo_storage_warn_ratio = 0.85
        mongo_storage_stop_ratio = 0.90
    qdrant_pressure = await sample_qdrant_pressure(
        qdrant_url,
        timeout_s=0.8,
        memory_limit_bytes=qdrant_memory_limit_bytes,
        memory_warn_ratio=qdrant_memory_warn_ratio,
        memory_stop_ratio=qdrant_memory_stop_ratio,
    )
    pressure = build_ingestion_pressure_snapshot(
        backend_rss_mb=current_process_rss_mb(),
        ram_cap_mb=ram_cap_mb,
        rss_soft_limit_mb=rss_soft_limit_mb,
        active_repairs=active_runs,
        graph_jobs=graph_jobs,
        extraction_jobs=extraction_jobs,
        summary_missing=max(
            _int(summary_counts["retrieval_parent_total"])
            - _int(summary_counts["retrieval_parent_done"]),
            0,
        )
        + max(
            _int(doc_counts.get("total"))
            - _int(
                summary_counts.get(
                    "document_synced_done",
                    _int(summary_counts.get("document_both_done")),
                )
            ),
            0,
        ),
        mongo_stats=mongo_stats,
        mongo_storage_warn_ratio=mongo_storage_warn_ratio,
        mongo_storage_stop_ratio=mongo_storage_stop_ratio,
        qdrant_pressure=qdrant_pressure,
        neo4j_pressure=neo4j_pressure,
        qdrant_write_concurrency=qdrant_write_concurrency,
        neo4j_write_concurrency=neo4j_write_concurrency,
    )

    try:
        from services.ingestion.provider_call_telemetry import provider_efficiency_snapshot

        provider_efficiency = await provider_efficiency_snapshot(
            db,
            corpus_id=corpus_id,
        )
    except Exception:
        provider_efficiency = {}
    try:
        scheduler_state = await db["ingest_scheduler_state"].find_one(
            {"_id": corpus_id},
            {
                "_id": 0,
                "idle_ticks": 1,
                "no_op_cycles": 1,
                "next_eligible_at": 1,
                "updated_at": 1,
                "last_changed": 1,
            },
        ) or {}
    except Exception:
        scheduler_state = {}
    queue_telemetry: dict[str, Any] = {"dead_letter_total": 0, "lanes": {}}
    now = datetime.now(timezone.utc)
    for lane, collection, counts in (
        ("source", "source_parse_jobs", source_parse_jobs),
        ("document", "document_pipeline_jobs", document_pipeline_jobs),
        ("extraction", "extraction_jobs", extraction_jobs),
        ("summary", "summary_jobs", summary_jobs),
        ("graph", "graph_promotion_jobs", graph_jobs),
    ):
        dead_letters = _int(counts.get("dead_letter"))
        oldest_age_seconds = 0
        try:
            oldest = await db[collection].find_one(
                {
                    "corpus_id": corpus_id,
                    "status": {"$in": ["queued", "running"]},
                },
                {"_id": 0, "created_at": 1, "updated_at": 1},
                sort=[("created_at", 1)],
            )
            stamp = (oldest or {}).get("created_at") or (oldest or {}).get("updated_at")
            if isinstance(stamp, datetime):
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                oldest_age_seconds = max(0, int((now - stamp).total_seconds()))
        except Exception:
            pass
        queue_telemetry["lanes"][lane] = {
            "dead_letter": dead_letters,
            "oldest_actionable_age_seconds": oldest_age_seconds,
        }
        queue_telemetry["dead_letter_total"] += dead_letters

    return build_corpus_readiness_snapshot(
        corpus_id=corpus_id,
        document_counts=doc_counts,
        stage_counts=stage_counts,
        chunk_counts=chunk_counts,
        summary_counts=summary_counts,
        graph_counts=graph_counts,
        idempotency_counts=idempotency_counts,
        repair_counts={
            "active_runs": active_runs,
            "latest_runs": latest_runs,
            "source_parse_jobs": source_parse_jobs,
            "graph_promotion_jobs": graph_jobs,
            "extraction_jobs": extraction_jobs,
            "provider_lane_health": provider_lane_health,
            "summary_jobs": summary_jobs,
            "document_pipeline_jobs": document_pipeline_jobs,
            "provider_efficiency": provider_efficiency,
            "scheduler_state": scheduler_state,
            "queue_telemetry": queue_telemetry,
        },
        pressure=pressure,
    )


async def get_materialized_corpus_readiness(db: Any, corpus_id: str) -> dict[str, Any] | None:
    row = await db[READINESS_COLLECTION].find_one({"_id": corpus_id}, {"_id": 0})
    return dict(row) if row else None


async def materialize_corpus_readiness(
    db: Any,
    corpus_id: str,
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute and persist the current corpus readiness view.

    Batch rows are intentionally not used as truth here. The materialized row is
    a cached read-model for dashboards/repair monitors; callers that need the
    freshest possible state should call this instead of reading old batch rows.
    """

    snapshot = snapshot or await compute_corpus_readiness(db, corpus_id)
    record = build_corpus_readiness_record(snapshot)
    await db[READINESS_COLLECTION].replace_one(
        {"_id": corpus_id},
        {"_id": corpus_id, **record},
        upsert=True,
    )
    try:
        from services.retriever import invalidate_retrieval_cache

        invalidate_retrieval_cache()
    except Exception:
        pass
    return record
