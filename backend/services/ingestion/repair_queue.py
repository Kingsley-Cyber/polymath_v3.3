"""Durable relation repair queue for deferred Gemma graph repair.

The hot path should not block first-pass ingestion on slow relation repair.
This module stores one repair job per questionable relation, then exposes
lease-based claim/update helpers so background workers can drain jobs with
at-least-once semantics and idempotent graph writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument, UpdateOne

logger = logging.getLogger(__name__)

COLLECTION = "graph_repair_queue"

QUEUE_STATUS_QUEUED = "queued"
QUEUE_STATUS_LEASED = "leased"
QUEUE_STATUS_SUCCEEDED = "succeeded"
QUEUE_STATUS_DISCARDED = "discarded"
QUEUE_STATUS_FAILED = "failed"
QUEUE_STATUS_DEAD_LETTER = "dead_letter"

REPAIR_NOT_REQUIRED = "not_required"
REPAIR_QUEUED = "queued"
REPAIR_PROCESSING = "processing"
REPAIR_COMPLETE = "complete"
REPAIR_PARTIAL = "partial"
REPAIR_FAILED = "failed"

GRAPH_PARTIAL = "graph_partial"
GRAPH_READY = "graph_ready"
GRAPH_NEEDS_BACKFILL = "needs_backfill"
GRAPH_RETRY_SCHEDULED = "graph_retry_scheduled"

PENDING_QUEUE_STATUSES = {QUEUE_STATUS_QUEUED, QUEUE_STATUS_LEASED}
COMPLETE_QUEUE_STATUSES = {QUEUE_STATUS_SUCCEEDED, QUEUE_STATUS_DISCARDED}
FAILED_QUEUE_STATUSES = {QUEUE_STATUS_FAILED, QUEUE_STATUS_DEAD_LETTER}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _as_plain_dict(value: Any) -> dict:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value)


def make_repair_id(
    *,
    corpus_id: str,
    doc_id: str,
    chunk_id: str,
    relation: dict,
    source_sentence: str,
    schema_version: str,
) -> str:
    """Deterministic id so retries/reingests upsert the same repair job."""
    key = {
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "subject": _clean(relation.get("subject")),
        "predicate": _clean(relation.get("predicate")),
        "object": _clean(relation.get("object")),
        "source_sentence": _clean(source_sentence),
        "schema_version": schema_version,
    }
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()
    return f"relrepair:{digest}"


def _schema_snapshot(schema: Any | None) -> dict:
    if schema is None:
        return {}
    return {
        "entity_schema": list(getattr(schema, "entity_schema", None) or []),
        "relation_schema": list(getattr(schema, "relation_schema", None) or []),
        "strict": str(getattr(schema, "strict", "soft") or "soft"),
    }


def _candidate_doc(
    candidate: Any,
    *,
    max_attempts: int,
) -> dict:
    row = _as_plain_dict(candidate)
    relation = _as_plain_dict(row.get("relation") or row.get("failed_triple") or {})
    source_sentence = str(
        row.get("source_sentence")
        or relation.get("source_sentence")
        or relation.get("evidence_phrase")
        or ""
    ).strip()
    schema_version = str(row.get("schema_version") or "polymath.extract.v2")
    repair_id = str(row.get("repair_id") or "") or make_repair_id(
        corpus_id=str(row.get("corpus_id") or ""),
        doc_id=str(row.get("doc_id") or ""),
        chunk_id=str(row.get("chunk_id") or ""),
        relation=relation,
        source_sentence=source_sentence,
        schema_version=schema_version,
    )
    now = datetime.utcnow()
    return {
        "repair_id": repair_id,
        "corpus_id": str(row.get("corpus_id") or ""),
        "doc_id": str(row.get("doc_id") or ""),
        "chunk_id": str(row.get("chunk_id") or ""),
        "schema_version": schema_version,
        "schema_snapshot": row.get("schema_snapshot") or {},
        "schema_lens_id": row.get("schema_lens_id"),
        "source_sentence": source_sentence,
        "failed_triple": relation,
        "entity_names": list(row.get("entity_names") or []),
        "entity_snapshot": list(row.get("entity_snapshot") or []),
        "reasons": list(row.get("reasons") or []),
        "status": QUEUE_STATUS_QUEUED,
        "attempts": 0,
        "max_attempts": max(1, int(row.get("max_attempts") or max_attempts or 3)),
        "lease_owner": None,
        "lease_until": None,
        "next_attempt_at": now,
        "extraction_model_original": str(
            relation.get("extraction_model") or row.get("extraction_model_original") or ""
        ),
        "repair_model": str(row.get("repair_model") or "Gemma-4-E4B"),
        "result_relation": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }


async def enqueue_relation_repairs(
    db: AsyncIOMotorDatabase,
    candidates: list[Any],
    *,
    max_attempts: int = 3,
) -> dict[str, int]:
    """Bulk upsert repair candidates. Existing queued/completed jobs remain durable."""
    if not candidates:
        return {"queued": 0, "matched": 0, "upserted": 0}
    docs = [_candidate_doc(candidate, max_attempts=max_attempts) for candidate in candidates]
    now = datetime.utcnow()
    ops = []
    for doc in docs:
        repair_id = doc["repair_id"]
        ops.append(
            UpdateOne(
                {"repair_id": repair_id},
                {
                    "$setOnInsert": doc,
                    "$set": {
                        "updated_at": now,
                        "max_attempts": doc["max_attempts"],
                    },
                },
                upsert=True,
            )
        )
    result = await db[COLLECTION].bulk_write(ops, ordered=False)
    logger.info(
        "phase=graph_repair_enqueue queued=%d matched=%d upserted=%d",
        len(docs),
        result.matched_count,
        result.upserted_count,
    )
    return {
        "queued": len(docs),
        "matched": int(result.matched_count or 0),
        "upserted": int(result.upserted_count or 0),
    }


async def repair_counts_for_doc(
    db: AsyncIOMotorDatabase,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": 0,
        "queued": 0,
        "leased": 0,
        "succeeded": 0,
        "discarded": 0,
        "failed": 0,
        "dead_letter": 0,
        "pending": 0,
        "complete": 0,
        "terminal_failed": 0,
    }
    cursor = db[COLLECTION].aggregate(
        [
            {"$match": {"corpus_id": corpus_id, "doc_id": doc_id}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
    )
    async for row in cursor:
        status = str(row.get("_id") or "")
        count = int(row.get("count") or 0)
        counts[status] = count
        counts["total"] += count
    counts["pending"] = sum(counts.get(status, 0) for status in PENDING_QUEUE_STATUSES)
    counts["complete"] = sum(counts.get(status, 0) for status in COMPLETE_QUEUE_STATUSES)
    counts["terminal_failed"] = sum(
        counts.get(status, 0) for status in FAILED_QUEUE_STATUSES
    )
    return counts


def repair_status_from_counts(counts: dict[str, int]) -> str:
    total = int(counts.get("total") or 0)
    if total <= 0:
        return REPAIR_NOT_REQUIRED
    pending = int(counts.get("pending") or 0)
    terminal_failed = int(counts.get("terminal_failed") or 0)
    complete = int(counts.get("complete") or 0)
    if pending:
        return REPAIR_PROCESSING if int(counts.get("leased") or 0) else REPAIR_QUEUED
    if terminal_failed and complete:
        return REPAIR_PARTIAL
    if terminal_failed:
        return REPAIR_FAILED
    return REPAIR_COMPLETE


async def refresh_document_repair_state(
    db: AsyncIOMotorDatabase,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, Any]:
    """Refresh write_state repair counters from the queue collection."""
    counts = await repair_counts_for_doc(db, corpus_id=corpus_id, doc_id=doc_id)
    status = repair_status_from_counts(counts)
    doc = await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"write_state": 1, "ghost_b_failures": 1, "_id": 0},
    )
    ws = (doc or {}).get("write_state") or {}
    ghost_failures = (doc or {}).get("ghost_b_failures") or []
    graph_status = ws.get("graph_status")
    if int(counts.get("pending") or 0) or int(counts.get("terminal_failed") or 0):
        if graph_status not in {GRAPH_NEEDS_BACKFILL, GRAPH_RETRY_SCHEDULED}:
            graph_status = GRAPH_PARTIAL
    elif status == REPAIR_COMPLETE and ws.get("neo4j_written") and not ghost_failures:
        graph_status = GRAPH_READY

    updates: dict[str, Any] = {
        "write_state.repair_status": status,
        "write_state.repair_total": int(counts.get("total") or 0),
        "write_state.repair_pending": int(counts.get("pending") or 0),
        "write_state.repair_succeeded": int(counts.get("succeeded") or 0),
        "write_state.repair_discarded": int(counts.get("discarded") or 0),
        "write_state.repair_failed": int(counts.get("terminal_failed") or 0),
        "decision_trace.repair_status": status,
        "decision_trace.repair_total": int(counts.get("total") or 0),
        "decision_trace.repair_pending": int(counts.get("pending") or 0),
        "updated_at": datetime.utcnow(),
    }
    if graph_status:
        updates["write_state.graph_status"] = graph_status
        updates["decision_trace.graph_status"] = graph_status
    await db["documents"].update_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"$set": updates},
    )
    return {"status": status, "counts": counts, "graph_status": graph_status}


async def claim_next_repair(
    db: AsyncIOMotorDatabase,
    *,
    owner: str,
    lease_seconds: int,
    corpus_id: str | None = None,
    doc_id: str | None = None,
) -> dict | None:
    now = datetime.utcnow()
    query: dict[str, Any] = {
        "$and": [
            {
                "$or": [
                    {
                        "status": QUEUE_STATUS_QUEUED,
                        "next_attempt_at": {"$lte": now},
                    },
                    {
                        "status": QUEUE_STATUS_LEASED,
                        "lease_until": {"$lt": now},
                    },
                ]
            },
            {"$expr": {"$lt": ["$attempts", "$max_attempts"]}},
        ]
    }
    if corpus_id:
        query["corpus_id"] = corpus_id
    if doc_id:
        query["doc_id"] = doc_id
    return await db[COLLECTION].find_one_and_update(
        query,
        {
            "$set": {
                "status": QUEUE_STATUS_LEASED,
                "lease_owner": owner,
                "lease_until": now + timedelta(seconds=max(30, int(lease_seconds or 300))),
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("next_attempt_at", 1), ("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def mark_repair_succeeded(
    db: AsyncIOMotorDatabase,
    *,
    repair_id: str,
    result_relation: dict | None,
    discarded: bool = False,
) -> None:
    now = datetime.utcnow()
    await db[COLLECTION].update_one(
        {"repair_id": repair_id},
        {
            "$set": {
                "status": QUEUE_STATUS_DISCARDED if discarded else QUEUE_STATUS_SUCCEEDED,
                "result_relation": result_relation,
                "lease_owner": None,
                "lease_until": None,
                "last_error": None,
                "completed_at": now,
                "updated_at": now,
            }
        },
    )


async def mark_repair_failed(
    db: AsyncIOMotorDatabase,
    *,
    repair: dict,
    error: str,
    backoff_seconds: int,
) -> None:
    now = datetime.utcnow()
    attempts = int(repair.get("attempts") or 0)
    max_attempts = int(repair.get("max_attempts") or 3)
    terminal = attempts >= max_attempts
    await db[COLLECTION].update_one(
        {"repair_id": repair["repair_id"]},
        {
            "$set": {
                "status": QUEUE_STATUS_DEAD_LETTER if terminal else QUEUE_STATUS_QUEUED,
                "lease_owner": None,
                "lease_until": None,
                "next_attempt_at": now
                + timedelta(seconds=max(1, int(backoff_seconds or 60))),
                "last_error": str(error or "")[:1000],
                "updated_at": now,
            }
        },
    )
