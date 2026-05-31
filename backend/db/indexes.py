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
    # CRITICAL: language_override="_text_language" (a field that never exists
    # in our chunk docs) disables MongoDB's per-doc stemmer-override behavior.
    # Without this, MongoDB looks at the chunk's `language` field (which we
    # use for code-lane semantics: "luau", "python", "tsx", etc.) and treats
    # it as a stemmer hint — exploding the bulk write with code 17262
    # "language override unsupported: luau" because our domain languages
    # aren't in the snowball stemmer whitelist (en, fr, de, es, ru, ...).
    # The index is rebuilt if the existing one used the default override so
    # the fix takes effect on already-deployed instances.
    try:
        existing = await db["chunks"].index_information()
        info = existing.get("chunks_text_search")
        if info and info.get("language_override", "language") != "_text_language":
            await db["chunks"].drop_index("chunks_text_search")
            logger.info("Dropped legacy chunks_text_search index (language_override mismatch)")
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("Could not check legacy chunks text index: %s", exc)
    try:
        await db["chunks"].create_index(
            [("text", "text"), ("heading_path", "text")],
            name="chunks_text_search",
            weights={"heading_path": 5, "text": 1},
            default_language="english",
            language_override="_text_language",
        )
    except Exception as exc:
        logger.warning("Could not create chunks text index: %s", exc)
    logger.info("Indexes ensured: chunks")

    # --- parent_chunks ---
    # Parent rows are the hydration/summarization unit. They used to live
    # inline in documents.parent_chunks, which breaks on large books.
    await db["parent_chunks"].create_index(
        [("corpus_id", 1), ("doc_id", 1), ("parent_id", 1)],
        unique=True,
        name="corpus_doc_parent_unique",
    )
    await db["parent_chunks"].create_index(
        [("corpus_id", 1), ("doc_id", 1)],
        name="parent_chunks_doc",
    )
    await db["parent_chunks"].create_index("parent_id")
    logger.info("Indexes ensured: parent_chunks")

    # --- ghost_b_extractions ---
    # One durable Ghost B checkpoint per child chunk. Successful rows feed
    # graph backfill; error rows can be retried independently.
    await db["ghost_b_extractions"].create_index(
        [("corpus_id", 1), ("doc_id", 1), ("chunk_id", 1)],
        unique=True,
        name="corpus_doc_chunk_extraction_unique",
    )
    await db["ghost_b_extractions"].create_index(
        [("corpus_id", 1), ("doc_id", 1), ("status", 1)],
        name="ghost_b_extractions_doc_status",
    )
    await db["ghost_b_extractions"].create_index("chunk_id")
    logger.info("Indexes ensured: ghost_b_extractions")

    # --- ghost_b_error_events ---
    # Sampled forensic rows for Ghost B extraction failures. These are small by
    # design: no child text, only raw output snippets plus failure metadata.
    await db["ghost_b_error_events"].create_index("run_id")
    await db["ghost_b_error_events"].create_index("doc_id")
    await db["ghost_b_error_events"].create_index(
        [("corpus_id", 1), ("doc_id", 1), ("created_at", -1)],
        name="ghost_b_error_doc_time",
    )
    await db["ghost_b_error_events"].create_index(
        [("event", 1), ("created_at", -1)],
        name="ghost_b_error_event_time",
    )
    logger.info("Indexes ensured: ghost_b_error_events")

    # --- ingest batches ---
    await db["ingest_batches"].create_index("batch_id", unique=True)
    await db["ingest_batches"].create_index([("user_id", 1), ("created_at", -1)])
    await db["ingest_batches"].create_index([("corpus_id", 1), ("created_at", -1)])
    await db["ingest_batch_items"].create_index("item_id", unique=True)
    await db["ingest_batch_items"].create_index(
        [("batch_id", 1), ("ordinal", 1)],
        name="ingest_batch_items_order",
    )
    await db["ingest_batch_items"].create_index(
        [("batch_id", 1), ("status", 1), ("ordinal", 1)],
        name="ingest_batch_items_status_order",
    )
    await db["ingest_batch_items"].create_index(
        [("status", 1), ("lease_until", 1)],
        name="ingest_batch_items_stale_lease",
    )
    await db["ingest_batch_items"].create_index("doc_id")
    logger.info("Indexes ensured: ingest_batches / ingest_batch_items")

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
