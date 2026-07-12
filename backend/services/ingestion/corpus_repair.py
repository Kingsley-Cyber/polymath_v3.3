"""Bounded corpus repair cycle.

Coordinates the short-term production repair lanes:
failure metadata reconciliation and graph-promotion jobs. The cycle is dry-run
by default and bounded by explicit limits when applied.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from services.ingestion.document_summaries import backfill_document_summaries
from services.ingestion.document_pipeline_jobs import plan_document_pipeline_jobs
from services.ingestion.failure_reconciliation import reconcile_ghost_b_failure_metadata
from services.ingestion.extraction_jobs import plan_extraction_jobs, run_extraction_jobs
from services.ingestion.graph_promotion_jobs import (
    backfill_promoted_extraction_marks,
    plan_graph_promotion_jobs,
    run_graph_promotion_jobs,
)
from services.ingestion.readiness import (
    compute_corpus_readiness,
    materialize_corpus_readiness,
)
from services.ingestion.source_parse_jobs import (
    backfill_source_parse_stage_identity,
    plan_source_parse_jobs,
    run_source_parse_jobs,
)
from services.ingestion.stage_identity_repair import backfill_ghost_b_stage_identity
from services.ingestion.summary_jobs import (
    backfill_summary_stage_identity,
    plan_summary_jobs,
)


def build_repair_cycle_summary(
    *, steps: list[dict[str, Any]], readiness: dict[str, Any] | None
) -> dict[str, Any]:
    """Small pure summarizer for tests and API stability."""

    graph_jobs = ((readiness or {}).get("repair") or {}).get(
        "graph_promotion_jobs"
    ) or {}
    source_parse_jobs = ((readiness or {}).get("repair") or {}).get(
        "source_parse_jobs"
    ) or {}
    document_pipeline_jobs = ((readiness or {}).get("repair") or {}).get(
        "document_pipeline_jobs"
    ) or {}
    extraction_jobs = ((readiness or {}).get("repair") or {}).get(
        "extraction_jobs"
    ) or {}
    summary_jobs = ((readiness or {}).get("repair") or {}).get("summary_jobs") or {}
    planned_graph_jobs = {}
    planned_source_parse_jobs = {}
    planned_document_pipeline_jobs = {}
    planned_extraction_jobs = {}
    planned_summary_jobs = {}
    for step in steps:
        if step.get("name") in {
            "graph_promotion_plan",
            "graph_promotion_replan_after_extraction",
        }:
            planned_graph_jobs = (step.get("result") or {}).get("counts") or {}
        elif step.get("name") == "source_parse_job_plan":
            planned_source_parse_jobs = (step.get("result") or {}).get("counts") or {}
        elif step.get("name") == "document_pipeline_job_plan":
            planned_document_pipeline_jobs = (step.get("result") or {}).get(
                "counts"
            ) or {}
        elif step.get("name") == "extraction_job_plan":
            planned_extraction_jobs = (step.get("result") or {}).get("counts") or {}
        elif step.get("name") in {
            "summary_job_plan",
            "document_summary_replan_after_parent_summary",
        }:
            planned_summary_jobs = (step.get("result") or {}).get("counts") or {}
    graph_job_counts = graph_jobs or planned_graph_jobs
    source_parse_job_counts = source_parse_jobs or planned_source_parse_jobs
    document_pipeline_job_counts = (
        document_pipeline_jobs or planned_document_pipeline_jobs
    )
    extraction_job_counts = extraction_jobs or planned_extraction_jobs
    summary_job_counts = summary_jobs or planned_summary_jobs
    graph = (readiness or {}).get("graph") or {}
    summaries = (readiness or {}).get("summaries") or {}
    documents = (readiness or {}).get("documents") or {}
    pressure = (readiness or {}).get("pressure") or {}
    return {
        "readiness_status": (readiness or {}).get("status", "unknown"),
        "pressure_status": pressure.get("status", "unknown"),
        "pressure_recommendations": pressure.get("recommendations") or [],
        "queryable_docs": documents.get("queryable", 0),
        "total_docs": documents.get("total", 0),
        "main_summary_missing": summaries.get(
            "retrieval_parent_missing", summaries.get("body_parent_missing", 0)
        ),
        "document_summary_missing": summaries.get(
            "document_sync_missing",
            summaries.get("document_missing", 0),
        ),
        "document_summaries_built": sum(
            int(((step.get("result") or {}).get("built") or 0))
            for step in steps
            if step.get("name") == "document_summary_backfill"
        ),
        "graph_pending": graph.get("pending", 0),
        "failed_chunks": graph.get("failed_chunks", 0),
        "stale_failure_rows": graph.get("stale_failure_rows", 0),
        "ghost_b_stage_identity_backfilled": sum(
            int(((step.get("result") or {}).get("modified") or 0))
            for step in steps
            if step.get("name") == "ghost_b_stage_identity_backfill"
        ),
        "ghost_b_stage_identity_planned": sum(
            int(((step.get("result") or {}).get("planned") or 0))
            for step in steps
            if step.get("name") == "ghost_b_stage_identity_backfill"
        ),
        "source_parse_stage_identity_backfilled": sum(
            int(((step.get("result") or {}).get("modified") or 0))
            for step in steps
            if step.get("name") == "source_parse_stage_identity_backfill"
        ),
        "source_parse_stage_identity_planned": sum(
            int(((step.get("result") or {}).get("planned") or 0))
            for step in steps
            if step.get("name") == "source_parse_stage_identity_backfill"
        ),
        "summary_stage_identity_backfilled": sum(
            int(((step.get("result") or {}).get("modified") or 0))
            for step in steps
            if step.get("name") == "summary_stage_identity_backfill"
        ),
        "summary_stage_identity_planned": sum(
            int(((step.get("result") or {}).get("planned") or 0))
            for step in steps
            if step.get("name") == "summary_stage_identity_backfill"
        ),
        "promoted_extraction_marks_planned": sum(
            int(((step.get("result") or {}).get("planned_rows") or 0))
            for step in steps
            if step.get("name") == "promoted_extraction_mark_backfill"
        ),
        "promoted_extraction_marks_backfilled": sum(
            int(((step.get("result") or {}).get("modified_rows") or 0))
            for step in steps
            if step.get("name") == "promoted_extraction_mark_backfill"
        ),
        "graph_jobs_queued": int(graph_job_counts.get("queued") or 0),
        "graph_jobs_running": int(graph_job_counts.get("running") or 0),
        "graph_jobs_blocked": int(graph_job_counts.get("blocked_failed_chunks") or 0)
        + int(graph_job_counts.get("blocked_no_extractions") or 0),
        "source_parse_jobs_queued": int(source_parse_job_counts.get("queued") or 0),
        "source_parse_jobs_running": int(source_parse_job_counts.get("running") or 0),
        "source_parse_jobs_blocked": int(source_parse_job_counts.get("failed") or 0)
        + int(source_parse_job_counts.get("failed_recoverable") or 0)
        + int(source_parse_job_counts.get("blocked_source_missing") or 0),
        "source_parse_jobs_started": sum(
            int(((step.get("result") or {}).get("runners_started") or 0))
            for step in steps
            if step.get("name") == "source_parse_job_run"
        ),
        "document_pipeline_jobs_queued": int(
            document_pipeline_job_counts.get("queued") or 0
        ),
        "document_pipeline_jobs_running": int(
            document_pipeline_job_counts.get("running") or 0
        ),
        "document_pipeline_jobs_blocked": int(
            document_pipeline_job_counts.get("failed") or 0
        )
        + int(document_pipeline_job_counts.get("blocked_no_source") or 0)
        + int(document_pipeline_job_counts.get("blocked_missing_chunks") or 0)
        + int(document_pipeline_job_counts.get("blocked_mongo_state") or 0),
        "document_pipeline_jobs_ran": sum(
            int(((step.get("result") or {}).get("claimed") or 0))
            for step in steps
            if step.get("name") == "document_pipeline_job_run"
        ),
        "document_pipeline_jobs_succeeded": sum(
            int(
                (((step.get("result") or {}).get("counts") or {}).get("succeeded") or 0)
            )
            for step in steps
            if step.get("name") == "document_pipeline_job_run"
        ),
        "extraction_jobs_queued": int(extraction_job_counts.get("queued") or 0),
        "extraction_jobs_failed": int(extraction_job_counts.get("provider_failed") or 0)
        + int(extraction_job_counts.get("validation_failed") or 0)
        + int(extraction_job_counts.get("failed") or 0),
        "extraction_jobs_blocked": int(
            extraction_job_counts.get("blocked_provider_contract") or 0
        ),
        "summary_jobs_queued": int(summary_job_counts.get("queued") or 0),
        "summary_jobs_running": int(summary_job_counts.get("running") or 0),
        "summary_jobs_waiting_dependencies": int(
            summary_job_counts.get("blocked_no_parent_summaries") or 0
        )
        + int(summary_job_counts.get("blocked_parent_summaries_incomplete") or 0),
        "summary_jobs_pending": int(summary_job_counts.get("queued") or 0)
        + int(summary_job_counts.get("running") or 0)
        + int(summary_job_counts.get("blocked_no_parent_summaries") or 0)
        + int(summary_job_counts.get("blocked_parent_summaries_incomplete") or 0),
        "summary_jobs_blocked": int(summary_job_counts.get("blocked_empty_source") or 0)
        + int(summary_job_counts.get("failed") or 0),
        "summary_jobs_ran": sum(
            int(((step.get("result") or {}).get("claimed") or 0))
            for step in steps
            if step.get("name") == "summary_job_run"
        ),
        "summary_jobs_succeeded": sum(
            int(
                (((step.get("result") or {}).get("counts") or {}).get("succeeded") or 0)
            )
            for step in steps
            if step.get("name") == "summary_job_run"
        ),
        "backpressure_blocked_steps": [
            step.get("name")
            for step in steps
            if step.get("status") == "skipped_pressure"
        ],
        "steps": [
            {
                "name": step.get("name"),
                "status": step.get("status"),
                "changed": bool(step.get("changed")),
            }
            for step in steps
        ],
    }


def _backpressure_allowed(readiness: dict[str, Any], key: str) -> bool:
    pressure = readiness.get("pressure") or {}
    backpressure = pressure.get("backpressure") or {}
    return backpressure.get(key) is not False


def _pressure_skip_step(
    name: str, readiness: dict[str, Any], key: str
) -> dict[str, Any]:
    pressure = readiness.get("pressure") or {}
    return {
        "name": name,
        "status": "skipped_pressure",
        "changed": False,
        "result": {
            "reason": f"{key}=false",
            "pressure_status": pressure.get("status", "unknown"),
            "pressure_reasons": pressure.get("reasons") or [],
            "pressure_recommendations": pressure.get("recommendations") or [],
        },
    }


async def _refresh_backpressure_readiness(
    db: Any,
    *,
    corpus_id: str,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Re-read corpus pressure immediately before starting expensive work.

    The repair cycle is deliberately bounded, but on a constrained Mac the
    pressure state can change after planning or after a previous runner writes
    artifacts. Use a fresh readiness snapshot as the brake for execution lanes;
    fall back to the earlier snapshot if refresh itself fails.
    """

    try:
        return await compute_corpus_readiness(db, corpus_id)
    except (
        Exception
    ):  # noqa: BLE001 - pressure refresh failure should not crash repair planning
        return fallback


async def run_bounded_corpus_repair_cycle(
    db: Any,
    *,
    corpus_id: str,
    user_id: str,
    ingestion_service: Any = None,
    qdrant_client: Any = None,
    neo4j_driver: Any = None,
    apply: bool = False,
    reconcile_failures: bool = True,
    failure_reconcile_limit: int = 5000,
    backfill_promoted_extraction_marks_rows: bool = False,
    promoted_extraction_marks_backfill_limit: int = 100,
    backfill_source_parse_stage_identity_rows: bool = False,
    source_parse_stage_identity_backfill_limit: int = 1000,
    backfill_ghost_b_stage_identity_rows: bool = False,
    ghost_b_stage_identity_backfill_limit: int = 1000,
    plan_source_parse_job_rows: bool = True,
    source_parse_job_plan_limit: int = 500,
    run_source_parse_job_rows: bool = False,
    source_parse_job_run_limit: int = 25,
    source_parse_start_runners: bool = False,
    plan_document_pipeline_job_rows: bool = True,
    document_pipeline_job_plan_limit: int = 500,
    run_document_pipeline_job_rows: bool = False,
    document_pipeline_job_run_limit: int = 25,
    plan_graph_jobs: bool = True,
    graph_plan_limit: int = 100,
    graph_max_chunks: int | None = None,
    plan_extraction_job_rows: bool = True,
    extraction_job_plan_limit: int = 500,
    run_extraction_job_rows: bool = False,
    extraction_job_run_limit: int = 25,
    plan_summary_job_rows: bool = True,
    summary_job_plan_limit: int = 500,
    backfill_summary_stage_identity_rows: bool = False,
    summary_stage_identity_backfill_limit: int = 1000,
    run_summary_job_rows: bool = False,
    summary_job_run_limit: int = 25,
    run_document_summaries: bool = False,
    document_summary_limit: int = 10,
    run_graph_jobs: bool = False,
    graph_run_limit: int = 3,
    record_run: bool = True,
) -> dict[str, Any]:
    """Run one safe repair cycle.

    Dry-run returns the planned actions. Applied cycles mutate only bounded
    repair metadata and graph jobs; graph execution requires ``run_graph_jobs``.
    """

    started = datetime.utcnow()
    steps: list[dict[str, Any]] = []

    readiness_before = await compute_corpus_readiness(db, corpus_id)

    if reconcile_failures:
        result = await reconcile_ghost_b_failure_metadata(
            db,
            corpus_id=corpus_id,
            apply=apply,
            limit=failure_reconcile_limit,
        )
        steps.append(
            {
                "name": "failure_reconciliation",
                "status": result.get("status"),
                "changed": bool(
                    result.get("affected_docs") or result.get("stale_split_rows")
                ),
                "result": result,
            }
        )

    if backfill_ghost_b_stage_identity_rows:
        result = await backfill_ghost_b_stage_identity(
            db,
            corpus_id=corpus_id,
            apply=apply,
            limit=ghost_b_stage_identity_backfill_limit,
        )
        steps.append(
            {
                "name": "ghost_b_stage_identity_backfill",
                "status": result.get("status"),
                "changed": bool(
                    result.get("modified") if apply else result.get("planned")
                ),
                "result": result,
            }
        )

    if backfill_promoted_extraction_marks_rows:
        result = await backfill_promoted_extraction_marks(
            db,
            corpus_id=corpus_id,
            apply=apply,
            limit=promoted_extraction_marks_backfill_limit,
        )
        steps.append(
            {
                "name": "promoted_extraction_mark_backfill",
                "status": result.get("status"),
                "changed": bool(
                    result.get("modified_rows") if apply else result.get("planned_rows")
                ),
                "result": result,
            }
        )

    if backfill_source_parse_stage_identity_rows:
        result = await backfill_source_parse_stage_identity(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=source_parse_stage_identity_backfill_limit,
        )
        steps.append(
            {
                "name": "source_parse_stage_identity_backfill",
                "status": result.get("status"),
                "changed": bool(
                    result.get("modified") if apply else result.get("planned")
                ),
                "result": result,
            }
        )

    if plan_source_parse_job_rows:
        result = await plan_source_parse_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=source_parse_job_plan_limit,
        )
        steps.append(
            {
                "name": "source_parse_job_plan",
                "status": result.get("status"),
                "changed": bool(result.get("planned")),
                "result": result,
            }
        )

    if run_source_parse_job_rows:
        if not apply:
            steps.append(
                {
                    "name": "source_parse_job_run",
                    "status": "skipped_dry_run",
                    "changed": False,
                    "result": {"reason": "apply=false"},
                }
            )
        else:
            pressure_readiness = await _refresh_backpressure_readiness(
                db,
                corpus_id=corpus_id,
                fallback=readiness_before,
            )
            if not _backpressure_allowed(pressure_readiness, "source_parse_allowed"):
                steps.append(
                    _pressure_skip_step(
                        "source_parse_job_run",
                        pressure_readiness,
                        "source_parse_allowed",
                    )
                )
            else:
                result = await run_source_parse_jobs(
                    db,
                    corpus_id=corpus_id,
                    user_id=user_id,
                    ingestion_service=(
                        ingestion_service if source_parse_start_runners else None
                    ),
                    limit=source_parse_job_run_limit,
                    start_runners=source_parse_start_runners,
                )
                steps.append(
                    {
                        "name": "source_parse_job_run",
                        "status": result.get("status"),
                        "changed": bool(result.get("eligible_items")),
                        "result": result,
                    }
                )

    if plan_document_pipeline_job_rows:
        result = await plan_document_pipeline_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=document_pipeline_job_plan_limit,
        )
        steps.append(
            {
                "name": "document_pipeline_job_plan",
                "status": result.get("status"),
                "changed": bool(
                    result.get("planned") or result.get("artifact_reconciled")
                ),
                "result": result,
            }
        )

    if run_document_pipeline_job_rows:
        if not apply:
            steps.append(
                {
                    "name": "document_pipeline_job_run",
                    "status": "skipped_dry_run",
                    "changed": False,
                    "result": {"reason": "apply=false"},
                }
            )
        elif ingestion_service is None:
            steps.append(
                {
                    "name": "document_pipeline_job_run",
                    "status": "skipped_no_service",
                    "changed": False,
                    "result": {"reason": "ingestion_service is unavailable"},
                }
            )
        else:
            pressure_readiness = await _refresh_backpressure_readiness(
                db,
                corpus_id=corpus_id,
                fallback=readiness_before,
            )
            if not _backpressure_allowed(
                pressure_readiness, "document_pipeline_allowed"
            ):
                steps.append(
                    _pressure_skip_step(
                        "document_pipeline_job_run",
                        pressure_readiness,
                        "document_pipeline_allowed",
                    )
                )
            else:
                result = await ingestion_service.run_document_pipeline_jobs(
                    corpus_id=corpus_id,
                    user_id=user_id,
                    limit=document_pipeline_job_run_limit,
                )
                steps.append(
                    {
                        "name": "document_pipeline_job_run",
                        "status": result.get("status"),
                        "changed": bool(result.get("claimed")),
                        "result": result,
                    }
                )

    if plan_graph_jobs:
        result = await plan_graph_promotion_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=graph_plan_limit,
            max_chunks=graph_max_chunks,
        )
        steps.append(
            {
                "name": "graph_promotion_plan",
                "status": result.get("status"),
                "changed": bool(result.get("planned")),
                "result": result,
            }
        )

    if plan_extraction_job_rows:
        result = await plan_extraction_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=extraction_job_plan_limit,
            include_succeeded=False,
        )
        steps.append(
            {
                "name": "extraction_job_plan",
                "status": result.get("status"),
                "changed": bool(result.get("planned")),
                "result": result,
            }
        )

    if backfill_summary_stage_identity_rows:
        result = await backfill_summary_stage_identity(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=summary_stage_identity_backfill_limit,
        )
        steps.append(
            {
                "name": "summary_stage_identity_backfill",
                "status": result.get("status"),
                "changed": bool(
                    result.get("modified") if apply else result.get("planned")
                ),
                "result": result,
            }
        )

    if plan_summary_job_rows:
        result = await plan_summary_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=summary_job_plan_limit,
        )
        steps.append(
            {
                "name": "summary_job_plan",
                "status": result.get("status"),
                "changed": bool(
                    result.get("planned") or result.get("artifact_reconciled")
                ),
                "result": result,
            }
        )

    if run_summary_job_rows:
        if not apply:
            steps.append(
                {
                    "name": "summary_job_run",
                    "status": "skipped_dry_run",
                    "changed": False,
                    "result": {"reason": "apply=false"},
                }
            )
        elif ingestion_service is None:
            steps.append(
                {
                    "name": "summary_job_run",
                    "status": "skipped_no_service",
                    "changed": False,
                    "result": {"reason": "ingestion_service is unavailable"},
                }
            )
        else:
            pressure_readiness = await _refresh_backpressure_readiness(
                db,
                corpus_id=corpus_id,
                fallback=readiness_before,
            )
            if not _backpressure_allowed(
                pressure_readiness, "summary_backfill_allowed"
            ):
                steps.append(
                    _pressure_skip_step(
                        "summary_job_run",
                        pressure_readiness,
                        "summary_backfill_allowed",
                    )
                )
            else:
                result = await ingestion_service.run_summary_jobs(
                    corpus_id=corpus_id,
                    user_id=user_id,
                    limit=summary_job_run_limit,
                )
                steps.append(
                    {
                        "name": "summary_job_run",
                        "status": result.get("status"),
                        "changed": bool(result.get("claimed")),
                        "result": result,
                    }
                )

    summary_run_step = next(
        (step for step in reversed(steps) if step.get("name") == "summary_job_run"),
        None,
    )
    summary_run_result = (summary_run_step or {}).get("result") or {}
    summary_run_counts = summary_run_result.get("counts") or {}
    parent_summary_jobs_succeeded = (
        int(summary_run_result.get("parent_claimed") or 0) > 0
        and int(summary_run_counts.get("succeeded") or 0) > 0
    )
    should_replan_documents_after_parent_summary = bool(
        apply
        and plan_summary_job_rows
        and run_summary_job_rows
        and parent_summary_jobs_succeeded
    )
    if should_replan_documents_after_parent_summary:
        result = await plan_summary_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=True,
            limit=summary_job_plan_limit,
            kinds=["document_summary"],
        )
        steps.append(
            {
                "name": "document_summary_replan_after_parent_summary",
                "status": result.get("status"),
                "changed": bool(result.get("planned")),
                "result": result,
            }
        )
        if ingestion_service is None:
            steps.append(
                {
                    "name": "document_summary_run_after_parent_summary",
                    "status": "skipped_no_service",
                    "changed": False,
                    "result": {"reason": "ingestion_service is unavailable"},
                }
            )
        else:
            pressure_readiness = await _refresh_backpressure_readiness(
                db,
                corpus_id=corpus_id,
                fallback=readiness_before,
            )
            if not _backpressure_allowed(
                pressure_readiness, "summary_backfill_allowed"
            ):
                steps.append(
                    _pressure_skip_step(
                        "document_summary_run_after_parent_summary",
                        pressure_readiness,
                        "summary_backfill_allowed",
                    )
                )
            elif result.get("planned"):
                result = await ingestion_service.run_summary_jobs(
                    corpus_id=corpus_id,
                    user_id=user_id,
                    limit=summary_job_run_limit,
                    kinds=["document_summary"],
                )
                steps.append(
                    {
                        "name": "document_summary_run_after_parent_summary",
                        "status": result.get("status"),
                        "changed": bool(result.get("claimed")),
                        "result": result,
                    }
                )

    if run_extraction_job_rows:
        if not apply:
            steps.append(
                {
                    "name": "extraction_job_run",
                    "status": "skipped_dry_run",
                    "changed": False,
                    "result": {"reason": "apply=false"},
                }
            )
        elif qdrant_client is None:
            steps.append(
                {
                    "name": "extraction_job_run",
                    "status": "skipped_no_qdrant",
                    "changed": False,
                    "result": {"reason": "qdrant_client is unavailable"},
                }
            )
        else:
            pressure_readiness = await _refresh_backpressure_readiness(
                db,
                corpus_id=corpus_id,
                fallback=readiness_before,
            )
            if not _backpressure_allowed(
                pressure_readiness, "extraction_backfill_allowed"
            ):
                steps.append(
                    _pressure_skip_step(
                        "extraction_job_run",
                        pressure_readiness,
                        "extraction_backfill_allowed",
                    )
                )
            else:
                if ingestion_service is not None:
                    result = await ingestion_service.run_extraction_jobs(
                        corpus_id=corpus_id,
                        user_id=user_id,
                        limit=extraction_job_run_limit,
                    )
                else:
                    result = await run_extraction_jobs(
                        db,
                        qdrant_client=qdrant_client,
                        corpus_id=corpus_id,
                        user_id=user_id,
                        limit=extraction_job_run_limit,
                    )
                steps.append(
                    {
                        "name": "extraction_job_run",
                        "status": result.get("status"),
                        "changed": bool(result.get("claimed")),
                        "result": result,
                    }
                )

    extraction_run_step = next(
        (step for step in reversed(steps) if step.get("name") == "extraction_job_run"),
        None,
    )
    extraction_claimed = bool(
        ((extraction_run_step or {}).get("result") or {}).get("claimed")
    )
    should_replan_graph_after_extraction = bool(
        apply and extraction_claimed and (plan_graph_jobs or run_graph_jobs)
    )
    if should_replan_graph_after_extraction:
        result = await plan_graph_promotion_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=True,
            limit=graph_plan_limit,
            max_chunks=graph_max_chunks,
        )
        steps.append(
            {
                "name": "graph_promotion_replan_after_extraction",
                "status": result.get("status"),
                "changed": bool(result.get("planned")),
                "result": result,
            }
        )

    if run_document_summaries:
        if not apply:
            steps.append(
                {
                    "name": "document_summary_backfill",
                    "status": "skipped_dry_run",
                    "changed": False,
                    "result": {"reason": "apply=false"},
                }
            )
        else:
            pressure_readiness = await _refresh_backpressure_readiness(
                db,
                corpus_id=corpus_id,
                fallback=readiness_before,
            )
            if not _backpressure_allowed(
                pressure_readiness, "summary_backfill_allowed"
            ):
                steps.append(
                    _pressure_skip_step(
                        "document_summary_backfill",
                        pressure_readiness,
                        "summary_backfill_allowed",
                    )
                )
            else:
                result = await backfill_document_summaries(
                    db,
                    corpus_id=corpus_id,
                    qdrant_client=qdrant_client,
                    user_id=user_id,
                    limit=document_summary_limit,
                )
                steps.append(
                    {
                        "name": "document_summary_backfill",
                        "status": result.get("status"),
                        "changed": bool(result.get("built")),
                        "result": result,
                    }
                )

    if run_graph_jobs:
        if not apply:
            steps.append(
                {
                    "name": "graph_promotion_run",
                    "status": "skipped_dry_run",
                    "changed": False,
                    "result": {"reason": "apply=false"},
                }
            )
        elif neo4j_driver is None:
            steps.append(
                {
                    "name": "graph_promotion_run",
                    "status": "skipped_no_neo4j",
                    "changed": False,
                    "result": {"reason": "neo4j_driver is unavailable"},
                }
            )
        else:
            pressure_readiness = await _refresh_backpressure_readiness(
                db,
                corpus_id=corpus_id,
                fallback=readiness_before,
            )
            if not _backpressure_allowed(pressure_readiness, "graph_promotion_allowed"):
                steps.append(
                    _pressure_skip_step(
                        "graph_promotion_run",
                        pressure_readiness,
                        "graph_promotion_allowed",
                    )
                )
            else:
                if ingestion_service is not None:
                    result = await ingestion_service.run_graph_promotion_jobs(
                        corpus_id=corpus_id,
                        user_id=user_id,
                        limit=graph_run_limit,
                    )
                else:
                    result = await run_graph_promotion_jobs(
                        db,
                        qdrant_client=qdrant_client,
                        neo4j_driver=neo4j_driver,
                        corpus_id=corpus_id,
                        user_id=user_id,
                        limit=graph_run_limit,
                    )
                steps.append(
                    {
                        "name": "graph_promotion_run",
                        "status": result.get("status"),
                        "changed": bool(
                            (result.get("counts") or {}).get("done")
                            or (result.get("counts") or {}).get("noop")
                        ),
                        "result": result,
                    }
                )

    if apply:
        reconciled = 0
        examined = 0
        reconciliation_status = "complete"
        try:
            from services.ingestion.batches import (
                reconcile_pending_batch_enrichment_truth,
            )

            batch_result = await reconcile_pending_batch_enrichment_truth(
                db,
                corpus_id=corpus_id,
                limit=500,
            )
            reconciled = int(batch_result.get("promoted") or 0)
            examined = int(batch_result.get("examined") or 0)
        except (AttributeError, TypeError):
            # Lightweight test/maintenance database adapters may not expose
            # batch history; enrichment repair remains optional in that case.
            reconciliation_status = "skipped_unavailable"
        if reconciliation_status == "complete":
            steps.append(
                {
                    "name": "batch_enrichment_reconciliation",
                    "status": reconciliation_status,
                    "changed": reconciled > 0,
                    "result": {"examined": examined, "promoted": reconciled},
                }
            )

    readiness_after = await compute_corpus_readiness(db, corpus_id)
    summary = build_repair_cycle_summary(steps=steps, readiness=readiness_after)
    result = {
        "status": "planned" if not apply else "complete",
        "apply": bool(apply),
        "corpus_id": corpus_id,
        "started_at": started,
        "completed_at": datetime.utcnow(),
        "readiness_before": readiness_before,
        "readiness_after": readiness_after,
        "summary": summary,
        "steps": steps,
    }

    if apply:
        if record_run:
            run_id = f"corpus_repair_cycle_{started.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
            await db["ingest_repair_runs"].insert_one(
                {
                    "run_id": run_id,
                    "kind": "corpus_repair_cycle",
                    "status": "complete",
                    "corpus_id": corpus_id,
                    "user_id": user_id,
                    "counts": summary,
                    "started_at": started,
                    "completed_at": result["completed_at"],
                    "updated_at": result["completed_at"],
                }
            )
            result["run_id"] = run_id
        readiness_after = await materialize_corpus_readiness(db, corpus_id)
        result["readiness_after"] = readiness_after
        result["summary"] = build_repair_cycle_summary(
            steps=steps, readiness=readiness_after
        )

    return result
