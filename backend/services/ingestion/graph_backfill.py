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
from services.graph.neo4j_writer import write_document_graph
from services.ingestion.worker import (
    GRAPH_NEEDS_BACKFILL,
    GRAPH_READY,
    _build_ghost_pool,
    _ghost_b_partial_warning,
    _rehydrate_ghost_b_staging,
    _select_ghost_b_extraction_policy,
)
from services.storage.qdrant_writer import retrieve_schema_for_chunk

logger = logging.getLogger(__name__)


_PARTIAL_PREFIX = "Ghost B graph extraction partial:"
_BACKFILL_PREFIX = "Ghost B backfill"
_AUTO_BACKFILL_FAILED_PREFIX = "Ghost B auto-backfill failed:"


def _failure_from_dict(row: dict[str, Any]) -> ExtractionFailureItem:
    return ExtractionFailureItem(
        chunk_id=str(row.get("chunk_id") or ""),
        doc_id=str(row.get("doc_id") or ""),
        corpus_id=str(row.get("corpus_id") or ""),
        model=str(row.get("model") or ""),
        lane=int(row.get("lane") or 0),
        attempts=int(row.get("attempts") or 0),
        error_type=str(row.get("error_type") or "unknown"),
        error_message=str(row.get("error_message") or "")[:1000],
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
        "graph_completeness",
        "large_doc_child_threshold",
        "full_extract_max_children",
        "max_entities_per_chunk",
        "max_relations_per_chunk",
        "max_completion_tokens",
        "skipped_low_value_by_kind",
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

    failed_ids = list(dict.fromkeys(f.chunk_id for f in failures if f.chunk_id))
    chunk_rows = await db["chunks"].find(
        {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "chunk_id": {"$in": failed_ids},
        },
        {"chunk_id": 1, "text": 1, "_id": 0},
    ).to_list(length=None)
    chunk_by_id = {row["chunk_id"]: row for row in chunk_rows}
    tasks = [
        ExtractionTask(
            chunk_id=chunk_id,
            doc_id=doc_id,
            corpus_id=corpus_id,
            text=str(chunk_by_id[chunk_id].get("text") or ""),
        )
        for chunk_id in failed_ids
        if chunk_id in chunk_by_id
    ]
    if not tasks:
        raise RuntimeError("Failed chunk records exist, but no matching chunks were found")

    corpus = await db["corpora"].find_one({"corpus_id": corpus_id})
    live_cfg = (corpus or {}).get("default_ingestion_config") or {}
    from services.ingestion_service import build_effective_config

    config = build_effective_config(
        frozen_base=doc.get("ingestion_config") or live_cfg,
        live_corpus=live_cfg,
        ingest_overrides=None,
    )
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

    async def _schema_resolver(kind: str, query_vec: list[float], top_k: int) -> list[str]:
        return await retrieve_schema_for_chunk(qdrant_client, corpus_id, kind, query_vec, top_k)

    report = await extract_entities(
        tasks,
        schema=schema_ctx,
        schema_lens=doc.get("schema_lens")
        or (doc.get("ghost_b_metrics") or {}).get("schema_lens"),
        chunk_vectors=None,
        schema_resolver=_schema_resolver,
        pool=pool,
        model=None,
        return_report=True,
        extraction_mode=policy.extraction_mode,  # type: ignore[arg-type]
        max_entities_per_chunk=policy.max_entities_per_chunk,
        max_relations_per_chunk=policy.max_relations_per_chunk,
        max_completion_tokens_override=policy.max_completion_tokens,
        metrics_context={
            **policy.metrics(),
            "extraction_strategy": f"{policy.extraction_strategy}_backfill",
        },
    )
    if not isinstance(report, ExtractionBatchReport):
        raise RuntimeError("Ghost B did not return a batch report")

    recovered_ids = {result.chunk_id for result in report.results}
    retry_failure_by_id = {failure.chunk_id: failure for failure in report.failures}
    remaining_failures = [
        retry_failure_by_id.get(failure.chunk_id, failure)
        for failure in failures
        if failure.chunk_id not in recovered_ids
    ]

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
    graph_status = GRAPH_NEEDS_BACKFILL if remaining_failures else GRAPH_READY
    graph_completeness = (
        "needs-backfill"
        if remaining_failures
        else str(metrics.get("graph_completeness") or "graph-complete")
    )

    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "ghost_b_staging": [asdict(result) for result in staged_results],
                "ghost_b_failures": [asdict(failure) for failure in remaining_failures],
                "ghost_b_metrics": metrics,
                "write_state.warnings": warnings,
                "write_state.graph_status": graph_status,
                "write_state.graph_extracted_chunk_count": len(staged_results),
                "write_state.graph_failed_chunk_count": len(remaining_failures),
                "write_state.graph_extraction_strategy": str(
                    metrics.get("extraction_strategy") or ""
                ),
                "write_state.graph_completeness": graph_completeness,
                "write_state.graph_extraction_finished_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
        },
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
    }
