"""
MongoDB reader — scoped read operations for retrieval and hydration.

All queries scope by corpus_id to prevent cross-corpus bleed.
"""

import logging

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


async def get_corpus(db: AsyncIOMotorDatabase, corpus_id: str) -> dict | None:
    return await db["corpora"].find_one({"corpus_id": corpus_id})


async def list_corpora(
    db: AsyncIOMotorDatabase,
    user_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    query: dict = {}
    if user_id:
        query["user_id"] = user_id
    cursor = db["corpora"].find(query).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_document(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str | None = None,
) -> dict | None:
    """Fetch a document record.

    When `corpus_id` is provided, scope the lookup so the same content-hashed
    doc_id in a different corpus doesn't shadow a fresh ingest. Legacy
    single-arg callers still work (they accept cross-corpus lookup).
    """
    q: dict = {"doc_id": doc_id}
    if corpus_id is not None:
        q["corpus_id"] = corpus_id
    return await db["documents"].find_one(q)


async def get_parent_chunks(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
) -> list[dict]:
    """Return parent chunk rows, with legacy inline fallback."""
    try:
        rows = await db["parent_chunks"].find(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"_id": 0},
        ).sort("parent_id", 1).to_list(length=None)
        if rows:
            return rows
    except Exception as exc:
        logger.debug("parent_chunks split lookup skipped: %s", exc)

    try:
        doc = await db["documents"].find_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"parent_chunks": 1},
        )
    except Exception as exc:
        logger.debug("legacy parent_chunks lookup skipped: %s", exc)
        return []
    if not doc:
        return []
    return doc.get("parent_chunks", [])


async def get_parent_by_id(
    db: AsyncIOMotorDatabase,
    parent_id: str,
    doc_id: str,
    corpus_id: str | None = None,
) -> dict | None:
    """Fetch a single parent chunk by parent_id, with legacy inline fallback."""
    q: dict = {"doc_id": doc_id, "parent_id": parent_id}
    if corpus_id is not None:
        q["corpus_id"] = corpus_id
    try:
        row = await db["parent_chunks"].find_one(q, {"_id": 0})
        if row:
            return row
    except Exception as exc:
        logger.debug("parent_chunks split lookup skipped: %s", exc)

    doc_q: dict = {"doc_id": doc_id, "parent_chunks.parent_id": parent_id}
    if corpus_id is not None:
        doc_q["corpus_id"] = corpus_id
    try:
        doc = await db["documents"].find_one(
            doc_q,
            {"parent_chunks.$": 1},
        )
    except Exception as exc:
        logger.debug("legacy parent lookup skipped: %s", exc)
        return None
    if not doc or not doc.get("parent_chunks"):
        return None
    return doc["parent_chunks"][0]


async def get_chunks(
    db: AsyncIOMotorDatabase,
    chunk_ids: list[str],
    corpus_id: str,
) -> list[dict]:
    """Fetch child chunks by chunk_id list, scoped to corpus."""
    if not chunk_ids:
        return []
    cursor = db["chunks"].find({"chunk_id": {"$in": chunk_ids}, "corpus_id": corpus_id})
    return await cursor.to_list(length=len(chunk_ids))


async def list_all_user_documents(
    db: AsyncIOMotorDatabase,
    user_id: str,
    limit: int = 100,
) -> list[dict]:
    """List all documents across all corpora for a user, sorted by ingested_at desc."""
    cursor = (
        db["documents"]
        .find({"user_id": user_id}, {"_id": 0, "parent_chunks": 0, "ghost_b_staging": 0})
        .sort("ingested_at", -1)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


async def read_ghost_b_staging(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
) -> list[dict] | None:
    """Return Ghost B staging rows, preferring ``ghost_b_extractions``.

    Returns None in both cases: doc missing, or doc present but without the
    staging field (legacy pre-feature document). Callers distinguish via
    write_state flags.
    """
    try:
        rows = await db["ghost_b_extractions"].find(
            {"doc_id": doc_id, "corpus_id": corpus_id, "status": "ok"},
            {"_id": 0},
        ).sort("chunk_id", 1).to_list(length=None)
        if rows:
            return rows
    except Exception as exc:
        logger.debug("ghost_b_extractions split lookup skipped: %s", exc)

    try:
        doc = await db["documents"].find_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"ghost_b_staging": 1},
        )
    except Exception as exc:
        logger.debug("legacy ghost_b_staging lookup skipped: %s", exc)
        return None
    if not doc:
        return None
    return doc.get("ghost_b_staging")


async def read_ghost_b_failures(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
) -> list[dict]:
    """Return Ghost B error rows, with legacy inline fallback."""
    try:
        rows = await db["ghost_b_extractions"].find(
            {"doc_id": doc_id, "corpus_id": corpus_id, "status": "error"},
            {"_id": 0},
        ).sort("chunk_id", 1).to_list(length=None)
        if rows:
            return rows
    except Exception as exc:
        logger.debug("ghost_b_extractions failure lookup skipped: %s", exc)

    try:
        doc = await db["documents"].find_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"ghost_b_failures": 1},
        )
    except Exception as exc:
        logger.debug("legacy ghost_b_failures lookup skipped: %s", exc)
        return []
    return list((doc or {}).get("ghost_b_failures") or [])


async def count_parent_chunks(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
) -> int:
    """Count parents from the split collection, falling back to legacy inline docs."""
    try:
        count = int(
            await db["parent_chunks"].count_documents(
                {"doc_id": doc_id, "corpus_id": corpus_id}
            )
        )
        if count:
            return count
    except Exception as exc:
        logger.debug("parent_chunks count skipped: %s", exc)
    try:
        doc = await db["documents"].find_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"parent_chunks.parent_id": 1},
        )
    except Exception as exc:
        logger.debug("legacy parent count lookup skipped: %s", exc)
        return 0
    parents = (doc or {}).get("parent_chunks") or []
    return len(parents) if isinstance(parents, list) else 0


async def list_documents(
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List all documents in a corpus, scoped to user. Sorted by ingested_at desc.

    Each record is decorated with a `chunk_count` field = number of CHILD
    chunks in the `chunks` collection for that doc. That's what gets embedded
    and searched — the retrieval unit, not the context-hydration unit.
    Separate from ``parent_count`` / parent_chunks rows, which count parent
    hydration units.
    """
    query: dict = {"corpus_id": corpus_id}
    if user_id:
        query["user_id"] = user_id
    # Project out the Mongo _id (ObjectId isn't JSON-serializable by FastAPI's
    # default encoder; the API identifies docs by doc_id anyway).
    cursor = (
        db["documents"]
        .find(query, {"_id": 0, "parent_chunks": 0, "ghost_b_staging": 0})
        .sort("ingested_at", -1)
        .skip(offset)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    # Inject child chunk_count via a single aggregation instead of N separate
    # countDocuments() calls. Cheap on any corpus with a corpus_id + doc_id
    # compound index on chunks (which we have).
    if docs:
        doc_ids = [d["doc_id"] for d in docs]
        pipeline = [
            {"$match": {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}}},
            {"$group": {"_id": "$doc_id", "count": {"$sum": 1}}},
        ]
        counts = {row["_id"]: row["count"] async for row in db["chunks"].aggregate(pipeline)}
        for d in docs:
            d["chunk_count"] = counts.get(d["doc_id"], 0)
    return docs
