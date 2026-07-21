"""ID-level reconciliation for summary text vs summary vectors.

This module is the durable version of the 2026-07-21 manual repair receipt:
Mongo summary text is not enough for query-time readiness unless the matching
Qdrant summary vector exists for the same stable parent_id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import UpdateOne

from models.schemas import IngestionConfig
from services.embedder import embed_batch
from services.ingestion.section_classifier import parent_summary_required_clause
from services.ingestion.summary_backfill import summary_index_text
from services.ingestion.summary_jobs import SUMMARY_TEXT_CLAUSE
from services.storage.qdrant_writer import _col_for_corpus, upsert_summaries

SUMMARY_VECTOR_PROJECTION: dict[str, int] = {
    "_id": 0,
    "parent_id": 1,
    "doc_id": 1,
    "corpus_id": 1,
    "source_tier": 1,
    "summary": 1,
    "retrieval_text": 1,
    "text": 1,
    "parent_text": 1,
    "child_ids": 1,
    "domain": 1,
    "topics": 1,
    "semantic_chunk_type": 1,
    "key_terms": 1,
    "mechanisms": 1,
    "schema_version": 1,
    "summary_type": 1,
    "central_claim": 1,
    "key_points": 1,
    "main_mechanism": 1,
    "concept_tags": 1,
    "entity_hints": 1,
    "retrieval_uses": 1,
    "abstraction_level": 1,
    "source_child_ids": 1,
    "source_hash": 1,
    "summary_model": 1,
    "summary_created_at": 1,
    "validation_status": 1,
    "repair_status": 1,
    "quality_score": 1,
    "quality_flags": 1,
    "heading_path": 1,
    "filename": 1,
    "doc_name": 1,
    "metadata": 1,
    "facet_ids": 1,
    "facet_text": 1,
    "content_facet_ids": 1,
    "content_facet_text": 1,
    "content_facet_source": 1,
    "content_facet_confidence": 1,
    "doc_facet_ids": 1,
    "facet_schema_version": 1,
    "chunk_kind": 1,
    "language": 1,
}


def _target_summary_kinds(corpus: dict[str, Any] | None) -> list[str]:
    cfg = IngestionConfig(**(((corpus or {}).get("default_ingestion_config")) or {}))
    kinds = [
        kind
        for kind in (cfg.target_qdrant_collections or ["hrag"])
        if kind in {"hrag", "naive"}
    ]
    return kinds or ["hrag"]


async def _required_parent_summary_rows(
    db: Any,
    *,
    corpus_id: str,
    parent_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "$and": [parent_summary_required_clause(), SUMMARY_TEXT_CLAUSE],
    }
    if parent_ids is not None:
        query["parent_id"] = {
            "$in": sorted({str(parent_id) for parent_id in parent_ids if str(parent_id)})
        }
    return await db["parent_chunks"].find(
        query,
        SUMMARY_VECTOR_PROJECTION,
    ).to_list(length=None)


async def required_parent_summary_ids(db: Any, *, corpus_id: str) -> set[str]:
    rows = await _required_parent_summary_rows(db, corpus_id=corpus_id)
    return {
        str(row.get("parent_id") or "")
        for row in rows
        if str(row.get("parent_id") or "")
    }


async def indexed_summary_parent_ids(
    qdrant_client: Any,
    *,
    corpus_id: str,
    collection_kind: str,
) -> set[str]:
    from qdrant_client import models

    collection_name = _col_for_corpus(corpus_id, collection_kind)
    seen: set[str] = set()
    offset = None
    count_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="corpus_id",
                match=models.MatchValue(value=corpus_id),
            ),
            models.FieldCondition(
                key="chunk_type",
                match=models.MatchValue(value="summary"),
            ),
        ]
    )
    while True:
        points, offset = await qdrant_client.scroll(
            collection_name=collection_name,
            scroll_filter=count_filter,
            limit=2048,
            offset=offset,
            with_payload=["parent_id", "corpus_id", "chunk_type"],
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            parent_id = str(payload.get("parent_id") or "")
            if parent_id:
                seen.add(parent_id)
        if offset is None:
            break
    return seen


async def audit_parent_summary_vector_integrity(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    target_kinds: list[str] | None = None,
    sample_limit: int = 25,
) -> dict[str, Any]:
    """Return the ID-join receipt for required Mongo summaries vs Qdrant."""

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "default_ingestion_config": 1},
    )
    kinds = target_kinds or _target_summary_kinds(corpus)
    required_ids = await required_parent_summary_ids(db, corpus_id=corpus_id)
    by_collection: dict[str, Any] = {}
    missing_union: set[str] = set()
    for kind in kinds:
        indexed_ids = await indexed_summary_parent_ids(
            qdrant_client,
            corpus_id=corpus_id,
            collection_kind=kind,
        )
        missing = required_ids - indexed_ids
        missing_union.update(missing)
        by_collection[kind] = {
            "required_mongo_ids": len(required_ids),
            "qdrant_indexed_ids": len(indexed_ids),
            "missing_ids": len(missing),
            "missing_sample": sorted(missing)[: max(0, int(sample_limit or 0))],
        }
    return {
        "status": "healthy" if not missing_union else "missing_vectors",
        "corpus_id": corpus_id,
        "target_kinds": kinds,
        "required_mongo_ids": len(required_ids),
        "missing_union_ids": len(missing_union),
        "collections": by_collection,
    }


async def _mark_docs_summary_indexed(
    db: Any,
    *,
    corpus_id: str,
    doc_ids: set[str],
) -> int:
    if not doc_ids:
        return 0
    parent_clause = parent_summary_required_clause()
    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    for doc_id in sorted(doc_ids):
        required = await db["parent_chunks"].count_documents(
            {"corpus_id": corpus_id, "doc_id": doc_id, "$and": [parent_clause]}
        )
        summarized = await db["parent_chunks"].count_documents(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "$and": [parent_clause, SUMMARY_TEXT_CLAUSE],
            }
        )
        ops.append(
            UpdateOne(
                {"corpus_id": corpus_id, "doc_id": doc_id},
                {
                    "$set": {
                        "write_state.summaries_indexed": bool(required and summarized >= required),
                        "write_state.summary_points": summarized,
                        "write_state.summary_backfilled_at": now,
                    }
                },
            )
        )
    if not ops:
        return 0
    result = await db["documents"].bulk_write(ops, ordered=False)
    return int(getattr(result, "modified_count", 0) or 0)


async def repair_parent_summary_vector_integrity(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    target_kinds: list[str] | None = None,
    batch_size: int = 256,
    max_missing: int | None = None,
) -> dict[str, Any]:
    """Repair only vectors missing by parent_id set difference.

    This never generates summary text and never calls an LLM. It embeds the
    already-validated Mongo summaries and upserts them through the existing
    summary-vector writer.
    """

    before = await audit_parent_summary_vector_integrity(
        db,
        qdrant_client,
        corpus_id=corpus_id,
        target_kinds=target_kinds,
    )
    missing_ids: set[str] = set()
    for receipt in (before.get("collections") or {}).values():
        missing_ids.update(str(value) for value in receipt.get("missing_sample") or [])
    if int(before.get("missing_union_ids") or 0) > len(missing_ids):
        required = await required_parent_summary_ids(db, corpus_id=corpus_id)
        for kind in before.get("target_kinds") or []:
            indexed = await indexed_summary_parent_ids(
                qdrant_client,
                corpus_id=corpus_id,
                collection_kind=str(kind),
            )
            missing_ids.update(required - indexed)
    if max_missing is not None:
        missing_ids = set(sorted(missing_ids)[: max(0, int(max_missing))])
    if not missing_ids:
        return {
            "status": "healthy",
            "corpus_id": corpus_id,
            "before": before,
            "written": 0,
            "after": before,
        }

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "user_id": 1, "default_ingestion_config": 1},
    )
    cfg = IngestionConfig(**(((corpus or {}).get("default_ingestion_config")) or {}))
    kinds = target_kinds or _target_summary_kinds(corpus)
    effective_user_id = str(user_id or (corpus or {}).get("user_id") or "")
    rows = await _required_parent_summary_rows(
        db,
        corpus_id=corpus_id,
        parent_ids=missing_ids,
    )
    row_parent_ids = {
        str(row.get("parent_id") or "")
        for row in rows
        if str(row.get("parent_id") or "")
    }
    missing_without_text = sorted(missing_ids - row_parent_ids)
    written = 0
    doc_ids: set[str] = set()
    batch = max(1, min(int(batch_size or 256), 720))
    for idx in range(0, len(rows), batch):
        chunk = rows[idx : idx + batch]
        texts = [summary_index_text(row) for row in chunk]
        vectors = await embed_batch(
            texts,
            mode="local",
            expected_dim=cfg.embedding_dimension,
            expected_model_id=cfg.embedding_model_id,
        )
        payloads = [
            {
                **row,
                "retrieval_text": summary_index_text(row),
                "user_id": effective_user_id,
                "source_tier": row.get("source_tier") or "parent",
            }
            for row in chunk
        ]
        written += int(
            await upsert_summaries(
                qdrant_client,
                corpus_id,
                payloads,
                vectors,
                target_kinds=kinds,
            )
            or 0
        )
        doc_ids.update(
            str(row.get("doc_id") or "")
            for row in chunk
            if str(row.get("doc_id") or "")
        )
    docs_updated = await _mark_docs_summary_indexed(
        db,
        corpus_id=corpus_id,
        doc_ids=doc_ids,
    )
    after = await audit_parent_summary_vector_integrity(
        db,
        qdrant_client,
        corpus_id=corpus_id,
        target_kinds=kinds,
    )
    return {
        "status": "healthy" if after.get("status") == "healthy" else "partial",
        "corpus_id": corpus_id,
        "target_kinds": kinds,
        "missing_requested": len(missing_ids),
        "missing_without_summary_text": len(missing_without_text),
        "missing_without_summary_text_sample": missing_without_text[:25],
        "written": written,
        "docs_updated": docs_updated,
        "before": before,
        "after": after,
    }
