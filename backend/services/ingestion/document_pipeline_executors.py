"""Executors for durable document-stage repair jobs.

These helpers repair already-materialized document artifacts. They do not
parse source files, run Ghost B, or promote graph data; those remain separate
queues. The goal is to make ``document_pipeline_jobs`` executable for the
safe stages where durable Mongo rows already contain enough source truth.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from services.storage.record_status import with_active_records


SUMMARY_TEXT_CLAUSE: dict[str, Any] = {
    "summary": {"$exists": True, "$nin": [None, ""]}
}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _facet_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "facet_ids": _list(row.get("facet_ids")),
        "facet_text": row.get("facet_text") or "",
        "content_facet_ids": _list(row.get("content_facet_ids")),
        "content_facet_text": row.get("content_facet_text") or "",
        "content_facet_source": row.get("content_facet_source") or "",
        "content_facet_confidence": row.get("content_facet_confidence"),
    }


def _stage_after_qdrant(
    *,
    config: Any,
    doc: dict[str, Any],
    summary_gate_required: bool,
    summaries_indexed: bool,
) -> tuple[str, dict[str, str] | None, str | None]:
    from config import get_settings

    settings = get_settings()
    write_state = _dict(doc.get("write_state"))
    pending: list[str] = []
    if summary_gate_required and not summaries_indexed:
        pending.append("summary")
    if (
        bool(getattr(config, "use_neo4j", False))
        and bool(getattr(settings, "NEO4J_ENABLED", True))
        and write_state.get("neo4j_written") is not True
    ):
        pending.append("graph")
    if pending:
        stage = "queryable_with_pending_" + "_and_".join(pending)
        return (
            stage,
            {
                "summary": "pending" if "summary" in pending else "complete",
                "graph": "pending" if "graph" in pending else "complete",
            },
            "Queryable; pending enrichment lanes: " + ", ".join(pending) + ".",
        )
    # This executor repairs Qdrant/queryability only. The canonical ingest
    # worker promotes a document to fully_enriched only after graph/summary
    # lanes and verification have completed, so keep this stage honest.
    return "queryable", {"summary": "complete", "graph": "complete"}, None


async def _load_rows_for_doc(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    doc = await db["documents"].find_one(
        with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
        {"_id": 0},
    )
    parents = await db["parent_chunks"].find(
        with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
        {"_id": 0},
    ).sort("parent_id", 1).to_list(length=None)
    children = await db["chunks"].find(
        with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
        {"_id": 0},
    ).sort("chunk_id", 1).to_list(length=None)
    return doc, parents, children


def _rehydrate_chunks(
    *,
    doc: dict[str, Any],
    parent_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    from services.ingestion import tier_chunker
    from services.ingestion.section_classifier import ChunkKind

    source_tier = str(doc.get("source_tier") or "tier_c")
    children: list[Any] = []
    for row in child_rows:
        text = str(row.get("text") or "")
        children.append(
            tier_chunker.ChildChunk(
                chunk_id=str(row.get("chunk_id") or ""),
                parent_id=str(row.get("parent_id") or ""),
                doc_id=str(row.get("doc_id") or doc.get("doc_id") or ""),
                corpus_id=str(row.get("corpus_id") or doc.get("corpus_id") or ""),
                text=text,
                heading_path=_list(row.get("heading_path")) or None,
                source_tier=str(row.get("source_tier") or source_tier),
                token_count=_int(row.get("token_count")) or max(1, len(text.split())),
                page_start=row.get("page_start"),
                page_end=row.get("page_end"),
                chunk_kind=str(row.get("chunk_kind") or ChunkKind.BODY),
                language=row.get("language"),
                metadata=_dict(row.get("metadata")),
            )
        )
    children_by_parent: dict[str, list[Any]] = {}
    for child in children:
        children_by_parent.setdefault(child.parent_id, []).append(child)

    parents: list[Any] = []
    for row in parent_rows:
        text = str(row.get("text") or "")
        parent_id = str(row.get("parent_id") or "")
        parents.append(
            tier_chunker.ParentChunk(
                parent_id=parent_id,
                doc_id=str(row.get("doc_id") or doc.get("doc_id") or ""),
                corpus_id=str(row.get("corpus_id") or doc.get("corpus_id") or ""),
                text=text,
                heading_path=_list(row.get("heading_path")) or None,
                source_tier=str(row.get("source_tier") or source_tier),
                children=children_by_parent.get(parent_id, []),
                page_start=row.get("page_start"),
                page_end=row.get("page_end"),
                chunk_kind=str(row.get("chunk_kind") or ChunkKind.BODY),
                language=row.get("language"),
                metadata=_dict(row.get("metadata")),
            )
        )
    facet_profile = _dict(doc.get("facet_profile"))
    facet_profile = {
        **facet_profile,
        "child_facets": {
            str(row.get("chunk_id") or ""): _facet_from_row(row)
            for row in child_rows
            if row.get("chunk_id")
        },
        "parent_facets": {
            str(row.get("parent_id") or ""): _facet_from_row(row)
            for row in parent_rows
            if row.get("parent_id")
        },
    }
    return parents, children, facet_profile


async def mark_documents_persisted_from_artifacts(
    db: Any,
    *,
    corpus_id: str,
    doc_ids: list[str],
    limit: int = 25,
) -> dict[str, Any]:
    """Repair ``write_state.mongo_written`` from split Mongo artifacts."""

    selected = [str(doc_id) for doc_id in doc_ids if str(doc_id).strip()][: max(1, int(limit or 25))]
    counts: dict[str, int] = {}
    docs: list[dict[str, Any]] = []
    for doc_id in selected:
        doc, parent_rows, child_rows = await _load_rows_for_doc(
            db,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        if not doc:
            status = "failed"
            reason = "document_missing"
        elif not child_rows:
            status = "blocked_missing_chunks"
            reason = "missing_chunks"
        else:
            summary_count = sum(1 for row in parent_rows if str(row.get("summary") or "").strip())
            await db["documents"].update_one(
                {"corpus_id": corpus_id, "doc_id": doc_id},
                {
                    "$set": {
                        "child_count": len(child_rows),
                        "parent_count": len(parent_rows),
                        "summary_count": summary_count,
                        "write_state.mongo_written": True,
                        "ingest_stage": (
                            doc.get("ingest_stage")
                            if _dict(doc.get("write_state")).get("qdrant_written") is True
                            else "mongo"
                        ),
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            status = "succeeded"
            reason = "mongo_artifacts_present"
        counts[status] = counts.get(status, 0) + 1
        docs.append({"doc_id": doc_id, "status": status, "reason": reason})
    return {
        "status": "complete" if not counts.get("failed") else "partial",
        "corpus_id": corpus_id,
        "attempted": len(selected),
        "counts": counts,
        "docs": docs,
    }


async def embed_documents_to_qdrant_from_artifacts(
    db: Any,
    *,
    qdrant_client: Any,
    corpus_id: str,
    doc_ids: list[str],
    limit: int = 10,
) -> dict[str, Any]:
    """Repair Qdrant child/summary vectors from Mongo chunk artifacts."""

    from services.ingestion import worker
    from services.storage import mongo_writer

    selected = [str(doc_id) for doc_id in doc_ids if str(doc_id).strip()][: max(1, int(limit or 10))]
    corpus = await db["corpora"].find_one(
        with_active_records({"corpus_id": corpus_id}),
        {"_id": 0, "default_ingestion_config": 1},
    )
    live_cfg = (corpus or {}).get("default_ingestion_config") or {}
    counts: dict[str, int] = {}
    docs: list[dict[str, Any]] = []
    for doc_id in selected:
        started = time.monotonic()
        try:
            doc, parent_rows, child_rows = await _load_rows_for_doc(
                db,
                corpus_id=corpus_id,
                doc_id=doc_id,
            )
            if not doc:
                status, reason = "failed", "document_missing"
            elif not child_rows:
                status, reason = "blocked_missing_chunks", "missing_chunks"
            elif _dict(doc.get("write_state")).get("mongo_written") is not True:
                status, reason = "blocked_mongo_state", "mongo_write_not_complete"
            else:
                from services.ingestion_service import build_effective_config

                config = build_effective_config(
                    frozen_base=doc.get("ingestion_config") or live_cfg,
                    live_corpus=live_cfg,
                    ingest_overrides=None,
                )
                parents, children, facet_profile = _rehydrate_chunks(
                    doc=doc,
                    parent_rows=parent_rows,
                    child_rows=child_rows,
                )
                summaries = worker._reconstruct_summaries_from_mongo(parents, parent_rows)
                summary_targets = worker._summary_target_kinds(config)
                summary_gate_required = bool(
                    getattr(config, "chunk_summarization", False)
                    and summary_targets
                    and worker._summarizable_parents(parents)
                )
                summary_write_required = bool(summary_targets and summaries)

                async with worker._embed_phase_semaphore():
                    vec_map, summary_vec_map = await worker._embed_batch_for_doc(
                        children=children,
                        summaries=summaries,
                        config=config,
                    )

                from services.storage.sparse_encoder import encode_text as _bm25_encode

                def _build_sparse_maps() -> tuple[dict[str, Any], dict[str, Any]]:
                    return (
                        {
                            c.chunk_id: _bm25_encode(worker._searchable_text(c))
                            for c in children
                            if c.chunk_id in vec_map
                        },
                        {
                            s.parent_id: _bm25_encode(worker._summary_vector_text(s))
                            for s in summaries
                            if s.parent_id in summary_vec_map
                        },
                    )

                child_sparse_map, summary_sparse_map = await asyncio.to_thread(_build_sparse_maps)
                async with worker._qdrant_write_semaphore():
                    await worker._write_qdrant_for_doc(
                        qdrant_client=qdrant_client,
                        doc_id=doc_id,
                        corpus_id=corpus_id,
                        user_id=str(doc.get("user_id") or ""),
                        filename=str(doc.get("filename") or doc_id),
                        parents=parents,
                        children=children,
                        vec_map=vec_map,
                        summaries=summaries,
                        summary_vec_map=summary_vec_map,
                        config=config,
                        child_sparse_map=child_sparse_map,
                        summary_sparse_map=summary_sparse_map,
                        facet_profile=facet_profile,
                    )
                write_updates: dict[str, Any] = {"qdrant_written": True}
                summaries_indexed = False
                if summary_write_required:
                    write_updates["summaries_indexed"] = True
                    write_updates["summary_points"] = len(summaries)
                    summaries_indexed = True
                elif not summary_gate_required:
                    write_updates["summaries_indexed"] = False
                    write_updates["summary_points"] = 0
                await mongo_writer.update_write_state(
                    db,
                    doc_id,
                    corpus_id=corpus_id,
                    **write_updates,
                )
                stage, enrichment_status, enrichment_reason = _stage_after_qdrant(
                    config=config,
                    doc=doc,
                    summary_gate_required=summary_gate_required,
                    summaries_indexed=summaries_indexed,
                )
                await db["documents"].update_one(
                    {"corpus_id": corpus_id, "doc_id": doc_id},
                    {
                        "$set": {
                            "ingest_stage": stage,
                            "queryable": True,
                            "enrichment_status": enrichment_status,
                            "enrichment_pending_reason": enrichment_reason,
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                status, reason = "succeeded", "qdrant_write_complete"
        except Exception as exc:  # noqa: BLE001 - bounded per-doc executor
            status, reason = "failed", str(exc)[:500]
        counts[status] = counts.get(status, 0) + 1
        docs.append(
            {
                "doc_id": doc_id,
                "status": status,
                "reason": reason,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        )
    return {
        "status": "complete" if not counts.get("failed") else "partial",
        "corpus_id": corpus_id,
        "attempted": len(selected),
        "counts": counts,
        "docs": docs,
    }
