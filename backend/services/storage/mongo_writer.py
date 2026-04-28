"""
MongoDB writer — idempotent upsert operations for the ingestion pipeline.

Write order: documents → chunks → update write_state.
All operations use replace_one with upsert=True (MERGE semantics).
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
    ingestion_config, write_state, parent_chunks (inline array).
    """
    await db["documents"].replace_one(
        {"doc_id": doc["doc_id"], "corpus_id": doc["corpus_id"]},
        doc,
        upsert=True,
    )


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
    result = await db["documents"].delete_one(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    return result.deleted_count > 0


async def stash_ghost_b(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    results: list,
) -> None:
    """Persist GHOST B output as `ghost_b_staging` on the document record.

    Accepts either the list of `@dataclass` ExtractionResult instances that
    ghost_b returns or an already-serialized list of dicts. The worker's
    hot path folds this into the atomic Mongo write via `_write_mongo_all`;
    this helper is exposed for tests and manual ops (re-stashing after a
    staging field was accidentally dropped, etc.).

    Dataclasses are converted to plain dicts via `dataclasses.asdict` so
    Mongo stores them verbatim — read-back rehydrates with manual
    EntityItem(**e) / RelationItem(**x) construction (see worker.py).
    """
    serialized: list[dict] = []
    for r in results:
        if is_dataclass(r) and not isinstance(r, type):
            serialized.append(asdict(r))
        elif isinstance(r, dict):
            serialized.append(r)
        else:
            raise TypeError(
                f"stash_ghost_b: unsupported entry type {type(r).__name__}"
            )

    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "ghost_b_staging": serialized,
                "updated_at": datetime.utcnow(),
            }
        },
    )
