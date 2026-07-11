"""Durable document-stage ingestion job planner.

This is the bridge between old batch-centric ingest state and the production
pipeline shape. It materializes document-level parse/chunk/persist/embed gaps
from durable artifacts so the UI and repair cycle can show unfinished local
pipeline work without treating a stale batch row as truth.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Awaitable, Callable

from pymongo import UpdateOne

from db.queue_integrity import bulk_upsert_durable_jobs
from services.ingestion.job_leases import (
    DEAD_LETTER_JOB_STATUS,
    claim_runnable_jobs,
    reclaim_expired_running_jobs,
    retire_superseded_jobs,
)
from services.ingestion.stage_identity import document_stage_identity
from services.storage.record_status import with_active_records

ACTIVE_STATUSES = {"queued", "running"}
FAILED_STATUSES = {
    "failed",
    "blocked_no_source",
    "blocked_missing_chunks",
    "blocked_mongo_state",
}
TERMINAL_STATUSES = {"succeeded", "skipped"}
SUPERSEDABLE_STATUSES = ACTIVE_STATUSES | FAILED_STATUSES
FAILED_INGEST_STAGES = {"failed", "setup_failed", "chunk_failed"}
TERMINAL_SKIP_INGEST_STAGES = {"skipped_duplicate"}
RUNNABLE_STATUSES = ("queued",)
EXECUTOR_BACKED_KINDS = {"persist_document", "embed_document"}
SourceRunner = Callable[..., Awaitable[dict[str, Any]]]
DocumentStageRunner = Callable[..., Awaitable[dict[str, Any]]]


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_qdrant_vector_gap(write_state: dict[str, Any]) -> bool:
    if write_state.get("verified") is not False:
        return False
    return any(
        "child vectors" in str(error).lower()
        or "qdrant" in str(error).lower()
        for error in (write_state.get("verify_errors") or [])
    )


async def reconcile_satisfied_document_pipeline_jobs(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 5000,
) -> int:
    """Retire stale queue rows when document artifacts already satisfy them."""

    statuses = sorted({*SUPERSEDABLE_STATUSES, DEAD_LETTER_JOB_STATUS})
    safe_limit = max(1, min(int(limit or 5000), 50000))
    rows = await db["document_pipeline_jobs"].find(
        {"corpus_id": corpus_id, "status": {"$in": statuses}},
        {"_id": 0, "job_id": 1, "doc_id": 1, "kind": 1},
    ).limit(safe_limit).to_list(length=safe_limit)
    if not rows:
        return 0
    doc_ids = sorted({str(row.get("doc_id") or "") for row in rows if row.get("doc_id")})
    docs = await db["documents"].find(
        {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}},
        {"_id": 0, "doc_id": 1, "write_state": 1},
    ).to_list(length=len(doc_ids))
    doc_map = {str(row.get("doc_id") or ""): row for row in docs}

    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        doc = doc_map.get(doc_id) or {}
        write_state = doc.get("write_state") or {}
        kind = str(row.get("kind") or "")
        if kind == "chunk_document":
            satisfied = await _count(
                db,
                "chunks",
                with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
            ) > 0
        elif kind == "persist_document":
            satisfied = bool(write_state.get("mongo_written"))
        elif kind == "embed_document":
            satisfied = bool(write_state.get("qdrant_written")) and not _has_qdrant_vector_gap(
                write_state
            )
        else:
            satisfied = False
        if not satisfied:
            continue
        ops.append(
            UpdateOne(
                {"job_id": row.get("job_id"), "status": {"$in": statuses}},
                {
                    "$set": {
                        "status": "superseded",
                        "reason": "artifact_already_satisfied",
                        "artifact_reconciled_at": now,
                        "updated_at": now,
                        "lease_until": None,
                    },
                    "$unset": {"runner": "", "started_at": ""},
                },
            )
        )
    if not ops:
        return 0
    result = await db["document_pipeline_jobs"].bulk_write(ops, ordered=False)
    return int(getattr(result, "modified_count", 0) or 0)


def document_pipeline_contract(doc: dict[str, Any] | None) -> dict[str, Any]:
    cfg = (doc or {}).get("ingestion_config") or {}
    return {
        "embedding_model_id": (doc or {}).get("embedding_model_id") or cfg.get("embedding_model_id"),
        "embedding_dimension": cfg.get("embedding_dimension"),
        "embed_mode": cfg.get("embed_mode"),
        "child_chunk_algorithm": cfg.get("child_chunk_algorithm"),
        "parent_chunk_tokens": cfg.get("parent_chunk_tokens"),
        "child_chunk_tokens": cfg.get("child_chunk_tokens"),
        "chunk_overlap": cfg.get("chunk_overlap"),
        "target_qdrant_collections": cfg.get("target_qdrant_collections"),
    }


def document_pipeline_contract_hash(doc: dict[str, Any] | None) -> str:
    return _stable_hash(document_pipeline_contract(doc))


def document_source_fingerprint(doc: dict[str, Any] | None) -> str:
    doc = doc or {}
    source_identity = doc.get("source_identity") or {}
    for value in (
        source_identity.get("content_sha256"),
        doc.get("content_sha256"),
        source_identity.get("source_key"),
        doc.get("source_key"),
        doc.get("source_path"),
        doc.get("backend_path"),
        doc.get("file_path"),
        doc.get("filename"),
    ):
        if value:
            return str(value)
    return _stable_hash(
        {
            "doc_id": doc.get("doc_id"),
            "updated_at": doc.get("updated_at"),
            "chunk_count": doc.get("chunk_count"),
        }
    )


def document_pipeline_job_id(
    *,
    corpus_id: str,
    doc_id: str,
    kind: str,
    source_fingerprint: str,
    contract_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{corpus_id}:{doc_id}:{kind}:{source_fingerprint}:{contract_hash}".encode("utf-8")
    ).hexdigest()
    return f"doc_stage_{digest[:24]}"


def _has_source_pointer(doc: dict[str, Any]) -> bool:
    source_identity = doc.get("source_identity") or {}
    return any(
        bool(doc.get(field))
        for field in ("source_path", "backend_path", "file_path", "original_path", "filename")
    ) or any(
        bool(source_identity.get(field))
        for field in ("source_key", "content_sha256", "url", "youtube_video_id")
    )


def build_document_pipeline_job(
    *,
    doc: dict[str, Any],
    kind: str,
    child_chunks: int,
    parent_chunks: int,
) -> dict[str, Any]:
    corpus_id = str(doc.get("corpus_id") or "")
    doc_id = str(doc.get("doc_id") or "")
    ingest_stage = str(doc.get("ingest_stage") or "")
    write_state = doc.get("write_state") or {}
    contract_hash = document_pipeline_contract_hash(doc)
    source_fingerprint = document_source_fingerprint(doc)
    stage_identity = document_stage_identity(
        doc=doc,
        pipeline_contract_hash=contract_hash,
    )

    status = "queued"
    reason = "pending"
    if kind == "chunk_document":
        if ingest_stage in TERMINAL_SKIP_INGEST_STAGES:
            status = "skipped"
            reason = "duplicate_document"
        elif ingest_stage in FAILED_INGEST_STAGES:
            status = "failed"
            reason = ingest_stage
        elif not _has_source_pointer(doc):
            status = "blocked_no_source"
            reason = "source_pointer_missing"
        else:
            reason = "missing_chunks"
    elif kind == "persist_document":
        if child_chunks <= 0:
            status = "blocked_missing_chunks"
            reason = "missing_chunks"
        else:
            reason = "chunks_not_marked_mongo_written"
    elif kind == "embed_document":
        if child_chunks <= 0:
            status = "blocked_missing_chunks"
            reason = "missing_chunks"
        elif write_state.get("mongo_written") is not True:
            status = "blocked_mongo_state"
            reason = "mongo_write_not_complete"
        elif _has_qdrant_vector_gap(write_state):
            reason = "qdrant_vector_mismatch"
        else:
            reason = "missing_qdrant_vectors"

    return {
        "job_id": document_pipeline_job_id(
            corpus_id=corpus_id,
            doc_id=doc_id,
            kind=kind,
            source_fingerprint=source_fingerprint,
            contract_hash=contract_hash,
        ),
        "kind": kind,
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "user_id": str(doc.get("user_id") or ""),
        "filename": doc.get("filename"),
        "status": status,
        "reason": reason,
        "ingest_stage": ingest_stage or None,
        "write_state": {
            "mongo_written": bool(write_state.get("mongo_written")),
            "qdrant_written": bool(write_state.get("qdrant_written")),
            "neo4j_written": bool(write_state.get("neo4j_written")),
            "verified": write_state.get("verified"),
        },
        "child_chunks": int(child_chunks or 0),
        "parent_chunks": int(parent_chunks or 0),
        "source_fingerprint": source_fingerprint,
        "pipeline_contract_hash": contract_hash,
        "pipeline_contract": document_pipeline_contract(doc),
        "stage_identity": stage_identity,
    }


def classify_document_pipeline_jobs(
    *,
    doc: dict[str, Any],
    child_chunks: int,
    parent_chunks: int,
) -> list[dict[str, Any]]:
    write_state = doc.get("write_state") or {}
    jobs: list[dict[str, Any]] = []
    if child_chunks <= 0:
        jobs.append(
            build_document_pipeline_job(
                doc=doc,
                kind="chunk_document",
                child_chunks=child_chunks,
                parent_chunks=parent_chunks,
            )
        )
        return jobs
    if write_state.get("mongo_written") is not True:
        jobs.append(
            build_document_pipeline_job(
                doc=doc,
                kind="persist_document",
                child_chunks=child_chunks,
                parent_chunks=parent_chunks,
            )
        )
    if write_state.get("qdrant_written") is not True or _has_qdrant_vector_gap(write_state):
        jobs.append(
            build_document_pipeline_job(
                doc=doc,
                kind="embed_document",
                child_chunks=child_chunks,
                parent_chunks=parent_chunks,
            )
        )
    return jobs


async def _count(db: Any, collection: str, query: dict[str, Any]) -> int:
    try:
        return _int(await db[collection].count_documents(query))
    except Exception:
        return 0


async def _doc_for_job(db: Any, *, corpus_id: str, doc_id: str) -> dict[str, Any] | None:
    try:
        return await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "_id": 0,
                "doc_id": 1,
                "corpus_id": 1,
                "user_id": 1,
                "filename": 1,
                "ingest_stage": 1,
                "write_state": 1,
                "source_identity": 1,
                "source_key": 1,
                "content_sha256": 1,
                "source_path": 1,
                "backend_path": 1,
                "file_path": 1,
                "original_path": 1,
            },
        )
    except Exception:
        return None


async def _job_status_from_artifacts(
    db: Any,
    *,
    corpus_id: str,
    job: dict[str, Any],
    source_requested: bool = False,
    source_error: str | None = None,
    executor_errors_by_kind: dict[str, str] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    doc_id = str(job.get("doc_id") or "")
    kind = str(job.get("kind") or "")
    doc = await _doc_for_job(db, corpus_id=corpus_id, doc_id=doc_id)
    if not doc:
        return "failed", "document_missing", {"last_error": "document row missing"}
    child_chunks = await _count(
        db,
        "chunks",
        with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
    )
    parent_chunks = await _count(
        db,
        "parent_chunks",
        with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
    )
    write_state = doc.get("write_state") or {}
    metadata = {
        "child_chunks": child_chunks,
        "parent_chunks": parent_chunks,
        "write_state": {
            "mongo_written": bool(write_state.get("mongo_written")),
            "qdrant_written": bool(write_state.get("qdrant_written")),
            "neo4j_written": bool(write_state.get("neo4j_written")),
            "verified": write_state.get("verified"),
        },
    }
    if str(doc.get("ingest_stage") or "") in TERMINAL_SKIP_INGEST_STAGES:
        return "skipped", "duplicate_document", metadata
    executor_error = (executor_errors_by_kind or {}).get(kind)
    if kind == "chunk_document":
        if child_chunks > 0:
            return "succeeded", "chunks_present", metadata
        if not _has_source_pointer(doc):
            return "blocked_no_source", "source_pointer_missing", metadata
        if source_error:
            metadata["last_error"] = source_error
            return "failed", "source_runner_error", metadata
        if source_requested:
            return "queued", "source_parse_requested", metadata
        return "queued", "missing_chunks", metadata
    if kind == "persist_document":
        if child_chunks <= 0:
            return "blocked_missing_chunks", "missing_chunks", metadata
        if write_state.get("mongo_written") is True:
            return "succeeded", "mongo_write_complete", metadata
        if executor_error:
            metadata["last_error"] = executor_error
            return "failed", "executor_error", metadata
        return "queued", "chunks_not_marked_mongo_written", metadata
    if kind == "embed_document":
        if child_chunks <= 0:
            return "blocked_missing_chunks", "missing_chunks", metadata
        if write_state.get("mongo_written") is not True:
            return "blocked_mongo_state", "mongo_write_not_complete", metadata
        if write_state.get("qdrant_written") is True:
            return "succeeded", "qdrant_write_complete", metadata
        if executor_error:
            metadata["last_error"] = executor_error
            return "failed", "executor_error", metadata
        return "queued", "missing_qdrant_vectors", metadata
    return "failed", "unknown_document_pipeline_job_kind", metadata


async def _reconcile_document_pipeline_jobs(
    db: Any,
    *,
    corpus_id: str,
    jobs: list[dict[str, Any]],
    source_requested: bool = False,
    source_error: str | None = None,
    executor_errors_by_kind: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    counts: dict[str, int] = {}
    previews: list[dict[str, Any]] = []
    ops = []
    for job in jobs:
        status, reason, metadata = await _job_status_from_artifacts(
            db,
            corpus_id=corpus_id,
            job=job,
            source_requested=source_requested and job.get("kind") == "chunk_document",
            source_error=source_error if job.get("kind") == "chunk_document" else None,
            executor_errors_by_kind=executor_errors_by_kind,
        )
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
        elif status == "queued":
            update["completed_at"] = None
        ops.append(UpdateOne({"job_id": job.get("job_id")}, {"$set": update}, upsert=False))
        previews.append(
            {
                "job_id": job.get("job_id"),
                "kind": job.get("kind"),
                "doc_id": job.get("doc_id"),
                "status": status,
                "reason": reason,
            }
        )
    if ops:
        await db["document_pipeline_jobs"].bulk_write(ops, ordered=False)
    return {"counts": counts, "jobs": previews[:50]}


async def plan_document_pipeline_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 500,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 500), 10000))
    kinds_set = set(kinds or ["chunk_document", "persist_document", "embed_document"])
    artifact_reconciled = (
        await reconcile_satisfied_document_pipeline_jobs(
            db,
            corpus_id=corpus_id,
            limit=max(limit, 5000),
        )
        if apply
        else 0
    )
    query = with_active_records({"corpus_id": corpus_id})
    if user_id:
        query["user_id"] = user_id
    rows = await db["documents"].find(
        query,
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "filename": 1,
            "ingest_stage": 1,
            "write_state": 1,
            "ingestion_config": 1,
            "embedding_model_id": 1,
            "source_identity": 1,
            "source_key": 1,
            "content_sha256": 1,
            "source_path": 1,
            "backend_path": 1,
            "file_path": 1,
            "original_path": 1,
            "updated_at": 1,
            "chunk_count": 1,
        },
    ).limit(limit).to_list(length=limit)

    jobs: list[dict[str, Any]] = []
    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            continue
        child_chunks = await _count(
            db,
            "chunks",
            with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
        )
        parent_chunks = await _count(
            db,
            "parent_chunks",
            with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
        )
        for job in classify_document_pipeline_jobs(
            doc=row,
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
        ):
            if job["kind"] in kinds_set:
                jobs.append(job)
        if len(jobs) >= limit:
            jobs = jobs[:limit]
            break

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
        "artifact_reconciled": artifact_reconciled,
    }
    if not apply or not jobs:
        return result

    now = datetime.utcnow()
    ops = [
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
        for job in jobs
    ]
    await bulk_upsert_durable_jobs(db["document_pipeline_jobs"], ops)
    result["superseded"] = await retire_superseded_jobs(
        db,
        collection_name="document_pipeline_jobs",
        jobs=jobs,
        identity_fields=("corpus_id", "doc_id", "kind"),
        supersedable_statuses=SUPERSEDABLE_STATUSES,
        now=now,
    )
    return result


async def run_document_pipeline_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 25,
    statuses: list[str] | None = None,
    kinds: list[str] | None = None,
    source_runner: SourceRunner | None = None,
    persist_runner: DocumentStageRunner | None = None,
    embed_runner: DocumentStageRunner | None = None,
) -> dict[str, Any]:
    """Run/reconcile a bounded slice of document-stage jobs.

    The old batch worker still owns source parsing/chunking. Safe local repair
    stages can execute from already-materialized artifacts: persist_document
    marks split Mongo artifacts as durable, and embed_document writes Qdrant
    vectors from Mongo chunks/summaries. Every selected job is reconciled from
    artifact truth after runner callbacks return.
    """

    limit = max(1, min(int(limit or 25), 500))
    now = datetime.utcnow()
    reclaimed = await reclaim_expired_running_jobs(
        db,
        collection_name="document_pipeline_jobs",
        corpus_id=corpus_id,
        user_id=user_id,
        now=now,
    )
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "status": {"$in": statuses or list(RUNNABLE_STATUSES)},
    }
    if kinds:
        query["kind"] = {"$in": kinds}
    jobs = await db["document_pipeline_jobs"].find(
        query,
        {"_id": 0},
    ).sort("updated_at", 1).limit(limit).to_list(length=limit)
    if not jobs:
        return {
            "status": "empty",
            "corpus_id": corpus_id,
            "claimed": 0,
            "reclaimed": reclaimed,
            "source_requested": False,
            "source_result": None,
            "executor_missing_kinds": [],
            "runner_results": {},
            "counts": {},
            "jobs": [],
        }

    candidate_count = len(jobs)
    jobs = await claim_runnable_jobs(
        db,
        collection_name="document_pipeline_jobs",
        jobs=jobs,
        runnable_statuses=statuses or list(RUNNABLE_STATUSES),
        now=now,
        runner="document_pipeline_jobs.run",
        increment_attempt=True,
    )
    if not jobs:
        return {
            "status": "empty",
            "corpus_id": corpus_id,
            "user_id": user_id,
            "claimed": 0,
            "candidates": candidate_count,
            "reclaimed": reclaimed,
            "source_requested": False,
            "source_result": None,
            "executor_missing_kinds": [],
            "runner_results": {},
            "counts": {},
            "jobs": [],
        }

    source_jobs = [job for job in jobs if job.get("kind") == "chunk_document"]
    persist_jobs = [job for job in jobs if job.get("kind") == "persist_document"]
    embed_jobs = [job for job in jobs if job.get("kind") == "embed_document"]
    source_result: dict[str, Any] | None = None
    source_requested = False
    source_error: str | None = None
    runner_results: dict[str, Any] = {}
    executor_errors_by_kind: dict[str, str] = {}
    missing_executor_kinds: set[str] = set()
    if source_jobs and source_runner is not None:
        try:
            source_result = await source_runner(limit=len(source_jobs))
            runner_results["chunk_document"] = source_result
            source_requested = bool(
                source_result.get("eligible_items")
                or source_result.get("requested")
                or source_result.get("runners_started")
            )
        except Exception as exc:  # noqa: BLE001 - reconcile preserves retry state
            source_error = str(exc)[:500]
            source_result = {"status": "failed", "error": source_error}
            runner_results["chunk_document"] = source_result

    async def _run_stage(
        *,
        kind: str,
        stage_jobs: list[dict[str, Any]],
        runner: DocumentStageRunner | None,
    ) -> None:
        if not stage_jobs:
            return
        if runner is None:
            missing_executor_kinds.add(kind)
            return
        doc_ids = sorted({str(job.get("doc_id") or "") for job in stage_jobs if job.get("doc_id")})
        try:
            result = await runner(
                doc_ids=doc_ids,
                limit=len(stage_jobs),
            )
            runner_results[kind] = result
            result_counts = result.get("counts") or {}
            if result.get("status") in {"failed", "partial"} and int(result_counts.get("failed") or 0) > 0:
                executor_errors_by_kind[kind] = str(
                    result.get("error")
                    or result.get("reason")
                    or f"{kind} runner reported failed documents"
                )[:500]
        except Exception as exc:  # noqa: BLE001 - reconcile records per-job truth
            executor_errors_by_kind[kind] = str(exc)[:500]
            runner_results[kind] = {
                "status": "failed",
                "error": executor_errors_by_kind[kind],
            }

    await _run_stage(kind="persist_document", stage_jobs=persist_jobs, runner=persist_runner)
    await _run_stage(kind="embed_document", stage_jobs=embed_jobs, runner=embed_runner)

    reconciled = await _reconcile_document_pipeline_jobs(
        db,
        corpus_id=corpus_id,
        jobs=jobs,
        source_requested=source_requested,
        source_error=source_error,
        executor_errors_by_kind=executor_errors_by_kind,
    )
    counts = reconciled["counts"]
    blocked_count = sum(
        int(counts.get(status) or 0)
        for status in ("blocked_no_source", "blocked_missing_chunks", "blocked_mongo_state")
    )
    executor_missing_kinds = sorted(missing_executor_kinds)
    executor_missing = bool(counts.get("queued") and executor_missing_kinds)
    status = "complete"
    if source_error:
        status = "partial"
    elif executor_errors_by_kind:
        status = "partial"
    elif counts.get("failed"):
        status = "partial"
    elif blocked_count:
        status = "blocked"
    elif counts.get("queued") and source_requested:
        status = "requested"
    elif executor_missing:
        status = "executor_unavailable"
    elif counts.get("queued"):
        status = "pending"

    return {
        "status": status,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "claimed": len(jobs),
        "reclaimed": reclaimed,
        "source_claimed": len(source_jobs),
        "source_requested": source_requested,
        "source_result": source_result,
        "executor_missing_kinds": executor_missing_kinds if executor_missing else [],
        "runner_results": runner_results,
        "counts": counts,
        "jobs": reconciled["jobs"],
    }


async def list_document_pipeline_jobs(
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
    rows = await db["document_pipeline_jobs"].find(
        query,
        {"_id": 0},
    ).sort("updated_at", -1).limit(max(1, min(int(limit or 100), 1000))).to_list(length=None)
    status_rows = await db["document_pipeline_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    kind_rows = await db["document_pipeline_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$kind", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    return {
        "corpus_id": corpus_id,
        "counts": {str(row["_id"]): int(row["count"]) for row in status_rows},
        "kind_counts": {str(row["_id"]): int(row["count"]) for row in kind_rows},
        "jobs": rows,
    }
