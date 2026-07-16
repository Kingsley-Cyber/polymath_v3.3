# backend/db/indexes.py
# MongoDB index definitions for all collections.
# Called once at startup. Idempotent — safe to call on every restart.

import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import OperationFailure

from db.queue_integrity import (
    DURABLE_JOB_COLLECTIONS,
    ensure_durable_job_queue_integrity,
)

logger = logging.getLogger(__name__)


async def _ensure_index(collection, *args, **kwargs):
    """Create an index, retrying if another process aborts the build mid-startup."""
    for attempt in range(4):
        try:
            return await collection.create_index(*args, **kwargs)
        except OperationFailure as exc:
            if exc.code != 276 and "IndexBuildAborted" not in str(exc):
                raise
            if attempt == 3:
                raise
            delay = 0.5 * (attempt + 1)
            logger.warning(
                "Mongo index build aborted for %s; retrying in %.1fs: %s",
                collection.name,
                delay,
                exc,
            )
            await asyncio.sleep(delay)


async def _drop_legacy_single_field_unique_index(collection, field: str) -> list[str]:
    """Retire every unique index whose complete key is one content-ID field."""

    dropped: list[str] = []
    existing = await collection.index_information()
    for index_name, index_info in existing.items():
        index_keys = tuple(tuple(value) for value in index_info.get("key", []))
        if index_info.get("unique") and index_keys == ((field, 1),):
            await collection.drop_index(index_name)
            dropped.append(str(index_name))
    return dropped


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
    await _ensure_index(db["conversations"], "updated_at")
    await _ensure_index(db["conversations"], "created_at")
    logger.info("Indexes ensured: conversations")

    # --- messages ---
    await _ensure_index(db["messages"], "conversation_id")
    await _ensure_index(
        db["messages"],
        [("conversation_id", 1), ("created_at", 1)],
        name="messages_conv_time",
    )
    logger.info("Indexes ensured: messages")

    # --- users ---
    await _ensure_index(db["users"], "username", unique=True)
    logger.info("Indexes ensured: users (unique username)")

    # --- corpora ---
    await _ensure_index(db["corpora"], "corpus_id", unique=True)
    await _ensure_index(db["corpora"], "user_id")
    logger.info("Indexes ensured: corpora")

    # --- documents ---
    # doc_id is content-hashed — not globally unique when the same file lands
    # in two corpora. Unique key is the compound (corpus_id, doc_id). Drop any
    # legacy "doc_id_1" unique index from pre-refactor DBs; safe on fresh DBs.
    for index_name in await _drop_legacy_single_field_unique_index(
        db["documents"], "doc_id"
    ):
        logger.info("Dropped legacy global document identity index: %s", index_name)
    await _ensure_index(
        db["documents"],
        [("corpus_id", 1), ("doc_id", 1)],
        unique=True,
        name="corpus_doc_unique",
    )
    await _ensure_index(db["documents"], "doc_id")  # non-unique cross-corpus lookup
    await _ensure_index(db["documents"], "corpus_id")
    await _ensure_index(db["documents"], "user_id")
    logger.info("Indexes ensured: documents")

    # --- chunks ---
    # Same rationale as documents: chunk_id is derived from content-hashed
    # doc_id, so uniqueness must include corpus_id.
    for index_name in await _drop_legacy_single_field_unique_index(
        db["chunks"], "chunk_id"
    ):
        logger.info("Dropped legacy global chunk identity index: %s", index_name)
    await _ensure_index(
        db["chunks"],
        [("corpus_id", 1), ("chunk_id", 1)],
        unique=True,
        name="corpus_chunk_unique",
    )
    await _ensure_index(db["chunks"], "chunk_id")  # non-unique cross-corpus lookup
    await _ensure_index(db["chunks"], "parent_id")
    await _ensure_index(db["chunks"], "doc_id")
    await _ensure_index(db["chunks"], "user_id")
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
            logger.info(
                "Dropped legacy chunks_text_search index (language_override mismatch)"
            )
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("Could not check legacy chunks text index: %s", exc)
    try:
        await _ensure_index(
            db["chunks"],
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
    await _ensure_index(
        db["parent_chunks"],
        [("corpus_id", 1), ("doc_id", 1), ("parent_id", 1)],
        unique=True,
        name="corpus_doc_parent_unique",
    )
    await _ensure_index(
        db["parent_chunks"],
        [("corpus_id", 1), ("doc_id", 1)],
        name="parent_chunks_doc",
    )
    await _ensure_index(db["parent_chunks"], "parent_id")
    logger.info("Indexes ensured: parent_chunks")

    # --- summary_tree ---
    # node_id is content-derived and therefore repeats when the same source is
    # intentionally ingested into multiple corpora.  The durable tree instance
    # is identified by (corpus_id, node_id), exactly like documents/chunks.
    # Retire any legacy single-field UNIQUE index before creating the compound
    # identity.  Do not assume the legacy index kept Mongo's default name: a
    # deployment may have named it explicitly.
    for index_name in await _drop_legacy_single_field_unique_index(
        db["summary_tree"], "node_id"
    ):
        logger.info(
            "Dropped legacy global summary-tree identity index: %s",
            index_name,
        )
    await _ensure_index(
        db["summary_tree"],
        [("corpus_id", 1), ("node_id", 1)],
        unique=True,
        name="summary_tree_corpus_node_unique",
    )
    await _ensure_index(db["summary_tree"], "node_id")
    await _ensure_index(
        db["summary_tree"],
        [("corpus_id", 1), ("doc_id", 1), ("node_type", 1)],
        name="summary_tree_corpus_doc_type",
    )
    logger.info("Indexes ensured: summary_tree")

    # --- ghost_b_extractions ---
    # One durable Ghost B checkpoint per child chunk. Successful rows feed
    # graph backfill; error rows can be retried independently.
    await _ensure_index(
        db["ghost_b_extractions"],
        [("corpus_id", 1), ("doc_id", 1), ("chunk_id", 1)],
        unique=True,
        name="corpus_doc_chunk_extraction_unique",
    )
    await _ensure_index(
        db["ghost_b_extractions"],
        [("corpus_id", 1), ("doc_id", 1), ("status", 1)],
        name="ghost_b_extractions_doc_status",
    )
    await _ensure_index(db["ghost_b_extractions"], "chunk_id")
    logger.info("Indexes ensured: ghost_b_extractions")

    # --- relation support records ---
    # Canonical per-chunk support rows for graph relations. These are replaced
    # per document during graph promotion and filtered by corpus during delete.
    await _ensure_index(
        db["relation_support_records"],
        "support_id",
        unique=True,
        name="relation_support_support_id_unique",
    )
    await _ensure_index(
        db["relation_support_records"],
        [("corpus_id", 1), ("doc_id", 1), ("status", 1)],
        name="relation_support_doc_status",
    )
    await _ensure_index(
        db["relation_support_records"],
        [("corpus_id", 1), ("status", 1)],
        name="relation_support_corpus_status",
    )
    await _ensure_index(
        db["relation_support_records"],
        [("edge_key", 1), ("status", 1), ("corpus_id", 1)],
        name="relation_support_edge_status",
    )
    await _ensure_index(db["relation_support_records"], "chunk_id")
    logger.info("Indexes ensured: relation_support_records")

    # --- ghost_b_error_events ---
    # Sampled forensic rows for Ghost B extraction failures. These are small by
    # design: no child text, only raw output snippets plus failure metadata.
    await _ensure_index(db["ghost_b_error_events"], "run_id")
    await _ensure_index(db["ghost_b_error_events"], "doc_id")
    await _ensure_index(
        db["ghost_b_error_events"],
        [("corpus_id", 1), ("doc_id", 1), ("created_at", -1)],
        name="ghost_b_error_doc_time",
    )
    await _ensure_index(
        db["ghost_b_error_events"],
        [("event", 1), ("created_at", -1)],
        name="ghost_b_error_event_time",
    )
    await _ensure_index(
        db["ghost_b_error_events"],
        [("corpus_id", 1), ("created_at", -1), ("event", 1)],
        name="ghost_b_error_corpus_time_event",
    )
    logger.info("Indexes ensured: ghost_b_error_events")

    # --- ingest batches ---
    await _ensure_index(db["ingest_batches"], "batch_id", unique=True)
    await _ensure_index(db["ingest_batches"], [("user_id", 1), ("created_at", -1)])
    await _ensure_index(db["ingest_batches"], [("corpus_id", 1), ("created_at", -1)])
    await _ensure_index(db["ingest_batch_items"], "item_id", unique=True)
    await _ensure_index(
        db["ingest_batch_items"],
        [("batch_id", 1), ("ordinal", 1)],
        name="ingest_batch_items_order",
    )
    await _ensure_index(
        db["ingest_batch_items"],
        [("batch_id", 1), ("status", 1), ("ordinal", 1)],
        name="ingest_batch_items_status_order",
    )
    await _ensure_index(
        db["ingest_batch_items"],
        [("status", 1), ("lease_until", 1)],
        name="ingest_batch_items_stale_lease",
    )
    await _ensure_index(db["ingest_batch_items"], "doc_id")
    logger.info("Indexes ensured: ingest_batches / ingest_batch_items")

    # --- durable ingestion repair queues ---
    # These collections are the production read models behind corpus readiness:
    # every repair cycle counts by corpus/status, claims queued jobs by
    # updated_at/lease_until, and reconciles stale rows by deterministic
    # job_id/stage_identity. Without these indexes the control plane works on
    # toy corpora but degrades into collection scans on large libraries.
    # Planners, claimers, and reconcilers all use deterministic job_id as the
    # queue identity. Repair historical duplicates before any runner starts,
    # then make that invariant enforceable by Mongo rather than convention.
    await ensure_durable_job_queue_integrity(db)
    for name in DURABLE_JOB_COLLECTIONS:
        await _ensure_index(
            db[name],
            [("corpus_id", 1), ("status", 1), ("updated_at", 1)],
            name=f"{name}_corpus_status_updated",
        )
        await _ensure_index(
            db[name],
            [("status", 1), ("lease_until", 1), ("updated_at", 1)],
            name=f"{name}_status_lease_updated",
        )
        await _ensure_index(
            db[name],
            [("corpus_id", 1), ("doc_id", 1), ("status", 1)],
            name=f"{name}_corpus_doc_status",
        )
        await _ensure_index(
            db[name],
            [("corpus_id", 1), ("stage_identity.stage_key", 1)],
            name=f"{name}_stage_key",
            sparse=True,
        )
        await _ensure_index(
            db[name],
            [("corpus_id", 1), ("stage_identity.source_file_hash", 1)],
            name=f"{name}_source_file_hash",
            sparse=True,
        )
    await _ensure_index(
        db["extraction_jobs"],
        [("corpus_id", 1), ("chunk_id", 1), ("status", 1)],
        name="extraction_jobs_corpus_chunk_status",
    )
    await _ensure_index(
        db["summary_jobs"],
        [("corpus_id", 1), ("kind", 1), ("parent_id", 1), ("status", 1)],
        name="summary_jobs_parent_status",
        sparse=True,
    )
    await _ensure_index(
        db["summary_jobs"],
        [("corpus_id", 1), ("kind", 1), ("doc_id", 1), ("status", 1)],
        name="summary_jobs_doc_status",
    )
    logger.info("Indexes ensured: durable ingestion repair queues")

    # --- settings ---
    await _ensure_index(db["settings"], "user_id", unique=True)
    logger.info("Indexes ensured: settings (unique user_id)")

    # --- model_profiles (Phase 19.3 — custom chat model profiles) ---
    await _ensure_index(db["model_profiles"], "profile_id", unique=True)
    await _ensure_index(db["model_profiles"], "user_id")
    logger.info("Indexes ensured: model_profiles")

    # --- model_pool (Phase E — unified model pool) ---
    await _ensure_index(db["model_pool"], "entry_id", unique=True)
    await _ensure_index(db["model_pool"], "user_id")
    await _ensure_index(db["model_pool"], [("user_id", 1), ("enabled", 1)])
    logger.info("Indexes ensured: model_pool")

    # --- user_query_preferences (Phase F — per-user role→pool mappings + ollama exclusions) ---
    await _ensure_index(db["user_query_preferences"], "user_id", unique=True)
    logger.info("Indexes ensured: user_query_preferences (unique user_id)")
