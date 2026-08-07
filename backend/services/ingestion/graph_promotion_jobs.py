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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pymongo import UpdateOne

from db.queue_integrity import bulk_upsert_durable_jobs
from models.release_stamp import copy_stamp
from models.release_state import (
    ReleasePin,
    blocked_no_release_state,
    graph_write_allowed,
    missing_release_conditions,
)
from services.ingestion.job_leases import lease_deadline, reclaim_expired_running_jobs
from services.ingestion.stage_identity import graph_promotion_stage_identity, stable_stage_hash

GRAPH_VERIFY_PATTERN = r"(neo4j|has_chunk)"
CLAIM_PROMOTION_REASON = "claims_unpromoted"
LOCAL_CLAIM_SCHEMA_VERSION = "polymath.extract.local_extraction.v1"

# Deny-by-default release gate (owner sequencing decision 2026-08-03).
# off/shadow/enforce — staged rollout; authority comes exclusively from the
# categorical ReleasePin registry. A complete ReleaseStamp or a complete
# TemporalEnvelope never authorizes a canonical Neo4j write.
ReleaseGateMode = Literal["off", "shadow", "enforce"]
VALID_RELEASE_GATE_MODES: tuple[str, ...] = ("off", "shadow", "enforce")
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
            "contract": (
                "graph_promotion.v2.claims"
                if row.get("reason") == CLAIM_PROMOTION_REASON
                else "graph_promotion.v1"
            ),
            "graph_store": "neo4j",
            "use_neo4j": cfg.get("use_neo4j", True),
            "reason": graph_gap_reason(row),
            "claim_promotion_required": bool(row.get("claim_promotion_required")),
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
        "claim_promotion_required": False,
        # Step 3 — release stamp of the extraction artifacts this attempt
        # promotes, copied verbatim (null when unknown). Descriptive only;
        # it neither permits nor denies the write.
        "release_stamp": copy_stamp(row.get("release_stamp")),
    }


async def _claim_promotion_candidate_rows(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 100,
    max_chunks: int | None = None,
) -> list[dict[str, Any]]:
    if not await _corpus_graph_required(db, corpus_id=corpus_id):
        return []
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "corpus_id": corpus_id,
                "status": "ok",
                "schema_version": LOCAL_CLAIM_SCHEMA_VERSION,
                "claim_compilation.claims.0": {"$exists": True},
                "$or": [
                    {"claim_graph_promoted_at": {"$exists": False}},
                    {"claim_graph_promoted_at": None},
                    {"claim_graph_promote_version": {"$ne": "polymath.promote.v2-claims"}},
                ],
            }
        },
        {
            "$group": {
                "_id": "$doc_id",
                "staged_extractions": {"$sum": 1},
                "claims_in": {
                    "$sum": {
                        "$size": {
                            "$ifNull": ["$claim_compilation.claims", []]
                        }
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {"$limit": max(1, int(limit or 100))},
    ]
    rows = await db["ghost_b_extractions"].aggregate(pipeline).to_list(
        length=max(1, int(limit or 100))
    )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        doc_id = str(row.get("_id") or "")
        if not doc_id:
            continue
        doc_query: dict[str, Any] = {"corpus_id": corpus_id, "doc_id": doc_id}
        if user_id:
            doc_query["user_id"] = user_id
        doc = await db["documents"].find_one(
            doc_query,
            {
                "_id": 0,
                "doc_id": 1,
                "corpus_id": 1,
                "user_id": 1,
                "filename": 1,
                "ingestion_config": 1,
            },
        )
        if not doc:
            continue
        child_chunks = await _count(
            db,
            "chunks",
            {"corpus_id": corpus_id, "doc_id": doc_id},
        )
        if max_chunks is not None and child_chunks > max_chunks:
            continue
        cfg = dict(doc.get("ingestion_config") or {})
        cfg["use_neo4j"] = True
        candidate = {
            "job_id": graph_job_id(
                corpus_id=corpus_id,
                doc_id=doc_id,
                reason=CLAIM_PROMOTION_REASON,
            ),
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "user_id": str(doc.get("user_id") or ""),
            "filename": doc.get("filename"),
            "status": "queued",
            "reason": CLAIM_PROMOTION_REASON,
            "child_chunks": child_chunks,
            "parent_chunks": await _count(
                db,
                "parent_chunks",
                {"corpus_id": corpus_id, "doc_id": doc_id},
            ),
            "staged_extractions": _int(row.get("staged_extractions")),
            "claim_promotion_required": True,
            "claim_unpromoted_extractions": _int(row.get("staged_extractions")),
            "claims_in": _int(row.get("claims_in")),
            "extraction_artifact_ids": [],
            "extraction_artifact_count": 0,
            "graph_contract_hash": stable_stage_hash(
                {
                    "contract": "graph_promotion.v2.claims",
                    "graph_store": "neo4j",
                    "use_neo4j": True,
                    "reason": CLAIM_PROMOTION_REASON,
                }
            ),
            "failure_rows": 0,
            "failed_chunks": 0,
            "ingestion_config": cfg,
            # Step 3 — claim leg carries no extraction stamp today; null
            # keeps the attempt record honest until stamps flow from claims.
            "release_stamp": None,
        }
        candidate["stage_identity"] = graph_promotion_stage_identity(
            doc={**doc, "ingestion_config": cfg},
            extraction_artifact_ids=[],
            graph_contract_hash=str(candidate["graph_contract_hash"]),
        )
        candidates.append(candidate)
    return candidates


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

    # Ordering is the crash-safety contract: the census derives promotion
    # state from ghost rows' promoted_at, so the ghost stamp is the commit
    # point and must land LAST. A crash between the two writes then leaves
    # ghost rows unstamped → the census still plans a re-promotion, whose
    # rerun converges both collections. The reverse order strands
    # extraction_jobs at "succeeded" forever because the census is already
    # satisfied and never replans.
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
    ghost_result = await db["ghost_b_extractions"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
        {"$set": base_set},
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
    by_doc_id: dict[str, dict[str, Any]] = {}
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
            by_doc_id[str(candidate.get("doc_id") or "")] = candidate
    for claim_candidate in await _claim_promotion_candidate_rows(
        db,
        corpus_id=corpus_id,
        user_id=user_id,
        limit=limit,
        max_chunks=max_chunks,
    ):
        existing = by_doc_id.get(str(claim_candidate.get("doc_id") or ""))
        if existing:
            existing["claim_promotion_required"] = True
            existing["claim_unpromoted_extractions"] = _int(
                claim_candidate.get("claim_unpromoted_extractions")
            )
            existing["claims_in"] = _int(claim_candidate.get("claims_in"))
            continue
        enriched.append(claim_candidate)
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
    if not apply:
        return result

    reevaluation = await reevaluate_stale_graph_promotion_jobs(
        db, corpus_id=corpus_id, user_id=user_id, limit=max(limit, 500)
    )
    result["blocked_reevaluation"] = reevaluation
    if not plan:
        # Nothing new to materialize — stale/blocked jobs were still
        # reconsidered above (owner §4.3 self-healing).
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


GRAPH_BLOCKED_REEVALUATION_STATUSES: tuple[str, ...] = (
    "blocked_no_extractions",
    "blocked_failed_chunks",
)


async def reevaluate_stale_graph_promotion_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 500,
) -> dict[str, int]:
    """Reconcile durable graph jobs against live document state (owner §4.3).

    A blocked dependency is reconsidered automatically when the required
    artifact appears: a blocked job whose document is a fresh candidate again
    adopts the candidate's classification. A runnable or blocked job whose
    document no longer carries a graph gap becomes ``noop`` — the gap is
    resolved, so the job must not linger in the pressure/repair census.
    Release-gated jobs are untouched: they retry deterministically when the
    release advances.
    """

    limit = max(1, min(int(limit or 500), 5000))
    reevaluable = sorted(set(ACTIVE_STATUSES) | set(GRAPH_BLOCKED_REEVALUATION_STATUSES))
    query: dict[str, Any] = {"corpus_id": corpus_id, "status": {"$in": reevaluable}}
    if user_id:
        query["user_id"] = user_id
    jobs = await db["graph_promotion_jobs"].find(query, {"_id": 0}).limit(limit).to_list(
        length=limit
    )
    if not jobs:
        return {"reevaluated": 0, "requeued": 0, "resolved": 0, "still_blocked": 0}

    candidates = {
        str(row["job_id"]): row
        for row in await _candidate_rows(
            db, corpus_id=corpus_id, user_id=user_id, limit=max(limit, 100)
        )
    }

    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    requeued = resolved = still_blocked = 0
    for job in jobs:
        current = str(job.get("status") or "")
        candidate = candidates.get(str(job.get("job_id") or ""))
        if candidate is not None:
            new_status = str(candidate.get("status") or "")
            if new_status == current:
                if current in GRAPH_BLOCKED_REEVALUATION_STATUSES:
                    still_blocked += 1
                continue
            ops.append(
                UpdateOne(
                    {"job_id": job["job_id"], "status": current},
                    {
                        "$set": {
                            **candidate,
                            "updated_at": now,
                            "last_reevaluated_at": now,
                            "reevaluation_reason": "dependency_reconsidered",
                        }
                    },
                )
            )
            if new_status == "queued":
                requeued += 1
            else:
                still_blocked += 1
            continue

        doc = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": job.get("doc_id")},
            {"_id": 0, "write_state": 1, "ingestion_config": 1, "ingest_stage": 1},
        )
        if doc is None:
            noop_reason = "document_gone"
        elif ((doc.get("ingestion_config") or {}).get("use_neo4j", True)) is False:
            noop_reason = "neo4j_disabled"
        elif graph_gap_reason(doc) is None:
            noop_reason = "graph_gap_resolved"
        else:
            still_blocked += 1
            continue
        ops.append(
            UpdateOne(
                {"job_id": job["job_id"], "status": current},
                {
                    "$set": {
                        "status": "noop",
                        "noop_reason": noop_reason,
                        "updated_at": now,
                        "last_reevaluated_at": now,
                        "reevaluation_reason": "stale_job_reconciled",
                    },
                    "$unset": {"lease_until": "", "runner": ""},
                },
            )
        )
        resolved += 1

    if ops:
        await db["graph_promotion_jobs"].bulk_write(ops, ordered=False)
    return {
        "reevaluated": len(jobs),
        "requeued": requeued,
        "resolved": resolved,
        "still_blocked": still_blocked,
    }


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


# ---------------------------------------------------------------------------
# Deny-by-default release gate for canonical Neo4j writes
# ---------------------------------------------------------------------------


def release_gate_mode() -> ReleaseGateMode:
    """Staged rollout flag for the graph-promotion release gate.

    ``GRAPH_PROMOTION_RELEASE_GATE=off|shadow|enforce`` (default ``off``).
    Unrecognized values fail closed to ``off`` semantics ONLY via settings
    validation (Literal type); this helper never invents a permissive mode.
    """

    from config import get_settings

    mode = str(get_settings().GRAPH_PROMOTION_RELEASE_GATE or "off").strip().lower()
    if mode not in VALID_RELEASE_GATE_MODES:
        return "off"
    return mode  # type: ignore[return-value]


def active_release_pin() -> ReleasePin | None:
    """The active categorical release bundle from the fail-closed registry.

    Backed by ``services.control_plane.release_registry``: exactly one
    valid active entry yields its validated ``ReleasePin``; every other
    state (missing registry, ambiguity, malformed entry, failed
    categorical state, hash mismatch) resolves to None — the
    deny-by-default state where ``graph_write_allowed`` refuses every
    canonical write. Artifact stamps (ReleaseStamp) are descriptive
    identity, never write authority, and are never consumed here.
    """

    from services.control_plane.release_registry import load_release_registry

    return load_release_registry().release_pin


def instrument_canonical_writer_calls(counter: dict[str, int]) -> None:
    """Enforcement-audit observability owned by the authorized module.

    Wraps the canonical graph writers with call counters so enforcement
    probes can prove "zero writer executions while blocked" directly.
    This lives inside the single authorized execution module: probes and
    scripts consume this helper instead of importing the writers, which
    keeps the global no-bypass invariant intact. The wrappers only count;
    they never grant, alter, or bypass release authorization.
    """

    import services.ingestion.graph_backfill as graph_backfill
    import services.ingestion.promote as promote_mod

    real_backfill = graph_backfill.backfill_failed_graph_chunks
    real_promote = promote_mod.promote_claims_to_graph

    async def counting_backfill(**kwargs: Any):
        counter["backfill_failed_graph_chunks"] = (
            counter.get("backfill_failed_graph_chunks", 0) + 1
        )
        return await real_backfill(**kwargs)

    async def counting_promote(*args: Any, **kwargs: Any):
        counter["promote_claims_to_graph"] = (
            counter.get("promote_claims_to_graph", 0) + 1
        )
        return await real_promote(*args, **kwargs)

    graph_backfill.backfill_failed_graph_chunks = counting_backfill
    promote_mod.promote_claims_to_graph = counting_promote


def evaluate_release_gate(
    release: Any, *, mode: str | None = None
) -> dict[str, Any]:
    """Deterministic gate decision for one canonical write attempt.

    Authority is exclusive: only a ``ReleasePin`` instance can ever yield
    ``would_allow``. Any other value — None, a complete ReleaseStamp dict,
    a TemporalEnvelope, anything — is treated as "no active release" and
    yields ``would_block`` with the exact missing conditions.
    """

    gate_mode = mode if mode in VALID_RELEASE_GATE_MODES else release_gate_mode()
    pin = release if isinstance(release, ReleasePin) else None
    if graph_write_allowed(pin):
        decision = "would_allow"
        missing: list[str] = []
    else:
        decision = "would_block"
        missing = list(missing_release_conditions(pin))
    return {
        "gate": "graph_promotion_release_gate",
        "mode": gate_mode,
        "decision": decision,
        "missing_conditions": missing,
        "release_id": getattr(pin, "release_id", None) if pin else None,
    }


class GraphWriteAuthorization(BaseModel):
    """One shared authorization result for every canonical graph write.

    Both write surfaces — the durable promotion runner and the operator
    manual-repair path — consume THIS result; neither interprets the
    ReleasePin independently. ``enforcement_applied`` distinguishes a real
    enforce-mode denial/allowance from off/shadow pass-through.
    """

    model_config = ConfigDict(frozen=True)

    allowed: bool
    enforcement_applied: bool
    decision: dict[str, Any]


def authorize_canonical_graph_write(
    *,
    mode: str,
    release_pin: ReleasePin | None,
) -> GraphWriteAuthorization:
    """The single canonical graph-write authorization seam.

    off     → preserve existing behavior (allowed, no enforcement).
    shadow  → record the decision, continue (allowed, no enforcement).
    enforce → allowed only when the active ReleasePin proves every
              mandatory categorical state and artifact-hash match.
    """

    decision = evaluate_release_gate(release_pin, mode=mode)
    if mode in ("off", "shadow"):
        return GraphWriteAuthorization(
            allowed=True,
            enforcement_applied=False,
            decision=decision,
        )
    return GraphWriteAuthorization(
        allowed=decision["decision"] == "would_allow",
        enforcement_applied=True,
        decision=decision,
    )


# Distinct nonzero exit code for a release-policy block in operator CLIs.
# Not a graph failure, not an operational error: policy denial.
RELEASE_POLICY_BLOCK_EXIT_CODE = 77


def evaluate_cli_graph_write_gate(
    *, script_name: str, operator: str
) -> dict[str, Any]:
    """Shared release-gate check for operator CLI scripts.

    Resolves the active release ONCE per run through the same
    ``authorize_canonical_graph_write`` seam as the durable runner and the
    manual-repair service method — scripts never interpret the ReleasePin
    themselves. Call this BEFORE connecting clients, mutating state,
    touching counters, or calling any writer.

    off     → not blocked; decision recorded for trace continuity.
    shadow  → not blocked; decision recorded; execution continues.
    enforce → blocked unless a qualifying active ReleasePin exists.
    """

    mode = release_gate_mode()
    pin = active_release_pin()
    auth = authorize_canonical_graph_write(mode=mode, release_pin=pin)
    if not auth.allowed:
        blocked = blocked_no_release_state(pin)
        return {
            "blocked": True,
            "exit_code": RELEASE_POLICY_BLOCK_EXIT_CODE,
            "payload": {
                **blocked,
                "gate_mode": mode,
                "release_gate": auth.decision,
                "script": script_name,
                "operator": operator,
            },
        }
    return {
        "blocked": False,
        "gate_mode": mode,
        "release_gate": auth.decision,
        "script": script_name,
        "operator": operator,
    }


def cli_operator_identity() -> str:
    """Best-effort operator identity for CLI gate records."""

    import getpass
    import os

    return os.environ.get("USER") or getpass.getuser() or "unknown"


async def run_graph_promotion_jobs(
    db: Any,
    *,
    qdrant_client: Any,
    neo4j_driver: Any,
    corpus_id: str,
    user_id: str,
    limit: int = 5,
    release: ReleasePin | None = None,
) -> dict[str, Any]:
    from services.ingestion.graph_backfill import backfill_failed_graph_chunks
    from services.ingestion.promote import promote_claims_to_graph

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
        "blocked_no_release": 0,
        "failed": 0,
        "lost_ownership": 0,
    }
    if reclaimed:
        counts["reclaimed"] = reclaimed
    results: list[dict[str, Any]] = []
    gate_mode = release_gate_mode()

    # Shadow A/B run contract: the release state is resolved ONCE at run
    # start, never re-resolved per candidate. Every job in this run
    # consumes the same authorization; a registry change mid-run can
    # never produce mixed decisions within one run. The run handle pins
    # the registry hash so mutation mid-run fails closed instead of
    # silently extending authority.
    from services.control_plane.release_registry import begin_registry_run

    registry_handle = begin_registry_run()
    resolution = registry_handle.resolution
    if release is None:
        release = resolution.release_pin
    auth = authorize_canonical_graph_write(mode=gate_mode, release_pin=release)
    run_gate_decision = auth.decision if gate_mode in ("shadow", "enforce") else None
    if run_gate_decision is not None:
        # The shadow/enforce trace records WHY authority was absent: a
        # fail-closed registry resolution is distinct from an evaluated
        # failing pin. Identity is relayed only when one truly exists.
        if resolution.status != "ok" and release is None:
            run_gate_decision = {
                **run_gate_decision,
                "registry_resolution": resolution.reason,
            }

    for position, job in enumerate(jobs):
        job_id = str(job["job_id"])
        runner = "graph_promotion_jobs.run"

        # Deny-by-default release gate, evaluated BEFORE lease acquisition:
        # a blocked job never counts a Neo4j write attempt, never leaves the
        # durable queue (stays queued → retryable), and is reconsidered
        # exactly once per run after the release advances. Authority comes
        # exclusively from the categorical ReleasePin — a complete
        # ReleaseStamp or TemporalEnvelope can never authorize this write.
        if not auth.allowed:
            pin = release if isinstance(release, ReleasePin) else None
            blocked = blocked_no_release_state(pin)
            await db["graph_promotion_jobs"].update_one(
                {"job_id": job_id, "status": "queued"},
                {
                    "$set": {
                        "last_release_gate": blocked,
                        "last_release_gate_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            counts["blocked_no_release"] += 1
            results.append(
                {
                    "job_id": job_id,
                    "doc_id": job.get("doc_id"),
                    "status": "blocked_no_release",
                    "attempted_status": "blocked_no_release",
                    "release_gate": blocked,
                }
            )
            continue
        gate_decision = run_gate_decision

        # Registry mutation during one run fails closed: the remaining
        # jobs stop here, intact and retryable. The resolution itself is
        # never refreshed mid-run — decisions cannot mix within one run.
        if not registry_handle.verify_unchanged():
            remaining_jobs = jobs[position:]
            counts["registry_mutated"] = len(remaining_jobs)
            for remaining in remaining_jobs:
                results.append(
                    {
                        "job_id": str(remaining["job_id"]),
                        "doc_id": remaining.get("doc_id"),
                        "status": "blocked_registry_mutated",
                        "attempted_status": "blocked_registry_mutated",
                    }
                )
            break

        started_at = now
        lease_until = lease_deadline(now)
        lease = await db["graph_promotion_jobs"].update_one(
            {"job_id": job_id, "status": "queued"},
            {
                "$set": {
                    "status": "running",
                    "runner": runner,
                    "started_at": started_at,
                    "last_run_at": started_at,
                    "lease_until": lease_until,
                    "updated_at": datetime.utcnow(),
                },
                "$inc": {"neo4j_write_attempts": 1},
            },
        )
        if not getattr(lease, "modified_count", 0):
            continue
        started_perf = time.perf_counter()
        try:
            claim_only = job.get("reason") == CLAIM_PROMOTION_REASON
            if claim_only:
                claim_result = await promote_claims_to_graph(
                    db,
                    neo4j_driver,
                    corpus_id=corpus_id,
                    doc_id=str(job["doc_id"]),
                )
                result = {
                    "status": claim_result.get("status") or "noop",
                    "neo4j_flushed": claim_result.get("status")
                    in {"done", "partial"},
                    "remaining_failed_chunks": 0,
                    "recovered_chunks": 0,
                    "staged_results_written": 0,
                    "full_replay": False,
                    "claim_promotion": claim_result,
                }
            else:
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
                claim_result: dict[str, Any] = {}
                if job.get("claim_promotion_required"):
                    claim_result = await promote_claims_to_graph(
                        db,
                        neo4j_driver,
                        corpus_id=corpus_id,
                        doc_id=str(job["doc_id"]),
                    )
                    result["claim_promotion"] = claim_result
            neo4j_write_latency_ms = _elapsed_ms(started_perf)
            result["neo4j_write_latency_ms"] = neo4j_write_latency_ms
            result["neo4j_write_latency_source"] = "graph_promotion_job"
            remaining_failed = _int(result.get("remaining_failed_chunks"))
            neo4j_flushed = bool(result.get("neo4j_flushed"))
            claim_status = str(claim_result.get("status") or "")
            if claim_status in {"done", "partial"}:
                final_status = "done" if remaining_failed <= 0 else "partial"
            elif result.get("status") == "noop":
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
            completion_set: dict[str, Any] = {
                "status": final_status,
                "result": result,
                "claim_promotion_result": claim_result,
                "neo4j_write_latency_ms": neo4j_write_latency_ms,
                "neo4j_write_latency_source": "graph_promotion_job",
                "promoted_counts": promoted_counts,
                "lease_until": None,
                "completed_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            if gate_decision is not None:
                # Shadow mode: the decision is recorded verbatim but never
                # altered execution — enforce mode never reaches this point
                # with a would_block decision.
                completion_set["release_gate_shadow"] = gate_decision
            completion = await db["graph_promotion_jobs"].update_one(
                {
                    "job_id": job_id,
                    "status": "running",
                    "runner": runner,
                    "started_at": started_at,
                    "last_run_at": started_at,
                    "lease_until": lease_until,
                },
                {
                    "$set": completion_set,
                    "$unset": {"failure_reason": "", "runner": "", "started_at": ""},
                },
            )
            completed_owned = int(getattr(completion, "modified_count", 0) or 0) > 0
            if completed_owned:
                counts[final_status] += 1
            else:
                counts["lost_ownership"] += 1
            results.append(
                {
                    "job_id": job_id,
                    "doc_id": job.get("doc_id"),
                    "status": final_status if completed_owned else "lost_ownership",
                    "attempted_status": final_status,
                    "neo4j_write_latency_ms": neo4j_write_latency_ms,
                    "promoted_counts": promoted_counts,
                    "claim_promotion_result": claim_result,
                    **(
                        {"release_gate_shadow": gate_decision}
                        if gate_decision is not None
                        else {}
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001
            neo4j_write_latency_ms = _elapsed_ms(started_perf)
            failure_set: dict[str, Any] = {
                "status": "failed",
                "failure_reason": str(exc)[:1000],
                "neo4j_write_latency_ms": neo4j_write_latency_ms,
                "neo4j_write_latency_source": "graph_promotion_job",
                "lease_until": None,
                "completed_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            if gate_decision is not None:
                # No execution without an authorization record: the shadow
                # decision accompanies the failure path too. It never
                # reclassifies the failure — release policy is not a cause.
                failure_set["release_gate_shadow"] = gate_decision
            completion = await db["graph_promotion_jobs"].update_one(
                {
                    "job_id": job_id,
                    "status": "running",
                    "runner": runner,
                    "started_at": started_at,
                    "last_run_at": started_at,
                    "lease_until": lease_until,
                },
                {
                    "$set": failure_set,
                    "$unset": {"runner": "", "started_at": ""},
                },
            )
            completed_owned = int(getattr(completion, "modified_count", 0) or 0) > 0
            if completed_owned:
                counts["failed"] += 1
            else:
                counts["lost_ownership"] += 1
            results.append(
                {
                    "job_id": job_id,
                    "doc_id": job.get("doc_id"),
                    "status": "failed" if completed_owned else "lost_ownership",
                    "attempted_status": "failed",
                    "neo4j_write_latency_ms": neo4j_write_latency_ms,
                    "failure_reason": str(exc)[:300],
                    **(
                        {"release_gate_shadow": gate_decision}
                        if gate_decision is not None
                        else {}
                    ),
                }
            )

    return {
        "corpus_id": corpus_id,
        "status": "complete",
        "counts": counts,
        "results": results,
        # Additive run-level shadow diagnostics (Shadow A contract): the
        # release state resolved once at run start.
        **(
            {
                "release_gate_shadow": {
                    "gate_mode": gate_mode,
                    "active_release_resolved_once": True,
                    "release_registry_entry": registry_handle.resolution.entry_id,
                    "registry_identity": registry_handle.resolution.trace_identity(),
                    "caller": "run_graph_promotion_jobs",
                    "operator": user_id,
                    **run_gate_decision,
                }
            }
            if run_gate_decision is not None
            else {}
        ),
    }
