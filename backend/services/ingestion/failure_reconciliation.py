"""Ghost B failure-metadata reconciliation.

Extraction failures are useful only while they point at live chunks. Large
re-ingests and repair passes can leave old failure rows behind, which makes the
corpus look broken and causes retry jobs to chase chunk ids that no longer
exist. This module classifies those rows as stale and realigns document-level
failure counters from current chunk/extraction truth.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from uuid import uuid4

from pymongo import UpdateMany, UpdateOne

from services.ingestion.extraction_jobs import extraction_contract_hash
from services.ingestion.stage_identity import chunk_hash as live_chunk_hash

STALE_FAILURE_STATUS = "stale_chunk_reference"
STALE_CHUNK_REFERENCE = "stale_chunk_reference"
STALE_CHUNK_HASH_MISMATCH = "stale_chunk_hash_mismatch"
STALE_EXTRACTION_CONTRACT_MISMATCH = "stale_extraction_contract_mismatch"
EXTRACTION_JOB_RECONCILE_STATUSES = (
    "queued",
    "running",
    "provider_failed",
    "validation_failed",
    "failed",
)
DOC_IDENTITY_PROJECTION: dict[str, int] = {
    "_id": 0,
    "doc_id": 1,
    "corpus_id": 1,
    "user_id": 1,
    "filename": 1,
    "updated_at": 1,
    "source_identity": 1,
    "source_key": 1,
    "content_sha256": 1,
    "source_file_hash": 1,
    "ingestion_config": 1,
    "schema_lens": 1,
    "ghost_b_failures": 1,
    "ghost_b_failure_count": 1,
}
CHUNK_IDENTITY_PROJECTION: dict[str, int] = {
    "_id": 0,
    "doc_id": 1,
    "chunk_id": 1,
    "parent_id": 1,
    "text": 1,
    "text_hash": 1,
    "chunk_hash": 1,
    "chunk_version": 1,
    "updated_at": 1,
}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _chunk_id(row: dict[str, Any]) -> str:
    return str(row.get("chunk_id") or "").strip()


def _stage_identity(row: dict[str, Any]) -> dict[str, Any]:
    identity = row.get("stage_identity")
    return identity if isinstance(identity, dict) else {}


def _stored_chunk_hash(row: dict[str, Any]) -> str:
    return str(row.get("chunk_hash") or _stage_identity(row).get("chunk_hash") or "").strip()


def _stored_contract_hash(row: dict[str, Any]) -> str:
    return str(
        row.get("extraction_contract_hash")
        or _stage_identity(row).get("extraction_contract_hash")
        or ""
    ).strip()


def classify_failure_row_staleness(
    row: dict[str, Any],
    *,
    chunk: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    current_contract_hash: str | None = None,
) -> str | None:
    """Return stale reason when a failure row no longer references live truth.

    Legacy rows often lack stored hashes. Those rows remain active when the
    chunk still exists because drift cannot be proven safely.
    """

    if not chunk:
        return STALE_CHUNK_REFERENCE

    stored_hash = _stored_chunk_hash(row)
    if stored_hash:
        current_hash = live_chunk_hash(chunk)
        if current_hash and stored_hash != current_hash:
            return STALE_CHUNK_HASH_MISMATCH

    stored_contract = _stored_contract_hash(row)
    if stored_contract:
        current_contract = current_contract_hash or extraction_contract_hash(doc)
        if current_contract and stored_contract != current_contract:
            return STALE_EXTRACTION_CONTRACT_MISMATCH

    return None


def repair_action_for_stale_reason(reason: str | None) -> str:
    if reason == STALE_EXTRACTION_CONTRACT_MISMATCH:
        return "requeue_with_current_contract"
    if reason == STALE_CHUNK_HASH_MISMATCH:
        return "clear_or_reextract_chunk"
    return "clear_or_rechunk_doc"


async def classify_stale_failure_rows(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 5000,
) -> dict[str, Any]:
    """Bounded scan of active Ghost B errors that no longer match live truth."""

    limit = max(1, min(int(limit or 5000), 50000))
    candidate_error_rows = await db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "status": "error"},
    ).limit(limit).to_list(length=limit)

    docs = await db["documents"].find(
        {"corpus_id": corpus_id, "ghost_b_failure_count": {"$gt": 0}},
        DOC_IDENTITY_PROJECTION,
    ).to_list(length=None)
    doc_by_id = {str(doc.get("doc_id") or ""): doc for doc in docs if doc.get("doc_id")}
    candidate_doc_ids = set(doc_by_id) | {
        str(row.get("doc_id") or "") for row in candidate_error_rows if row.get("doc_id")
    }

    if candidate_doc_ids:
        missing_docs = candidate_doc_ids - set(doc_by_id)
        if missing_docs:
            extra_docs = await db["documents"].find(
                {"corpus_id": corpus_id, "doc_id": {"$in": sorted(missing_docs)}},
                DOC_IDENTITY_PROJECTION,
            ).to_list(length=None)
            for doc in extra_docs:
                if doc.get("doc_id"):
                    doc_by_id[str(doc["doc_id"])] = doc

    chunks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if candidate_doc_ids:
        chunk_rows = await db["chunks"].find(
            {"corpus_id": corpus_id, "doc_id": {"$in": sorted(candidate_doc_ids)}},
            CHUNK_IDENTITY_PROJECTION,
        ).to_list(length=None)
        for row in chunk_rows:
            doc_id = str(row.get("doc_id") or "")
            chunk_id = _chunk_id(row)
            if doc_id and chunk_id:
                chunks_by_key[(doc_id, chunk_id)] = row

    contract_by_doc = {
        doc_id: extraction_contract_hash(doc)
        for doc_id, doc in doc_by_id.items()
    }
    stale_rows: list[dict[str, Any]] = []
    for row in candidate_error_rows:
        doc_id = str(row.get("doc_id") or "")
        chunk_id = _chunk_id(row)
        reason = classify_failure_row_staleness(
            row,
            chunk=chunks_by_key.get((doc_id, chunk_id)),
            doc=doc_by_id.get(doc_id),
            current_contract_hash=contract_by_doc.get(doc_id),
        )
        if reason:
            stale_row = dict(row)
            stale_row["stale_reason"] = reason
            stale_rows.append(stale_row)

    stale_by_doc = Counter(
        str(row.get("doc_id") or "") for row in stale_rows if row.get("doc_id")
    )
    stale_reason_counts = Counter(
        str(row.get("stale_reason") or STALE_FAILURE_STATUS) for row in stale_rows
    )
    return {
        "limit": limit,
        "scanned_error_rows": len(candidate_error_rows),
        "scan_limit_reached": len(candidate_error_rows) >= limit,
        "candidate_doc_ids": candidate_doc_ids,
        "doc_by_id": doc_by_id,
        "chunks_by_key": chunks_by_key,
        "stale_rows": stale_rows,
        "stale_by_doc": stale_by_doc,
        "stale_reason_counts": stale_reason_counts,
    }


def build_document_failure_reconciliation(
    *,
    doc: dict[str, Any],
    split_error_rows: list[dict[str, Any]] | None = None,
    live_chunk_ids: set[str] | None = None,
    stale_chunk_ids: set[str] | None = None,
    stale_split_count: int = 0,
) -> dict[str, Any]:
    """Return the corrected document failure state.

    Split ``ghost_b_extractions`` rows are authoritative. Inline
    ``documents.ghost_b_failures`` are a legacy fallback and are retained only
    when no split rows exist and their chunk ids still exist.
    """

    split_error_rows = split_error_rows or []
    live_chunk_ids = live_chunk_ids or set()
    stale_chunk_ids = stale_chunk_ids or set()
    inline_failures = [dict(row) for row in (doc.get("ghost_b_failures") or [])]
    existing_count = _int(doc.get("ghost_b_failure_count") or len(inline_failures))

    stale_inline = 0
    orphaned = 0
    if split_error_rows:
        remaining = [dict(row) for row in split_error_rows if _chunk_id(row)]
    elif inline_failures:
        remaining = []
        for row in inline_failures:
            chunk_id = _chunk_id(row)
            if chunk_id and chunk_id in stale_chunk_ids:
                stale_inline += 1
            elif chunk_id and chunk_id in live_chunk_ids:
                remaining.append(row)
            elif chunk_id:
                stale_inline += 1
            else:
                orphaned += 1
    else:
        remaining = []
        if existing_count:
            orphaned = existing_count

    remaining_count = len(remaining)
    stale_total = int(stale_split_count) + stale_inline
    sample_drift = (doc.get("ghost_b_failures") or [])[:20] != remaining[:20]
    counter_drift = existing_count != remaining_count
    needs_update = counter_drift or stale_total > 0 or orphaned > 0 or sample_drift

    return {
        "doc_id": str(doc.get("doc_id") or ""),
        "existing_count": existing_count,
        "remaining_count": remaining_count,
        "remaining_failures": remaining[:20],
        "stale_split_count": int(stale_split_count),
        "stale_inline_count": stale_inline,
        "stale_total": stale_total,
        "orphaned_count": orphaned,
        "counter_drift": counter_drift,
        "sample_drift": sample_drift,
        "needs_update": needs_update,
    }


async def reconcile_ghost_b_failure_metadata(
    db: Any,
    *,
    corpus_id: str,
    apply: bool = False,
    limit: int = 5000,
) -> dict[str, Any]:
    """Classify stale Ghost B failures and realign document counters.

    ``apply=False`` is a dry run. ``limit`` caps the number of split error rows
    classified stale in one call so the operation remains safe on large corpora.
    """

    now = datetime.utcnow()
    stale_scan = await classify_stale_failure_rows(
        db,
        corpus_id=corpus_id,
        limit=limit,
    )
    limit = int(stale_scan["limit"])
    candidate_doc_ids = stale_scan["candidate_doc_ids"]
    doc_by_id = stale_scan["doc_by_id"]
    chunks_by_key = stale_scan["chunks_by_key"]
    stale_rows = stale_scan["stale_rows"]
    stale_by_doc = stale_scan["stale_by_doc"]
    stale_chunk_ids_by_doc: dict[str, set[str]] = defaultdict(set)
    for row in stale_rows:
        doc_id = str(row.get("doc_id") or "")
        chunk_id = _chunk_id(row)
        if doc_id and chunk_id:
            stale_chunk_ids_by_doc[doc_id].add(chunk_id)
    if candidate_doc_ids:
        reconciled_rows = await db["ghost_b_extractions"].find(
            {
                "corpus_id": corpus_id,
                "doc_id": {"$in": sorted(candidate_doc_ids)},
                "status": STALE_FAILURE_STATUS,
            },
            {"_id": 0, "doc_id": 1, "chunk_id": 1},
        ).to_list(length=None)
        for row in reconciled_rows:
            doc_id = str(row.get("doc_id") or "")
            chunk_id = _chunk_id(row)
            if doc_id and chunk_id:
                stale_chunk_ids_by_doc[doc_id].add(chunk_id)
    stale_reason_counts = stale_scan["stale_reason_counts"]
    stale_ids = {row.get("_id") for row in stale_rows if row.get("_id") is not None}
    split_rows_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunk_ids_by_doc: dict[str, set[str]] = defaultdict(set)
    if candidate_doc_ids:
        split_filter: dict[str, Any] = {
            "corpus_id": corpus_id,
            "doc_id": {"$in": sorted(candidate_doc_ids)},
            "status": "error",
        }
        if stale_ids:
            split_filter["_id"] = {"$nin": list(stale_ids)}
        split_rows = await db["ghost_b_extractions"].find(
            split_filter,
            {"_id": 0},
        ).sort("chunk_id", 1).to_list(length=None)
        for row in split_rows:
            doc_id = str(row.get("doc_id") or "")
            if doc_id:
                split_rows_by_doc[doc_id].append(row)

        for row in chunks_by_key.values():
            doc_id = str(row.get("doc_id") or "")
            chunk_id = _chunk_id(row)
            if doc_id and chunk_id:
                chunk_ids_by_doc[doc_id].add(chunk_id)

    all_states: list[dict[str, Any]] = []
    doc_states: list[dict[str, Any]] = []
    for doc_id, doc in sorted(doc_by_id.items()):
        state = build_document_failure_reconciliation(
            doc=doc,
            split_error_rows=split_rows_by_doc.get(doc_id, []),
            live_chunk_ids=chunk_ids_by_doc.get(doc_id, set()),
            stale_chunk_ids=stale_chunk_ids_by_doc.get(doc_id, set()),
            stale_split_count=stale_by_doc.get(doc_id, 0),
        )
        all_states.append(state)
        if state["needs_update"]:
            state["filename"] = doc.get("filename")
            doc_states.append(state)

    result: dict[str, Any] = {
        "status": "planned" if not apply else "complete",
        "corpus_id": corpus_id,
        "apply": bool(apply),
        "limit": limit,
        "scanned_failure_rows": int(stale_scan["scanned_error_rows"]),
        "stale_scan_limit_reached": bool(stale_scan["scan_limit_reached"]),
        "stale_split_rows": len(stale_rows),
        "stale_reason_counts": dict(sorted(stale_reason_counts.items())),
        "affected_docs": len(doc_states),
        "counter_drift_docs": sum(1 for state in doc_states if state["counter_drift"]),
        "sample_drift_docs": sum(1 for state in doc_states if state["sample_drift"]),
        "stale_docs": sum(1 for state in doc_states if state["stale_total"] > 0),
        "orphaned_docs": sum(1 for state in doc_states if state["orphaned_count"] > 0),
        "documents_cleared": sum(1 for state in doc_states if state["remaining_count"] == 0),
        "remaining_failed_chunks": sum(int(state["remaining_count"]) for state in all_states),
        "affected_remaining_failed_chunks": sum(
            int(state["remaining_count"]) for state in doc_states
        ),
        "stale_inline_rows": sum(int(state["stale_inline_count"]) for state in doc_states),
        "orphaned_failure_refs": sum(int(state["orphaned_count"]) for state in doc_states),
        "stale_extraction_jobs_skipped": 0,
        "sample": [
            {
                "doc_id": state["doc_id"],
                "filename": state.get("filename"),
                "existing_count": state["existing_count"],
                "remaining_count": state["remaining_count"],
                "stale_total": state["stale_total"],
                "orphaned_count": state["orphaned_count"],
                "counter_drift": state["counter_drift"],
                "sample_drift": state["sample_drift"],
                "stale_reasons": {
                    reason: count
                    for reason, count in Counter(
                        str(row.get("stale_reason") or STALE_FAILURE_STATUS)
                        for row in stale_rows
                        if str(row.get("doc_id") or "") == state["doc_id"]
                    ).items()
                },
            }
            for state in doc_states[:20]
        ],
    }

    if not apply:
        return result

    row_ops = [
        UpdateOne(
            {"_id": row["_id"]},
            {
                "$set": {
                    "status": STALE_FAILURE_STATUS,
                    "failure_status": STALE_FAILURE_STATUS,
                    "stale_reason": row.get("stale_reason") or STALE_FAILURE_STATUS,
                    "repair_action": repair_action_for_stale_reason(row.get("stale_reason")),
                    "previous_status": "error",
                    "reconciled_at": now,
                    "updated_at": now,
                }
            },
        )
        for row in stale_rows
        if row.get("_id") is not None
    ]
    if row_ops:
        await db["ghost_b_extractions"].bulk_write(row_ops, ordered=False)

    stale_chunk_reference_ops: list[UpdateMany] = []
    stale_chunk_reference_keys: set[tuple[str, str]] = set()
    for row in stale_rows:
        if row.get("stale_reason") != STALE_CHUNK_REFERENCE:
            continue
        doc_id = str(row.get("doc_id") or "")
        chunk_id = _chunk_id(row)
        if not doc_id or not chunk_id:
            continue
        key = (doc_id, chunk_id)
        if key in stale_chunk_reference_keys:
            continue
        stale_chunk_reference_keys.add(key)
        stale_chunk_reference_ops.append(
            UpdateMany(
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "chunk_id": chunk_id,
                    "status": {"$in": list(EXTRACTION_JOB_RECONCILE_STATUSES)},
                },
                {
                    "$set": {
                        "status": "skipped",
                        "reason": STALE_CHUNK_REFERENCE,
                        "source_status": STALE_FAILURE_STATUS,
                        "stale_reason": STALE_CHUNK_REFERENCE,
                        "repair_action": repair_action_for_stale_reason(STALE_CHUNK_REFERENCE),
                        "failure_status": STALE_FAILURE_STATUS,
                        "stale_extraction_reconciled_at": now,
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
    stale_extraction_jobs_skipped = 0
    if stale_chunk_reference_ops:
        job_result = await db["extraction_jobs"].bulk_write(
            stale_chunk_reference_ops,
            ordered=False,
        )
        stale_extraction_jobs_skipped = int(
            getattr(job_result, "modified_count", 0) or 0
        )

    doc_ops = []
    for state in doc_states:
        doc_ops.append(
            UpdateOne(
                {"corpus_id": corpus_id, "doc_id": state["doc_id"]},
                {
                    "$set": {
                        "ghost_b_failures": state["remaining_failures"],
                        "ghost_b_failure_count": state["remaining_count"],
                        "ghost_b_stale_failure_count": state["stale_total"],
                        "ghost_b_orphaned_failure_count": state["orphaned_count"],
                        "ghost_b_failure_reconciled_at": now,
                        "updated_at": now,
                    }
                },
            )
        )
    if doc_ops:
        await db["documents"].bulk_write(doc_ops, ordered=False)

    result["stale_extraction_jobs_skipped"] = stale_extraction_jobs_skipped
    changed = bool(row_ops or doc_ops or stale_extraction_jobs_skipped)
    result["changed"] = changed
    result["run_recorded"] = changed
    if not changed:
        # A healthy corpus should not accumulate an ingest_repair_runs row on
        # every scheduler tick. The returned result remains observable in the
        # tick telemetry, but durable repair history is reserved for work.
        return result
    run_id = f"ghost_b_failure_reconcile_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    await db["ingest_repair_runs"].insert_one(
        {
            "run_id": run_id,
            "kind": "ghost_b_failure_reconcile",
            "status": "complete",
            "corpus_id": corpus_id,
            "apply": True,
            "counts": {
                key: value
                for key, value in result.items()
                if key not in {"sample", "corpus_id", "status", "apply"}
            },
            "sample": result["sample"],
            "started_at": now,
            "completed_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
    )
    result["run_id"] = run_id
    return result
