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
from pymongo import ReplaceOne

logger = logging.getLogger(__name__)


async def upsert_corpus(db: AsyncIOMotorDatabase, corpus_doc: dict) -> None:
    """Insert or replace corpus record by corpus_id."""
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
    """
    await db["documents"].replace_one(
        {"doc_id": doc["doc_id"], "corpus_id": doc["corpus_id"]},
        doc,
        upsert=True,
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
        row = dict(parent)
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


async def upsert_chunks(db: AsyncIOMotorDatabase, chunks: list[dict]) -> None:
    """Bulk upsert child chunks by chunk_id. Idempotent."""
    if not chunks:
        return
    # Scope upsert by (corpus_id, chunk_id) to match the compound unique index.
    # Each chunk record carries its own corpus_id.
    ops = [
        ReplaceOne({"corpus_id": c["corpus_id"], "chunk_id": c["chunk_id"]}, c, upsert=True)
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
    """Delete a corpus record by corpus_id. Returns True if deleted."""
    result = await db["corpora"].delete_one({"corpus_id": corpus_id})
    return result.deleted_count > 0


async def delete_documents_by_corpus(db: AsyncIOMotorDatabase, corpus_id: str) -> int:
    """Delete all documents belonging to a corpus. Returns count deleted."""
    await db["parent_chunks"].delete_many({"corpus_id": corpus_id})
    await db["ghost_b_extractions"].delete_many({"corpus_id": corpus_id})
    result = await db["documents"].delete_many({"corpus_id": corpus_id})
    return result.deleted_count


async def delete_chunks_by_corpus(db: AsyncIOMotorDatabase, corpus_id: str) -> int:
    """Delete all chunks belonging to a corpus. Returns count deleted."""
    result = await db["chunks"].delete_many({"corpus_id": corpus_id})
    return result.deleted_count


async def delete_chunks_by_doc(
    db: AsyncIOMotorDatabase, corpus_id: str, doc_id: str
) -> int:
    """Delete all chunks for a single document. Returns count deleted."""
    result = await db["chunks"].delete_many(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    return result.deleted_count


async def delete_document(
    db: AsyncIOMotorDatabase, corpus_id: str, doc_id: str
) -> bool:
    """Delete a single document record. Returns True if deleted."""
    await db["parent_chunks"].delete_many(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    await db["ghost_b_extractions"].delete_many(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    result = await db["documents"].delete_one(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    return result.deleted_count > 0


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
