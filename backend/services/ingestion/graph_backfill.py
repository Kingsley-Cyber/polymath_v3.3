"""Retry failed Ghost B graph extraction chunks after a document lands.

The main ingestion path is allowed to commit Mongo/Qdrant/Neo4j chunk coverage
when a small number of Ghost B chunk calls fail. This module closes those graph
holes later by retrying only the failed chunks and patching Neo4j incrementally.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient

from models.schemas import IngestionConfig
from services.ghost_b import (
    ExtractionBatchReport,
    ExtractionFailureItem,
    ExtractionResult,
    ExtractionTask,
    SchemaContext,
    extract_entities,
    summarize_extraction_batch,
)
from services.graph.entity_quality import mark_graph_metrics_stale
from services.graph.neo4j_writer import write_document_graph
from services.ingestion.worker import (
    GRAPH_NEEDS_BACKFILL,
    GRAPH_RETRY_SCHEDULED,
    GRAPH_READY,
    _build_ghost_pool,
    _graph_extraction_engine,
    _ghost_b_partial_warning,
    _rehydrate_ghost_b_staging,
    _select_ghost_b_extraction_policy,
)
from services.storage.qdrant_writer import retrieve_schema_for_chunk

logger = logging.getLogger(__name__)


_PARTIAL_PREFIX = "Ghost B graph extraction partial:"
_BACKFILL_PREFIX = "Ghost B backfill"
_AUTO_BACKFILL_FAILED_PREFIX = "Ghost B auto-backfill failed:"


def _set_if_present(updates: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    updates[key] = value


def _failure_from_dict(row: dict[str, Any]) -> ExtractionFailureItem:
    retry_after = row.get("retry_after")
    if retry_after and not isinstance(retry_after, datetime):
        try:
            retry_after = datetime.fromisoformat(str(retry_after))
        except Exception:
            retry_after = None
    return ExtractionFailureItem(
        chunk_id=str(row.get("chunk_id") or ""),
        doc_id=str(row.get("doc_id") or ""),
        corpus_id=str(row.get("corpus_id") or ""),
        model=str(row.get("model") or ""),
        lane=int(row.get("lane") or 0),
        attempts=int(row.get("attempts") or 0),
        error_type=str(row.get("error_type") or "unknown"),
        error_message=str(row.get("error_message") or "")[:1000],
        retryable=bool(row.get("retryable", True)),
        retry_after=retry_after,
        backfill_attempt_count=int(row.get("backfill_attempt_count") or 0),
        lane_state=row.get("lane_state"),
    )


def _clean_graph_warnings(warnings: list[str]) -> list[str]:
    return [
        warning
        for warning in warnings
        if not warning.startswith(_PARTIAL_PREFIX)
        and not warning.startswith(_BACKFILL_PREFIX)
        and not warning.startswith(_AUTO_BACKFILL_FAILED_PREFIX)
    ]


def _merge_metrics(
    *,
    total_chunks: int,
    staged_results: list[ExtractionResult],
    remaining_failures: list[ExtractionFailureItem],
    previous_metrics: dict,
    retry_metrics: dict,
) -> dict:
    merged = summarize_extraction_batch(
        total_chunks=total_chunks,
        results=staged_results,
        failures=remaining_failures,
        call_metrics=[],
        models=list(
            dict.fromkeys(
                [
                    *[str(m) for m in previous_metrics.get("models", [])],
                    *[str(m) for m in retry_metrics.get("models", [])],
                ]
            )
        ),
    )
    for key in (
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "attempt_count",
        "json_recovery_count",
        "estimated_cost_tokens",
        "compact_extraction_chunks",
        "deep_extraction_chunks",
        "full_extraction_chunks",
        "skipped_low_value_chunks",
        "retryable_failed_chunks",
        "retry_budget_exhausted_count",
        "all_lanes_exhausted_count",
        "lane_cooling_down_count",
        "provider_error_count",
        "rate_limited_count",
        "timeout_count",
    ):
        merged[key] = int(previous_metrics.get(key) or 0) + int(
            retry_metrics.get(key) or 0
        )
    requested = int(merged.get("requested_chunks") or total_chunks or 0)
    attempts = int(merged.get("attempt_count") or 0)
    recoveries = int(merged.get("json_recovery_count") or 0)
    prompt_tokens = int(merged.get("prompt_tokens") or 0)
    merged["avg_prompt_tokens_per_chunk"] = (
        round(prompt_tokens / requested, 2) if requested else 0.0
    )
    merged["json_recovery_rate"] = (
        round(recoveries / requested, 4) if requested else 0.0
    )
    merged["json_recovery_attempt_rate"] = (
        round(recoveries / attempts, 4) if attempts else 0.0
    )
    for key in (
        "extraction_strategy",
        "extraction_mode",
        "graph_completeness",
        "large_doc_child_threshold",
        "full_extract_max_children",
        "max_entities_per_chunk",
        "max_relations_per_chunk",
        "max_completion_tokens",
        "skipped_low_value_by_kind",
        "graph_retry_after",
        "lane_state_counts",
        "disabled_lane_count",
        "retry_policy",
    ):
        if retry_metrics.get(key) is not None:
            merged[key] = retry_metrics.get(key)
        elif previous_metrics.get(key) is not None:
            merged[key] = previous_metrics.get(key)
    merged["total_duration_seconds"] = round(
        float(previous_metrics.get("total_duration_seconds") or 0.0)
        + float(retry_metrics.get("total_duration_seconds") or 0.0),
        3,
    )
    old_errors = dict(previous_metrics.get("error_counts") or {})
    new_errors = dict(retry_metrics.get("error_counts") or {})
    for key, value in new_errors.items():
        old_errors[key] = int(old_errors.get(key) or 0) + int(value or 0)
    merged["error_counts"] = old_errors
    return merged


async def backfill_failed_graph_chunks(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver: Any,
    corpus_id: str,
    doc_id: str,
    user_id: str,
) -> dict:
    """Retry failed Ghost B chunks and patch Neo4j without reingesting the file."""
    if neo4j_driver is None:
        raise RuntimeError("Neo4j driver is not available")

    doc = await db["documents"].find_one({"doc_id": doc_id, "corpus_id": corpus_id})
    if not doc:
        raise ValueError("Document not found")

    failures = [
        _failure_from_dict(row)
        for row in (doc.get("ghost_b_failures") or [])
        if row.get("chunk_id")
    ]
    if not failures:
        return {
            "status": "noop",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "retried_chunks": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": 0,
        }

    now = datetime.utcnow()
    not_ready = [
        failure
        for failure in failures
        if failure.retry_after is not None and failure.retry_after > now
    ]
    if not_ready:
        retry_after = min(f.retry_after for f in not_ready if f.retry_after is not None)
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "$set": {
                    "write_state.graph_status": GRAPH_RETRY_SCHEDULED,
                    "write_state.graph_retry_after": retry_after,
                    "write_state.graph_retryable_failed_chunk_count": len(failures),
                    "decision_trace.graph_status": GRAPH_RETRY_SCHEDULED,
                    "decision_trace.graph_retry_after": retry_after,
                    "updated_at": now,
                }
            },
        )
        return {
            "status": "retry_scheduled",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "retried_chunks": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": len(failures),
            "retry_after": retry_after.isoformat(),
        }

    corpus = await db["corpora"].find_one({"corpus_id": corpus_id})
    live_cfg = (corpus or {}).get("default_ingestion_config") or {}
    from services.ingestion_service import build_effective_config

    config = build_effective_config(
        frozen_base=doc.get("ingestion_config") or live_cfg,
        live_corpus=live_cfg,
        ingest_overrides=None,
    )
    max_chunks = max(1, int(getattr(config, "graph_backfill_max_chunks", 100) or 100))
    max_attempts = max(
        1, int(getattr(config, "graph_backfill_max_attempts_per_chunk", 2) or 2)
    )
    eligible_failures = [
        failure
        for failure in failures
        if failure.retryable and failure.backfill_attempt_count < max_attempts
    ][:max_chunks]
    if not eligible_failures:
        retry_after = None
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "$set": {
                    "write_state.graph_status": GRAPH_NEEDS_BACKFILL,
                    "write_state.graph_retryable_failed_chunk_count": 0,
                    "write_state.graph_last_backfill_error": "No retryable failed chunks remain within backfill attempt budget.",
                    "decision_trace.graph_status": GRAPH_NEEDS_BACKFILL,
                    "updated_at": now,
                }
            },
        )
        return {
            "status": "retry_budget_exhausted",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "retried_chunks": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": len(failures),
            "retry_after": retry_after,
        }

    failed_ids = list(dict.fromkeys(f.chunk_id for f in eligible_failures if f.chunk_id))
    chunk_rows = await db["chunks"].find(
        {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "chunk_id": {"$in": failed_ids},
        },
        {"chunk_id": 1, "text": 1, "heading_path": 1, "chunk_kind": 1, "_id": 0},
    ).to_list(length=None)
    chunk_by_id = {row["chunk_id"]: row for row in chunk_rows}
    tasks = [
        ExtractionTask(
            chunk_id=chunk_id,
            doc_id=doc_id,
            corpus_id=corpus_id,
            text=str(chunk_by_id[chunk_id].get("text") or ""),
            document_title=str(doc.get("filename") or doc_id),
            heading_path=chunk_by_id[chunk_id].get("heading_path"),
            chunk_kind=str(chunk_by_id[chunk_id].get("chunk_kind") or "body"),
        )
        for chunk_id in failed_ids
        if chunk_id in chunk_by_id
    ]
    if not tasks:
        raise RuntimeError("Failed chunk records exist, but no matching chunks were found")

    previous_metrics = doc.get("ghost_b_metrics") or {}
    policy = _select_ghost_b_extraction_policy(
        config,
        total_children=int(doc.get("chunk_count") or previous_metrics.get("total_children") or len(tasks)),
        body_children=int(previous_metrics.get("body_children") or previous_metrics.get("requested_chunks") or len(tasks)),
        skipped_low_value_by_kind=previous_metrics.get("skipped_low_value_by_kind") or {},
    )
    schema_ctx = SchemaContext(
        entity_schema=config.entity_schema,
        relation_schema=config.relation_schema,
        strict=config.schema_strict,
    )
    pool = (
        _build_ghost_pool(config.summary_models)
        if config.models_linked or not config.extraction_models
        else _build_ghost_pool(config.extraction_models)
    )
    repair_pool = _build_ghost_pool(
        getattr(config, "extraction_repair_models", None) or []
    )

    async def _schema_resolver(kind: str, query_vec: list[float], top_k: int) -> list[str]:
        return await retrieve_schema_for_chunk(qdrant_client, corpus_id, kind, query_vec, top_k)

    schema_lens = doc.get("schema_lens") or (doc.get("ghost_b_metrics") or {}).get("schema_lens")
    llm_kwargs = {
        "schema": schema_ctx,
        "schema_lens": schema_lens,
        "chunk_vectors": None,
        "schema_resolver": _schema_resolver,
        "pool": pool,
        "repair_pool": repair_pool,
        "model": None,
        "return_report": True,
        "extraction_mode": policy.extraction_mode,
        "max_entities_per_chunk": policy.max_entities_per_chunk,
        "max_relations_per_chunk": policy.max_relations_per_chunk,
        "max_completion_tokens_override": policy.max_completion_tokens,
        "per_chunk_max_attempts": getattr(config, "graph_backfill_max_attempts_per_chunk", 2),
        "per_doc_max_failed_chunks_before_pause": max_chunks,
        "per_lane_max_consecutive_failures": getattr(
            config, "graph_per_lane_max_consecutive_failures", 2
        ),
        "per_lane_cooldown_seconds": getattr(
            config, "graph_per_lane_cooldown_seconds", 300
        ),
        "metrics_context": {
            **policy.metrics(),
            "extraction_strategy": f"{policy.extraction_strategy}_backfill",
            "graph_extraction_engine_requested": _graph_extraction_engine(config),
        },
    }
    report = await extract_entities(tasks, **llm_kwargs)
    if not isinstance(report, ExtractionBatchReport):
        raise RuntimeError("Ghost B did not return a batch report")

    recovered_ids = {result.chunk_id for result in report.results}
    retry_failure_by_id = {failure.chunk_id: failure for failure in report.failures}
    attempted_ids = {failure.chunk_id for failure in eligible_failures}
    remaining_failures = [
        retry_failure_by_id.get(
            failure.chunk_id,
            ExtractionFailureItem(
                **{
                    **asdict(failure),
                    "backfill_attempt_count": int(failure.backfill_attempt_count or 0)
                    + (1 if failure.chunk_id in attempted_ids else 0),
                }
            ),
        )
        for failure in failures
        if failure.chunk_id not in recovered_ids
    ]
    for failure in remaining_failures:
        if failure.chunk_id in attempted_ids and failure.chunk_id in retry_failure_by_id:
            failure.backfill_attempt_count = int(failure.backfill_attempt_count or 0) + 1

    staged_results = _rehydrate_ghost_b_staging(doc.get("ghost_b_staging") or [])
    staged_by_chunk = {result.chunk_id: result for result in staged_results}
    for result in report.results:
        staged_by_chunk[result.chunk_id] = result
    staged_results = list(staged_by_chunk.values())

    all_chunk_ids = [
        row["chunk_id"]
        async for row in db["chunks"].find(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"chunk_id": 1, "_id": 0},
        )
    ]
    if report.results:
        await write_document_graph(
            driver=neo4j_driver,
            doc_id=doc_id,
            corpus_id=corpus_id,
            extraction_results=report.results,
            user_id=user_id,
            file_id=doc.get("file_id"),
            all_chunk_ids=all_chunk_ids,
        )
        await mark_graph_metrics_stale(
            db,
            corpus_id,
            reason="graph_backfill_write",
        )

    warnings = _clean_graph_warnings((doc.get("write_state") or {}).get("warnings") or [])
    if remaining_failures:
        warnings.append(
            _ghost_b_partial_warning(
                extracted=max(len(all_chunk_ids) - len(remaining_failures), 0),
                total=len(all_chunk_ids),
            )
        )
        warnings.append(
            f"Ghost B backfill retried {len(tasks)} chunks and recovered {len(recovered_ids)}; "
            f"{len(remaining_failures)} still need graph extraction."
        )
    elif recovered_ids:
        warnings.append(
            f"Ghost B backfill recovered {len(recovered_ids)} failed graph chunks."
        )

    metrics = _merge_metrics(
        total_chunks=len(all_chunk_ids),
        staged_results=staged_results,
        remaining_failures=remaining_failures,
        previous_metrics=previous_metrics,
        retry_metrics=report.metrics,
    )
    retry_after_values = [
        failure.retry_after
        for failure in remaining_failures
        if failure.retry_after is not None and failure.retry_after > datetime.utcnow()
    ]
    next_retry_after = min(retry_after_values) if retry_after_values else None
    graph_status = (
        GRAPH_RETRY_SCHEDULED
        if remaining_failures and next_retry_after
        else GRAPH_NEEDS_BACKFILL
        if remaining_failures
        else GRAPH_READY
    )
    graph_completeness = (
        "needs-backfill"
        if remaining_failures
        else str(metrics.get("graph_completeness") or "graph-complete")
    )
    now = datetime.utcnow()
    update_fields: dict[str, Any] = {
        "ghost_b_staging": [asdict(result) for result in staged_results],
        "ghost_b_failures": [asdict(failure) for failure in remaining_failures],
        "ghost_b_metrics": metrics,
        "write_state.warnings": warnings,
        "write_state.graph_status": graph_status,
        "write_state.graph_extracted_chunk_count": len(staged_results),
        "write_state.graph_failed_chunk_count": len(remaining_failures),
        "write_state.graph_completeness": graph_completeness,
        "write_state.graph_extraction_finished_at": now,
        "write_state.graph_retry_after": next_retry_after,
        "write_state.graph_backfill_attempt_count": int(
            ((doc.get("write_state") or {}).get("graph_backfill_attempt_count") or 0)
        )
        + 1,
        "write_state.graph_last_backfill_error": (
            None if not remaining_failures else "Some failed graph chunks remain after bounded backfill."
        ),
        "write_state.graph_retryable_failed_chunk_count": sum(
            1 for failure in remaining_failures if failure.retryable
        ),
        "decision_trace.graph_status": graph_status,
        "decision_trace.graph_extracted_chunks": len(staged_results),
        "decision_trace.graph_failed_chunks": len(remaining_failures),
        "decision_trace.graph_requested_chunks": len(all_chunk_ids),
        "decision_trace.graph_completeness": graph_completeness,
        "decision_trace.graph_retry_after": next_retry_after,
        "decision_trace.vector_ready": True,
        "updated_at": now,
    }
    extraction_strategy = metrics.get("extraction_strategy")
    _set_if_present(
        update_fields,
        "write_state.graph_extraction_strategy",
        str(extraction_strategy) if extraction_strategy is not None else None,
    )
    _set_if_present(
        update_fields,
        "decision_trace.graph_strategy",
        str(extraction_strategy) if extraction_strategy is not None else None,
    )
    _set_if_present(
        update_fields,
        "decision_trace.graph_mode",
        metrics.get("extraction_mode"),
    )

    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"$set": update_fields},
    )
    logger.info(
        "phase=ghost_b_backfill doc=%s corpus=%s retried=%d recovered=%d remaining=%d",
        doc_id[:12],
        corpus_id[:8],
        len(tasks),
        len(recovered_ids),
        len(remaining_failures),
    )
    return {
        "status": "done",
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "retried_chunks": len(tasks),
        "recovered_chunks": len(recovered_ids),
        "remaining_failed_chunks": len(remaining_failures),
        "retry_after": next_retry_after.isoformat() if next_retry_after else None,
    }
