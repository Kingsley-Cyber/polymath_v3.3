"""Lease and exhaustion helpers for durable ingestion repair queues.

Job leases prevent duplicate execution of one queue row.  Lane leases sit one
level above that and prevent two controllers (auto-repair, a manual repair, or
a batch worker) from driving the same corpus/lane concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument, UpdateMany
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

DEFAULT_JOB_LEASE_SECONDS = 15 * 60
DEFAULT_JOB_MAX_ATTEMPTS = 5
DEFAULT_LANE_LEASE_SECONDS = 30 * 60
SUPERSEDED_JOB_STATUS = "superseded"
DEAD_LETTER_JOB_STATUS = "dead_letter"
LANE_LEASE_COLLECTION = "ingest_lane_leases"

# Corpora whose lane-lease heartbeat failed to renew IN THIS PROCESS. A runner
# that keeps executing after losing its lane lease is invisible to every other
# controller — the exact double-writer topology the leases exist to prevent.
# Workers consult lanes_lost() before claiming new items and stand down.
_LOST_LANES: set[str] = set()


def lanes_lost(corpus_id: str) -> bool:
    """True when this process lost a lane lease for ``corpus_id``."""

    return str(corpus_id) in _LOST_LANES


def normalize_failure_class(value: Any) -> str:
    """Return a stable, non-secret failure class for queue telemetry."""

    text = str(value or "").strip()
    if not text:
        return "attempt_limit_exhausted"
    first = text.split(":", 1)[0].strip()
    normalized = re.sub(r"[^a-z0-9]+", "_", first.lower()).strip("_")
    return normalized[:80] or "attempt_limit_exhausted"


async def acquire_lane_lease(
    db: Any,
    *,
    corpus_id: str,
    lane: str,
    owner: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LANE_LEASE_SECONDS,
) -> dict[str, Any] | None:
    """Atomically acquire one corpus/lane lease, reclaiming expired leases."""

    now = now or datetime.utcnow()
    lease_id = uuid4().hex
    key = f"{corpus_id}:{lane}"
    deadline = lease_deadline(now, lease_seconds=lease_seconds)
    compatibility_lease = {
        "_id": key,
        "corpus_id": corpus_id,
        "lane": lane,
        "owner": owner,
        "lease_id": lease_id,
        "lease_until": deadline,
    }
    try:
        collection = db[LANE_LEASE_COLLECTION]
        if not hasattr(collection, "find_one_and_update"):
            return compatibility_lease
        row = await collection.find_one_and_update(
            {
                "_id": key,
                "$or": [
                    {"lease_until": {"$lte": now}},
                    {"lease_until": {"$exists": False}},
                    {"owner": owner},
                ],
            },
            {
                "$set": {
                    "corpus_id": corpus_id,
                    "lane": lane,
                    "owner": owner,
                    "lease_id": lease_id,
                    "lease_until": deadline,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None
    except (AssertionError, KeyError, AttributeError, TypeError):
        return compatibility_lease
    except Exception:
        return None
    return row if row and row.get("lease_id") == lease_id else None


async def release_lane_lease(
    db: Any,
    *,
    corpus_id: str,
    lane: str,
    lease_id: str,
    now: datetime | None = None,
) -> bool:
    """Release only the lease instance owned by this caller."""

    try:
        collection = db[LANE_LEASE_COLLECTION]
        if not hasattr(collection, "delete_one"):
            return True
        result = await collection.delete_one(
            {
                "_id": f"{corpus_id}:{lane}",
                "lease_id": lease_id,
            }
        )
        return int(getattr(result, "deleted_count", 0) or 0) > 0
    except (AssertionError, KeyError, AttributeError, TypeError):
        return True
    except Exception:
        # Expiry is the final safety net if a process disappears mid-lane.
        return False


async def renew_lane_lease(
    db: Any,
    *,
    corpus_id: str,
    lane: str,
    lease_id: str,
    lease_seconds: int = DEFAULT_LANE_LEASE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Extend only the current lease instance."""
    now = now or datetime.utcnow()
    try:
        result = await db[LANE_LEASE_COLLECTION].update_one(
            {"_id": f"{corpus_id}:{lane}", "lease_id": lease_id},
            {
                "$set": {
                    "lease_until": now
                    + timedelta(seconds=max(60, int(lease_seconds or 0))),
                    "heartbeat_at": now,
                    "updated_at": now,
                }
            },
        )
        return int(getattr(result, "modified_count", 0) or 0) > 0
    except (AssertionError, KeyError, AttributeError, TypeError):
        return True
    except Exception:
        return False


@asynccontextmanager
async def corpus_lane_lease(
    db: Any,
    *,
    corpus_id: str,
    lane: str,
    owner: str,
    lease_seconds: int = DEFAULT_LANE_LEASE_SECONDS,
):
    """Yield the lease row, or ``None`` when another controller owns it."""

    lease = await acquire_lane_lease(
        db,
        corpus_id=corpus_id,
        lane=lane,
        owner=owner,
        lease_seconds=lease_seconds,
    )
    heartbeat_task: asyncio.Task | None = None
    if lease:
        # A fresh acquire means this process legitimately owns the lane
        # again — clear any stale loss marker from a previous run.
        _LOST_LANES.discard(str(corpus_id))

        async def _heartbeat() -> None:
            interval = max(20.0, float(lease_seconds) / 3.0)
            while True:
                await asyncio.sleep(interval)
                renewed = await renew_lane_lease(
                    db,
                    corpus_id=corpus_id,
                    lane=lane,
                    lease_id=str(lease.get("lease_id") or ""),
                    lease_seconds=lease_seconds,
                )
                if not renewed:
                    # The lease is gone (expired + reclaimed, or deleted).
                    # Returning silently here used to leave the runner
                    # executing with no lease at all — flag the corpus so
                    # batch workers stop claiming new items.
                    _LOST_LANES.add(str(corpus_id))
                    logger.critical(
                        "lane lease renewal FAILED corpus=%s lane=%s — "
                        "runner is now leaseless; workers will stand down",
                        corpus_id,
                        lane,
                    )
                    return

        heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        yield lease
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        if lease:
            await release_lane_lease(
                db,
                corpus_id=corpus_id,
                lane=lane,
                lease_id=str(lease.get("lease_id") or ""),
            )


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
    max_attempts: int | None = None,
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

    if max_attempts is None:
        try:
            from config import get_settings

            max_attempts = int(
                getattr(get_settings(), "INGEST_JOB_MAX_ATTEMPTS", DEFAULT_JOB_MAX_ATTEMPTS)
                or DEFAULT_JOB_MAX_ATTEMPTS
            )
        except Exception:
            max_attempts = DEFAULT_JOB_MAX_ATTEMPTS
    deadline = lease_deadline(now, lease_seconds=lease_seconds)
    claimed: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        try:
            attempts = int(job.get("attempt_count") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if increment_attempt and attempts >= max(1, int(max_attempts or 0)):
            last_error = job.get("last_error") or job.get("error") or job.get("reason")
            try:
                await db[collection_name].update_one(
                    {
                        "job_id": job_id,
                        "status": {"$in": statuses},
                    },
                    {
                        "$set": {
                            "status": DEAD_LETTER_JOB_STATUS,
                            "reason": "attempt_limit_exhausted",
                            "failure_class": normalize_failure_class(last_error),
                            "last_actionable_error": str(last_error or "")[:500],
                            "dead_lettered_at": now,
                            "updated_at": now,
                            "lease_until": None,
                        },
                        "$unset": {"runner": "", "started_at": ""},
                    },
                    upsert=False,
                )
            except Exception:
                pass
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
