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
    EntityItem,
    ExtractionBatchReport,
    ExtractionFailureItem,
    ExtractionResult,
    ExtractionTask,
    FactItem,
    RelationItem,
    summarize_extraction_batch,
)
from services.graph.neo4j_writer import write_document_graph
from services.ingestion.section_classifier import ChunkKind, should_skip_ghost_b
from services.storage import mongo_reader, mongo_writer

logger = logging.getLogger(__name__)


_PARTIAL_PREFIX = "Ghost B graph extraction partial:"
_BACKFILL_PREFIX = "Ghost B backfill"


def _ghost_b_partial_warning(
    *,
    extracted: int,
    total: int,
) -> str:
    skipped = max(total - extracted, 0)
    return (
        f"Ghost B graph extraction partial: {extracted}/{total} chunks produced "
        f"entities/relations; {skipped} chunks remain available for vector RAG "
        "but have no extracted graph entities."
    )


def _rehydrate_ghost_b_staging(staged: list[dict[str, Any]]) -> list[ExtractionResult]:
    """Reconstruct ExtractionResult dataclasses from Mongo-stored staging rows."""

    out: list[ExtractionResult] = []
    for row in staged:
        out.append(
            ExtractionResult(
                schema_version=row.get("schema_version", "polymath.extract.v1"),
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                corpus_id=row["corpus_id"],
                text=row.get("text", ""),
                entities=[EntityItem(**item) for item in row.get("entities", [])],
                relations=[RelationItem(**item) for item in row.get("relations", [])],
                facts=[FactItem(**item) for item in row.get("facts", [])],
                entity_remap_count=row.get("entity_remap_count", 0),
                entity_drop_count=row.get("entity_drop_count", 0),
                relation_remap_count=row.get("relation_remap_count", 0),
                relation_drop_count=row.get("relation_drop_count", 0),
                domain_range_remap_count=row.get("domain_range_remap_count", 0),
                domain_range_warn_count=row.get("domain_range_warn_count", 0),
                endpoint_completion_count=row.get("endpoint_completion_count", 0),
                evidence_cue_repair_count=row.get("evidence_cue_repair_count", 0),
                semantic_direction_repair_count=row.get("semantic_direction_repair_count", 0),
                semantic_direction_drop_count=row.get("semantic_direction_drop_count", 0),
                entity_evidence_drop_count=row.get("entity_evidence_drop_count", 0),
                citation_drop_count=row.get("citation_drop_count", 0),
                strict_entity_drop_count=row.get("strict_entity_drop_count", 0),
                strict_relation_drop_count=row.get("strict_relation_drop_count", 0),
                evidence_drop_count=row.get("evidence_drop_count", 0),
                fact_drop_count=row.get("fact_drop_count", 0),
                schema_lens_id=row.get("schema_lens_id"),
                # T-HOOK-1 — rehydrated rows are re-stashed via ReplaceOne, so
                # the additive capture fields must round-trip or a backfill
                # pass would silently erase persisted temporal captures.
                temporal_captures=list(row.get("temporal_captures") or []),
                temporal_capture_version=row.get("temporal_capture_version"),
            )
        )
    return out


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
        provider=str(row.get("provider") or ""),
        schema_mode=str(row.get("schema_mode") or ""),
        output_mode=str(row.get("output_mode") or ""),
        json_repair_mode=str(row.get("json_repair_mode") or ""),
        semantic_verifier_mode=str(row.get("semantic_verifier_mode") or ""),
        provider_card=(
            row.get("provider_card")
            if isinstance(row.get("provider_card"), dict)
            else {}
        ),
    )


def _clean_graph_warnings(warnings: list[str]) -> list[str]:
    return [
        warning
        for warning in warnings
        if not warning.startswith(_PARTIAL_PREFIX)
        and not warning.startswith(_BACKFILL_PREFIX)
    ]


def _is_graph_verify_error(raw: object) -> bool:
    text = str(raw or "").lower()
    return "neo4j" in text or "has_chunk" in text


def _graph_verify_mismatch(write_state: dict[str, Any]) -> bool:
    if write_state.get("verified") is True:
        return False
    return any(_is_graph_verify_error(err) for err in write_state.get("verify_errors") or [])


def _write_state_after_graph_flush(write_state: dict[str, Any]) -> dict[str, Any]:
    remaining_verify_errors = [
        err
        for err in write_state.get("verify_errors") or []
        if not _is_graph_verify_error(err)
    ]
    return {
        "write_state.neo4j_written": True,
        "write_state.verify_errors": remaining_verify_errors,
        "write_state.verified": True if not remaining_verify_errors else False,
    }


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
    for key in ("total_tokens", "prompt_tokens", "completion_tokens", "attempt_count"):
        merged[key] = int(previous_metrics.get(key) or 0) + int(
            retry_metrics.get(key) or 0
        )
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


def _schema_lens_id(doc: dict, metrics: dict) -> str | None:
    value = (doc.get("ingestion_config") or {}).get("schema_lens_id") or metrics.get(
        "schema_lens"
    )
    return value if isinstance(value, str) else None


def _graph_parent_count(doc: dict) -> int:
    if doc.get("parent_count") is not None:
        try:
            return int(doc.get("parent_count") or 0)
        except Exception:
            pass
    parents = doc.get("parent_chunks") or []
    return len(parents) if isinstance(parents, list) else 0


async def _chunk_parent_map(
    db: AsyncIOMotorDatabase,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, str]:
    rows = await db["chunks"].find(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"chunk_id": 1, "parent_id": 1, "_id": 0},
    ).to_list(length=None)
    return {
        str(row.get("chunk_id") or ""): str(row.get("parent_id") or "")
        for row in rows
        if row.get("chunk_id")
    }


async def _write_graph_results(
    *,
    db: AsyncIOMotorDatabase,
    neo4j_driver: Any,
    doc: dict,
    corpus_id: str,
    doc_id: str,
    user_id: str,
    extraction_results: list[ExtractionResult],
    all_chunk_ids: list[str],
    metrics: dict,
    chunk_parent_ids: dict[str, str] | None = None,
    parent_count: int | None = None,
) -> None:
    from services.graph.projection_runner import project_document_via_control_plane

    # COMPILE GATE (owner architecture 2026-08-12): raw encoder proposals are
    # the ledger; only compiled, gated facts become graph edges. Without this
    # the graph accumulated untypeable "other" nodes and edges whose endpoints
    # were never entities.
    try:
        from services.extraction.extraction_compiler import filter_results_to_compiled

        corpus_row = await db["corpora"].find_one(
            {"corpus_id": corpus_id}, {"ontology_domain": 1}
        )
        extraction_results, compile_metrics = filter_results_to_compiled(
            extraction_results,
            domain=str((corpus_row or {}).get("ontology_domain") or "") or None,
            result_cls=ExtractionResult,
        )
        metrics = {**(metrics or {}), "compiler": compile_metrics}
        logger.info(
            "compile gate: doc=%s domain=%s facts_to_graph=%s accepted=%s review=%s rejected=%s",
            doc_id[:12],
            compile_metrics.get("domain"),
            compile_metrics.get("graph_written_facts"),
            compile_metrics.get("accepted"),
            compile_metrics.get("review"),
            compile_metrics.get("rejected"),
        )
    except Exception as exc:  # noqa: BLE001 — never lose a graph write to the gate
        logger.warning("compile gate skipped for doc=%s: %s", doc_id[:12], exc)

    await project_document_via_control_plane(
        db=db,
        neo4j_driver=neo4j_driver,
        corpus_id=corpus_id,
        doc_id=doc_id,
        write_fn=write_document_graph,
        write_kwargs={
            "driver": neo4j_driver,
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "extraction_results": extraction_results,
            "user_id": user_id,
            "file_id": doc.get("file_id"),
            "all_chunk_ids": all_chunk_ids,
            "filename": doc.get("filename"),
            "parent_count": (
                parent_count if parent_count is not None else _graph_parent_count(doc)
            ),
            "schema_lens_id": _schema_lens_id(doc, metrics),
            "ghost_b_success_rate": (
                float(metrics["success_rate"])
                if metrics.get("success_rate") is not None
                else None
            ),
            "ghost_b_extracted": (
                int(metrics["extracted_chunks"])
                if metrics.get("extracted_chunks") is not None
                else None
            ),
            "ghost_b_total": (
                int(metrics["requested_chunks"])
                if metrics.get("requested_chunks") is not None
                else None
            ),
            "db": db,
            "chunk_parent_ids": chunk_parent_ids,
        },
    )


async def _load_backfill_config(
    *,
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    doc: dict,
) -> IngestionConfig:
    corpus = await db["corpora"].find_one({"corpus_id": corpus_id})
    live_cfg = (corpus or {}).get("default_ingestion_config") or {}
    from services.ingestion_service import build_effective_config

    return build_effective_config(
        frozen_base=doc.get("ingestion_config") or live_cfg,
        live_corpus=live_cfg,
        ingest_overrides=None,
    )


async def _extract_tasks(
    *,
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    doc_id: str,
    chunk_ids: list[str] | None = None,
) -> tuple[list[ExtractionTask], list[str]]:
    query: dict[str, Any] = {"doc_id": doc_id, "corpus_id": corpus_id}
    if chunk_ids is not None:
        query["chunk_id"] = {"$in": chunk_ids}
    rows = await db["chunks"].find(
        query,
        {"chunk_id": 1, "text": 1, "chunk_kind": 1, "parent_id": 1, "_id": 0},
    ).to_list(length=None)
    all_chunk_ids = [str(row.get("chunk_id") or "") for row in rows if row.get("chunk_id")]
    tasks = [
        ExtractionTask(
            chunk_id=str(row["chunk_id"]),
            doc_id=doc_id,
            corpus_id=corpus_id,
            text=str(row.get("text") or ""),
            metadata={"parent_id": str(row.get("parent_id") or "")},
        )
        for row in rows
        if row.get("chunk_id")
        and str(row.get("text") or "").strip()
        and not should_skip_ghost_b(str(row.get("chunk_kind") or ChunkKind.BODY))
    ]
    return tasks, all_chunk_ids


async def _run_ghost_b_backfill(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    corpus_id: str,
    tasks: list[ExtractionTask],
    config: IngestionConfig,
) -> ExtractionBatchReport:
    from services.ingestion.extraction_contract import resolve_configured_engine

    engine = str(
        await resolve_configured_engine(
            db, getattr(config, "extraction_engine", "graphify_cpu")
        )
        or "graphify_cpu"
    )
    if engine == "off":
        return ExtractionBatchReport(
            results=[],
            failures=[],
            metrics={
                "engine": "off",
                "requested_chunks": len(tasks),
                "extracted_chunks": 0,
                "failed_chunks": 0,
                "skipped": True,
            },
        )
    if engine == "ghost_b_llm":
        # Seam completion (2026-08-10): the dead-last enrichment pass rides
        # this backfill path, so it must honor engine selection exactly like
        # the inline worker dispatch — same pool builder, same lifecycle
        # auto-start, same ExtractionResponse flow into the graph writer.
        if not tasks:
            return ExtractionBatchReport(
                results=[], failures=[], metrics={"engine": engine}
            )
        from services.ghost_b import SchemaContext, extract_entities
        from services.ingestion.model_lifecycle import ensure_model_lifecycle_ready
        from services.ingestion.worker import _build_ghost_pool

        pool = _build_ghost_pool(
            list(getattr(config, "extraction_models", None) or [])
        )
        if not pool:
            raise RuntimeError(
                "ghost_b_llm requires a configured extraction_models pool"
            )
        ready_pool = await ensure_model_lifecycle_ready(
            pool, purpose="graph_backfill"
        )
        if ready_pool is not None:
            pool = ready_pool
        if not pool:
            raise RuntimeError(
                "ghost_b_llm has no lifecycle-ready extraction_models lanes"
            )
        from config import get_settings as _gs

        schema = SchemaContext(
            entity_schema=config.entity_schema,
            relation_schema=config.relation_schema,
            strict=config.schema_strict,
        )
        # Sibling batching (owner ruling 2026-08-12): prompt once per parent
        # group, keep one artifact per child. See sibling_batching module for
        # why this beats parent-level JOBS on blast radius.
        from services.extraction import sibling_batching as _sib

        groups = None
        prompt_tasks = tasks
        if _sib.batching_enabled() and len(tasks) > 1:
            candidate = _sib.group_sibling_tasks(tasks)
            if len(candidate) < len(tasks):
                groups = candidate
                prompt_tasks = [
                    _sib.merge_group(group, ExtractionTask) for group in candidate
                ]
        report = await extract_entities(
            prompt_tasks,
            schema=schema,
            pool=pool,
            return_report=True,
            enable_facts=_gs().EXTRACTION_ENABLE_FACTS,
        )
        if isinstance(report, ExtractionBatchReport):
            if groups:
                expanded, failures = _sib.expand_report(
                    report,
                    groups,
                    result_cls=ExtractionResult,
                    failure_cls=ExtractionFailureItem,
                )
                report = ExtractionBatchReport(
                    results=expanded,
                    failures=failures,
                    metrics={
                        **(report.metrics or {}),
                        "sibling_groups": len(groups),
                        "sibling_children": len(tasks),
                    },
                )
            report.metrics["engine"] = "ghost_b_llm"
            return report
        return ExtractionBatchReport(
            results=list(report), failures=[], metrics={"engine": "ghost_b_llm"}
        )
    if engine == "encoder":
        # Thin encoder path: window -> GLiNER-Relex -> exact offset
        # attribution -> per-child results. No model in this process, so
        # lanes stay small; no LLM, so output is reproducible.
        from services.extraction.thin_encoder_path import run_thin_extraction

        if not tasks:
            return ExtractionBatchReport(
                results=[], failures=[], metrics={"engine": "encoder"}
            )
        # Domain vocabularies live on the corpus (IngestionConfig.entity_schema /
        # relation_schema); they are what the encoder is asked to find.
        results, metrics = await run_thin_extraction(
            tasks,
            entity_labels=list(getattr(config, "entity_schema", None) or []) or None,
            relation_labels=list(getattr(config, "relation_schema", None) or []) or None,
        )
        return ExtractionBatchReport(results=results, failures=[], metrics=metrics)
    if engine != "graphify_cpu":
        raise RuntimeError(f"unsupported extraction engine: {engine}")
    if not tasks:
        return ExtractionBatchReport(results=[], failures=[], metrics={"engine": engine})

    from services.extraction.graphify_pipeline import run_graphify_pipeline

    output = await run_graphify_pipeline(
        db=db,
        corpus_id=corpus_id,
        doc_id=tasks[0].doc_id,
        text="\n\n".join(task.text for task in tasks),
        children=tasks,
        source_uri="graph_backfill",
    )
    return output.report


async def backfill_failed_graph_chunks(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver: Any,
    corpus_id: str,
    doc_id: str,
    user_id: str,
    allow_extraction: bool = True,
) -> dict:
    """Retry failed Ghost B chunks AND/OR flush staged extraction to Neo4j.

    Pt 9 — this function now serves two complementary purposes that share
    the same Mongo state and the same writer call:

    1. **Failure retry** — if `ghost_b_failures` is non-empty and
       ``allow_extraction`` is true, re-extract those chunks via Ghost B and
       merge the results into staging.
    2. **Neo4j flush** — if `write_state.neo4j_written` is not True, or
       verification proves Neo4j chunk links are broken, and `ghost_b_staging`
       is non-empty, fire `write_document_graph` against the staged results so
       Neo4j catches up.

    Either trigger (or both) leads to the same idempotent MERGE pass —
    safe to call repeatedly. The function is no-op only when there's
    genuinely nothing to do (no failures AND Neo4j already written, or
    no staging available).
    """
    if neo4j_driver is None:
        raise RuntimeError("Neo4j driver is not available")

    doc = await db["documents"].find_one({"doc_id": doc_id, "corpus_id": corpus_id})
    if not doc:
        raise ValueError("Document not found")

    parent_count = await mongo_reader.count_parent_chunks(db, doc_id, corpus_id)
    if not parent_count:
        parent_count = _graph_parent_count(doc)

    failure_rows = await mongo_reader.read_ghost_b_failures(db, doc_id, corpus_id)
    failures = [
        _failure_from_dict(row)
        for row in failure_rows
        if row.get("chunk_id")
    ]
    write_state = doc.get("write_state") or {}
    neo4j_already_written = bool(write_state.get("neo4j_written"))
    neo4j_verify_mismatch = _graph_verify_mismatch(write_state)
    staged_raw = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id) or []
    needs_graph_repair = (not neo4j_already_written) or neo4j_verify_mismatch
    needs_neo4j_flush = needs_graph_repair and bool(staged_raw)
    needs_full_replay = (
        not failures
        and not needs_neo4j_flush
        and needs_graph_repair
        and bool(write_state.get("mongo_written"))
        and bool(write_state.get("qdrant_written"))
    )

    # Pt 9 — early-return only when there's truly nothing to do.
    if not failures and not needs_neo4j_flush and not needs_full_replay:
        return {
            "status": "noop",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "retried_chunks": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": 0,
            "neo4j_flushed": False,
        }

    # Pt 9 — when Neo4j needs flushing and staged rows already exist, take a
    # fast path that skips Ghost B entirely and just runs the writer on those
    # existing results. Graph-promotion jobs call this helper with
    # ``allow_extraction=False`` so promotion can flush known-good artifacts
    # without rerunning Graphify extraction for failed chunks.
    if needs_neo4j_flush and (not failures or not allow_extraction):
        staged_results = _rehydrate_ghost_b_staging(staged_raw)
        if not staged_results:
            return {
                "status": "noop",
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "retried_chunks": 0,
                "recovered_chunks": 0,
                "remaining_failed_chunks": len(failures) if not allow_extraction else 0,
                "neo4j_flushed": False,
                "extraction_retry_skipped": not allow_extraction,
            }
        all_chunk_ids = [
            row["chunk_id"]
            async for row in db["chunks"].find(
                {"doc_id": doc_id, "corpus_id": corpus_id},
                {"chunk_id": 1, "_id": 0},
            )
        ]
        doc_metrics = doc.get("ghost_b_metrics") or {}
        parent_by_chunk = await _chunk_parent_map(db, corpus_id=corpus_id, doc_id=doc_id)
        await _write_graph_results(
            db=db,
            neo4j_driver=neo4j_driver,
            doc=doc,
            corpus_id=corpus_id,
            doc_id=doc_id,
            user_id=user_id,
            extraction_results=staged_results,
            all_chunk_ids=all_chunk_ids,
            chunk_parent_ids=parent_by_chunk,
            metrics=doc_metrics,
            parent_count=parent_count,
        )
        # Flip the flag — same contract the worker uses on success.
        update_set = {
            **_write_state_after_graph_flush(write_state),
            "updated_at": datetime.utcnow(),
        }
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"$set": update_set},
        )
        logger.info(
            "phase=ghost_b_backfill_flush doc=%s corpus=%s chunks=%d staged=%d",
            doc_id[:12], corpus_id[:8], len(all_chunk_ids), len(staged_results),
        )
        return {
            "status": "flushed_to_neo4j",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "retried_chunks": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": len(failures) if not allow_extraction else 0,
            "neo4j_flushed": True,
            "staged_results_written": len(staged_results),
            "extraction_retry_skipped": not allow_extraction,
        }

    # Pt 10 — full replay fallback. Some partial docs made it through
    # Mongo/Qdrant with `neo4j_written=False`, no failures, and no staging
    # left to flush. The raw file bytes are gone, but Mongo still has the child
    # chunks. Re-run Ghost B from those chunks and write a normal full graph.
    if needs_full_replay:
        if not allow_extraction:
            return {
                "status": "blocked_extraction_replay_required",
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "retried_chunks": 0,
                "recovered_chunks": 0,
                "remaining_failed_chunks": 0,
                "neo4j_flushed": False,
                "full_replay_required": True,
                "extraction_retry_skipped": True,
            }
        tasks, all_chunk_ids = await _extract_tasks(
            db=db,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        config = await _load_backfill_config(db=db, corpus_id=corpus_id, doc=doc)
        if not tasks:
            metrics = summarize_extraction_batch(
                total_chunks=len(all_chunk_ids),
                results=[],
                failures=[],
                call_metrics=[],
                models=[],
            )
            parent_by_chunk = await _chunk_parent_map(db, corpus_id=corpus_id, doc_id=doc_id)
            await _write_graph_results(
                db=db,
                neo4j_driver=neo4j_driver,
                doc=doc,
                corpus_id=corpus_id,
                doc_id=doc_id,
                user_id=user_id,
                extraction_results=[],
                all_chunk_ids=all_chunk_ids,
                chunk_parent_ids=parent_by_chunk,
                metrics=metrics,
            )
            await db["documents"].update_one(
                {"doc_id": doc_id, "corpus_id": corpus_id},
                {"$set": {
                    "ghost_b_staging_count": 0,
                    "ghost_b_failures": [],
                    "ghost_b_failure_count": 0,
                    "ghost_b_metrics": metrics,
                    **_write_state_after_graph_flush(write_state),
                    "updated_at": datetime.utcnow(),
                },
                "$unset": {"ghost_b_staging": ""},
                },
            )
            return {
                "status": "replayed_from_chunks",
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "retried_chunks": 0,
                "recovered_chunks": 0,
                "remaining_failed_chunks": 0,
                "neo4j_flushed": True,
                "full_replay": True,
            }

        report = await _run_ghost_b_backfill(
            db=db,
            qdrant_client=qdrant_client,
            corpus_id=corpus_id,
            tasks=tasks,
            config=config,
        )
        if not report.results and report.failures:
            raise RuntimeError(
                "Ghost B full replay could not recover any chunks from Mongo"
            )
        staged_results = list(report.results)
        remaining_failures = list(report.failures)
        metrics = _merge_metrics(
            total_chunks=len(all_chunk_ids),
            staged_results=staged_results,
            remaining_failures=remaining_failures,
            previous_metrics={},
            retry_metrics=report.metrics,
        )
        parent_by_chunk = await _chunk_parent_map(db, corpus_id=corpus_id, doc_id=doc_id)
        await _write_graph_results(
            db=db,
            neo4j_driver=neo4j_driver,
            doc=doc,
            corpus_id=corpus_id,
            doc_id=doc_id,
            user_id=user_id,
            extraction_results=staged_results,
            all_chunk_ids=all_chunk_ids,
            chunk_parent_ids=parent_by_chunk,
            metrics=metrics,
            parent_count=parent_count,
        )
        warnings = _clean_graph_warnings(
            (doc.get("write_state") or {}).get("warnings") or []
        )
        if remaining_failures:
            warnings.append(
                _ghost_b_partial_warning(
                    extracted=max(len(tasks) - len(remaining_failures), 0),
                    total=len(tasks),
                )
            )
            warnings.append(
                "Ghost B full replay from Mongo chunks recovered "
                f"{len(staged_results)} chunks; {len(remaining_failures)} still failed."
            )
        else:
            warnings.append(
                f"Ghost B full replay from Mongo chunks recovered {len(staged_results)} chunks."
            )
        await mongo_writer.stash_ghost_b(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            results=[asdict(result) for result in staged_results],
        )
        await mongo_writer.stash_ghost_b_failures(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            failures=[asdict(failure) for failure in remaining_failures],
        )
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"$set": {
                "ghost_b_staging_count": len(staged_results),
                "ghost_b_failures": [asdict(failure) for failure in remaining_failures][:20],
                "ghost_b_failure_count": len(remaining_failures),
                "ghost_b_metrics": metrics,
                **_write_state_after_graph_flush(write_state),
                "write_state.warnings": warnings,
                "updated_at": datetime.utcnow(),
            },
            "$unset": {"ghost_b_staging": ""},
            },
        )
        logger.info(
            "phase=ghost_b_backfill_full_replay doc=%s corpus=%s chunks=%d recovered=%d remaining=%d",
            doc_id[:12],
            corpus_id[:8],
            len(tasks),
            len(staged_results),
            len(remaining_failures),
        )
        return {
            "status": "replayed_from_chunks",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "retried_chunks": len(tasks),
            "recovered_chunks": len(staged_results),
            "remaining_failed_chunks": len(remaining_failures),
            "neo4j_flushed": True,
            "full_replay": True,
        }

    failed_ids = list(dict.fromkeys(f.chunk_id for f in failures if f.chunk_id))
    if not allow_extraction:
        return {
            "status": "blocked_extraction_required",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "retried_chunks": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": len(failures),
            "neo4j_flushed": False,
            "extraction_retry_skipped": True,
        }
    tasks, _ = await _extract_tasks(
        db=db,
        corpus_id=corpus_id,
        doc_id=doc_id,
        chunk_ids=failed_ids,
    )
    if not tasks:
        raise RuntimeError("Failed chunk records exist, but no matching chunks were found")

    config = await _load_backfill_config(db=db, corpus_id=corpus_id, doc=doc)
    report = await _run_ghost_b_backfill(
        db=db,
        qdrant_client=qdrant_client,
        corpus_id=corpus_id,
        tasks=tasks,
        config=config,
    )

    recovered_ids = {result.chunk_id for result in report.results}
    retry_failure_by_id = {failure.chunk_id: failure for failure in report.failures}
    remaining_failures = [
        retry_failure_by_id.get(failure.chunk_id, failure)
        for failure in failures
        if failure.chunk_id not in recovered_ids
    ]

    staged_results = _rehydrate_ghost_b_staging(staged_raw)
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
    graph_results_to_write = staged_results if needs_neo4j_flush else list(report.results)
    if graph_results_to_write:
        # Anchor metadata from Mongo so the Neo4j Document mirrors filename +
        # ghost_b health without a follow-up backfill pass.
        doc_metrics = doc.get("ghost_b_metrics") or {}
        success_rate = doc_metrics.get("success_rate")
        extracted = doc_metrics.get("extracted_chunks")
        total = doc_metrics.get("requested_chunks")
        schema_lens_id = (
            (doc.get("ingestion_config") or {}).get("schema_lens_id")
            or doc_metrics.get("schema_lens")
        )

        parent_by_chunk = await _chunk_parent_map(db, corpus_id=corpus_id, doc_id=doc_id)
        from services.graph.projection_runner import project_document_via_control_plane

        await project_document_via_control_plane(
            db=db,
            neo4j_driver=neo4j_driver,
            corpus_id=corpus_id,
            doc_id=doc_id,
            write_fn=write_document_graph,
            write_kwargs={
                "driver": neo4j_driver,
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "extraction_results": graph_results_to_write,
                "user_id": user_id,
                "file_id": doc.get("file_id"),
                "all_chunk_ids": all_chunk_ids,
                "filename": doc.get("filename"),
                "parent_count": parent_count,
                "schema_lens_id": (
                    schema_lens_id if isinstance(schema_lens_id, str) else None
                ),
                "ghost_b_success_rate": (
                    float(success_rate) if success_rate is not None else None
                ),
                "ghost_b_extracted": (
                    int(extracted) if extracted is not None else None
                ),
                "ghost_b_total": int(total) if total is not None else None,
                "db": db,
                "chunk_parent_ids": parent_by_chunk,
            },
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
        previous_metrics=doc.get("ghost_b_metrics") or {},
        retry_metrics=report.metrics,
    )

    # Pt 9 — if the writer was called (report.results non-empty), flip
    # `neo4j_written=True`. Pre-Pt-9 the function updated everything else
    # but left this flag untouched, so subsequent backfill calls saw
    # neo4j_written=False and re-fired the writer. MERGE makes that
    # safe but wasteful. Flipping the flag makes the contract honest.
    await mongo_writer.stash_ghost_b(
        db,
        doc_id=doc_id,
        corpus_id=corpus_id,
        results=[asdict(result) for result in staged_results],
    )
    await mongo_writer.stash_ghost_b_failures(
        db,
        doc_id=doc_id,
        corpus_id=corpus_id,
        failures=[asdict(failure) for failure in remaining_failures],
    )
    update_set: dict[str, Any] = {
        "ghost_b_staging_count": len(staged_results),
        "ghost_b_failures": [asdict(failure) for failure in remaining_failures][:20],
        "ghost_b_failure_count": len(remaining_failures),
        "ghost_b_metrics": metrics,
        "write_state.warnings": warnings,
        "updated_at": datetime.utcnow(),
    }
    if graph_results_to_write:
        update_set.update(_write_state_after_graph_flush(write_state))

    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"$set": update_set, "$unset": {"ghost_b_staging": ""}},
    )
    logger.info(
        "phase=ghost_b_backfill doc=%s corpus=%s retried=%d recovered=%d remaining=%d neo4j_written=%s",
        doc_id[:12],
        corpus_id[:8],
        len(tasks),
        len(recovered_ids),
        len(remaining_failures),
        bool(report.results),
    )
    return {
        "status": "done",
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "retried_chunks": len(tasks),
        "recovered_chunks": len(recovered_ids),
        "remaining_failed_chunks": len(remaining_failures),
        "neo4j_flushed": bool(graph_results_to_write),
        "staged_results_written": len(graph_results_to_write),
    }
