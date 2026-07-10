"""Durable source/parse job read model.

Large ingests already create a durable ``ingest_batch_items`` manifest before
heavy parsing starts. This module turns those manifest rows into an explicit
source/parse queue so corpus readiness can explain work that exists before a
document row is available.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pymongo import UpdateOne

from db.queue_integrity import bulk_upsert_durable_jobs
from services.ingestion.job_leases import (
    claim_runnable_jobs,
    reclaim_expired_running_jobs,
    retire_superseded_jobs,
)
from services.ingestion.stage_identity import source_parse_stage_identity

SOURCE_PARSE_JOBS_COLLECTION = "source_parse_jobs"
STAGE_IDENTITY_MISSING_CLAUSE: dict[str, Any] = {
    "$or": [
        {"stage_identity": {"$exists": False}},
        {"stage_identity": None},
        {"stage_identity.identity_version": {"$exists": False}},
        {"stage_identity.identity_version": None},
        {"stage_identity.identity_version": ""},
    ]
}
PENDING_STATUSES = ("queued", "running")
FAILED_STATUSES = ("failed", "failed_recoverable", "blocked_source_missing")
TERMINAL_STATUSES = ("succeeded", "skipped")
RUNNABLE_JOB_STATUSES = ("queued", "failed_recoverable")
RUNNABLE_ITEM_STATUSES = ("queued", "failed_recoverable", "staged")
SUPERSEDABLE_STATUSES = set(PENDING_STATUSES) | set(FAILED_STATUSES)
IGNORED_BATCH_STATUSES = {"cancelled"}
IGNORED_ITEM_STATUSES = {"cancelled"}
PARSED_STAGES = {
    "parsed",
    "chunked",
    "extracted",
    "indexed",
    "queryable",
    "summary_pending",
    "summarized",
    "summary_complete",
    "graph_pending",
    "graph_extracted",
    "promoted",
    "graph_promoted",
    "fully_enriched",
}
PARSED_PHASES = {
    "chunking",
    "summaries",
    "summary",
    "summary_tree",
    "ghosts",
    "mongo",
    "embedding",
    "qdrant",
    "neo4j",
    "verifying",
    "awaiting_summary",
    "complete",
    "fully_enriched",
    "queryable_with_pending_summary",
    "queryable_with_pending_graph",
    "queryable_with_pending_summary_and_graph",
}


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def source_parse_contract(batch: dict[str, Any] | None) -> dict[str, Any]:
    batch = batch or {}
    options = batch.get("options") or {}
    return {
        "source": batch.get("source"),
        "root_path": batch.get("root_path"),
        "recursive": bool(batch.get("recursive", True)),
        "extensions": batch.get("extensions") or [],
        "profile": options.get("profile"),
        "use_neo4j": options.get("use_neo4j"),
        "chunk_summarization": options.get("chunk_summarization"),
        "defer_summaries": options.get("defer_summaries"),
    }


def source_parse_contract_hash(batch: dict[str, Any] | None) -> str:
    return _stable_hash(source_parse_contract(batch))


def source_parse_job_id(
    *,
    corpus_id: str,
    batch_id: str,
    item_id: str,
    source_fingerprint: str,
    contract_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{corpus_id}:{batch_id}:{item_id}:{source_fingerprint}:{contract_hash}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"source_parse_{digest[:24]}"


def source_fingerprint(item: dict[str, Any]) -> str:
    return _stable_hash(
        {
            "source": item.get("source"),
            "source_key": item.get("source_key"),
            "source_identity": item.get("source_identity"),
            "content_sha256": item.get("content_sha256"),
            "source_file_hash": item.get("source_file_hash"),
            "source_path": item.get("source_path"),
            "stored_path": item.get("stored_path"),
            "relative_path": item.get("relative_path"),
            "filename": item.get("filename"),
            "size_bytes": item.get("size_bytes"),
            "mtime": item.get("mtime"),
        }
    )


def _source_pointer(item: dict[str, Any]) -> str:
    return str(item.get("stored_path") or item.get("source_path") or "")


def _source_exists(item: dict[str, Any]) -> bool | None:
    pointer = _source_pointer(item)
    if not pointer:
        return None
    # Browser uploads have a user-facing source_path like "foo.pdf" and a real
    # stored_path. Only filesystem-check absolute/container paths.
    if not Path(pointer).is_absolute():
        return None
    try:
        return Path(pointer).is_file()
    except OSError:
        return False


def classify_source_parse_status(item: dict[str, Any]) -> tuple[str, str]:
    raw_status = str(item.get("status") or "").strip()
    phase = str(item.get("phase") or "").strip()
    stage = str(item.get("stage") or "").strip()
    failure_stage = str(item.get("failure_stage") or "").strip()

    if raw_status in IGNORED_ITEM_STATUSES:
        return "skipped", "item_cancelled"
    if raw_status == "skipped":
        return "skipped", "duplicate_or_policy_skip"
    if item.get("doc_id") or stage in PARSED_STAGES or phase in PARSED_PHASES:
        return "succeeded", "parsed_or_document_created"
    if failure_stage == "source_missing":
        return "blocked_source_missing", "source_missing"
    if raw_status in {"failed", "failed_recoverable"}:
        return raw_status, failure_stage or phase or "source_parse_failed"
    if raw_status == "running":
        return "running", phase or "source_parse_running"
    if raw_status in {"queued", "staged"}:
        source_exists = _source_exists(item)
        if source_exists is False:
            return "blocked_source_missing", "source_missing"
        return "queued", "awaiting_source_parse"
    return "queued", "awaiting_source_parse"


def build_source_parse_job(
    *,
    item: dict[str, Any],
    batch: dict[str, Any] | None,
) -> dict[str, Any]:
    corpus_id = str(item.get("corpus_id") or "")
    batch_id = str(item.get("batch_id") or "")
    item_id = str(item.get("item_id") or "")
    contract_hash = source_parse_contract_hash(batch)
    fingerprint = source_fingerprint(item)
    status, reason = classify_source_parse_status(item)
    stage_identity = source_parse_stage_identity(
        item=item,
        batch=batch,
        source_fingerprint=fingerprint,
        source_parse_contract_hash=contract_hash,
    )
    return {
        "job_id": source_parse_job_id(
            corpus_id=corpus_id,
            batch_id=batch_id,
            item_id=item_id,
            source_fingerprint=fingerprint,
            contract_hash=contract_hash,
        ),
        "kind": "source_parse",
        "corpus_id": corpus_id,
        "batch_id": batch_id,
        "item_id": item_id,
        "user_id": str(item.get("user_id") or (batch or {}).get("user_id") or ""),
        "filename": item.get("filename"),
        "relative_path": item.get("relative_path"),
        "source_path": item.get("source_path"),
        "stored_path": item.get("stored_path"),
        "status": status,
        "reason": reason,
        "batch_status": (batch or {}).get("status"),
        "item_status": item.get("status"),
        "phase": item.get("phase"),
        "stage": item.get("stage"),
        "failure_stage": item.get("failure_stage"),
        "doc_id": item.get("doc_id"),
        "attempt_count": _int(item.get("attempts")),
        "size_bytes": _int(item.get("size_bytes")),
        "stored_bytes": _int(item.get("stored_bytes")),
        "source_fingerprint": fingerprint,
        "source_parse_contract_hash": contract_hash,
        "source_parse_contract": source_parse_contract(batch),
        "stage_identity": stage_identity,
        "error": item.get("error"),
    }


async def _active_batches(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None,
    limit: int,
) -> dict[str, dict[str, Any]]:
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if user_id:
        query["user_id"] = user_id
    if IGNORED_BATCH_STATUSES:
        query["status"] = {"$nin": sorted(IGNORED_BATCH_STATUSES)}
    rows = await db["ingest_batches"].find(
        query,
        {
            "_id": 0,
            "batch_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "source": 1,
            "root_path": 1,
            "recursive": 1,
            "extensions": 1,
            "status": 1,
            "options": 1,
            "created_at": 1,
            "updated_at": 1,
        },
    ).sort("created_at", -1).limit(max(1, limit)).to_list(length=max(1, limit))
    return {str(row.get("batch_id")): row for row in rows if row.get("batch_id")}


async def plan_source_parse_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 500), 10000))
    batches = await _active_batches(
        db,
        corpus_id=corpus_id,
        user_id=user_id,
        limit=limit,
    )
    if not batches:
        return {
            "status": "planned" if not apply else "complete",
            "apply": bool(apply),
            "corpus_id": corpus_id,
            "planned": 0,
            "counts": {},
            "jobs": [],
        }

    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "batch_id": {"$in": sorted(batches)},
        "status": {"$nin": sorted(IGNORED_ITEM_STATUSES)},
    }
    if user_id:
        query["user_id"] = user_id
    rows = await db["ingest_batch_items"].find(
        query,
        {
            "_id": 0,
            "item_id": 1,
            "batch_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "source": 1,
            "source_key": 1,
            "source_identity": 1,
            "content_sha256": 1,
            "source_file_hash": 1,
            "source_path": 1,
            "stored_path": 1,
            "relative_path": 1,
            "filename": 1,
            "size_bytes": 1,
            "stored_bytes": 1,
            "mtime": 1,
            "status": 1,
            "phase": 1,
            "stage": 1,
            "failure_stage": 1,
            "attempts": 1,
            "doc_id": 1,
            "error": 1,
            "ordinal": 1,
            "updated_at": 1,
        },
    ).sort("ordinal", 1).limit(limit).to_list(length=limit)

    jobs = [
        build_source_parse_job(item=row, batch=batches.get(str(row.get("batch_id") or "")))
        for row in rows
        if row.get("item_id")
    ]
    counts: dict[str, int] = {}
    for job in jobs:
        counts[str(job["status"])] = counts.get(str(job["status"]), 0) + 1

    result: dict[str, Any] = {
        "status": "planned" if not apply else "complete",
        "apply": bool(apply),
        "corpus_id": corpus_id,
        "planned": len(jobs),
        "counts": counts,
        "kind_counts": {"source_parse": len(jobs)} if jobs else {},
        "jobs": jobs[:50],
    }
    if not apply or not jobs:
        return result

    now = datetime.utcnow()
    jobs = await _preserve_active_source_parse_claims(db, jobs=jobs, now=now)
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
                },
            },
            upsert=True,
        )
        for job in jobs
    ]
    await bulk_upsert_durable_jobs(db[SOURCE_PARSE_JOBS_COLLECTION], ops)
    result["superseded"] = await retire_superseded_jobs(
        db,
        collection_name=SOURCE_PARSE_JOBS_COLLECTION,
        jobs=jobs,
        identity_fields=("corpus_id", "batch_id", "item_id"),
        supersedable_statuses=SUPERSEDABLE_STATUSES,
        now=now,
    )
    return result


async def backfill_source_parse_stage_identity(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 1000,
) -> dict[str, Any]:
    """Backfill stage_identity for legacy source/parse job rows.

    Source-parse jobs are the durable representation of ingest manifest rows.
    Older rows can predate stage identity, which makes an otherwise queryable
    corpus look like it still needs idempotency repair. This repair recomputes
    identity from the live manifest item and batch contract, and deliberately
    skips rows whose source item has disappeared.
    """

    limit = max(1, min(int(limit or 1000), 50000))
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        **STAGE_IDENTITY_MISSING_CLAUSE,
    }
    if user_id:
        query["user_id"] = user_id
    rows = await db[SOURCE_PARSE_JOBS_COLLECTION].find(
        query,
        {
            "_id": 1,
            "job_id": 1,
            "corpus_id": 1,
            "batch_id": 1,
            "item_id": 1,
            "user_id": 1,
            "status": 1,
        },
    ).limit(limit).to_list(length=limit)

    batch_ids = sorted({str(row.get("batch_id") or "") for row in rows if row.get("batch_id")})
    item_ids = sorted({str(row.get("item_id") or "") for row in rows if row.get("item_id")})

    batches_by_id: dict[str, dict[str, Any]] = {}
    if batch_ids:
        batch_rows = await db["ingest_batches"].find(
            {"corpus_id": corpus_id, "batch_id": {"$in": batch_ids}},
            {
                "_id": 0,
                "batch_id": 1,
                "corpus_id": 1,
                "user_id": 1,
                "source": 1,
                "root_path": 1,
                "recursive": 1,
                "extensions": 1,
                "status": 1,
                "options": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        ).to_list(length=None)
        batches_by_id = {
            str(row.get("batch_id") or ""): row
            for row in batch_rows
            if row.get("batch_id")
        }

    items_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if batch_ids and item_ids:
        item_rows = await db["ingest_batch_items"].find(
            {
                "corpus_id": corpus_id,
                "batch_id": {"$in": batch_ids},
                "item_id": {"$in": item_ids},
            },
            {
                "_id": 0,
                "item_id": 1,
                "batch_id": 1,
                "corpus_id": 1,
                "user_id": 1,
                "source": 1,
                "source_key": 1,
                "source_identity": 1,
                "content_sha256": 1,
                "source_file_hash": 1,
                "source_path": 1,
                "stored_path": 1,
                "relative_path": 1,
                "filename": 1,
                "size_bytes": 1,
                "stored_bytes": 1,
                "mtime": 1,
                "status": 1,
                "phase": 1,
                "stage": 1,
                "failure_stage": 1,
                "attempts": 1,
                "doc_id": 1,
                "error": 1,
                "ordinal": 1,
                "updated_at": 1,
            },
        ).to_list(length=None)
        items_by_key = {
            (str(row.get("batch_id") or ""), str(row.get("item_id") or "")): row
            for row in item_rows
            if row.get("batch_id") and row.get("item_id")
        }

    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    samples: list[dict[str, Any]] = []
    skipped_missing_batch = 0
    skipped_missing_item = 0
    skipped_missing_id = 0
    for row in rows:
        batch_id = str(row.get("batch_id") or "")
        item_id = str(row.get("item_id") or "")
        selector = {"_id": row["_id"]} if row.get("_id") is not None else (
            {"job_id": row.get("job_id")} if row.get("job_id") else None
        )
        if selector is None:
            skipped_missing_id += 1
            continue
        batch = batches_by_id.get(batch_id)
        if not batch:
            skipped_missing_batch += 1
            continue
        item = items_by_key.get((batch_id, item_id))
        if not item:
            skipped_missing_item += 1
            continue
        rebuilt = build_source_parse_job(item=item, batch=batch)
        update = {
            "source_fingerprint": rebuilt["source_fingerprint"],
            "source_parse_contract_hash": rebuilt["source_parse_contract_hash"],
            "source_parse_contract": rebuilt["source_parse_contract"],
            "stage_identity": rebuilt["stage_identity"],
            "stage_identity_repaired_at": now,
            "updated_at": now,
        }
        ops.append(UpdateOne(selector, {"$set": update}))
        if len(samples) < 20:
            samples.append(
                {
                    "job_id": row.get("job_id"),
                    "batch_id": batch_id,
                    "item_id": item_id,
                    "source_fingerprint": update["source_fingerprint"],
                    "source_parse_contract_hash": update["source_parse_contract_hash"],
                }
            )

    modified = 0
    if apply and ops:
        result = await db[SOURCE_PARSE_JOBS_COLLECTION].bulk_write(ops, ordered=False)
        modified = int(getattr(result, "modified_count", 0) or 0)

    return {
        "status": "planned" if not apply else "complete",
        "apply": bool(apply),
        "corpus_id": corpus_id,
        "limit": limit,
        "scanned": len(rows),
        "planned": len(ops),
        "modified": modified,
        "skipped_missing_batch": skipped_missing_batch,
        "skipped_missing_item": skipped_missing_item,
        "skipped_missing_id": skipped_missing_id,
        "samples": samples,
    }


async def _preserve_active_source_parse_claims(
    db: Any,
    *,
    jobs: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Keep live source-parse runner claims during read-model refresh.

    Source-parse rows mirror ``ingest_batch_items``. A repair tick can claim a
    source-parse job, then immediately refresh the mirror before the batch item
    has moved out of ``queued``. Without this guard the planner would erase the
    lease it just acquired, reopening the row to another runner.
    """

    job_ids = [str(job.get("job_id") or "") for job in jobs if job.get("job_id")]
    if not job_ids:
        return jobs
    try:
        existing_rows = await db[SOURCE_PARSE_JOBS_COLLECTION].find(
            {"job_id": {"$in": job_ids}},
            {
                "_id": 0,
                "job_id": 1,
                "status": 1,
                "runner": 1,
                "last_run_at": 1,
                "lease_until": 1,
                "run_requested_at": 1,
                "runner_deferred": 1,
            },
        ).to_list(length=len(job_ids))
    except Exception:
        return jobs

    existing_by_job = {
        str(row.get("job_id") or ""): row
        for row in existing_rows
        if row.get("job_id")
    }
    merged: list[dict[str, Any]] = []
    for job in jobs:
        local_job = dict(job)
        existing = existing_by_job.get(str(local_job.get("job_id") or ""))
        lease_until = (existing or {}).get("lease_until")
        if (
            local_job.get("status") == "queued"
            and (existing or {}).get("status") == "running"
            and isinstance(lease_until, datetime)
            and lease_until > now
        ):
            local_job["status"] = "running"
            local_job["reason"] = "run_requested"
            for field in (
                "runner",
                "last_run_at",
                "lease_until",
                "run_requested_at",
                "runner_deferred",
            ):
                if field in (existing or {}):
                    local_job[field] = existing[field]
        merged.append(local_job)
    return merged


async def list_source_parse_jobs(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 100,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if statuses:
        query["status"] = {"$in": statuses}
    rows = await db[SOURCE_PARSE_JOBS_COLLECTION].find(
        query,
        {"_id": 0},
    ).sort("updated_at", -1).limit(max(1, min(int(limit or 100), 1000))).to_list(length=None)
    status_rows = await db[SOURCE_PARSE_JOBS_COLLECTION].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    return {
        "corpus_id": corpus_id,
        "counts": {str(row["_id"]): int(row["count"]) for row in status_rows},
        "kind_counts": {"source_parse": sum(int(row["count"]) for row in status_rows)},
        "jobs": rows,
    }


async def run_source_parse_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str,
    ingestion_service: Any | None = None,
    limit: int = 25,
    statuses: list[str] | None = None,
    start_runners: bool = False,
) -> dict[str, Any]:
    """Resume eligible source/parse work through the durable batch runner.

    This intentionally does not parse files directly. The batch runner owns
    leases, attempt caps, memory admission, profile pass plans, and lifecycle
    holds. This function is the queue control-plane bridge.
    """

    from services.ingestion import batches

    limit = max(1, min(int(limit or 25), 500))
    now = datetime.utcnow()
    reclaimed = await reclaim_expired_running_jobs(
        db,
        collection_name=SOURCE_PARSE_JOBS_COLLECTION,
        corpus_id=corpus_id,
        user_id=user_id,
        now=now,
    )
    runnable_statuses = list(statuses or RUNNABLE_JOB_STATUSES)
    job_rows = await db[SOURCE_PARSE_JOBS_COLLECTION].find(
        {
            "corpus_id": corpus_id,
            "user_id": user_id,
            "status": {"$in": runnable_statuses},
        },
        {
            "_id": 0,
            "job_id": 1,
            "batch_id": 1,
            "item_id": 1,
            "status": 1,
        },
    ).sort("updated_at", 1).limit(limit).to_list(length=limit)
    if not job_rows:
        return {
            "status": "empty",
            "corpus_id": corpus_id,
            "requested": 0,
            "claimed": 0,
            "reclaimed": reclaimed,
            "eligible_items": 0,
            "batch_count": 0,
            "runners_started": 0,
            "runner_deferred": not start_runners,
            "counts": {},
        }

    can_claim_for_runner = bool(start_runners and ingestion_service is not None)
    candidate_count = len(job_rows)
    if can_claim_for_runner:
        job_rows = await claim_runnable_jobs(
            db,
            collection_name=SOURCE_PARSE_JOBS_COLLECTION,
            jobs=job_rows,
            runnable_statuses=runnable_statuses,
            now=now,
            runner="source_parse_jobs.run",
            increment_attempt=False,
            set_fields={
                "run_requested_at": now,
                "runner_deferred": False,
            },
        )
        if not job_rows:
            return {
                "status": "empty",
                "corpus_id": corpus_id,
                "requested": 0,
                "candidates": candidate_count,
                "claimed": 0,
                "reclaimed": reclaimed,
                "eligible_items": 0,
                "batch_count": 0,
                "runners_started": 0,
                "runner_deferred": False,
                "counts": {},
            }
    else:
        await db[SOURCE_PARSE_JOBS_COLLECTION].update_many(
            {
                "job_id": {"$in": [row["job_id"] for row in job_rows if row.get("job_id")]},
                "status": {"$in": runnable_statuses},
            },
            {
                "$set": {
                    "run_requested_at": now,
                    "runner_deferred": not start_runners,
                    "updated_at": now,
                }
            },
        )

    item_ids = [str(row.get("item_id") or "") for row in job_rows if row.get("item_id")]
    item_rows = await db["ingest_batch_items"].find(
        {
            "corpus_id": corpus_id,
            "user_id": user_id,
            "item_id": {"$in": item_ids},
            "status": {"$in": list(RUNNABLE_ITEM_STATUSES)},
        },
        {"_id": 0, "batch_id": 1, "item_id": 1, "status": 1},
    ).to_list(length=len(item_ids))
    batch_ids = sorted(
        {str(row.get("batch_id")) for row in item_rows if row.get("batch_id")}
    )

    runners_started = 0
    batch_results: list[dict[str, Any]] = []
    for batch_id in batch_ids:
        await batches.reconcile_stale_items(db, batch_id=batch_id, user_id=user_id)
        refreshed = await batches.refresh_batch_counts(db, batch_id, user_id=user_id)
        started = False
        if start_runners and ingestion_service is not None:
            started = batches.start_local_batch_runner(
                db=db,
                ingestion_service=ingestion_service,
                batch_id=batch_id,
                user_id=user_id,
            )
            if started:
                runners_started += 1
        batch_results.append(
            {
                "batch_id": batch_id,
                "status": refreshed.get("status"),
                "runner_started": started,
            }
        )

    refreshed_plan = await plan_source_parse_jobs(
        db,
        corpus_id=corpus_id,
        user_id=user_id,
        apply=True,
        limit=limit,
    )
    status = "empty"
    if batch_ids:
        status = "started" if runners_started else "requested" if start_runners else "deferred"
    return {
        "status": status,
        "corpus_id": corpus_id,
        "requested": len(job_rows),
        "claimed": len(job_rows) if can_claim_for_runner else 0,
        "reclaimed": reclaimed,
        "eligible_items": len(item_rows),
        "batch_count": len(batch_ids),
        "runners_started": runners_started,
        "runner_deferred": not start_runners,
        "batches": batch_results,
        "counts": refreshed_plan.get("counts") or {},
    }
