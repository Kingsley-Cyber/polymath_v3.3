"""Durable summary job planner.

Parent-summary and document-summary backfills are real ingestion work, but
historically they only existed as ad-hoc repair runs. This module materializes
missing summary work as an inspectable queue/read model so corpus readiness can
explain exactly what is pending or blocked.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Awaitable, Callable

from pymongo import UpdateOne

from db.queue_integrity import bulk_upsert_durable_jobs
from services.ingestion.document_summaries import MISSING_DOCUMENT_SUMMARY_CLAUSE
from services.ingestion.job_leases import (
    claim_runnable_jobs,
    reclaim_expired_running_jobs,
    retire_superseded_jobs,
)
from services.ingestion.section_classifier import parent_summary_required_clause
from services.ingestion.stage_identity import summary_stage_identity
from services.storage.record_status import with_active_records

ACTIVE_STATUSES = {"queued", "running"}
FAILED_STATUSES = {
    "failed",
    "blocked_empty_source",
    "blocked_no_parent_summaries",
    "blocked_parent_summaries_incomplete",
}
TERMINAL_STATUSES = {"succeeded", "skipped"}
SUPERSEDABLE_STATUSES = ACTIVE_STATUSES | FAILED_STATUSES
SUMMARY_TEXT_CLAUSE: dict[str, Any] = {
    "summary": {"$exists": True, "$nin": [None, ""]}
}
MISSING_PARENT_SUMMARY_CLAUSE: dict[str, Any] = {
    "$or": [{"summary": {"$exists": False}}, {"summary": None}, {"summary": ""}]
}
SUMMARY_RUNNABLE_STATUSES = ("queued",)
TERMINAL_SKIP_INGEST_STAGES = {"skipped_duplicate"}
STAGE_IDENTITY_MISSING_CLAUSE: dict[str, Any] = {
    "$or": [
        {"stage_identity": {"$exists": False}},
        {"stage_identity": None},
        {"stage_identity.identity_version": {"$exists": False}},
        {"stage_identity.identity_version": None},
        {"stage_identity.identity_version": ""},
    ]
}
SummaryParentRunner = Callable[..., Awaitable[dict[str, Any]]]
SummaryDocumentRunner = Callable[..., Awaitable[dict[str, Any]]]


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_model_entry(entry: Any, *, lane: int) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        raw = entry.model_dump()
    elif isinstance(entry, dict):
        raw = dict(entry)
    else:
        raw = {}
    return {
        "lane": lane,
        "provider_preset": raw.get("provider_preset") or raw.get("provider"),
        "model": raw.get("model") or raw.get("model_name"),
        "base_url": raw.get("base_url") or raw.get("api_base"),
        "max_concurrent": raw.get("max_concurrent"),
    }


def summary_provider_contract(corpus: dict[str, Any] | None) -> dict[str, Any]:
    cfg = ((corpus or {}).get("default_ingestion_config") or {})
    pool = list(cfg.get("summary_models") or [])
    return {
        "pool_source": "corpus_summary_models" if pool else "runtime_summary_settings",
        "pool_size": len(pool),
        "lanes": [_safe_model_entry(entry, lane=idx) for idx, entry in enumerate(pool)],
    }


def summary_contract_hash(corpus: dict[str, Any] | None) -> str:
    cfg = ((corpus or {}).get("default_ingestion_config") or {})
    relevant = {
        "summary_provider_contract": summary_provider_contract(corpus),
        "max_summary_tokens": cfg.get("max_summary_tokens"),
        "chunk_summarization": cfg.get("chunk_summarization"),
        "target_qdrant_collections": cfg.get("target_qdrant_collections"),
        "retrieval_parent_summary_predicate": "parent_summary_required_clause.v1",
        "document_summary_contract": "summary_tree.v1",
    }
    return _stable_hash(relevant)


def summary_job_id(
    *,
    corpus_id: str,
    kind: str,
    target_id: str,
    source_hash: str,
    contract_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{corpus_id}:{kind}:{target_id}:{source_hash}:{contract_hash}".encode("utf-8")
    ).hexdigest()
    prefix = "summary_parent" if kind == "retrieval_parent_summary" else "summary_doc"
    return f"{prefix}_{digest[:24]}"


def build_parent_summary_job(
    *,
    parent: dict[str, Any],
    doc: dict[str, Any] | None,
    corpus: dict[str, Any] | None,
) -> dict[str, Any]:
    corpus_id = str(parent.get("corpus_id") or "")
    parent_id = str(parent.get("parent_id") or "")
    source_hash = (
        str(parent.get("summary_source_hash") or parent.get("source_hash") or parent.get("text_hash") or "")
        or _text_hash(parent.get("text"))
    )
    contract_hash = summary_contract_hash(corpus)
    status = "queued" if str(parent.get("text") or "").strip() else "blocked_empty_source"
    stage_identity = summary_stage_identity(
        source=parent,
        doc=doc,
        source_hash=source_hash,
        summary_contract_hash=contract_hash,
    )
    return {
        "job_id": summary_job_id(
            corpus_id=corpus_id,
            kind="retrieval_parent_summary",
            target_id=parent_id,
            source_hash=source_hash,
            contract_hash=contract_hash,
        ),
        "kind": "retrieval_parent_summary",
        "corpus_id": corpus_id,
        "doc_id": str(parent.get("doc_id") or ""),
        "parent_id": parent_id,
        "user_id": str((doc or {}).get("user_id") or parent.get("user_id") or ""),
        "filename": (doc or {}).get("filename") or parent.get("filename"),
        "status": status,
        "reason": "missing_parent_summary" if status == "queued" else "empty_parent_text",
        "source_hash": source_hash,
        "summary_contract_hash": contract_hash,
        "stage_identity": stage_identity,
        "provider_contract": summary_provider_contract(corpus),
        "source_tier": parent.get("source_tier"),
        "chunk_kind": parent.get("chunk_kind"),
    }


def classify_document_summary_status(
    *,
    required_parent_count: int,
    summarized_parent_count: int,
) -> tuple[str, str]:
    missing_parent_count = max(int(required_parent_count or 0) - int(summarized_parent_count or 0), 0)
    if int(required_parent_count or 0) <= 0 or int(summarized_parent_count or 0) <= 0:
        return "blocked_no_parent_summaries", "no_parent_summary_context"
    if missing_parent_count > 0:
        return "blocked_parent_summaries_incomplete", "parent_summaries_incomplete"
    return "queued", "missing_document_summary"


def build_document_summary_job(
    *,
    doc: dict[str, Any],
    corpus: dict[str, Any] | None,
    required_parent_count: int,
    summarized_parent_count: int,
) -> dict[str, Any]:
    corpus_id = str(doc.get("corpus_id") or "")
    doc_id = str(doc.get("doc_id") or "")
    contract_hash = summary_contract_hash(corpus)
    source_hash = str(doc.get("content_sha256") or (doc.get("source_identity") or {}).get("content_sha256") or "")
    if not source_hash:
        source_hash = _stable_hash(
            {
                "doc_id": doc_id,
                "updated_at": doc.get("updated_at"),
                "chunk_count": doc.get("chunk_count"),
            }
        )
    status, reason = classify_document_summary_status(
        required_parent_count=required_parent_count,
        summarized_parent_count=summarized_parent_count,
    )
    stage_identity = summary_stage_identity(
        source=doc,
        doc=doc,
        source_hash=source_hash,
        summary_contract_hash=contract_hash,
    )
    return {
        "job_id": summary_job_id(
            corpus_id=corpus_id,
            kind="document_summary",
            target_id=doc_id,
            source_hash=source_hash,
            contract_hash=contract_hash,
        ),
        "kind": "document_summary",
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "user_id": str(doc.get("user_id") or ""),
        "filename": doc.get("filename"),
        "status": status,
        "reason": reason,
        "source_hash": source_hash,
        "summary_contract_hash": contract_hash,
        "stage_identity": stage_identity,
        "provider_contract": summary_provider_contract(corpus),
        "required_parent_count": int(required_parent_count or 0),
        "summarized_parent_count": int(summarized_parent_count or 0),
        "missing_parent_count": max(int(required_parent_count or 0) - int(summarized_parent_count or 0), 0),
    }


async def _count(db: Any, collection: str, query: dict[str, Any]) -> int:
    try:
        return int(await db[collection].count_documents(query))
    except Exception:
        return 0


def _non_empty(value: Any) -> bool:
    return bool(str(value or "").strip())


async def _doc_map(db: Any, *, corpus_id: str, doc_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not doc_ids:
        return {}
    rows = await db["documents"].find(
        {"corpus_id": corpus_id, "doc_id": {"$in": sorted(doc_ids)}},
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "filename": 1,
            "updated_at": 1,
            "chunk_count": 1,
            "content_sha256": 1,
            "source_identity": 1,
            "source_key": 1,
            "source_file_hash": 1,
            "ingest_stage": 1,
        },
    ).to_list(length=len(doc_ids))
    return {str(row.get("doc_id") or ""): row for row in rows if row.get("doc_id")}


async def _document_summary_exists(db: Any, *, corpus_id: str, doc_id: str) -> bool:
    state = await _document_summary_state(db, corpus_id=corpus_id, doc_id=doc_id)
    return bool(state.get("synced"))


async def _document_summary_state(db: Any, *, corpus_id: str, doc_id: str) -> dict[str, bool]:
    profile_done = False
    tree_done = False
    try:
        doc = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "doc_profile.summary": 1},
        )
        profile_done = _non_empty(((doc or {}).get("doc_profile") or {}).get("summary"))
    except Exception:
        pass
    try:
        row = await db["summary_tree"].find_one(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "node_type": "document",
                "summary": {"$exists": True, "$nin": [None, ""]},
            },
            {"_id": 0, "summary": 1},
        )
        tree_done = bool(row)
    except Exception:
        tree_done = False
    return {
        "profile_done": profile_done,
        "tree_done": tree_done,
        "usable": profile_done or tree_done,
        "synced": profile_done and tree_done,
    }


async def _document_summary_candidate_docs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return docs whose document-summary artifact pair is incomplete."""

    doc_match = with_active_records(
        {
            "corpus_id": corpus_id,
            "ingest_stage": {"$nin": sorted(TERMINAL_SKIP_INGEST_STAGES)},
        }
    )
    if user_id:
        doc_match["user_id"] = user_id
    projection = {
        "_id": 0,
        "doc_id": 1,
        "corpus_id": 1,
        "user_id": 1,
        "filename": 1,
        "updated_at": 1,
        "chunk_count": 1,
        "content_sha256": 1,
        "source_identity": 1,
    }
    try:
        rows = await db["documents"].aggregate([
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
                "$addFields": {
                    "document_profile_done": {
                        "$and": [
                            {"$ne": ["$doc_profile.summary", None]},
                            {"$ne": ["$doc_profile.summary", ""]},
                        ]
                    },
                    "document_tree_done": {
                        "$gt": [{"$size": "$document_summary_tree"}, 0]
                    },
                }
            },
            {
                "$match": {
                    "$or": [
                        {"document_profile_done": False},
                        {"document_tree_done": False},
                    ]
                }
            },
            {"$project": projection},
            {"$limit": limit},
        ]).to_list(length=limit)
        if rows:
            return rows
    except Exception:
        pass

    # Fallback for test doubles/older Mongo paths. This only sees profile
    # misses, but explicit job execution can still repair tree-only drift.
    fallback_query = with_active_records(
        {
            "corpus_id": corpus_id,
            "ingest_stage": {"$nin": sorted(TERMINAL_SKIP_INGEST_STAGES)},
            **MISSING_DOCUMENT_SUMMARY_CLAUSE,
        }
    )
    if user_id:
        fallback_query["user_id"] = user_id
    return await db["documents"].find(
        fallback_query,
        projection,
    ).limit(limit).to_list(length=limit)


async def _parent_job_status_after_run(
    db: Any,
    *,
    corpus_id: str,
    job: dict[str, Any],
    paused: bool = False,
    error: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    parent_id = str(job.get("parent_id") or "")
    parent = None
    if parent_id:
        try:
            parent = await db["parent_chunks"].find_one(
                {"corpus_id": corpus_id, "parent_id": parent_id},
                {"_id": 0, "summary": 1, "text": 1},
            )
        except Exception:
            parent = None
    if parent and _non_empty(parent.get("summary")):
        return "succeeded", "summary_present", {}
    if parent is None:
        return "failed", "parent_row_missing", {"missing_parent": True}
    if not _non_empty(parent.get("text")):
        return "blocked_empty_source", "empty_parent_text", {}
    if paused:
        return "queued", "paused_pressure", {}
    if error:
        return "failed", "runner_error", {"last_error": error}
    return "queued", "missing_parent_summary", {}


async def _document_job_status_after_run(
    db: Any,
    *,
    corpus_id: str,
    job: dict[str, Any],
    paused: bool = False,
    error: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    doc_id = str(job.get("doc_id") or "")
    if doc_id and await _document_summary_exists(db, corpus_id=corpus_id, doc_id=doc_id):
        return "succeeded", "document_summary_present", {}
    parent_clause = parent_summary_required_clause()
    required_parent_count = await _count(
        db,
        "parent_chunks",
        {"corpus_id": corpus_id, "doc_id": doc_id, "$and": [parent_clause]},
    )
    summarized_parent_count = await _count(
        db,
        "parent_chunks",
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "$and": [parent_clause, SUMMARY_TEXT_CLAUSE],
        },
    )
    status, reason = classify_document_summary_status(
        required_parent_count=required_parent_count,
        summarized_parent_count=summarized_parent_count,
    )
    metadata = {
        "required_parent_count": required_parent_count,
        "summarized_parent_count": summarized_parent_count,
        "missing_parent_count": max(required_parent_count - summarized_parent_count, 0),
    }
    if status != "queued":
        return status, reason, metadata
    if paused:
        return "queued", "paused_pressure", metadata
    if error:
        metadata["last_error"] = error
        return "failed", "runner_error", metadata
    return "queued", "missing_document_summary", metadata


async def _reconcile_claimed_summary_jobs(
    db: Any,
    *,
    corpus_id: str,
    jobs: list[dict[str, Any]],
    paused_kinds: set[str] | None = None,
    errors_by_kind: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    counts: dict[str, int] = {}
    previews: list[dict[str, Any]] = []
    ops = []
    paused_kinds = paused_kinds or set()
    errors_by_kind = errors_by_kind or {}
    for job in jobs:
        kind = str(job.get("kind") or "")
        if kind == "retrieval_parent_summary":
            status, reason, metadata = await _parent_job_status_after_run(
                db,
                corpus_id=corpus_id,
                job=job,
                paused=kind in paused_kinds,
                error=errors_by_kind.get(kind),
            )
        elif kind == "document_summary":
            status, reason, metadata = await _document_job_status_after_run(
                db,
                corpus_id=corpus_id,
                job=job,
                paused=kind in paused_kinds,
                error=errors_by_kind.get(kind),
            )
        else:
            status, reason, metadata = "failed", "unknown_summary_job_kind", {}
        counts[status] = counts.get(status, 0) + 1
        update: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "lease_until": None,
            "updated_at": now,
            "last_reconciled_at": now,
            **metadata,
        }
        if status == "succeeded":
            update["completed_at"] = now
            update.pop("last_error", None)
        elif status == "queued":
            update["completed_at"] = None
        ops.append(UpdateOne({"job_id": job.get("job_id")}, {"$set": update}, upsert=False))
        previews.append(
            {
                "job_id": job.get("job_id"),
                "kind": kind,
                "doc_id": job.get("doc_id"),
                "parent_id": job.get("parent_id"),
                "status": status,
                "reason": reason,
            }
        )
    if ops:
        await db["summary_jobs"].bulk_write(ops, ordered=False)
    return {"counts": counts, "jobs": previews[:50]}


async def plan_summary_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 500,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 500), 10000))
    kinds_set = set(kinds or ["retrieval_parent_summary", "document_summary"])
    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "corpus_id": 1, "user_id": 1, "default_ingestion_config": 1},
    )
    if not corpus:
        return {
            "status": "not_found",
            "apply": bool(apply),
            "corpus_id": corpus_id,
            "planned": 0,
            "counts": {},
            "jobs": [],
        }

    jobs: list[dict[str, Any]] = []
    remaining = limit

    if "retrieval_parent_summary" in kinds_set and remaining > 0:
        parent_query = with_active_records(
            {
                "corpus_id": corpus_id,
                "$and": [parent_summary_required_clause(), MISSING_PARENT_SUMMARY_CLAUSE],
            }
        )
        parent_rows = await db["parent_chunks"].find(
            parent_query,
            {
                "_id": 0,
                "parent_id": 1,
                "doc_id": 1,
                "corpus_id": 1,
                "source_tier": 1,
                "chunk_kind": 1,
                "text": 1,
                "summary_source_hash": 1,
                "source_hash": 1,
                "text_hash": 1,
                "filename": 1,
                "user_id": 1,
            },
        ).limit(remaining).to_list(length=remaining)
        docs = await _doc_map(
            db,
            corpus_id=corpus_id,
            doc_ids={str(row.get("doc_id") or "") for row in parent_rows if row.get("doc_id")},
        )
        jobs.extend(
            build_parent_summary_job(
                parent=row,
                doc=docs.get(str(row.get("doc_id") or "")),
                corpus=corpus,
            )
            for row in parent_rows
            if row.get("parent_id")
            and (
                docs.get(str(row.get("doc_id") or ""), {}).get("ingest_stage")
                not in TERMINAL_SKIP_INGEST_STAGES
            )
        )
        remaining = max(limit - len(jobs), 0)

    if "document_summary" in kinds_set and remaining > 0:
        doc_rows = await _document_summary_candidate_docs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            limit=remaining,
        )
        parent_clause = parent_summary_required_clause()
        for doc in doc_rows:
            doc_id = str(doc.get("doc_id") or "")
            if not doc_id:
                continue
            required_parent_count = await _count(
                db,
                "parent_chunks",
                {"corpus_id": corpus_id, "doc_id": doc_id, "$and": [parent_clause]},
            )
            summarized_parent_count = await _count(
                db,
                "parent_chunks",
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "$and": [parent_clause, SUMMARY_TEXT_CLAUSE],
                },
            )
            jobs.append(
                build_document_summary_job(
                    doc=doc,
                    corpus=corpus,
                    required_parent_count=required_parent_count,
                    summarized_parent_count=summarized_parent_count,
                )
            )

    counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for job in jobs:
        counts[str(job["status"])] = counts.get(str(job["status"]), 0) + 1
        kind_counts[str(job["kind"])] = kind_counts.get(str(job["kind"]), 0) + 1

    result: dict[str, Any] = {
        "status": "planned" if not apply else "complete",
        "apply": bool(apply),
        "corpus_id": corpus_id,
        "planned": len(jobs),
        "counts": counts,
        "kind_counts": kind_counts,
        "jobs": jobs[:50],
    }
    if not apply or not jobs:
        return result

    now = datetime.utcnow()
    ops = []
    for job in jobs:
        ops.append(
            UpdateOne(
                {"job_id": job["job_id"]},
                {
                    "$set": {
                        **job,
                        "updated_at": now,
                        "last_planned_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "attempt_count": 0,
                    },
                },
                upsert=True,
            )
        )
    await bulk_upsert_durable_jobs(db["summary_jobs"], ops)
    parent_jobs = [job for job in jobs if job.get("kind") == "retrieval_parent_summary"]
    document_jobs = [job for job in jobs if job.get("kind") == "document_summary"]
    result["superseded"] = 0
    result["superseded"] += await retire_superseded_jobs(
        db,
        collection_name="summary_jobs",
        jobs=parent_jobs,
        identity_fields=("corpus_id", "kind", "parent_id"),
        supersedable_statuses=SUPERSEDABLE_STATUSES,
        now=now,
    )
    result["superseded"] += await retire_superseded_jobs(
        db,
        collection_name="summary_jobs",
        jobs=document_jobs,
        identity_fields=("corpus_id", "kind", "doc_id"),
        supersedable_statuses=SUPERSEDABLE_STATUSES,
        now=now,
    )
    return result


async def backfill_summary_stage_identity(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 1000,
) -> dict[str, Any]:
    """Backfill stage_identity for existing summary job rows.

    This repairs the durable queue metadata in place. It does not generate
    summaries and it does not re-order the summary backlog; active queued jobs
    keep their status while gaining the contract identity needed for safe retry
    and readiness accounting.
    """

    limit = max(1, min(int(limit or 1000), 50000))
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        **STAGE_IDENTITY_MISSING_CLAUSE,
    }
    if user_id:
        query["user_id"] = user_id
    rows = await db["summary_jobs"].find(
        query,
        {
            "_id": 1,
            "job_id": 1,
            "kind": 1,
            "corpus_id": 1,
            "doc_id": 1,
            "parent_id": 1,
            "user_id": 1,
            "status": 1,
        },
    ).limit(limit).to_list(length=limit)

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "corpus_id": 1, "user_id": 1, "default_ingestion_config": 1},
    )
    if not corpus:
        return {
            "status": "not_found",
            "apply": bool(apply),
            "corpus_id": corpus_id,
            "limit": limit,
            "scanned": len(rows),
            "planned": 0,
            "modified": 0,
            "samples": [],
        }

    doc_ids = {str(row.get("doc_id") or "") for row in rows if row.get("doc_id")}
    docs = await _doc_map(db, corpus_id=corpus_id, doc_ids=doc_ids)
    parent_ids = sorted({str(row.get("parent_id") or "") for row in rows if row.get("parent_id")})
    parents_by_id: dict[str, dict[str, Any]] = {}
    if parent_ids:
        parent_rows = await db["parent_chunks"].find(
            {"corpus_id": corpus_id, "parent_id": {"$in": parent_ids}},
            {
                "_id": 0,
                "parent_id": 1,
                "doc_id": 1,
                "corpus_id": 1,
                "source_tier": 1,
                "chunk_kind": 1,
                "text": 1,
                "summary_source_hash": 1,
                "source_hash": 1,
                "text_hash": 1,
                "filename": 1,
                "user_id": 1,
            },
        ).to_list(length=len(parent_ids))
        parents_by_id = {
            str(row.get("parent_id") or ""): row
            for row in parent_rows
            if row.get("parent_id")
        }

    parent_clause = parent_summary_required_clause()
    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    samples: list[dict[str, Any]] = []
    skipped_missing_doc = 0
    skipped_missing_parent = 0
    skipped_unknown_kind = 0
    skipped_missing_id = 0
    for row in rows:
        selector = {"_id": row["_id"]} if row.get("_id") is not None else (
            {"job_id": row.get("job_id")} if row.get("job_id") else None
        )
        if selector is None:
            skipped_missing_id += 1
            continue
        kind = str(row.get("kind") or "")
        doc_id = str(row.get("doc_id") or "")
        doc = docs.get(doc_id)
        if not doc:
            skipped_missing_doc += 1
            continue
        if kind == "retrieval_parent_summary":
            parent = parents_by_id.get(str(row.get("parent_id") or ""))
            if not parent:
                skipped_missing_parent += 1
                continue
            rebuilt = build_parent_summary_job(parent=parent, doc=doc, corpus=corpus)
            update = {
                "source_hash": rebuilt["source_hash"],
                "summary_contract_hash": rebuilt["summary_contract_hash"],
                "stage_identity": rebuilt["stage_identity"],
                "provider_contract": rebuilt["provider_contract"],
                "stage_identity_repaired_at": now,
                "updated_at": now,
            }
        elif kind == "document_summary":
            required_parent_count = await _count(
                db,
                "parent_chunks",
                {"corpus_id": corpus_id, "doc_id": doc_id, "$and": [parent_clause]},
            )
            summarized_parent_count = await _count(
                db,
                "parent_chunks",
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "$and": [parent_clause, SUMMARY_TEXT_CLAUSE],
                },
            )
            rebuilt = build_document_summary_job(
                doc=doc,
                corpus=corpus,
                required_parent_count=required_parent_count,
                summarized_parent_count=summarized_parent_count,
            )
            update = {
                "source_hash": rebuilt["source_hash"],
                "summary_contract_hash": rebuilt["summary_contract_hash"],
                "stage_identity": rebuilt["stage_identity"],
                "provider_contract": rebuilt["provider_contract"],
                "required_parent_count": rebuilt["required_parent_count"],
                "summarized_parent_count": rebuilt["summarized_parent_count"],
                "missing_parent_count": rebuilt["missing_parent_count"],
                "stage_identity_repaired_at": now,
                "updated_at": now,
            }
        else:
            skipped_unknown_kind += 1
            continue
        ops.append(UpdateOne(selector, {"$set": update}))
        if len(samples) < 20:
            samples.append(
                {
                    "job_id": row.get("job_id"),
                    "kind": kind,
                    "doc_id": doc_id,
                    "parent_id": row.get("parent_id"),
                    "summary_contract_hash": update["summary_contract_hash"],
                }
            )

    modified = 0
    if apply and ops:
        result = await db["summary_jobs"].bulk_write(ops, ordered=False)
        modified = int(getattr(result, "modified_count", 0) or 0)

    return {
        "status": "planned" if not apply else "complete",
        "apply": bool(apply),
        "corpus_id": corpus_id,
        "limit": limit,
        "scanned": len(rows),
        "planned": len(ops),
        "modified": modified,
        "skipped_missing_doc": skipped_missing_doc,
        "skipped_missing_parent": skipped_missing_parent,
        "skipped_unknown_kind": skipped_unknown_kind,
        "skipped_missing_id": skipped_missing_id,
        "samples": samples,
    }


async def run_summary_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 25,
    statuses: list[str] | None = None,
    kinds: list[str] | None = None,
    parent_runner: SummaryParentRunner | None = None,
    document_runner: SummaryDocumentRunner | None = None,
) -> dict[str, Any]:
    """Run a bounded slice of materialized summary jobs.

    This is intentionally an orchestrator: the existing Ghost A and document
    summary backfill paths still do the heavy work. Job success is reconciled
    from Mongo artifacts after the run so provider responses cannot become
    readiness truth by themselves.
    """

    limit = max(1, min(int(limit or 25), 500))
    now = datetime.utcnow()
    reclaimed = await reclaim_expired_running_jobs(
        db,
        collection_name="summary_jobs",
        corpus_id=corpus_id,
        user_id=user_id,
        now=now,
    )
    runnable_statuses = statuses or list(SUMMARY_RUNNABLE_STATUSES)
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "status": {"$in": runnable_statuses},
    }
    if kinds:
        query["kind"] = {"$in": kinds}

    jobs = await db["summary_jobs"].find(
        query,
        {"_id": 0},
    ).sort("updated_at", 1).limit(limit).to_list(length=limit)
    if not jobs:
        return {
            "status": "empty",
            "corpus_id": corpus_id,
            "claimed": 0,
            "reclaimed": reclaimed,
            "counts": {},
            "runner_results": {},
            "jobs": [],
        }

    candidate_count = len(jobs)
    jobs = await claim_runnable_jobs(
        db,
        collection_name="summary_jobs",
        jobs=jobs,
        runnable_statuses=runnable_statuses,
        now=now,
        runner="summary_jobs.run",
        increment_attempt=True,
    )
    if not jobs:
        return {
            "status": "empty",
            "corpus_id": corpus_id,
            "claimed": 0,
            "candidates": candidate_count,
            "reclaimed": reclaimed,
            "counts": {},
            "runner_results": {},
            "jobs": [],
        }

    parent_jobs = [job for job in jobs if job.get("kind") == "retrieval_parent_summary"]
    document_jobs = [job for job in jobs if job.get("kind") == "document_summary"]
    runner_results: dict[str, Any] = {}
    paused_kinds: set[str] = set()
    errors_by_kind: dict[str, str] = {}

    if parent_jobs:
        if parent_runner is None:
            errors_by_kind["retrieval_parent_summary"] = "parent summary runner unavailable"
        else:
            try:
                doc_ids = sorted(
                    {str(job.get("doc_id") or "") for job in parent_jobs if job.get("doc_id")}
                )
                runner_results["retrieval_parent_summary"] = await parent_runner(
                    limit=len(parent_jobs),
                    doc_ids=doc_ids or None,
                )
                if runner_results["retrieval_parent_summary"].get("status") == "paused_pressure":
                    paused_kinds.add("retrieval_parent_summary")
            except Exception as exc:  # noqa: BLE001 - bounded runner records failure per job
                errors_by_kind["retrieval_parent_summary"] = str(exc)[:500]
                runner_results["retrieval_parent_summary"] = {
                    "status": "failed",
                    "error": errors_by_kind["retrieval_parent_summary"],
                }

    if document_jobs:
        if document_runner is None:
            errors_by_kind["document_summary"] = "document summary runner unavailable"
        else:
            try:
                doc_ids = sorted(
                    {str(job.get("doc_id") or "") for job in document_jobs if job.get("doc_id")}
                )
                runner_results["document_summary"] = await document_runner(
                    limit=len(document_jobs),
                    doc_ids=doc_ids or None,
                )
                if runner_results["document_summary"].get("status") == "paused_pressure":
                    paused_kinds.add("document_summary")
            except Exception as exc:  # noqa: BLE001 - bounded runner records failure per job
                errors_by_kind["document_summary"] = str(exc)[:500]
                runner_results["document_summary"] = {
                    "status": "failed",
                    "error": errors_by_kind["document_summary"],
                }

    reconciled = await _reconcile_claimed_summary_jobs(
        db,
        corpus_id=corpus_id,
        jobs=jobs,
        paused_kinds=paused_kinds,
        errors_by_kind=errors_by_kind,
    )
    counts = reconciled["counts"]
    status = "complete"
    if paused_kinds and not counts.get("succeeded"):
        status = "paused_pressure"
    elif errors_by_kind:
        status = "partial"
    elif counts.get("failed"):
        status = "partial"

    return {
        "status": status,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "claimed": len(jobs),
        "reclaimed": reclaimed,
        "parent_claimed": len(parent_jobs),
        "document_claimed": len(document_jobs),
        "counts": counts,
        "runner_results": runner_results,
        "jobs": reconciled["jobs"],
    }


async def list_summary_jobs(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 100,
    statuses: list[str] | None = None,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if statuses:
        query["status"] = {"$in": statuses}
    if kinds:
        query["kind"] = {"$in": kinds}
    rows = await db["summary_jobs"].find(
        query,
        {"_id": 0},
    ).sort("updated_at", -1).limit(max(1, min(int(limit or 100), 1000))).to_list(length=None)
    status_rows = await db["summary_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    kind_rows = await db["summary_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$kind", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    return {
        "corpus_id": corpus_id,
        "counts": {str(row["_id"]): int(row["count"]) for row in status_rows},
        "kind_counts": {str(row["_id"]): int(row["count"]) for row in kind_rows},
        "jobs": rows,
    }
