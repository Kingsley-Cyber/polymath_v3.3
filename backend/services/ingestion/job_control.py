"""Inspectable operator controls for durable ingestion jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.ingestion.job_leases import normalize_failure_class


LANE_COLLECTIONS = {
    "source": "source_parse_jobs",
    "document": "document_pipeline_jobs",
    "extraction": "extraction_jobs",
    "summary": "summary_jobs",
    "graph": "graph_promotion_jobs",
}


def collection_for_lane(lane: str) -> str:
    try:
        return LANE_COLLECTIONS[lane]
    except KeyError as exc:
        raise ValueError(f"Unsupported ingestion lane: {lane}") from exc


async def list_jobs(
    db: Any,
    *,
    corpus_id: str,
    lane: str | None = None,
    statuses: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    lanes = [lane] if lane else list(LANE_COLLECTIONS)
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if statuses:
        query["status"] = {"$in": statuses}
    rows: list[dict[str, Any]] = []
    per_lane = max(1, limit // max(1, len(lanes)))
    for lane_name in lanes:
        collection = collection_for_lane(lane_name)
        lane_rows = await db[collection].find(
            query,
            {
                "_id": 0,
                "job_id": 1,
                "corpus_id": 1,
                "doc_id": 1,
                "chunk_id": 1,
                "parent_id": 1,
                "kind": 1,
                "status": 1,
                "attempt_count": 1,
                "failure_class": 1,
                "last_actionable_error": 1,
                "created_at": 1,
                "updated_at": 1,
                "dead_lettered_at": 1,
                "superseded_at": 1,
                "operator_override_generation": 1,
            },
        ).sort("updated_at", -1).limit(per_lane).to_list(length=per_lane)
        rows.extend({**row, "lane": lane_name} for row in lane_rows)
    rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return {"items": rows[:limit], "count": min(len(rows), limit)}


async def control_job(
    db: Any,
    *,
    corpus_id: str,
    lane: str,
    job_id: str,
    action: str,
    reason: str,
    operator_user_id: str,
) -> dict[str, Any]:
    collection = collection_for_lane(lane)
    row = await db[collection].find_one(
        {"corpus_id": corpus_id, "job_id": job_id},
        {"_id": 0},
    )
    if not row:
        raise LookupError("Ingestion job not found")
    now = datetime.now(timezone.utc)
    audit = {
        "action": action,
        "reason": reason[:500],
        "operator_user_id": operator_user_id,
        "at": now,
        "prior_status": row.get("status"),
        "prior_attempt_count": int(row.get("attempt_count") or 0),
    }
    update: dict[str, Any]
    unset = {"lease_id": "", "lease_owner": "", "lease_expires_at": ""}
    if action == "retry":
        update = {
            "status": "queued",
            "attempt_count": 0,
            "failure_class": None,
            "last_actionable_error": None,
            "operator_override_generation": int(
                row.get("operator_override_generation") or 0
            ) + 1,
            "operator_override_at": now,
            "updated_at": now,
        }
        unset.update({"dead_lettered_at": "", "completed_at": ""})
    elif action == "supersede":
        update = {
            "status": "superseded",
            "superseded_at": now,
            "superseded_reason": reason[:500],
            "updated_at": now,
        }
    elif action == "dead_letter":
        update = {
            "status": "dead_letter",
            "dead_lettered_at": now,
            "failure_class": normalize_failure_class("operator_dead_letter"),
            "last_actionable_error": reason[:1000],
            "updated_at": now,
        }
    else:
        raise ValueError(f"Unsupported job action: {action}")
    await db[collection].update_one(
        {"corpus_id": corpus_id, "job_id": job_id},
        {"$set": update, "$unset": unset, "$push": {"operator_audit": audit}},
    )
    return {
        "job_id": job_id,
        "lane": lane,
        "action": action,
        "status": update["status"],
        "operator_override_generation": update.get(
            "operator_override_generation",
            row.get("operator_override_generation", 0),
        ),
    }
