"""
MongoDB reader — scoped read operations for retrieval and hydration.

All queries scope by corpus_id to prevent cross-corpus bleed.
"""

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


def _fallback_decision_trace(doc: dict[str, Any]) -> dict[str, Any]:
    ws = doc.get("write_state") or {}
    metrics = doc.get("ghost_b_metrics") or {}
    chunking = doc.get("chunking_config") or {}
    budgets = chunking.get("token_budgets") or {}
    vector_ready = bool(
        ws.get("vector_ready") or (ws.get("mongo_written") and ws.get("qdrant_written"))
    )
    graph_status = str(
        ws.get("graph_status")
        or ("graph_ready" if ws.get("neo4j_written") else "graph_pending")
    )
    parent_strategy = str(chunking.get("parent_strategy") or "unknown")
    graph_strategy = str(metrics.get("extraction_strategy") or "unknown")
    skipped = int(metrics.get("skipped_low_value_chunks") or 0)
    reasons: list[str] = []
    if parent_strategy == "pdf_page_grouped":
        reasons.append("PDF pages were grouped into token-sized parents.")
    elif parent_strategy.startswith("heading_bound"):
        reasons.append("Document headings were preserved as parent boundaries.")
    elif parent_strategy == "token_window":
        reasons.append("Weak document structure used token-window chunking.")
    if skipped:
        reasons.append(f"{skipped} low-value chunk(s) were skipped for graph extraction.")
    if vector_ready:
        reasons.append("Mongo and Qdrant are ready for vector/chat retrieval.")
    return {
        "file_profile": str(doc.get("source_mime") or "document"),
        "source_mime": str(doc.get("source_mime") or ""),
        "source_tier": str(doc.get("source_tier") or ""),
        "parser_strategy": "unknown_legacy",
        "structure_quality": "unknown",
        "chunking_strategy": parent_strategy,
        "child_strategy": str(chunking.get("child_strategy") or "unknown"),
        "parent_count": len(doc.get("parent_chunks") or []),
        "child_count": int(doc.get("chunk_count") or 0),
        "parent_target_tokens": int(budgets.get("parent_target") or 0),
        "child_target_tokens": int(budgets.get("child_target") or 0),
        "low_value_chunk_count": skipped,
        "low_value_chunk_kinds": metrics.get("skipped_low_value_by_kind") or {},
        "vector_ready": vector_ready,
        "graph_status": graph_status,
        "graph_strategy": graph_strategy,
        "graph_mode": str(metrics.get("extraction_mode") or "unknown"),
        "graph_completeness": str(metrics.get("graph_completeness") or ""),
        "reasons": reasons or ["Legacy document; decision trace was derived from stored metadata."],
        "warnings": ["Derived fallback trace for a document ingested before decision traces existed."],
    }


def _decision_trace_summary(trace: dict[str, Any]) -> str:
    chunking = str(trace.get("chunking_strategy") or "auto chunking").replace("_", " ")
    graph = str(trace.get("graph_strategy") or "graph policy").replace("_", " ")
    skipped = int(trace.get("low_value_chunk_count") or 0)
    parts = [chunking, graph]
    if skipped:
        parts.append(f"{skipped} low-value chunks skipped")
    return " - ".join(parts)


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
    """Return the parent_chunks inline array from a document record."""
    doc = await db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"parent_chunks": 1},
    )
    if not doc:
        return []
    return doc.get("parent_chunks", [])


async def get_parent_by_id(
    db: AsyncIOMotorDatabase,
    parent_id: str,
    doc_id: str,
) -> dict | None:
    """Fetch a single parent chunk by parent_id from the inline array."""
    doc = await db["documents"].find_one(
        {"doc_id": doc_id, "parent_chunks.parent_id": parent_id},
        {"parent_chunks.$": 1},
    )
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
        .find({"user_id": user_id}, {"_id": 0})
        .sort("ingested_at", -1)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


async def read_ghost_b_staging(
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
) -> list[dict] | None:
    """Return `ghost_b_staging` from the document record, or None if absent.

    Returns None in both cases: doc missing, or doc present but without the
    staging field (legacy pre-feature document). Callers distinguish via
    write_state flags.
    """
    doc = await db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"ghost_b_staging": 1},
    )
    if not doc:
        return None
    return doc.get("ghost_b_staging")


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
    Separate from `parent_chunks[].length` which is the inline parent count.
    """
    query: dict = {"corpus_id": corpus_id}
    if user_id:
        query["user_id"] = user_id
    # Project out the Mongo _id (ObjectId isn't JSON-serializable by FastAPI's
    # default encoder; the API identifies docs by doc_id anyway).
    cursor = (
        db["documents"]
        .find(query, {"_id": 0})
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
            if not d.get("decision_trace"):
                trace = _fallback_decision_trace(d)
                d["decision_trace"] = trace
                d["decision_trace_summary"] = _decision_trace_summary(trace)
    return docs
