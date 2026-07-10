"""Lease helpers for durable ingestion repair queues."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pymongo import UpdateMany

DEFAULT_JOB_LEASE_SECONDS = 15 * 60
SUPERSEDED_JOB_STATUS = "superseded"


def lease_deadline(
    now: datetime | None = None,
    *,
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
) -> datetime:
    return (now or datetime.utcnow()) + timedelta(seconds=max(60, int(lease_seconds or 0)))


async def reclaim_expired_running_jobs(
    db: Any,
    *,
    collection_name: str,
    corpus_id: str,
    user_id: str | None = None,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
) -> int:
    """Move expired ``running`` queue rows back to ``queued``.

    Older queue rows may not have ``lease_until``. For those, ``updated_at`` is
    treated as the stale marker so a worker restart cannot strand them forever.
    This helper is intentionally best-effort; fakes and old deployments that do
    not implement ``update_many`` should not block the repair runner.
    """

    now = now or datetime.utcnow()
    stale_before = now - timedelta(seconds=max(60, int(lease_seconds or 0)))
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "status": "running",
        "$or": [
            {"lease_until": {"$lte": now}},
            {"lease_until": {"$exists": False}, "updated_at": {"$lte": stale_before}},
            {"lease_until": None, "updated_at": {"$lte": stale_before}},
        ],
    }
    if user_id:
        query["user_id"] = user_id
    try:
        result = await db[collection_name].update_many(
            query,
            {
                "$set": {
                    "status": "queued",
                    "reason": "lease_expired",
                    "updated_at": now,
                    "last_reclaimed_at": now,
                },
                "$unset": {
                    "runner": "",
                    "started_at": "",
                    "lease_until": "",
                },
            },
        )
        return int(getattr(result, "modified_count", 0) or 0)
    except Exception:
        return 0


async def claim_runnable_jobs(
    db: Any,
    *,
    collection_name: str,
    jobs: list[dict[str, Any]],
    runnable_statuses: set[str] | tuple[str, ...] | list[str],
    now: datetime | None = None,
    runner: str,
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
    increment_attempt: bool = False,
    set_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Atomically claim queued/retryable queue rows.

    Planners commonly select candidate jobs with a read, then runners execute a
    bounded slice. In production two repair ticks or a manual button can overlap
    that read window. This helper flips each row to ``running`` only when Mongo
    confirms it is still in a runnable status, then returns only the rows this
    caller actually owns.
    """

    now = now or datetime.utcnow()
    statuses = sorted({str(status) for status in runnable_statuses if str(status)})
    if not jobs or not statuses:
        return []

    deadline = lease_deadline(now, lease_seconds=lease_seconds)
    claimed: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        update: dict[str, Any] = {
            "$set": {
                "status": "running",
                "runner": runner,
                "last_run_at": now,
                "lease_until": deadline,
                "updated_at": now,
                **(set_fields or {}),
            }
        }
        if increment_attempt:
            update["$inc"] = {"attempt_count": 1}
        try:
            result = await db[collection_name].update_one(
                {
                    "job_id": job_id,
                    "status": {"$in": statuses},
                },
                update,
                upsert=False,
            )
        except Exception:
            continue
        if int(getattr(result, "modified_count", 0) or 0) <= 0:
            continue
        local_job = dict(job)
        local_job.update(update["$set"])
        if increment_attempt:
            try:
                local_job["attempt_count"] = int(local_job.get("attempt_count") or 0) + 1
            except (TypeError, ValueError):
                local_job["attempt_count"] = 1
        claimed.append(local_job)
    return claimed


async def retire_superseded_jobs(
    db: Any,
    *,
    collection_name: str,
    jobs: list[dict[str, Any]],
    identity_fields: tuple[str, ...],
    supersedable_statuses: set[str] | tuple[str, ...] | list[str],
    now: datetime | None = None,
    reason: str = "stage_identity_superseded",
) -> int:
    """Mark older queue rows for the same artifact identity as superseded.

    Job ids intentionally include contract/content hashes. When a chunk,
    document, or provider contract changes, planners create a new job id for
    the same target. The old row is useful history, but it must not keep
    readiness stuck in queued/failed state.
    """

    if not jobs or not identity_fields:
        return 0
    now = now or datetime.utcnow()
    statuses = sorted({str(status) for status in supersedable_statuses if str(status)})
    if not statuses:
        return 0

    ops: list[UpdateMany] = []
    seen: set[tuple[Any, ...]] = set()
    for job in jobs:
        job_id = job.get("job_id")
        if not job_id:
            continue
        identity: dict[str, Any] = {}
        identity_key: list[Any] = [collection_name]
        missing_identity = False
        for field in identity_fields:
            value = job.get(field)
            if value is None or value == "":
                missing_identity = True
                break
            identity[field] = value
            identity_key.append(value)
        if missing_identity:
            continue
        identity_key.append(job_id)
        key_tuple = tuple(identity_key)
        if key_tuple in seen:
            continue
        seen.add(key_tuple)
        ops.append(
            UpdateMany(
                {
                    **identity,
                    "job_id": {"$ne": job_id},
                    "status": {"$in": statuses},
                },
                {
                    "$set": {
                        "status": SUPERSEDED_JOB_STATUS,
                        "reason": reason,
                        "superseded_by_job_id": job_id,
                        "superseded_at": now,
                        "updated_at": now,
                        "lease_until": None,
                    },
                    "$unset": {
                        "runner": "",
                        "started_at": "",
                    },
                },
            )
        )

    if not ops:
        return 0
    try:
        result = await db[collection_name].bulk_write(ops, ordered=False)
        return int(getattr(result, "modified_count", 0) or 0)
    except Exception:
        return 0
