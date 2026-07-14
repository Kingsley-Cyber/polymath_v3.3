"""
MongoDB writer — idempotent upsert operations for the ingestion pipeline.

Large ingest artifacts are stored outside the compact ``documents`` record:
parent context rows live in ``parent_chunks`` and Ghost B extraction rows live
in ``ghost_b_extractions``. This keeps every write below Mongo's 16MB BSON
document limit while preserving resumable checkpoints.
"""

import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReplaceOne, UpdateOne
from pymongo.errors import DuplicateKeyError

from models.contracts import ParentSummaryRecord, ParentSummaryWrite
from services.ingestion.bibliographic import (
    BIBLIO_DOC_FIELDS,
    merge_persisted_bibliographic,
    promote_bibliographic,
)
from services.ingestion.extraction_jobs import extraction_contract_hash
from services.ingestion.stage_identity import (
    chunk_hash as stage_chunk_hash,
    extraction_stage_identity,
    stable_stage_hash,
)
from services.storage.record_status import (
    ACTIVE_STATUS,
    DELETED_STATUS,
    DELETING_STATUS,
    mark_active,
)

logger = logging.getLogger(__name__)

_BIBLIOGRAPHIC_CAS_ATTEMPTS = 3


def _validate_parent_summary_row(parent: dict) -> dict:
    """Validate and normalize the summary portion of a parent row.

    Parent rows contain structural ingestion fields that deliberately do not
    belong to :class:`ParentSummaryRecord`, so the writer validates the
    contract-owned projection rather than rejecting unrelated parent fields.
    Rows with no generated summary remain valid structural/checkpoint rows.
    """
    row = dict(parent)
    if "summary" not in row or row.get("summary") is None:
        return row

    contract_fields = ParentSummaryRecord.model_fields
    payload = {name: row[name] for name in contract_fields if name in row}
    # Legacy/resume paths materialize every SummaryResult attribute and may
    # therefore pass explicit nulls instead of omitting newly-added capture
    # fields. Normalize only null/missing values; malformed non-null values
    # (including empty strings or wrong container types) must still fail.
    if payload.get("latent_concepts") is None:
        payload["latent_concepts"] = []
    if payload.get("temporal_class") is None:
        payload["temporal_class"] = "unknown"
    if payload.get("time_expressions") is None:
        payload["time_expressions"] = []
    record = ParentSummaryRecord.model_validate(payload)

    source_text = row.get("text")
    if source_text is None:
        source_text = row.get("parent_text")
    write = ParentSummaryWrite(
        parent_id=row["parent_id"],
        doc_id=row["doc_id"],
        corpus_id=row["corpus_id"],
        record=record,
        summary_updated_at=row.get("summary_updated_at") or datetime.utcnow(),
        source_text=source_text if isinstance(source_text, str) else None,
    )
    normalized = write.record.model_dump(mode="python", exclude_none=True)

    # Replace every contract-owned field with its validated representation.
    # This prevents a legacy/null value from surviving next to normalized
    # defaults such as ``temporal_class=unknown`` and empty capture arrays.
    for name in contract_fields:
        row.pop(name, None)
    row.update(normalized)
    return row


def _bibliographic_cas_filter(identity: dict, existing: dict | None) -> dict:
    """Build a compare-and-swap filter for durable bibliographic fields."""
    durable = existing if isinstance(existing, dict) else {}
    clauses: list[dict] = [dict(identity)]
    for field_name in BIBLIO_DOC_FIELDS:
        if field_name in durable:
            clauses.append(
                {
                    field_name: {
                        "$exists": True,
                        "$eq": durable[field_name],
                    }
                }
            )
        else:
            clauses.append({field_name: {"$exists": False}})
    return {"$and": clauses}


async def upsert_corpus(db: AsyncIOMotorDatabase, corpus_doc: dict) -> None:
    """Insert or replace corpus record by corpus_id."""
    corpus_doc = mark_active(dict(corpus_doc))
    await db["corpora"].replace_one(
        {"corpus_id": corpus_doc["corpus_id"]},
        corpus_doc,
        upsert=True,
    )


async def upsert_document(db: AsyncIOMotorDatabase, doc: dict) -> None:
    """
    Insert or replace document record, keyed by (corpus_id, doc_id).
    Content-hashed doc_id is not globally unique — the same file ingested
    into two corpora must produce two independent records.

    doc must include: doc_id, corpus_id, user_id, source_tier,
    ingestion_config, write_state. Bulk artifacts such as parent chunks and
    Ghost B staging must be written to their own collections.

    T-HOOK-3: parse-time bibliographic identity rides
    ``routing_trace["bibliographic"]`` from ``finalize_source_meta``;
    ``promote_bibliographic`` lifts it into the top-level document fields
    (author/title/language/document_date/source_published_at/
    date_confidence/bibliographic_provenance) at this storage boundary,
    non-clobbering.
    """
    collection = db["documents"]
    doc = promote_bibliographic(mark_active(dict(doc)))
    identity = {"doc_id": doc["doc_id"], "corpus_id": doc["corpus_id"]}
    projection = {field_name: 1 for field_name in BIBLIO_DOC_FIELDS}
    for attempt in range(_BIBLIOGRAPHIC_CAS_ATTEMPTS):
        existing = await collection.find_one(identity, projection)
        replacement = merge_persisted_bibliographic(doc, existing)
        cas_filter = _bibliographic_cas_filter(identity, existing)
        try:
            result = await collection.replace_one(
                cas_filter,
                replacement,
                upsert=existing is None,
            )
        except DuplicateKeyError:
            # A first insert raced a concurrent enrichment. Re-read and merge
            # instead of replacing that newly durable bibliographic identity.
            continue
        if existing is None or getattr(result, "matched_count", 1) == 1:
            return
        logger.info(
            "Retrying document bibliographic CAS for corpus=%s doc=%s "
            "(attempt %d/%d)",
            doc["corpus_id"],
            doc["doc_id"],
            attempt + 1,
            _BIBLIOGRAPHIC_CAS_ATTEMPTS,
        )
    raise RuntimeError(
        "document bibliographic metadata changed concurrently after "
        f"{_BIBLIOGRAPHIC_CAS_ATTEMPTS} attempts: "
        f"corpus={doc['corpus_id']} doc={doc['doc_id']}"
    )


async def upsert_parent_chunks(
    db: AsyncIOMotorDatabase,
    parent_chunks: list[dict],
) -> None:
    """Bulk upsert parent chunks by (corpus_id, doc_id, parent_id)."""
    if not parent_chunks:
        return
    now = datetime.utcnow()
    ops = []
    for parent in parent_chunks:
        row = mark_active(_validate_parent_summary_row(parent))
        row["updated_at"] = now
        ops.append(
            ReplaceOne(
                {
                    "corpus_id": row["corpus_id"],
                    "doc_id": row["doc_id"],
                    "parent_id": row["parent_id"],
                },
                row,
                upsert=True,
            )
        )
    await db["parent_chunks"].bulk_write(ops, ordered=False)


async def write_parent_summaries(
    db: AsyncIOMotorDatabase,
    writes: list[ParentSummaryWrite],
) -> None:
    """Persist parent-summary updates through typed commands only.

    The runtime ``isinstance`` gate is intentional: callers may not pass a
    dict that merely resembles a validated artifact. The complete batch is
    type-checked before any operation is constructed, so one untyped/malformed
    command cannot produce a partial write.
    """

    if not writes:
        return
    invalid = [
        type(item).__name__
        for item in writes
        if not isinstance(item, ParentSummaryWrite)
    ]
    if invalid:
        raise TypeError(
            "write_parent_summaries accepts only ParentSummaryWrite models; "
            f"received {invalid}"
        )
    ops = [
        UpdateOne(
            {
                "parent_id": write.parent_id,
                "doc_id": write.doc_id,
                "corpus_id": write.corpus_id,
            },
            {"$set": write.mongo_update_fields()},
        )
        for write in writes
    ]
    await db["parent_chunks"].bulk_write(ops, ordered=False)


async def upsert_chunks(db: AsyncIOMotorDatabase, chunks: list[dict]) -> None:
    """Bulk upsert child chunks by chunk_id. Idempotent."""
    if not chunks:
        return
    # Scope upsert by (corpus_id, chunk_id) to match the compound unique index.
    # Each chunk record carries its own corpus_id.
    ops = [
        ReplaceOne(
            {"corpus_id": c["corpus_id"], "chunk_id": c["chunk_id"]},
            mark_active(dict(c)),
            upsert=True,
        )
        for c in chunks
    ]
    await db["chunks"].bulk_write(ops, ordered=False)


async def update_write_state(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str | None = None,
    **flags: Any,
) -> None:
    """
    Patch write_state flags on a document.
    Scope by corpus_id when provided — required after the (corpus_id, doc_id)
    compound-key migration so we update the correct row when the same
    content-hash lives in multiple corpora.
    """
    update_doc = {f"write_state.{k}": v for k, v in flags.items()}
    update_doc["updated_at"] = datetime.utcnow()
    filter_q: dict = {"doc_id": doc_id}
    if corpus_id is not None:
        filter_q["corpus_id"] = corpus_id
    await db["documents"].update_one(filter_q, {"$set": update_doc})


async def update_corpus(
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    updates: dict,
) -> dict | None:
    """
    Partial update of a corpus record. Automatically sets updated_at.
    Returns the updated document, or None if not found.
    """
    updates["updated_at"] = datetime.utcnow()
    result = await db["corpora"].find_one_and_update(
        {"corpus_id": corpus_id},
        {"$set": updates},
        return_document=True,
    )
    return result


async def delete_corpus(db: AsyncIOMotorDatabase, corpus_id: str) -> bool:
    """Mark a corpus deleted. Returns True if a corpus row existed."""
    now = datetime.utcnow()
    result = await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "status": DELETED_STATUS,
                "deleted_at": now,
                "updated_at": now,
            },
            "$unset": {"deleting_at": ""},
        },
    )
    return result.matched_count > 0


async def mark_corpus_deleting(
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    *,
    cleanup_owner: str | None = None,
    cleanup_lease_until: datetime | None = None,
) -> bool:
    """Mark a corpus as deleting before projection cleanup starts."""
    now = datetime.utcnow()
    cleanup_fields = {}
    if cleanup_owner and cleanup_lease_until:
        cleanup_fields = {
            "cleanup_owner": cleanup_owner,
            "cleanup_lease_until": cleanup_lease_until,
            "cleanup_status": "running",
        }
    result = await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "status": DELETING_STATUS,
                "deleting_at": now,
                "updated_at": now,
                **cleanup_fields,
            }
        },
    )
    return result.matched_count > 0


async def delete_documents_by_corpus(db: AsyncIOMotorDatabase, corpus_id: str) -> int:
    """Mark corpus documents and parent/support rows deleted."""
    now = datetime.utcnow()
    deleted = {"status": DELETED_STATUS, "deleted_at": now, "updated_at": now}
    await db["parent_chunks"].update_many({"corpus_id": corpus_id}, {"$set": deleted})
    await db["ghost_b_extractions"].update_many({"corpus_id": corpus_id}, {"$set": deleted})
    await db["relation_support_records"].update_many({"corpus_id": corpus_id}, {"$set": deleted})
    result = await db["documents"].update_many({"corpus_id": corpus_id}, {"$set": deleted})
    return result.modified_count


async def delete_chunks_by_corpus(db: AsyncIOMotorDatabase, corpus_id: str) -> int:
    """Mark all chunks belonging to a corpus deleted."""
    result = await db["chunks"].update_many(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "status": DELETED_STATUS,
                "deleted_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return result.modified_count


async def delete_chunks_by_doc(
    db: AsyncIOMotorDatabase, corpus_id: str, doc_id: str
) -> int:
    """Mark all chunks for a single document deleted."""
    result = await db["chunks"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "status": DELETED_STATUS,
                "deleted_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return result.modified_count


async def delete_document(
    db: AsyncIOMotorDatabase, corpus_id: str, doc_id: str
) -> bool:
    """Mark a single document and its support rows deleted."""
    now = datetime.utcnow()
    deleted = {"status": DELETED_STATUS, "deleted_at": now, "updated_at": now}
    await db["parent_chunks"].update_many({"corpus_id": corpus_id, "doc_id": doc_id}, {"$set": deleted})
    await db["ghost_b_extractions"].update_many({"corpus_id": corpus_id, "doc_id": doc_id}, {"$set": deleted})
    await db["relation_support_records"].update_many({"corpus_id": corpus_id, "doc_id": doc_id}, {"$set": deleted})
    result = await db["documents"].update_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"$set": deleted},
    )
    return result.matched_count > 0


async def retire_document_derived_state(
    db: AsyncIOMotorDatabase,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, int]:
    """Remove derived trees and retire durable jobs for a deleted document.

    Queue history remains inspectable, but no old lease or terminal artifact
    may masquerade as current truth when the same source is ingested again.
    Summary-tree rows are derived and are deleted outright so legacy readers
    that predate active-record filtering cannot reuse a stale document root.
    """

    now = datetime.utcnow()
    tree_result = await db["summary_tree"].delete_many(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    queue_counts: dict[str, int] = {}
    for collection_name in (
        "source_parse_jobs",
        "document_pipeline_jobs",
        "extraction_jobs",
        "summary_jobs",
        "graph_promotion_jobs",
    ):
        result = await db[collection_name].update_many(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "$set": {
                    "status": "superseded",
                    "reason": "document_deleted",
                    "document_deleted_at": now,
                    "updated_at": now,
                    "lease_until": None,
                },
                "$unset": {"runner": "", "started_at": ""},
            },
        )
        queue_counts[collection_name] = int(
            getattr(result, "modified_count", 0) or 0
        )
    await db["ingest_batch_items"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "document_deleted": True,
                "document_deleted_at": now,
                "updated_at": now,
            }
        },
    )
    return {
        "summary_tree": int(getattr(tree_result, "deleted_count", 0) or 0),
        **queue_counts,
    }


async def retire_corpus_derived_state(
    db: AsyncIOMotorDatabase,
    *,
    corpus_id: str,
) -> dict[str, int]:
    """Remove hierarchy rows and retire durable work for a deleted corpus."""

    now = datetime.utcnow()
    tree_result = await db["summary_tree"].delete_many({"corpus_id": corpus_id})
    queue_counts: dict[str, int] = {}
    for collection_name in (
        "source_parse_jobs",
        "document_pipeline_jobs",
        "extraction_jobs",
        "summary_jobs",
        "graph_promotion_jobs",
    ):
        result = await db[collection_name].update_many(
            {"corpus_id": corpus_id, "status": {"$ne": "superseded"}},
            {
                "$set": {
                    "status": "superseded",
                    "reason": "corpus_deleted",
                    "corpus_deleted_at": now,
                    "updated_at": now,
                    "lease_until": None,
                },
                "$unset": {"runner": "", "started_at": ""},
            },
        )
        queue_counts[collection_name] = int(
            getattr(result, "modified_count", 0) or 0
        )
    await db["ingest_batch_items"].update_many(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "corpus_deleted": True,
                "corpus_deleted_at": now,
                "updated_at": now,
            }
        },
    )
    return {
        "summary_tree": int(getattr(tree_result, "deleted_count", 0) or 0),
        **queue_counts,
    }


async def delete_ghost_b_extractions(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
) -> int:
    """Delete Ghost B extraction rows for one document."""
    result = await db["ghost_b_extractions"].delete_many(
        {"doc_id": doc_id, "corpus_id": corpus_id}
    )
    return result.deleted_count


async def _ghost_b_identity_context(
    db: AsyncIOMotorDatabase,
    *,
    doc_id: str,
    corpus_id: str,
    chunk_ids: list[str],
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str]:
    """Load current document/chunk identity used to stamp Ghost B rows."""

    clean_chunk_ids = sorted({str(chunk_id) for chunk_id in chunk_ids if chunk_id})
    doc = await db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
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
        },
    )
    chunks: dict[str, dict[str, Any]] = {}
    if clean_chunk_ids:
        rows = await db["chunks"].find(
            {
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "chunk_id": {"$in": clean_chunk_ids},
            },
            {
                "_id": 0,
                "chunk_id": 1,
                "parent_id": 1,
                "text": 1,
                "text_hash": 1,
                "chunk_hash": 1,
                "chunk_version": 1,
                "updated_at": 1,
            },
        ).to_list(length=len(clean_chunk_ids))
        chunks = {str(row.get("chunk_id") or ""): row for row in rows if row.get("chunk_id")}
    return doc, chunks, extraction_contract_hash(doc)


def _stamp_ghost_b_identity(
    row: dict[str, Any],
    *,
    chunk: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    contract_hash: str,
) -> None:
    if not chunk:
        return
    identity = extraction_stage_identity(
        chunk=chunk,
        doc=doc,
        extraction_contract_hash=contract_hash,
    )
    row["chunk_hash"] = stage_chunk_hash(chunk)
    row["chunk_version"] = identity.get("chunk_version")
    row["doc_version"] = identity.get("doc_version")
    row["extraction_contract_hash"] = contract_hash
    row["stage_identity"] = identity


def _ensure_ghost_b_artifact_id(row: dict[str, Any]) -> None:
    """Attach a compact durable handle for this staged extraction artifact.

    Successful LLM rows normally carry ``sha256:<raw-response-hash>`` from
    Ghost B. Legacy rows, deterministic extractors, and provider failures may
    not have a raw output body, so derive a stable id from the row identity and
    compact audit fields instead of storing raw prompt/response text.
    """

    if str(row.get("raw_output_artifact_id") or "").strip():
        return
    fingerprint = row.get("raw_output_fingerprint")
    if isinstance(fingerprint, dict):
        raw_sha = str(fingerprint.get("sha256") or "").strip()
        if raw_sha:
            row["raw_output_artifact_id"] = f"sha256:{raw_sha}"
            return
    identity = row.get("stage_identity") if isinstance(row.get("stage_identity"), dict) else {}
    row["raw_output_artifact_id"] = "derived:" + stable_stage_hash(
        {
            "doc_id": row.get("doc_id"),
            "corpus_id": row.get("corpus_id"),
            "chunk_id": row.get("chunk_id"),
            "chunk_hash": row.get("chunk_hash") or identity.get("chunk_hash"),
            "extraction_contract_hash": (
                row.get("extraction_contract_hash")
                or identity.get("extraction_contract_hash")
            ),
            "status": row.get("status"),
            "model": row.get("model"),
            "provider": row.get("provider"),
            "lane": row.get("lane"),
            "attempts": row.get("attempts"),
            "prompt_hash": row.get("prompt_hash"),
            "error_type": row.get("error_type"),
        }
    )


async def stash_ghost_b(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    results: list,
) -> None:
    """Persist GHOST B output as per-chunk rows.

    Accepts either the list of `@dataclass` ExtractionResult instances that
    ghost_b returns or an already-serialized list of dicts. The worker's
    hot path writes through this helper after the compact document row is
    created. Each child extraction is independently checkpointed under
    ``ghost_b_extractions``.

    Dataclasses are converted to plain dicts via `dataclasses.asdict` so
    Mongo stores the extraction payload verbatim plus status metadata.
    """
    now = datetime.utcnow()
    serialized: list[dict] = []
    for r in results:
        if is_dataclass(r) and not isinstance(r, type):
            row = asdict(r)
        elif isinstance(r, dict):
            row = dict(r)
        else:
            raise TypeError(
                f"stash_ghost_b: unsupported entry type {type(r).__name__}"
            )
        if not row.get("chunk_id"):
            raise ValueError("stash_ghost_b: each result must include chunk_id")
        row["doc_id"] = doc_id
        row["corpus_id"] = corpus_id
        row["status"] = "ok"
        row["updated_at"] = now
        serialized.append(row)

    if serialized:
        doc, chunks_by_id, contract_hash = await _ghost_b_identity_context(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            chunk_ids=[str(row.get("chunk_id") or "") for row in serialized],
        )
        for row in serialized:
            _stamp_ghost_b_identity(
                row,
                chunk=chunks_by_id.get(str(row.get("chunk_id") or "")),
                doc=doc,
                contract_hash=contract_hash,
            )
            _ensure_ghost_b_artifact_id(row)
        ops = [
            ReplaceOne(
                {
                    "doc_id": doc_id,
                    "corpus_id": corpus_id,
                    "chunk_id": row["chunk_id"],
                },
                row,
                upsert=True,
            )
            for row in serialized
        ]
        await db["ghost_b_extractions"].bulk_write(ops, ordered=False)

    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "ghost_b_staging_count": len(serialized),
                "updated_at": datetime.utcnow(),
            },
            "$unset": {"ghost_b_staging": ""},
        },
    )


async def replace_relation_support_for_document(
    db: AsyncIOMotorDatabase,
    *,
    doc_id: str,
    corpus_id: str,
    records: list[dict],
) -> int:
    """Replace active relation-support records for one document.

    Neo4j stores materialized traversal edges. This Mongo collection stores the
    canonical per-chunk support rows those edges summarize.
    """
    now = datetime.utcnow()
    await db["relation_support_records"].update_many(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "status": DELETED_STATUS,
                "deleted_at": now,
                "updated_at": now,
            }
        },
    )
    if not records:
        return 0

    ops = []
    for record in records:
        row = mark_active(dict(record))
        row["updated_at"] = now
        ops.append(
            UpdateOne(
                {"support_id": row["support_id"]},
                {
                    "$set": row,
                    "$setOnInsert": {"created_at": now},
                    "$unset": {"deleted_at": ""},
                },
                upsert=True,
            )
        )
    await db["relation_support_records"].bulk_write(ops, ordered=False)
    return len(ops)


async def stash_ghost_b_failures(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    failures: list,
) -> None:
    """Persist Ghost B extraction failures as per-chunk error rows.

    ``documents.ghost_b_failures`` keeps only a small compatibility sample so
    the document record cannot grow with a provider-wide failure.
    """
    now = datetime.utcnow()
    serialized: list[dict] = []
    for failure in failures:
        if is_dataclass(failure) and not isinstance(failure, type):
            row = asdict(failure)
        elif isinstance(failure, dict):
            row = dict(failure)
        else:
            raise TypeError(
                f"stash_ghost_b_failures: unsupported entry type {type(failure).__name__}"
            )
        if not row.get("chunk_id"):
            continue
        row["doc_id"] = doc_id
        row["corpus_id"] = corpus_id
        row["status"] = "error"
        row["updated_at"] = now
        serialized.append(row)

    if serialized:
        doc, chunks_by_id, contract_hash = await _ghost_b_identity_context(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            chunk_ids=[str(row.get("chunk_id") or "") for row in serialized],
        )
        for row in serialized:
            _stamp_ghost_b_identity(
                row,
                chunk=chunks_by_id.get(str(row.get("chunk_id") or "")),
                doc=doc,
                contract_hash=contract_hash,
            )
            _ensure_ghost_b_artifact_id(row)
        ops = [
            ReplaceOne(
                {
                    "doc_id": doc_id,
                    "corpus_id": corpus_id,
                    "chunk_id": row["chunk_id"],
                },
                row,
                upsert=True,
            )
            for row in serialized
        ]
        await db["ghost_b_extractions"].bulk_write(ops, ordered=False)

    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "ghost_b_failures": serialized[:20],
                "ghost_b_failure_count": len(serialized),
                "updated_at": now,
            }
        },
    )
