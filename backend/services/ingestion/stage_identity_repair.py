"""Bounded stage-identity repair helpers.

Legacy ingestion rows can predate the durable stage_identity contract. Repair
them incrementally by recomputing identity from live document/chunk state. This
module intentionally does not guess when the live chunk is gone; stale failure
reconciliation owns that path.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from pymongo import UpdateOne

from services.ingestion.extraction_jobs import extraction_contract_hash
from services.ingestion.stage_identity import (
    chunk_hash as stage_chunk_hash,
    extraction_stage_identity,
    stable_stage_hash,
)

STAGE_IDENTITY_MISSING_CLAUSE: dict[str, Any] = {
    "$or": [
        {"stage_identity": {"$exists": False}},
        {"stage_identity": None},
        {"stage_identity.identity_version": {"$exists": False}},
        {"stage_identity.identity_version": None},
        {"stage_identity.identity_version": ""},
    ]
}

GHOST_B_ROW_PROJECTION: dict[str, int] = {
    "_id": 1,
    "corpus_id": 1,
    "doc_id": 1,
    "chunk_id": 1,
    "status": 1,
    "model": 1,
    "provider": 1,
    "lane": 1,
    "attempts": 1,
    "prompt_hash": 1,
    "error_type": 1,
    "raw_output_fingerprint": 1,
    "raw_output_artifact_id": 1,
}

DOC_IDENTITY_PROJECTION: dict[str, int] = {
    "_id": 0,
    "doc_id": 1,
    "corpus_id": 1,
    "updated_at": 1,
    "source_identity": 1,
    "source_key": 1,
    "content_sha256": 1,
    "source_file_hash": 1,
    "ingestion_config": 1,
    "schema_lens": 1,
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


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("doc_id") or ""), str(row.get("chunk_id") or "")


def _derived_artifact_id(row: dict[str, Any], identity: dict[str, Any]) -> str:
    fingerprint = row.get("raw_output_fingerprint")
    if isinstance(fingerprint, dict):
        raw_sha = str(fingerprint.get("sha256") or "").strip()
        if raw_sha:
            return f"sha256:{raw_sha}"
    return "derived:" + stable_stage_hash(
        {
            "corpus_id": row.get("corpus_id"),
            "doc_id": row.get("doc_id"),
            "chunk_id": row.get("chunk_id"),
            "chunk_hash": identity.get("chunk_hash"),
            "extraction_contract_hash": identity.get("extraction_contract_hash"),
            "status": row.get("status"),
            "model": row.get("model"),
            "provider": row.get("provider"),
            "lane": row.get("lane"),
            "attempts": row.get("attempts"),
            "prompt_hash": row.get("prompt_hash"),
            "error_type": row.get("error_type"),
        }
    )


def build_ghost_b_stage_identity_update(
    row: dict[str, Any],
    *,
    doc: dict[str, Any],
    chunk: dict[str, Any],
    contract_hash: str,
    now: datetime,
) -> dict[str, Any]:
    """Return the Mongo update fields for one live Ghost B artifact row."""

    identity = extraction_stage_identity(
        chunk=chunk,
        doc=doc,
        extraction_contract_hash=contract_hash,
    )
    update = {
        "chunk_hash": stage_chunk_hash(chunk),
        "chunk_version": identity.get("chunk_version"),
        "doc_version": identity.get("doc_version"),
        "extraction_contract_hash": contract_hash,
        "stage_identity": identity,
        "stage_identity_repaired_at": now,
        "updated_at": now,
    }
    if not str(row.get("raw_output_artifact_id") or "").strip():
        update["raw_output_artifact_id"] = _derived_artifact_id(row, identity)
    return update


async def backfill_ghost_b_stage_identity(
    db: Any,
    *,
    corpus_id: str,
    apply: bool = False,
    limit: int = 1000,
    statuses: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Backfill stage_identity for legacy Ghost B artifact rows.

    The operation is bounded and idempotent. Rows whose live doc/chunk cannot be
    found are reported but not modified; stale reconciliation handles those.
    """

    limit = max(1, min(int(limit or 1000), 50000))
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        **STAGE_IDENTITY_MISSING_CLAUSE,
    }
    if statuses:
        clean_statuses = sorted({str(status) for status in statuses if str(status)})
        if clean_statuses:
            query["status"] = {"$in": clean_statuses}

    rows = await db["ghost_b_extractions"].find(
        query,
        GHOST_B_ROW_PROJECTION,
    ).limit(limit).to_list(length=limit)

    doc_ids = sorted({str(row.get("doc_id") or "") for row in rows if row.get("doc_id")})
    chunk_ids = sorted({str(row.get("chunk_id") or "") for row in rows if row.get("chunk_id")})

    docs_by_id: dict[str, dict[str, Any]] = {}
    if doc_ids:
        docs = await db["documents"].find(
            {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}},
            DOC_IDENTITY_PROJECTION,
        ).to_list(length=None)
        docs_by_id = {str(doc.get("doc_id") or ""): doc for doc in docs if doc.get("doc_id")}

    chunks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if doc_ids and chunk_ids:
        chunks = await db["chunks"].find(
            {
                "corpus_id": corpus_id,
                "doc_id": {"$in": doc_ids},
                "chunk_id": {"$in": chunk_ids},
            },
            CHUNK_IDENTITY_PROJECTION,
        ).to_list(length=None)
        chunks_by_key = {
            (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or "")): chunk
            for chunk in chunks
            if chunk.get("doc_id") and chunk.get("chunk_id")
        }

    now = datetime.utcnow()
    status_counts = Counter(str(row.get("status") or "unknown") for row in rows)
    ops: list[UpdateOne] = []
    samples: list[dict[str, Any]] = []
    skipped_missing_doc = 0
    skipped_missing_chunk = 0
    skipped_missing_id = 0
    for row in rows:
        row_id = row.get("_id")
        if row_id is None:
            skipped_missing_id += 1
            continue
        doc_id, chunk_id = _row_key(row)
        doc = docs_by_id.get(doc_id)
        if not doc:
            skipped_missing_doc += 1
            continue
        chunk = chunks_by_key.get((doc_id, chunk_id))
        if not chunk:
            skipped_missing_chunk += 1
            continue
        update = build_ghost_b_stage_identity_update(
            row,
            doc=doc,
            chunk=chunk,
            contract_hash=extraction_contract_hash(doc),
            now=now,
        )
        ops.append(UpdateOne({"_id": row_id}, {"$set": update}))
        if len(samples) < 20:
            samples.append(
                {
                    "doc_id": doc_id,
                    "chunk_id": chunk_id,
                    "status": row.get("status"),
                    "chunk_hash": update.get("chunk_hash"),
                    "extraction_contract_hash": update.get("extraction_contract_hash"),
                }
            )

    modified = 0
    if apply and ops:
        result = await db["ghost_b_extractions"].bulk_write(ops, ordered=False)
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
        "skipped_missing_chunk": skipped_missing_chunk,
        "skipped_missing_id": skipped_missing_id,
        "status_counts": dict(sorted(status_counts.items())),
        "sample": samples,
    }
