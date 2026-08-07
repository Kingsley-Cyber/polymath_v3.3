"""Durable intake + workflow ledger (`ingestion_runs`, `stage_attempts`).

The ledger is the authoritative record that a document was accepted for
ingestion and what has been attempted since. It is Mongo-backed (the
repository's stated Temporal alternative):

- `ingestion_runs` — one row per (corpus_id, doc_id). Deterministic run_id,
  durable from the moment of intake. Status lifecycle:
  intake -> reconciling -> query_ready | degraded.
- `stage_attempts` — append-only receipts, one per planner/executor
  invocation the reconciler drives. A provider failure mutates one attempt
  receipt, never run/document state.
- `control_plane_outbox` — transactional intent written at intake. The
  reconciler is the guaranteed consumer: a crashed upload request can never
  strand a document without an actionable run row.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

RUNS_COLLECTION = "ingestion_runs"
STAGE_ATTEMPTS_COLLECTION = "stage_attempts"
OUTBOX_COLLECTION = "control_plane_outbox"
GRAPHIFY_RECEIPTS_COLLECTION = "graphify_stage_receipts"

RUN_STATUS_INTAKE = "intake"
RUN_STATUS_RECONCILING = "reconciling"
RUN_STATUS_QUERY_READY = "query_ready"
RUN_STATUS_DEGRADED = "degraded"
RUN_STATUS_EXCLUDED = "excluded"

_LEDGER_SCHEMA_VERSION = "ingestion_run.v1"


def graphify_receipt_id(
    *, run_id: str, stage: str, input_hash: str, release: str,
) -> str:
    digest = hashlib.sha256(
        f"{run_id}:{stage}:{input_hash}:{release}".encode("utf-8")
    ).hexdigest()
    return f"graphify_receipt_{digest[:24]}"


async def get_graphify_stage_receipt(
    db: Any,
    *,
    receipt_id: str,
) -> dict[str, Any] | None:
    return await db[GRAPHIFY_RECEIPTS_COLLECTION].find_one(
        {"receipt_id": receipt_id}, {"_id": 0},
    )


async def record_graphify_stage_receipt(
    db: Any,
    *,
    receipt_id: str,
    run_id: str,
    corpus_id: str,
    doc_id: str,
    stage: str,
    status: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Upsert the current receipt for one deterministic Graphify stage input."""
    collection = db[GRAPHIFY_RECEIPTS_COLLECTION]
    existing = await collection.find_one({"receipt_id": receipt_id}, {"_id": 0})
    if existing and existing.get("status") == "passed":
        return existing
    attempt_no = int((existing or {}).get("attempt_no") or 0) + 1
    now = _utcnow()
    row = {
        "receipt_id": receipt_id,
        "schema_version": "polymath.graphify_stage_receipt.v1",
        "run_id": run_id,
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "stage": stage,
        "status": status,
        "attempt_no": attempt_no,
        "retry_count": max(0, attempt_no - 1),
        "receipt": receipt,
        "updated_at": now,
        "created_at": (existing or {}).get("created_at") or now,
    }
    await collection.update_one(
        {"receipt_id": receipt_id}, {"$set": row}, upsert=True,
    )
    return row


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_id_for(*, corpus_id: str, doc_id: str) -> str:
    digest = hashlib.sha256(f"{corpus_id}:{doc_id}".encode("utf-8")).hexdigest()
    return f"run_{digest[:24]}"


async def ensure_ledger_indexes(db: Any) -> None:
    try:
        await db[RUNS_COLLECTION].create_index("run_id", unique=True)
        await db[RUNS_COLLECTION].create_index(
            [("corpus_id", 1), ("doc_id", 1)], unique=True
        )
        await db[RUNS_COLLECTION].create_index([("corpus_id", 1), ("status", 1)])
        await db[STAGE_ATTEMPTS_COLLECTION].create_index(
            [("run_id", 1), ("stage", 1), ("attempt_no", 1)]
        )
        await db[STAGE_ATTEMPTS_COLLECTION].create_index(
            [("corpus_id", 1), ("created_at", -1)]
        )
        await db[OUTBOX_COLLECTION].create_index(
            [("consumed_at", 1), ("corpus_id", 1)]
        )
        await db[GRAPHIFY_RECEIPTS_COLLECTION].create_index("receipt_id", unique=True)
        await db[GRAPHIFY_RECEIPTS_COLLECTION].create_index(
            [("corpus_id", 1), ("doc_id", 1), ("stage", 1)]
        )
    except Exception as exc:  # noqa: BLE001 — index creation is best-effort
        logger.warning("control-plane ledger index creation failed: %s", exc)


async def create_ingestion_run(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
    user_id: str | None = None,
    source: str = "upload",
    filename: str | None = None,
    reason: str = "intake",
) -> dict[str, Any]:
    """Durably record intent to fully ingest one document (idempotent upsert).

    Also writes the outbox intent in the same call path so the reconciler is
    guaranteed to discover the run even if the caller crashes immediately
    after this returns.
    """

    now = _utcnow()
    run_id = run_id_for(corpus_id=corpus_id, doc_id=doc_id)
    await db[RUNS_COLLECTION].update_one(
        {"run_id": run_id},
        {
            "$set": {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "updated_at": now,
                "last_intake_reason": reason,
                **({"user_id": user_id} if user_id else {}),
                **({"filename": filename} if filename else {}),
            },
            "$setOnInsert": {
                "run_id": run_id,
                "schema_version": _LEDGER_SCHEMA_VERSION,
                "status": RUN_STATUS_INTAKE,
                "source": source,
                "created_at": now,
                "certificate_id": None,
                "proof": None,
            },
        },
        upsert=True,
    )
    await db[OUTBOX_COLLECTION].update_one(
        {"run_id": run_id, "consumed_at": None},
        {
            "$set": {"updated_at": now, "reason": reason},
            "$setOnInsert": {
                "run_id": run_id,
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "kind": "reconcile_run",
                "created_at": now,
                "consumed_at": None,
            },
        },
        upsert=True,
    )
    row = await db[RUNS_COLLECTION].find_one({"run_id": run_id}, {"_id": 0})
    return row or {"run_id": run_id, "corpus_id": corpus_id, "doc_id": doc_id}


async def get_run(db: Any, *, run_id: str) -> dict[str, Any] | None:
    return await db[RUNS_COLLECTION].find_one({"run_id": run_id}, {"_id": 0})


async def list_runs(
    db: Any,
    *,
    corpus_id: str,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if status:
        query["status"] = status
    return await db[RUNS_COLLECTION].find(query, {"_id": 0}).sort(
        "updated_at", -1
    ).limit(max(1, min(int(limit or 200), 2000))).to_list(length=None)


async def update_run_from_proof(
    db: Any,
    *,
    run_id: str,
    proof: dict[str, Any],
) -> None:
    """Project the authoritative proof onto the run row (display state only).

    The run row's status is derived FROM the proof — never the reverse.
    """

    if proof.get("excluded"):
        status = RUN_STATUS_EXCLUDED
    elif proof.get("query_ready"):
        status = RUN_STATUS_QUERY_READY
    elif proof.get("readiness_source") == "unavailable":
        status = RUN_STATUS_RECONCILING
    elif any(
        int(entry.get("count") or 0) > 0
        and str(entry.get("status") or "") == "dead_letter"
        for entry in (proof.get("blocking") or [])
    ):
        status = RUN_STATUS_DEGRADED
    else:
        status = RUN_STATUS_RECONCILING
    await db[RUNS_COLLECTION].update_one(
        {"run_id": run_id},
        {
            "$set": {
                "status": status,
                "certificate_id": proof.get("certificate_id"),
                "proof": {
                    "query_ready": proof.get("query_ready"),
                    "missing_counts": proof.get("missing_counts"),
                    "blocking": proof.get("blocking"),
                    "readiness_source": proof.get("readiness_source"),
                },
                "updated_at": _utcnow(),
            }
        },
    )


async def record_stage_attempt(
    db: Any,
    *,
    corpus_id: str,
    stage: str,
    action: str,
    status: str,
    run_id: str | None = None,
    doc_id: str | None = None,
    job_id: str | None = None,
    executor: str | None = None,
    failure_class: str | None = None,
    receipt: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Append one immutable stage-attempt receipt."""

    now = _utcnow()
    scope: dict[str, Any] = {"corpus_id": corpus_id, "stage": stage}
    if run_id:
        scope["run_id"] = run_id
    attempt_no = int(await db[STAGE_ATTEMPTS_COLLECTION].count_documents(scope)) + 1
    row: dict[str, Any] = {
        "corpus_id": corpus_id,
        "run_id": run_id,
        "doc_id": doc_id,
        "stage": stage,
        "action": action,
        "attempt_no": attempt_no,
        "status": status,
        "job_id": job_id,
        "executor": executor,
        "failure_class": failure_class,
        "receipt": receipt or {},
        "duration_ms": duration_ms,
        "created_at": now,
    }
    await db[STAGE_ATTEMPTS_COLLECTION].insert_one(dict(row))
    row.pop("_id", None)
    return row


async def reconcile_runs_for_corpus(
    db: Any,
    *,
    corpus_id: str,
    limit: int | None = None,
) -> dict[str, int]:
    """Ensure every active document has a durable run row.

    This is the existing-corpus backfill: it is literally the reconciler's
    first cycle, not a bespoke migration script.
    """

    from services.control_plane.desired_state import active_document_ids

    doc_ids = await active_document_ids(db, corpus_id=corpus_id, limit=limit)
    if not doc_ids:
        return {"docs": 0, "created": 0}
    existing = await db[RUNS_COLLECTION].find(
        {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}},
        {"_id": 0, "doc_id": 1},
    ).to_list(length=None)
    existing_ids = {str(r.get("doc_id") or "") for r in existing}
    created = 0
    for doc_id in doc_ids:
        if doc_id in existing_ids:
            continue
        await create_ingestion_run(
            db,
            corpus_id=corpus_id,
            doc_id=doc_id,
            source="reconciler_backfill",
            reason="ledger_backfill",
        )
        created += 1
    return {"docs": len(doc_ids), "created": created}


async def sweep_outbox(
    db: Any,
    *,
    corpus_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"consumed_at": None}
    if corpus_id:
        query["corpus_id"] = corpus_id
    return await db[OUTBOX_COLLECTION].find(query, {"_id": 0}).sort(
        "created_at", 1
    ).limit(max(1, min(int(limit or 500), 5000))).to_list(length=None)


async def mark_outbox_consumed(
    db: Any,
    *,
    run_ids: list[str],
) -> int:
    if not run_ids:
        return 0
    result = await db[OUTBOX_COLLECTION].update_many(
        {"run_id": {"$in": run_ids}, "consumed_at": None},
        {"$set": {"consumed_at": _utcnow()}},
    )
    return int(getattr(result, "modified_count", 0) or 0)
