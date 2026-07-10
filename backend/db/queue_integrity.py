"""Integrity migration for durable ingestion job queues.

Every queue planner uses deterministic ``job_id`` values and upserts by that
identity.  Claim and reconciliation paths therefore require exactly one row
per job id.  Older deployments created non-unique indexes, allowing concurrent
planners to insert duplicates and leaving mixed states such as one succeeded
row plus one expired running row for the same summary.

This module is deliberately independent of the ingestion services so it can
run during database startup before any durable runner starts.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pymongo import UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure

logger = logging.getLogger(__name__)


DURABLE_JOB_COLLECTIONS: tuple[str, ...] = (
    "source_parse_jobs",
    "document_pipeline_jobs",
    "extraction_jobs",
    "summary_jobs",
    "graph_promotion_jobs",
)

_SUCCESS_PRIORITY: dict[str, dict[str, int]] = {
    "source_parse_jobs": {"succeeded": 120, "skipped": 110},
    "document_pipeline_jobs": {"succeeded": 120, "skipped": 110},
    "extraction_jobs": {"promoted": 130, "succeeded": 120, "skipped": 110},
    "summary_jobs": {"succeeded": 120, "skipped": 110},
    "graph_promotion_jobs": {"done": 130, "noop": 120, "partial": 110},
}

_GENERIC_STATUS_PRIORITY: dict[str, int] = {
    "running": 90,
    "queued": 80,
    "failed_recoverable": 75,
    "provider_failed": 70,
    "validation_failed": 70,
    "failed": 60,
    "blocked_provider_contract": 50,
    "blocked_no_source": 50,
    "blocked_missing_chunks": 50,
    "blocked_mongo_state": 50,
    "blocked_no_parent_summaries": 50,
    "blocked_parent_summaries_incomplete": 50,
    "blocked_failed_chunks": 50,
    "blocked_no_extractions": 50,
    "superseded": 10,
}


def _date_rank(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def _row_rank(collection_name: str, row: dict[str, Any], *, now: datetime) -> tuple:
    status = str(row.get("status") or "")
    priority = _SUCCESS_PRIORITY.get(collection_name, {}).get(
        status,
        _GENERIC_STATUS_PRIORITY.get(status, 0),
    )
    lease_until = row.get("lease_until")
    expired_running = (
        status == "running"
        and isinstance(lease_until, datetime)
        and lease_until <= now
    )
    if expired_running:
        priority = _GENERIC_STATUS_PRIORITY["queued"]
    return (
        priority,
        int(row.get("attempt_count") or 0),
        _date_rank(row.get("updated_at")),
        _date_rank(row.get("created_at")),
        str(row.get("_id") or ""),
    )


def select_canonical_job_row(
    collection_name: str,
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Select the row that preserves the strongest durable job truth."""

    if not rows:
        raise ValueError("rows must not be empty")
    now = now or datetime.utcnow()
    return max(rows, key=lambda row: _row_rank(collection_name, row, now=now))


def _dedup_update(
    collection_name: str,
    winner: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    status = str(winner.get("status") or "")
    set_fields: dict[str, Any] = {
        "attempt_count": max(int(row.get("attempt_count") or 0) for row in rows),
        "queue_integrity_checked_at": now,
        "duplicate_rows_removed": len(rows) - 1,
    }
    created_values = [row.get("created_at") for row in rows if isinstance(row.get("created_at"), datetime)]
    if created_values:
        set_fields["created_at"] = min(created_values)

    lease_until = winner.get("lease_until")
    expired_running = (
        status == "running"
        and isinstance(lease_until, datetime)
        and lease_until <= now
    )
    unset_fields: dict[str, str] = {}
    if expired_running:
        set_fields.update(
            {
                "status": "queued",
                "reason": "lease_expired_during_queue_dedup",
                "last_reclaimed_at": now,
            }
        )
        unset_fields.update({"runner": "", "started_at": "", "lease_until": ""})
    elif status in _SUCCESS_PRIORITY.get(collection_name, {}):
        set_fields["lease_until"] = None
        unset_fields.update({"runner": "", "started_at": ""})

    update: dict[str, dict[str, Any]] = {"$set": set_fields}
    if unset_fields:
        update["$unset"] = unset_fields
    return update


async def deduplicate_job_ids(
    collection: Any,
    *,
    collection_name: str | None = None,
    group_limit: int = 5000,
    now: datetime | None = None,
) -> dict[str, int]:
    """Remove duplicate deterministic job rows without losing success truth."""

    name = collection_name or str(getattr(collection, "name", ""))
    now = now or datetime.utcnow()
    groups = await collection.aggregate(
        [
            {"$match": {"job_id": {"$type": "string", "$ne": ""}}},
            {
                "$group": {
                    "_id": "$job_id",
                    "ids": {"$push": "$_id"},
                    "count": {"$sum": 1},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
            {"$sort": {"_id": 1}},
            {"$limit": max(1, int(group_limit or 1))},
        ]
    ).to_list(length=max(1, int(group_limit or 1)))

    removed = 0
    reconciled = 0
    for group in groups:
        ids = list(group.get("ids") or [])
        rows = await collection.find({"_id": {"$in": ids}}).to_list(length=len(ids))
        if len(rows) <= 1:
            continue
        winner = select_canonical_job_row(name, rows, now=now)
        winner_id = winner.get("_id")
        loser_ids = [row.get("_id") for row in rows if row.get("_id") != winner_id]
        await collection.update_one(
            {"_id": winner_id},
            _dedup_update(name, winner, rows, now=now),
        )
        if loser_ids:
            result = await collection.delete_many({"_id": {"$in": loser_ids}})
            removed += int(getattr(result, "deleted_count", 0) or 0)
        reconciled += 1

    return {
        "duplicate_groups": len(groups),
        "reconciled_groups": reconciled,
        "removed_rows": removed,
        "limit_reached": int(len(groups) >= max(1, int(group_limit or 1))),
    }


async def ensure_unique_job_id_index(
    collection: Any,
    *,
    collection_name: str | None = None,
) -> dict[str, Any]:
    """Deduplicate one queue and enforce its deterministic identity."""

    name = collection_name or str(getattr(collection, "name", ""))
    index_name = f"{name}_job_id"
    totals = {"duplicate_groups": 0, "reconciled_groups": 0, "removed_rows": 0}

    for attempt in range(4):
        report = await deduplicate_job_ids(collection, collection_name=name)
        for key in totals:
            totals[key] += int(report.get(key) or 0)

        try:
            indexes = await collection.index_information()
            current = indexes.get(index_name)
            if current and not bool(current.get("unique")):
                try:
                    await collection.drop_index(index_name)
                except Exception:
                    pass
            await collection.create_index(
                "job_id",
                name=index_name,
                unique=True,
                sparse=True,
            )
            return {**totals, "status": "ready", "index": index_name}
        except (DuplicateKeyError, OperationFailure) as exc:
            duplicate_error = isinstance(exc, DuplicateKeyError) or getattr(exc, "code", None) == 11000
            if not duplicate_error or attempt == 3:
                raise
            logger.warning(
                "Queue identity raced for %s; retrying dedup/index (%d/4)",
                name,
                attempt + 1,
            )

    raise RuntimeError(f"Could not enforce unique job_id for {name}")


async def ensure_durable_job_queue_integrity(db: Any) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for name in DURABLE_JOB_COLLECTIONS:
        reports[name] = await ensure_unique_job_id_index(
            db[name],
            collection_name=name,
        )
    removed = sum(int(report.get("removed_rows") or 0) for report in reports.values())
    if removed:
        logger.warning("Durable queue integrity removed %d duplicate rows: %s", removed, reports)
    else:
        logger.info("Durable queue identities verified unique")
    return {"status": "ready", "removed_rows": removed, "collections": reports}


async def bulk_upsert_durable_jobs(collection: Any, ops: list[Any]) -> Any:
    """Run deterministic job upserts safely across concurrent planners.

    Once job_id is unique, two planners can race between match and insert. The
    loser receives E11000 even though the desired row now exists. Retry the
    same operations as updates only; non-duplicate write failures still raise.
    """

    if not ops:
        return None
    try:
        return await collection.bulk_write(ops, ordered=False)
    except BulkWriteError as exc:
        write_errors = list((exc.details or {}).get("writeErrors") or [])
        if not write_errors or any(int(error.get("code") or 0) != 11000 for error in write_errors):
            raise
        retry_ops = [
            UpdateOne(op._filter, op._doc, upsert=False)  # noqa: SLF001 - pymongo op replay
            for op in ops
        ]
        logger.info(
            "Concurrent durable queue upsert raced for %s; replaying %d updates",
            getattr(collection, "name", "queue"),
            len(retry_ops),
        )
        return await collection.bulk_write(retry_ops, ordered=False)
