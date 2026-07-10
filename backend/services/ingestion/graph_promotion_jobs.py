"""Durable graph-promotion job queue.

Qdrant/Mongo readiness and Neo4j promotion fail independently. This module
materializes those graph gaps as idempotent jobs so promotion can be planned,
run, retried, and inspected without treating an old ingest batch as the source
of truth.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any

from pymongo import UpdateOne

from db.queue_integrity import bulk_upsert_durable_jobs
from services.ingestion.job_leases import lease_deadline, reclaim_expired_running_jobs
from services.ingestion.stage_identity import graph_promotion_stage_identity, stable_stage_hash

GRAPH_VERIFY_PATTERN = r"(neo4j|has_chunk)"
TERMINAL_STATUSES = {
    "done",
    "partial",
    "noop",
    "failed",
    "blocked_failed_chunks",
    "blocked_no_extractions",
}
ACTIVE_STATUSES = {"queued", "running"}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def graph_gap_reason(row: dict[str, Any]) -> str | None:
    write_state = row.get("write_state") or {}
    if write_state.get("neo4j_written") is not True:
        return "neo4j_missing"
    if write_state.get("verified") is True:
        return None
    for raw in write_state.get("verify_errors") or []:
        text = str(raw).lower()
        if "neo4j" in text or "has_chunk" in text:
            return "neo4j_verify_mismatch"
    return None


def graph_job_id(*, corpus_id: str, doc_id: str, reason: str) -> str:
    digest = hashlib.sha256(f"{corpus_id}:{doc_id}:{reason}".encode("utf-8")).hexdigest()
    return f"graph_promote_{digest[:24]}"


def graph_promotion_contract_hash(row: dict[str, Any]) -> str:
    cfg = row.get("ingestion_config") or {}
    return stable_stage_hash(
        {
            "contract": "graph_promotion.v1",
            "graph_store": "neo4j",
            "use_neo4j": cfg.get("use_neo4j", True),
            "reason": graph_gap_reason(row),
        }
    )


def extraction_artifact_id(row: dict[str, Any]) -> str:
    explicit = str(row.get("raw_output_artifact_id") or "").strip()
    if explicit:
        return explicit
    payload = {
        "doc_id": row.get("doc_id"),
        "chunk_id": row.get("chunk_id"),
        "chunk_hash": row.get("chunk_hash"),
        "extraction_contract_hash": row.get("extraction_contract_hash"),
        "raw_output_fingerprint": row.get("raw_output_fingerprint") or {},
        "status": row.get("status"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"derived:{digest}"


def classify_graph_promotion_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    if ((row.get("ingestion_config") or {}).get("use_neo4j", True)) is False:
        return None
    reason = graph_gap_reason(row)
    if reason is None:
        return None
    failure_count = _int(row.get("ghost_b_failure_count"))
    failure_rows = _int(row.get("failure_rows"))
    staged_extractions = _int(row.get("staged_extractions"))
    child_chunks = _int(row.get("child_chunks"))
    if staged_extractions <= 0:
        status = "blocked_no_extractions"
    elif failure_count or failure_rows:
        # Promote known-good extraction artifacts now; failed chunks remain
        # visible through failure counts and extraction jobs.
        status = "queued"
    else:
        status = "queued"
    extraction_artifact_ids = sorted({
        str(value)
        for value in (row.get("extraction_artifact_ids") or [])
        if str(value)
    })
    graph_contract = str(
        row.get("graph_contract_hash") or graph_promotion_contract_hash(row)
    )
    return {
        "job_id": graph_job_id(
            corpus_id=str(row.get("corpus_id") or ""),
            doc_id=str(row.get("doc_id") or ""),
            reason=reason,
        ),
        "corpus_id": str(row.get("corpus_id") or ""),
        "doc_id": str(row.get("doc_id") or ""),
        "user_id": str(row.get("user_id") or ""),
        "filename": row.get("filename"),
        "status": status,
        "reason": reason,
        "child_chunks": child_chunks,
        "parent_chunks": _int(row.get("parent_chunks")),
        "staged_extractions": staged_extractions,
        "extraction_artifact_ids": extraction_artifact_ids,
        "extraction_artifact_count": len(extraction_artifact_ids),
        "graph_contract_hash": graph_contract,
        "stage_identity": graph_promotion_stage_identity(
            doc=row,
            extraction_artifact_ids=extraction_artifact_ids,
            graph_contract_hash=graph_contract,
        ),
        "failure_rows": failure_rows,
        "failed_chunks": failure_count,
    }


async def _count(db: Any, collection: str, query: dict[str, Any]) -> int:
    try:
        return await db[collection].count_documents(query)
    except Exception:
        return 0


async def _corpus_graph_required(db: Any, *, corpus_id: str) -> bool:
    try:
        corpus = await db["corpora"].find_one(
            {"corpus_id": corpus_id},
            {"_id": 0, "default_ingestion_config.use_neo4j": 1},
        )
    except Exception:
        corpus = None
    cfg = (corpus or {}).get("default_ingestion_config") or {}
    return bool(cfg.get("use_neo4j", True))


def _unpromoted_doc_id_pipeline(*, corpus_id: str, limit: int) -> list[dict[str, Any]]:
    """Aggregate distinct unpromoted extraction documents before applying a cap."""

    return [
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
        {"$group": {"_id": "$doc_id"}},
        {"$sort": {"_id": 1}},
        {"$limit": max(1, int(limit or 100))},
    ]


def _promoted_doc_unmarked_extraction_pipeline(
    *,
    corpus_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Find graph-written docs whose legacy extraction artifacts lack promoted_at."""

    return [
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
        {"$sort": {"_id": 1}},
        {"$limit": max(1, int(limit or 100))},
        {
            "$lookup": {
                "from": "documents",
                "let": {"doc_id": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "corpus_id": corpus_id,
                            "$expr": {"$eq": ["$doc_id", "$$doc_id"]},
                            "write_state.qdrant_written": True,
                            "write_state.neo4j_written": True,
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "doc_id": 1,
                            "filename": 1,
                            "write_state": 1,
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "doc",
            }
        },
        {"$match": {"doc.0": {"$exists": True}}},
        {
            "$project": {
                "_id": 0,
                "doc_id": "$_id",
                "rows": 1,
                "filename": {"$arrayElemAt": ["$doc.filename", 0]},
            }
        },
    ]


def _modified_count(result: Any) -> int:
    try:
        return int(getattr(result, "modified_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _elapsed_ms(started_perf: float) -> float:
    return round(max(0.0, time.perf_counter() - started_perf) * 1000, 2)


async def mark_doc_extractions_promoted(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
    graph_job_id: str | None = None,
    result: dict[str, Any] | None = None,
    promoted_at: datetime | None = None,
) -> dict[str, int]:
    """Stamp successful extraction artifacts after a verified graph promotion.

    ``ghost_b_extractions`` is the durable extraction artifact store, while
    ``extraction_jobs`` is the retry/read model. Updating both keeps later
    replans from downgrading already-promoted chunks back to merely
    ``succeeded``.
    """

    promoted_at = promoted_at or datetime.utcnow()
    base_set: dict[str, Any] = {
        "promoted_at": promoted_at,
        "graph_promoted_at": promoted_at,
        "updated_at": promoted_at,
    }
    if graph_job_id:
        base_set["graph_promotion_job_id"] = graph_job_id
    if result:
        base_set["graph_promotion_result"] = {
            "status": result.get("status"),
            "neo4j_flushed": bool(result.get("neo4j_flushed")),
            "recovered_chunks": _int(result.get("recovered_chunks")),
            "remaining_failed_chunks": _int(result.get("remaining_failed_chunks")),
            "staged_results_written": _int(result.get("staged_results_written")),
            "full_replay": bool(result.get("full_replay")),
        }

    ghost_result = await db["ghost_b_extractions"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
        {"$set": base_set},
    )
    job_result = await db["extraction_jobs"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id, "status": "succeeded"},
        {
            "$set": {
                **base_set,
                "status": "promoted",
                "reason": "graph_promoted",
                "source_status": "ok",
            }
        },
    )
    return {
        "ghost_b_rows_promoted": _modified_count(ghost_result),
        "extraction_jobs_promoted": _modified_count(job_result),
    }


async def backfill_promoted_extraction_marks(
    db: Any,
    *,
    corpus_id: str,
    apply: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Stamp legacy extraction artifacts for docs already written to Neo4j.

    This is metadata reconciliation, not graph promotion. Without it,
    readiness can misread old successful graph writes as pending promotion just
    because their durable Ghost B rows predate ``promoted_at``.
    """

    rows = await db["ghost_b_extractions"].aggregate(
        _promoted_doc_unmarked_extraction_pipeline(
            corpus_id=corpus_id,
            limit=max(1, int(limit or 100)),
        )
    ).to_list(length=max(1, int(limit or 100)))
    planned_docs = len(rows)
    planned_rows = sum(_int(row.get("rows")) for row in rows)
    modified_docs = 0
    modified_rows = 0
    if apply:
        for row in rows:
            result = await mark_doc_extractions_promoted(
                db,
                corpus_id=corpus_id,
                doc_id=str(row.get("doc_id") or ""),
                result={
                    "status": "metadata_backfill",
                    "neo4j_flushed": False,
                    "recovered_chunks": 0,
                    "remaining_failed_chunks": 0,
                    "staged_results_written": 0,
                    "full_replay": False,
                },
            )
            changed = _int(result.get("ghost_b_rows_promoted")) + _int(
                result.get("extraction_jobs_promoted")
            )
            if changed:
                modified_docs += 1
                modified_rows += _int(result.get("ghost_b_rows_promoted"))
    status = "complete" if not rows else ("applied" if apply else "planned")
    return {
        "status": status,
        "planned_docs": planned_docs,
        "planned_rows": planned_rows,
        "modified_docs": modified_docs,
        "modified_rows": modified_rows,
        "apply": bool(apply),
        "limit": max(1, int(limit or 100)),
        "samples": rows[:10],
    }


async def _candidate_rows(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 100,
    max_chunks: int | None = None,
) -> list[dict[str, Any]]:
    graph_required = await _corpus_graph_required(db, corpus_id=corpus_id)
    if not graph_required:
        return []
    projection = {
        "_id": 0,
        "doc_id": 1,
        "corpus_id": 1,
        "user_id": 1,
        "filename": 1,
        "source_identity": 1,
        "source_key": 1,
        "content_sha256": 1,
        "source_file_hash": 1,
        "ingestion_config": 1,
        "ghost_b_failure_count": 1,
        "write_state": 1,
        "updated_at": 1,
    }
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "write_state.qdrant_written": True,
        "$or": [
            {"write_state.neo4j_written": {"$ne": True}},
            {
                "write_state.verified": {"$ne": True},
                "write_state.verify_errors": {
                    "$regex": GRAPH_VERIFY_PATTERN,
                    "$options": "i",
                },
            },
        ],
    }
    if user_id:
        query["user_id"] = user_id
    rows = await db["documents"].find(
        query,
        projection,
    ).to_list(length=None)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            continue
        child_chunks = await _count(
            db,
            "chunks",
            {"corpus_id": corpus_id, "doc_id": doc_id},
        )
        if max_chunks is not None and child_chunks > max_chunks:
            continue
        ingestion_config = dict(row.get("ingestion_config") or {})
        ingestion_config["use_neo4j"] = graph_required
        row["ingestion_config"] = ingestion_config
        row["child_chunks"] = child_chunks
        row["parent_chunks"] = await _count(
            db,
            "parent_chunks",
            {"corpus_id": corpus_id, "doc_id": doc_id},
        )
        row["staged_extractions"] = await _count(
            db,
            "ghost_b_extractions",
            {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
        )
        row["unpromoted_extractions"] = await _count(
            db,
            "ghost_b_extractions",
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "status": "ok",
                "$or": [
                    {"promoted_at": {"$exists": False}},
                    {"promoted_at": None},
                ],
            },
        )
        artifact_rows = await db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
            {
                "_id": 0,
                "doc_id": 1,
                "chunk_id": 1,
                "chunk_hash": 1,
                "extraction_contract_hash": 1,
                "raw_output_artifact_id": 1,
                "raw_output_fingerprint": 1,
                "status": 1,
            },
        ).to_list(length=None)
        row["extraction_artifact_ids"] = [extraction_artifact_id(item) for item in artifact_rows]
        row["failure_rows"] = await _count(
            db,
            "ghost_b_extractions",
            {"corpus_id": corpus_id, "doc_id": doc_id, "status": "error"},
        )
        candidate = classify_graph_promotion_candidate(row)
        if candidate:
            enriched.append(candidate)
    enriched.sort(
        key=lambda item: (
            0 if item["status"] == "queued" else 1,
            int(item.get("child_chunks") or 0),
            str(item.get("filename") or ""),
        )
    )
    return enriched[: max(1, int(limit or 100))]


async def plan_graph_promotion_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 100,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), 5000))
    plan = await _candidate_rows(
        db,
        corpus_id=corpus_id,
        user_id=user_id,
        limit=limit,
        max_chunks=max_chunks,
    )
    counts: dict[str, int] = {}
    for row in plan:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    result: dict[str, Any] = {
        "status": "planned" if not apply else "complete",
        "apply": bool(apply),
        "corpus_id": corpus_id,
        "planned": len(plan),
        "counts": counts,
        "jobs": plan[:50],
    }
    if not apply or not plan:
        return result

    now = datetime.utcnow()
    ops = []
    for row in plan:
        update = {
            "$set": {
                **row,
                "updated_at": now,
                "last_planned_at": now,
            },
            "$setOnInsert": {
                "created_at": now,
                "neo4j_write_attempts": 0,
            },
        }
        ops.append(UpdateOne({"job_id": row["job_id"]}, update, upsert=True))
    await bulk_upsert_durable_jobs(db["graph_promotion_jobs"], ops)
    return result


async def list_graph_promotion_jobs(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 100,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if statuses:
        query["status"] = {"$in": statuses}
    rows = await db["graph_promotion_jobs"].find(
        query,
        {"_id": 0},
    ).sort("updated_at", -1).limit(max(1, min(int(limit or 100), 500))).to_list(length=None)
    counts_rows = await db["graph_promotion_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    return {
        "corpus_id": corpus_id,
        "counts": {str(row["_id"]): int(row["count"]) for row in counts_rows},
        "jobs": rows,
    }


async def run_graph_promotion_jobs(
    db: Any,
    *,
    qdrant_client: Any,
    neo4j_driver: Any,
    corpus_id: str,
    user_id: str,
    limit: int = 5,
) -> dict[str, Any]:
    from services.ingestion.graph_backfill import backfill_failed_graph_chunks

    limit = max(1, min(int(limit or 5), 100))
    now = datetime.utcnow()
    reclaimed = await reclaim_expired_running_jobs(
        db,
        collection_name="graph_promotion_jobs",
        corpus_id=corpus_id,
        user_id=user_id,
        now=now,
    )
    jobs = await db["graph_promotion_jobs"].find(
        {"corpus_id": corpus_id, "status": "queued"},
        {"_id": 0},
    ).sort("updated_at", 1).limit(limit).to_list(length=limit)
    counts = {
        "planned": len(jobs),
        "done": 0,
        "partial": 0,
        "noop": 0,
        "blocked_no_extractions": 0,
        "failed": 0,
    }
    if reclaimed:
        counts["reclaimed"] = reclaimed
    results: list[dict[str, Any]] = []

    for job in jobs:
        job_id = str(job["job_id"])
        lease = await db["graph_promotion_jobs"].update_one(
            {"job_id": job_id, "status": "queued"},
            {
                "$set": {
                    "status": "running",
                    "started_at": now,
                    "lease_until": lease_deadline(now),
                    "updated_at": datetime.utcnow(),
                },
                "$inc": {"neo4j_write_attempts": 1},
            },
        )
        if not getattr(lease, "modified_count", 0):
            continue
        started_perf = time.perf_counter()
        try:
            result = await backfill_failed_graph_chunks(
                db=db,
                qdrant_client=qdrant_client,
                neo4j_driver=neo4j_driver,
                corpus_id=corpus_id,
                doc_id=str(job["doc_id"]),
                user_id=user_id or str(job.get("user_id") or ""),
                allow_extraction=False,
            )
            result = dict(result or {})
            neo4j_write_latency_ms = _elapsed_ms(started_perf)
            result["neo4j_write_latency_ms"] = neo4j_write_latency_ms
            result["neo4j_write_latency_source"] = "graph_promotion_job"
            remaining_failed = _int(result.get("remaining_failed_chunks"))
            neo4j_flushed = bool(result.get("neo4j_flushed"))
            if result.get("status") == "noop":
                final_status = "noop"
            elif result.get("status") in {
                "blocked_extraction_required",
                "blocked_extraction_replay_required",
            }:
                final_status = "blocked_no_extractions"
            elif remaining_failed > 0:
                final_status = "partial"
            elif neo4j_flushed:
                final_status = "done"
            else:
                final_status = "failed"
            promoted_counts = (
                await mark_doc_extractions_promoted(
                    db,
                    corpus_id=corpus_id,
                    doc_id=str(job["doc_id"]),
                    graph_job_id=job_id,
                    result=result,
                )
                if final_status in {"done", "partial"}
                else {"ghost_b_rows_promoted": 0, "extraction_jobs_promoted": 0}
            )
            counts[final_status] += 1
            await db["graph_promotion_jobs"].update_one(
                {"job_id": job_id},
                {
                    "$set": {
                        "status": final_status,
                        "result": result,
                        "neo4j_write_latency_ms": neo4j_write_latency_ms,
                        "neo4j_write_latency_source": "graph_promotion_job",
                        "promoted_counts": promoted_counts,
                        "lease_until": None,
                        "completed_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    },
                    "$unset": {"failure_reason": ""},
                },
            )
            results.append(
                {
                    "job_id": job_id,
                    "doc_id": job.get("doc_id"),
                    "status": final_status,
                    "neo4j_write_latency_ms": neo4j_write_latency_ms,
                    "promoted_counts": promoted_counts,
                }
            )
        except Exception as exc:  # noqa: BLE001
            neo4j_write_latency_ms = _elapsed_ms(started_perf)
            counts["failed"] += 1
            await db["graph_promotion_jobs"].update_one(
                {"job_id": job_id},
                {
                    "$set": {
                        "status": "failed",
                        "failure_reason": str(exc)[:1000],
                        "neo4j_write_latency_ms": neo4j_write_latency_ms,
                        "neo4j_write_latency_source": "graph_promotion_job",
                        "lease_until": None,
                        "completed_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            results.append(
                {
                    "job_id": job_id,
                    "doc_id": job.get("doc_id"),
                    "status": "failed",
                    "neo4j_write_latency_ms": neo4j_write_latency_ms,
                    "failure_reason": str(exc)[:300],
                }
            )

    return {
        "corpus_id": corpus_id,
        "status": "complete",
        "counts": counts,
        "results": results,
    }
