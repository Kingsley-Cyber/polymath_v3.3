"""Cheap, readiness-driven scheduling state for corpus repair.

The auto-repair loop runs frequently, but expensive planning/reconciliation
should run only when durable queue truth changes or an idle backoff expires.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from services.storage.record_status import with_active_records

SCHEDULER_STATE_COLLECTION = "ingest_scheduler_state"
ACTIVE_JOB_STATUSES = ("queued", "running")
FAILED_JOB_STATUSES = (
    "failed",
    "failed_recoverable",
    "provider_failed",
    "validation_failed",
    "blocked_provider_contract",
    "blocked_no_source",
    "blocked_missing_chunks",
    "blocked_mongo_state",
    "blocked_empty_source",
    "blocked_no_parent_summaries",
    "blocked_parent_summaries_incomplete",
    "blocked_source_missing",
)


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _status_counts(db: Any, collection: str, corpus_id: str) -> dict[str, int]:
    rows = await db[collection].aggregate(
        [
            {
                "$match": {
                    "corpus_id": corpus_id,
                    "status": {
                        "$in": [
                            *ACTIVE_JOB_STATUSES,
                            *FAILED_JOB_STATUSES,
                            "dead_letter",
                        ]
                    },
                }
            },
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
    ).to_list(length=None)
    return {str(row.get("_id")): int(row.get("count") or 0) for row in rows}


async def quick_repair_gap_snapshot(db: Any, corpus_id: str) -> dict[str, Any]:
    """Read bounded queue counters without materializing full readiness."""

    collections = (
        "source_parse_jobs",
        "document_pipeline_jobs",
        "extraction_jobs",
        "summary_jobs",
        "graph_promotion_jobs",
    )
    try:
        counts = await asyncio.gather(
            *[_status_counts(db, collection, corpus_id) for collection in collections]
        )
    except Exception:
        # A scheduler read failure must fail open: run the bounded repair cycle
        # rather than incorrectly declaring a corpus healthy. This path also
        # keeps lightweight unit-test database doubles compatible.
        material = {
            "queues": {},
            "ghost_failure_docs": 0,
            "active_total": 1,
            "failed_total": 0,
            "dead_letter_total": 0,
            "snapshot_unavailable": True,
        }
        return {
            **material,
            "actionable_total": 1,
            "fingerprint": _fingerprint(material),
        }
    queues = dict(zip(collections, counts, strict=True))
    ghost_failure_docs = int(
        await db["documents"].count_documents(
            with_active_records(
                {"corpus_id": corpus_id, "ghost_b_failure_count": {"$gt": 0}}
            )
        )
    )
    active_total = sum(
        int(statuses.get(status) or 0)
        for statuses in queues.values()
        for status in ACTIVE_JOB_STATUSES
    )
    failed_total = sum(
        int(statuses.get(status) or 0)
        for statuses in queues.values()
        for status in FAILED_JOB_STATUSES
    )
    dead_letter_total = sum(
        int(statuses.get("dead_letter") or 0) for statuses in queues.values()
    )
    material = {
        "queues": queues,
        "ghost_failure_docs": ghost_failure_docs,
        "active_total": active_total,
        "failed_total": failed_total,
        "dead_letter_total": dead_letter_total,
    }
    return {
        **material,
        "actionable_total": active_total + failed_total + ghost_failure_docs,
        "fingerprint": _fingerprint(material),
    }


def backoff_decision(
    *,
    snapshot: dict[str, Any],
    state: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """Pure scheduling decision used by production and unit tests."""

    state = state or {}
    changed = str(state.get("gap_fingerprint") or "") != str(
        snapshot.get("fingerprint") or ""
    )
    actionable = int(snapshot.get("actionable_total") or 0)
    next_eligible = state.get("next_eligible_at")
    if actionable <= 0:
        return {"should_run": False, "reason": "no_actionable_gaps", "changed": changed}
    if not changed and isinstance(next_eligible, datetime) and next_eligible > now:
        return {"should_run": False, "reason": "idle_backoff", "changed": False}
    return {"should_run": True, "reason": "gaps_changed" if changed else "backoff_due", "changed": changed}


async def load_scheduler_state(db: Any, corpus_id: str) -> dict[str, Any] | None:
    try:
        return await db[SCHEDULER_STATE_COLLECTION].find_one(
            {"_id": corpus_id},
            {"_id": 0},
        )
    except Exception:
        return None


async def record_scheduler_outcome(
    db: Any,
    *,
    corpus_id: str,
    snapshot: dict[str, Any],
    changed: bool,
    now: datetime | None = None,
    base_seconds: int = 120,
    max_seconds: int = 3600,
) -> dict[str, Any]:
    """Persist exponential idle backoff, resetting immediately on useful work."""

    now = now or datetime.utcnow()
    previous = await load_scheduler_state(db, corpus_id) or {}
    idle_ticks = 0 if changed else int(previous.get("idle_ticks") or 0) + 1
    no_op_cycles = int(previous.get("no_op_cycles") or 0) + (0 if changed else 1)
    delay = max(1, int(base_seconds or 1)) if changed else min(
        max(1, int(max_seconds or 1)),
        max(1, int(base_seconds or 1)) * (2 ** min(idle_ticks, 8)),
    )
    row = {
        "corpus_id": corpus_id,
        "gap_fingerprint": snapshot.get("fingerprint"),
        "last_snapshot": snapshot,
        "idle_ticks": idle_ticks,
        "no_op_cycles": no_op_cycles,
        "last_changed": bool(changed),
        "next_eligible_at": now + timedelta(seconds=delay),
        "updated_at": now,
    }
    try:
        await db[SCHEDULER_STATE_COLLECTION].update_one(
            {"_id": corpus_id},
            {"$set": row, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception:
        pass
    return row
