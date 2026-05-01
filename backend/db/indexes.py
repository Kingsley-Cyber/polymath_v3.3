# backend/db/indexes.py
# MongoDB index definitions for all collections.
# Called once at startup. Idempotent — safe to call on every restart.

import logging

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


async def create_all_indexes(db: AsyncIOMotorDatabase) -> None:
    """
    Create all MongoDB indexes across both collections.

    conversations:
        - updated_at  (list ordering, most-recent-first)
        - created_at  (time-range queries)

    messages:
        - conversation_id            (primary foreign-key lookup)
        - (conversation_id, created_at)  (ordered history retrieval)
    """
    # --- conversations ---
    await db["conversations"].create_index("updated_at")
    await db["conversations"].create_index("created_at")
    logger.info("Indexes ensured: conversations")

    # --- messages ---
    await db["messages"].create_index("conversation_id")
    await db["messages"].create_index(
        [("conversation_id", 1), ("created_at", 1)],
        name="messages_conv_time",
    )
    logger.info("Indexes ensured: messages")

    # --- users ---
    await db["users"].create_index("username", unique=True)
    logger.info("Indexes ensured: users (unique username)")

    # --- corpora ---
    await db["corpora"].create_index("corpus_id", unique=True)
    await db["corpora"].create_index("user_id")
    logger.info("Indexes ensured: corpora")

    # --- documents ---
    # doc_id is content-hashed — not globally unique when the same file lands
    # in two corpora. Unique key is the compound (corpus_id, doc_id). Drop any
    # legacy "doc_id_1" unique index from pre-refactor DBs; safe on fresh DBs.
    try:
        await db["documents"].drop_index("doc_id_1")
    except Exception:
        pass
    await db["documents"].create_index(
        [("corpus_id", 1), ("doc_id", 1)], unique=True, name="corpus_doc_unique"
    )
    await db["documents"].create_index("doc_id")  # non-unique cross-corpus lookup
    await db["documents"].create_index("corpus_id")
    await db["documents"].create_index("user_id")
    logger.info("Indexes ensured: documents")

    # --- chunks ---
    # Same rationale as documents: chunk_id is derived from content-hashed
    # doc_id, so uniqueness must include corpus_id.
    try:
        await db["chunks"].drop_index("chunk_id_1")
    except Exception:
        pass
    await db["chunks"].create_index(
        [("corpus_id", 1), ("chunk_id", 1)], unique=True, name="corpus_chunk_unique"
    )
    await db["chunks"].create_index("chunk_id")  # non-unique cross-corpus lookup
    await db["chunks"].create_index("parent_id")
    await db["chunks"].create_index("doc_id")
    await db["chunks"].create_index("user_id")
    try:
        await db["chunks"].create_index(
            [("text", "text"), ("heading_path", "text")],
            name="chunks_text_search",
            weights={"heading_path": 5, "text": 1},
            default_language="english",
        )
    except Exception as exc:
        logger.warning("Could not create chunks text index: %s", exc)
    logger.info("Indexes ensured: chunks")

    # --- settings ---
    await db["settings"].create_index("user_id", unique=True)
    logger.info("Indexes ensured: settings (unique user_id)")

    # --- model_profiles (Phase 19.3 — custom chat model profiles) ---
    await db["model_profiles"].create_index("profile_id", unique=True)
    await db["model_profiles"].create_index("user_id")
    logger.info("Indexes ensured: model_profiles")

    # --- model_pool (Phase E — unified model pool) ---
    await db["model_pool"].create_index("entry_id", unique=True)
    await db["model_pool"].create_index("user_id")
    await db["model_pool"].create_index([("user_id", 1), ("enabled", 1)])
    logger.info("Indexes ensured: model_pool")

    # --- user_query_preferences (Phase F — per-user role→pool mappings + ollama exclusions) ---
    await db["user_query_preferences"].create_index("user_id", unique=True)
    logger.info("Indexes ensured: user_query_preferences (unique user_id)")

    # --- ingestion_batches / ingestion_batch_items ---
    await db["ingestion_batches"].create_index("batch_id", unique=True)
    await db["ingestion_batches"].create_index("corpus_id")
    await db["ingestion_batches"].create_index("user_id")
    await db["ingestion_batches"].create_index([("status", 1), ("updated_at", 1)])
    await db["ingestion_batch_items"].create_index("upload_id", unique=True)
    await db["ingestion_batch_items"].create_index("batch_id")
    await db["ingestion_batch_items"].create_index([("batch_id", 1), ("status", 1)])
    await db["ingestion_batch_items"].create_index([("corpus_id", 1), ("content_hash", 1)])
    logger.info("Indexes ensured: ingestion batches")
