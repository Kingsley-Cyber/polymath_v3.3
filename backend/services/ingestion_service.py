"""
IngestionService — lifecycle manager for ingestion pipeline clients.

Owns:
  - AsyncQdrantClient (Qdrant)
  - Optional AsyncDriver (Neo4j)
Borrows:
  - AsyncIOMotorDatabase (shared from conversation_service._db at connect time)

Usage in main.py lifespan:
    await ingestion_service.connect(conversation_service._db)
    ...
    await ingestion_service.disconnect()
"""

import asyncio
import inspect
import logging
import hashlib
import mimetypes
import uuid
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Optional

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient
from pymongo import ReturnDocument
from services.ingestion.section_classifier import parent_summary_required_clause
from services.storage.record_status import with_active_records

logger = logging.getLogger(__name__)

STRONG_SOURCE_KINDS: frozenset[str] = frozenset(
    {"youtube_video", "url", "content_hash"}
)
QUERYABLE_INGEST_STAGES: frozenset[str] = frozenset(
    {
        "complete",
        "fully_enriched",
        "queryable_with_pending_graph",
        "queryable_with_pending_summary",
        "queryable_with_pending_summary_and_graph",
    }
)
CORPUS_CLEANUP_LEASE_MINUTES = 30


# ── Frozen / mutable field partition ───────────────────────────────────────
#
# Every IngestionConfig field lives in exactly one of these two sets. The
# worker snapshots ONLY frozen fields onto each document record via
# `freeze_snapshot()`; mutable fields are read live from the corpus record at
# ingest time. `update_corpus` rejects patches to frozen fields once the
# corpus has any ingested documents (doc_count > 0 → HTTP 409).
#
# Invariants enforced by tests (see test_frozen_mutable_split.py):
#   - FROZEN.isdisjoint(MUTABLE)
#   - FROZEN | MUTABLE == set(IngestionConfig.model_fields)

FROZEN_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "embedding_model",
        "embedding_dimension",
        "embedding_model_id",
        "parent_chunk_tokens",
        "child_chunk_tokens",
        "chunk_overlap",
        "child_chunk_algorithm",
        "semantic_split_threshold",
        "max_summary_tokens",
        "use_neo4j",
        "chunk_summarization",
        "target_qdrant_collections",
        "entity_schema",
        "relation_schema",
        "schema_strict",
        "docling_ocr_enabled",
        "preset",
    }
)

MUTABLE_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "embed_mode",
        "embed_base_url",
        "embed_api_key",
        "embed_max_concurrent",
        "embedding_models",
        "modal_containers",
        "summary_models",
        "extraction_models",
        "extraction_engine",
        "entity_confidence_threshold",
        "models_linked",
    }
)


class FrozenFieldError(ValueError):
    """Raised by update_corpus when a patch touches FROZEN fields on a
    non-empty corpus. Router maps this to HTTP 409 with a structured body.

    Attributes:
        fields: the frozen field names the caller attempted to change
        doc_count: current ingested-document count on the corpus
    """

    def __init__(self, fields: list[str], doc_count: int) -> None:
        self.fields = sorted(set(fields))
        self.doc_count = doc_count
        super().__init__(
            f"Corpus has {doc_count} ingested documents. Frozen fields can "
            f"only be changed on an empty corpus. Attempted: {self.fields}."
        )


def freeze_snapshot(config: IngestionConfig) -> dict:
    """Return the dict to persist onto a document record — frozen fields only.

    Mutable provider-wiring fields (embed_*, summary_models, extraction_models,
    extraction_engine, entity_confidence_threshold, models_linked,
    modal_containers) are
    deliberately excluded. The worker re-reads them live from the corpus on
    every ingest, so freezing them onto the doc snapshot would create two
    sources of truth and silently ignore user edits.
    """
    dump = config.model_dump()
    return {k: v for k, v in dump.items() if k in FROZEN_CONFIG_FIELDS}


def build_effective_config(
    *,
    frozen_base: dict,
    live_corpus: dict,
    ingest_overrides: dict | None = None,
) -> IngestionConfig:
    """Compose the effective IngestionConfig for a single ingest.

    Precedence (lowest → highest):
        frozen_base        — structural identity (doc snapshot on resume,
                             else corpus.default_ingestion_config)
        live_corpus        — mutable fields read live from corpus record
                             (embed_mode, pools, etc.)
        ingest_overrides   — ephemeral per-ingest overrides from the router

    The returned IngestionConfig is the single source of truth for the rest
    of the ingest job. Ingest overrides are NOT persisted.
    """
    merged: dict = {}
    # Start with frozen structural baseline
    for k in FROZEN_CONFIG_FIELDS:
        if k in frozen_base:
            merged[k] = frozen_base[k]
    # Overlay mutable fields from the live corpus
    for k in MUTABLE_CONFIG_FIELDS:
        if k in live_corpus:
            merged[k] = live_corpus[k]
    # Overlay per-ingest overrides (any field, frozen or mutable — the router
    # is responsible for only sending mutable overrides on non-empty corpora)
    if ingest_overrides:
        for k, v in ingest_overrides.items():
            if v is None:
                continue
            merged[k] = v
    return IngestionConfig(**merged)


async def _call_ingest_callback(callback: Any, *args: Any) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


def exact_source_duplicate_query(
    *,
    corpus_id: str,
    source_identity: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the exact-source duplicate query for strong source identities.

    Filename-only identity is intentionally excluded: it is useful for audit,
    but too weak to skip ingestion. URL/video ids and byte-content hashes are
    deterministic enough to prevent redundant parse/chunk/embed/extract work.
    """

    identity = source_identity or {}
    source_key = str(identity.get("source_key") or "").strip()
    source_kind = str(identity.get("source_kind") or "").strip()
    content_sha = str(identity.get("content_sha256") or "").strip()
    clauses: list[dict[str, Any]] = []
    # Byte/content identity is stricter than URL identity. Some crawled/exported
    # sources collapse many distinct files to the same channel/page URL; when a
    # content hash is available, never skip by source_key alone.
    if not content_sha and source_key and source_kind in STRONG_SOURCE_KINDS:
        clauses.extend(
            [
                {"source_key": source_key},
                {"source_identity.source_key": source_key},
            ]
        )
    if content_sha:
        clauses.extend(
            [
                {"source_identity.content_sha256": content_sha},
                {"content_sha256": content_sha},
                {"source_file_hash": content_sha},
            ]
        )
    if not clauses:
        return None
    return with_active_records(
        {
            "corpus_id": corpus_id,
            "ingest_stage": {
                "$nin": ["skipped_duplicate", "skipped_nonsemantic"]
            },
            "$and": [
                {
                    "$or": [
                        {"write_state.qdrant_written": True},
                        {"ingest_stage": {"$in": sorted(QUERYABLE_INGEST_STAGES)}},
                    ]
                }
            ],
            "$or": clauses,
        }
    )


def _summary_backfill_index_scope(
    *,
    generate: bool,
    limit: int | None,
    generated_parent_ids: list[str],
    summary_text_clause: dict,
    bounded_by_doc_ids: bool = False,
) -> tuple[str, list[dict]]:
    """Return the summary-index query scope for a backfill call.

    Operator probes often call ``generate=True, index=True, limit=N``. In that
    mode, indexing must stay bounded to the summaries generated in the same
    call. Full-corpus reindexing is still available through explicit index-only
    maintenance runs.

    A doc-scoped post-batch backfill is already bounded by the batch's doc_ids,
    so it may safely index every existing summary for those docs even when the
    generation leg is capped.
    """

    clauses: list[dict] = [summary_text_clause]
    if generate and limit is not None and not bounded_by_doc_ids:
        return "generated_in_call", [
            *clauses,
            {"parent_id": {"$in": generated_parent_ids}},
        ]
    if bounded_by_doc_ids:
        return "doc_scope_existing_summaries", clauses
    return "all_existing_summaries", clauses


# ── Preset normalization ───────────────────────────────────────────────────
#
# `IngestionConfig.preset` is a convenience shortcut. apply_preset() rewrites
# use_neo4j / chunk_summarization / target_qdrant_collections to match the
# chosen preset. "custom" is a sentinel that means "trust whatever the caller
# sent" — the toggles flow through unchanged.
#
# Kept as a module-level pure function (no IngestionService state) so it can
# be unit-tested in isolation and reused from the router or from tests.

_PRESET_MAP: dict[str, dict] = {
    "fast": {
        "use_neo4j": False,
        "chunk_summarization": False,
        "target_qdrant_collections": ["naive", "hrag"],
    },
    "balanced": {
        "use_neo4j": True,
        "chunk_summarization": False,
        "target_qdrant_collections": ["naive", "hrag", "graph"],
    },
    "deep": {
        "use_neo4j": True,
        "chunk_summarization": True,
        "target_qdrant_collections": ["naive", "hrag", "graph"],
    },
}


def apply_preset(config: IngestionConfig) -> IngestionConfig:
    """Normalize toggles according to the preset.

    'fast' / 'balanced' / 'deep' overwrite use_neo4j, chunk_summarization,
    and target_qdrant_collections so the stored config always matches what
    the preset promises. 'custom' returns the config unchanged.
    Returns a new IngestionConfig (via model_copy) — never mutates input.
    """
    preset = getattr(config, "preset", "custom") or "custom"
    if preset == "custom":
        return config
    overrides = _PRESET_MAP.get(preset)
    if overrides is None:
        logger.warning("apply_preset: unknown preset %r — treating as custom", preset)
        return config
    return config.model_copy(update=overrides)


class IngestionService:
    def __init__(self) -> None:
        self._db: Optional[AsyncIOMotorDatabase] = None
        self._qdrant: Optional[AsyncQdrantClient] = None
        self._neo4j = None  # neo4j.AsyncDriver when NEO4J_ENABLED
        self._settings = get_settings()
        self._cleanup_owner = f"corpus-cleanup:{uuid.uuid4()}"
        self._cleanup_tasks: set[asyncio.Task] = set()

    async def connect(self, db: AsyncIOMotorDatabase) -> None:
        """Called from lifespan startup. Receives the shared MongoDB db instance.

        Phase 7.5 — global polymath_* collections are no longer auto-created
        on boot. Each corpus owns its own family of 4 collections, provisioned
        lazily by `create_corpus`. The migration script handles existing data.
        """
        self._db = db
        self._qdrant = AsyncQdrantClient(
            url=self._settings.QDRANT_URL,
            timeout=self._settings.QDRANT_TIMEOUT_SECONDS,
        )
        logger.info("IngestionService: Qdrant connected (per-corpus collections)")
        await self._ensure_source_identity_indexes()

        if self._settings.NEO4J_ENABLED:
            from neo4j import AsyncGraphDatabase

            self._neo4j = AsyncGraphDatabase.driver(
                self._settings.NEO4J_URI,
                auth=(self._settings.NEO4J_USER, self._settings.NEO4J_PASSWORD),
            )
            logger.info("IngestionService: Neo4j connected")

        try:
            recovered = await self.recover_pending_corpus_purges(limit=4)
            if recovered:
                logger.info("Recovered %d durable corpus purge(s)", recovered)
        except Exception as exc:
            logger.warning("Durable corpus purge recovery skipped: %s", exc)

        # Retrieval readiness repair — ensure every existing corpus has the
        # Qdrant collection/index layout for its frozen embedding dimension and
        # ensure Neo4j retrieval indexes when graph is enabled. Non-fatal at
        # startup; the ingest worker enforces this per document before writes.
        try:
            from services.retrieval_readiness import (
                repair_retrieval_readiness_for_all_corpora,
            )

            readiness = await repair_retrieval_readiness_for_all_corpora(
                db=db,
                qdrant_client=self._qdrant,
                neo4j_driver=self._neo4j,
                neo4j_enabled=self._settings.NEO4J_ENABLED,
                default_dim=self._settings.EMBEDDING_DIMENSION,
            )
            logger.info(
                "Retrieval readiness repair: scanned=%d ready=%d failed=%d neo4j_schema_ready=%s",
                readiness["scanned"],
                readiness["ready"],
                readiness["failed"],
                readiness["neo4j_schema_ready"],
            )
            if readiness["failed"]:
                logger.warning(
                    "Retrieval readiness repair failures: %s",
                    readiness["reports"],
                )
        except Exception as exc:
            logger.warning("Retrieval readiness repair sweep skipped: %s", exc)

    @property
    def neo4j_driver(self):
        """Expose Neo4j async driver for graph router."""
        return self._neo4j

    @property
    def qdrant_client(self):
        """Expose the shared AsyncQdrantClient for read-only callers
        (Mission Control domain emergence). Do not write to collections
        from outside the ingestion pipeline."""
        return self._qdrant

    @property
    def db(self):
        """Expose the shared Motor database handle."""
        return self._db

    async def _ensure_source_identity_indexes(self) -> None:
        """Indexes for deterministic agent/source duplicate guardrails."""
        if self._db is None:
            return
        try:
            await self._db["documents"].create_index(
                [("source_key", 1), ("corpus_id", 1)],
                name="documents_source_key_corpus_idx",
                sparse=True,
                background=True,
            )
            await self._db["documents"].create_index(
                [("source_identity.source_key", 1), ("corpus_id", 1)],
                name="documents_source_identity_key_corpus_idx",
                sparse=True,
                background=True,
            )
            await self._db["documents"].create_index(
                [("source_identity.content_sha256", 1), ("corpus_id", 1)],
                name="documents_source_identity_content_sha_corpus_idx",
                sparse=True,
                background=True,
            )
            await self._db["documents"].create_index(
                [("youtube_video_id", 1), ("corpus_id", 1)],
                name="documents_youtube_video_corpus_idx",
                sparse=True,
                background=True,
            )
            from services.ingestion.corpus_lexicon import ensure_lexicon_indexes

            await ensure_lexicon_indexes(self._db)
        except Exception as exc:
            logger.warning("Source identity index setup skipped: %s", exc)

    async def disconnect(self) -> None:
        for task in tuple(self._cleanup_tasks):
            task.cancel()
        if self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)
        # P0.6 — graceful shutdown cancelled any running purge; release the
        # cleanup lease immediately so the replacement process can reclaim the
        # corpus without waiting out the full lease window.
        if self._db is not None:
            try:
                await self._db["corpora"].update_many(
                    {"cleanup_owner": self._cleanup_owner},
                    {
                        "$set": {
                            "cleanup_lease_until": datetime.utcnow(),
                            "cleanup_released_at": datetime.utcnow(),
                        }
                    },
                )
            except Exception:  # noqa: BLE001 — shutdown must not fail on this
                logger.warning(
                    "Cleanup lease release on shutdown failed", exc_info=True
                )
        if self._qdrant:
            await self._qdrant.close()
        if self._neo4j:
            await self._neo4j.close()
        logger.info("IngestionService: clients closed")

    async def migrate_extraction_engine(self, global_engine: str) -> dict:
        """Lifespan migration — stamp an EXPLICIT extraction_engine on every
        corpus whose config is missing/'inherit'.

        §13 ground-truth correction: the global Settings engine silently
        governed every corpus (a corpus ran cloud Qwen2.5-7B while its enabled
        sidecars idled and every screen looked green). Stamping the current
        global value preserves observed behavior exactly, but makes the
        contract per-corpus, visible, and deterministic from then on — the
        global engine remains only as the seed for legacy/unset configs.

        Idempotent: corpora already carrying an explicit engine are untouched.
        """
        if self._db is None:
            logger.warning("migrate_extraction_engine: DB not connected — skipping")
            return {"scanned": 0, "stamped": 0, "engine": global_engine}

        valid = {
            "off",
            "local",
            "cloud",
            "runpod_flash",
            "legacy_local",
            "dual",
            "local_then_cloud",
            "local_then_enrich",
        }
        engine = (global_engine or "").strip().lower()
        if engine not in valid:
            # Deterministic floor: "local" is now a private/provider LLM lane.
            # It may fail fast without a provider chip, but it will not silently
            # run the deprecated GLiNER/GLiREL sidecar.
            engine = "local"

        scanned = 0
        stamped: list[str] = []
        cursor = self._db["corpora"].find(
            {},
            projection={
                "corpus_id": 1,
                "default_ingestion_config.extraction_engine": 1,
            },
        )
        async for doc in cursor:
            scanned += 1
            current = (
                str(
                    (
                        (doc.get("default_ingestion_config") or {}).get(
                            "extraction_engine"
                        )
                    )
                    or ""
                )
                .strip()
                .lower()
            )
            if current in valid:
                continue  # already explicit
            await self._db["corpora"].update_one(
                {"corpus_id": doc["corpus_id"]},
                {"$set": {"default_ingestion_config.extraction_engine": engine}},
            )
            stamped.append(doc["corpus_id"])
        if stamped:
            logger.info(
                "migrate_extraction_engine: stamped %d corpora with engine=%r",
                len(stamped),
                engine,
            )
        return {"scanned": scanned, "stamped": len(stamped), "engine": engine}

    async def migrate_universal_schema(self, force: bool = False) -> dict:
        """Lifespan migration — patch corpora to the universal baked schema.

        Behavior:
          - force=False (default): corpora whose default_ingestion_config has
            null/empty schemas get patched. Corpora with an older universal
            relation list also receive newly-added universal predicates, while
            truly custom relation labels are preserved untouched.
          - force=True: every corpus is overwritten with the universal schema
            plus schema_strict='soft'. Use as the "reset to universal" lever.

        Idempotent: rerunning with force=False on a fully-patched database is
        a no-op. Logs the count of patched corpora. schema_strict is always
        coerced to 'soft' when a row is touched so legacy 'off' / 'hard' values
        don't outlive the migration.
        """
        from services.ghost_b import (
            UNIVERSAL_ENTITY_SCHEMA,
            UNIVERSAL_RELATION_SCHEMA,
        )

        if self._db is None:
            logger.warning("migrate_universal_schema: DB not connected — skipping")
            return {"scanned": 0, "patched": 0, "force": force}

        universal_entities = list(UNIVERSAL_ENTITY_SCHEMA)
        universal_relations = list(UNIVERSAL_RELATION_SCHEMA)

        scanned = 0
        patched_ids: list[str] = []
        cursor = self._db["corpora"].find(
            {},
            projection={
                "corpus_id": 1,
                "name": 1,
                "default_ingestion_config.entity_schema": 1,
                "default_ingestion_config.relation_schema": 1,
                "default_ingestion_config.schema_strict": 1,
            },
        )
        async for doc in cursor:
            scanned += 1
            cfg = doc.get("default_ingestion_config") or {}
            existing_entities = cfg.get("entity_schema")
            existing_relations = cfg.get("relation_schema")
            existing_strict = cfg.get("schema_strict")
            reasons: list[str] = []

            if force:
                new_entities = universal_entities
                new_relations = universal_relations
                reasons.append("force")
            else:
                new_entities = existing_entities
                new_relations = existing_relations
                if not existing_entities:
                    new_entities = universal_entities
                    reasons.append("null_entity_schema")
                elif all(et in universal_entities for et in existing_entities):
                    # Pt9e — additive extension of entity_schema, mirroring the
                    # relation logic immediately below. When UNIVERSAL_ENTITY_SCHEMA
                    # grows (e.g. Pt9a added Software + Standard), every existing
                    # corpus whose entity_schema is a strict subset of the new
                    # universal gets the missing terms APPENDED. Strict subset
                    # check (`all(et in universal_entities)`) protects custom
                    # schemas — a corpus with `entity_schema=["Gene","Protein"]`
                    # does NOT match the subset condition (Gene/Protein aren't
                    # in universal), so its custom vocab is preserved.
                    #
                    # Why this is safe under FROZEN_CONFIG_FIELDS: adding terms
                    # is monotonic. Existing extractions remain valid (their
                    # types still belong to the new vocab); only new extractions
                    # gain the option of emitting the added types. The FROZEN
                    # contract exists to prevent CHANGING the meaning of stored
                    # extractions — additive extension doesn't do that.
                    missing_entities = [
                        et for et in universal_entities if et not in existing_entities
                    ]
                    if missing_entities:
                        new_entities = [*existing_entities, *missing_entities]
                        reasons.append(
                            f"missing_universal_entities={len(missing_entities)}"
                        )
                if not existing_relations:
                    new_relations = universal_relations
                    reasons.append("null_relation_schema")
                elif all(rel in universal_relations for rel in existing_relations):
                    missing_relations = [
                        rel
                        for rel in universal_relations
                        if rel not in existing_relations
                    ]
                    if missing_relations:
                        body = [
                            rel for rel in existing_relations if rel != "related_to"
                        ]
                        additions = [
                            rel for rel in missing_relations if rel != "related_to"
                        ]
                        new_relations = [*body, *additions, "related_to"]
                        reasons.append(
                            f"missing_universal_relations={len(missing_relations)}"
                        )
                if existing_strict in ("off", "hard"):
                    reasons.append(f"legacy_strict={existing_strict}")

            if not reasons:
                continue

            await self._db["corpora"].update_one(
                {"corpus_id": doc["corpus_id"]},
                {
                    "$set": {
                        "default_ingestion_config.entity_schema": new_entities,
                        "default_ingestion_config.relation_schema": new_relations,
                        "default_ingestion_config.schema_strict": "soft",
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            patched_ids.append(doc["corpus_id"])
            logger.info(
                "migrate_universal_schema patched corpus_id=%s name=%s reasons=%s",
                doc["corpus_id"],
                doc.get("name", "<unnamed>"),
                ",".join(reasons),
            )

        logger.info(
            "migrate_universal_schema: scanned=%d patched=%d force=%s corpus_ids=%s",
            scanned,
            len(patched_ids),
            force,
            patched_ids or "[]",
        )
        return {
            "scanned": scanned,
            "patched": len(patched_ids),
            "force": force,
            "corpus_ids": patched_ids,
        }

    async def migrate_bare_model_names(self) -> dict:
        """Lifespan migration — rewrite ingestion bare model strings to include
        the LiteLLM provider prefix.

        Motivation: prior UI presets auto-filled `model = "deepseek-chat"` (no
        prefix). LiteLLM's wildcard router can't match that to `deepseek/*`
        and returns 400, cascading into Ghost A + Ghost B failures on every
        ingest through such a corpus.

        Scope:
          • `corpora.default_ingestion_config.{summary_models,extraction_models}`
            (per-corpus ingestion pools — `ModelProfileRef` with `provider_preset`).
        Chat/query model pools intentionally stay user-facing and may store
        provider-native ids (`deepseek-chat`, `glm-5.1`). Chat-time resolution
        normalizes those with `services.provider_presets.normalize_model_for_litellm`.

        Rules per entry:
          • If the stored model contains "/", assume it's already prefixed →
            skip (idempotent).
          • Else, look up the entry's preset id in the backend registry. If
            unknown, leave alone (user-authored custom config).
          • Else, rewrite `model = f"{litellm_provider}/{old_model}"`. Log
            an audit line per rewrite.

        Returns the historical counters. The chat-pool counters remain present
        for API compatibility but are no-ops.
        """
        from services.provider_presets import litellm_provider_for

        if self._db is None:
            logger.warning("migrate_bare_model_names: DB not connected — skipping")
            return {
                "corpora_patched": 0,
                "pool_entries_patched": 0,
                "corpus_ids": [],
                "settings_users_patched": 0,
                "model_pool_entries_patched": 0,
            }

        def _needs_rewrite(preset_id: str | None, model: str | None) -> str | None:
            """Return the rewritten model string, or None to skip."""
            if not model or not isinstance(model, str):
                return None
            if "/" in model:
                return None
            prefix = litellm_provider_for(preset_id)
            if not prefix:
                return None
            return f"{prefix}/{model}"

        # ── 1. Per-corpus ingestion pools (summary_models, extraction_models).
        corpus_ids: list[str] = []
        pool_entries_patched = 0
        cursor = self._db["corpora"].find(
            {},
            projection={
                "corpus_id": 1,
                "name": 1,
                "default_ingestion_config.summary_models": 1,
                "default_ingestion_config.extraction_models": 1,
            },
        )
        async for doc in cursor:
            cfg = doc.get("default_ingestion_config") or {}
            rewrites_for_doc: list[tuple[str, int, str, str]] = []
            new_summary = cfg.get("summary_models") or []
            new_extraction = cfg.get("extraction_models") or []

            for idx, entry in enumerate(new_summary):
                if not isinstance(entry, dict):
                    continue
                rewritten = _needs_rewrite(
                    entry.get("provider_preset"), entry.get("model")
                )
                if rewritten is None:
                    continue
                rewrites_for_doc.append(
                    ("summary_models", idx, entry["model"], rewritten)
                )
                entry["model"] = rewritten

            for idx, entry in enumerate(new_extraction):
                if not isinstance(entry, dict):
                    continue
                rewritten = _needs_rewrite(
                    entry.get("provider_preset"), entry.get("model")
                )
                if rewritten is None:
                    continue
                rewrites_for_doc.append(
                    ("extraction_models", idx, entry["model"], rewritten)
                )
                entry["model"] = rewritten

            if not rewrites_for_doc:
                continue

            await self._db["corpora"].update_one(
                {"corpus_id": doc["corpus_id"]},
                {
                    "$set": {
                        "default_ingestion_config.summary_models": new_summary,
                        "default_ingestion_config.extraction_models": new_extraction,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            corpus_ids.append(doc["corpus_id"])
            pool_entries_patched += len(rewrites_for_doc)
            for field, idx, old_model, new_model in rewrites_for_doc:
                logger.info(
                    "migrate_bare_model_names: corpus=%s field=%s idx=%d "
                    "old=%r new=%r",
                    doc["corpus_id"],
                    field,
                    idx,
                    old_model,
                    new_model,
                )

        # Chat/query model pools are resolved at use time so setup can keep
        # provider-native model ids visible to users.
        settings_users_patched = 0
        model_pool_entries_patched = 0

        result = {
            "corpora_patched": len(corpus_ids),
            "pool_entries_patched": pool_entries_patched,
            "corpus_ids": corpus_ids,
            "settings_users_patched": settings_users_patched,
            "model_pool_entries_patched": model_pool_entries_patched,
        }
        logger.info(
            "migrate_bare_model_names: corpora_patched=%d pool_entries=%d "
            "settings_users=%d model_pool_entries=%d corpus_ids=%s",
            result["corpora_patched"],
            result["pool_entries_patched"],
            result["settings_users_patched"],
            result["model_pool_entries_patched"],
            corpus_ids or "[]",
        )
        return result

    async def ingest(
        self,
        data: bytes,
        filename: str,
        corpus_id: str,
        user_id: str,
        ingestion_config: IngestionConfig,
        model: str,
        ingest_overrides: dict | None = None,
        source_url: str | None = None,
        source_identity: dict | None = None,
        on_doc_id: "Any | None" = None,
        on_phase: "Any | None" = None,
        target_stage: str | None = None,
        extraction_endpoint_urls: list[str] | None = None,
        defer_summaries: bool = False,
        duplicate_policy: str = "skip",
    ) -> IngestJobResponse:
        """Run the full ingestion pipeline for one document.

        `ingest_overrides` (Phase 21) carries ephemeral per-request overrides
        — embed wiring, synthesized ghost pools — that shadow the corpus's
        mutable defaults for this ingest only. Never persisted.

        `on_doc_id` (Phase K) is invoked with the resolved doc_id as soon as
        docling parse completes — the HTTP endpoint uses this to return a
        response before the long tail of ghost/embed/write runs.

        `on_phase` is an optional durable-batch hook called as the worker
        crosses coarse phase boundaries such as chunking, embedding, qdrant,
        neo4j, verifying, complete, or failed.
        """
        from services.ingestion.worker import run_ingest_job
        from services.ingestion.source_identity import (
            build_deterministic_filename,
            build_source_identity,
        )

        if source_identity is None:
            source_identity = build_source_identity(
                filename=filename,
                source_url=source_url,
                data=data,
            )
        deterministic_filename = build_deterministic_filename(
            filename=filename,
            source_url=source_url,
            data=data,
            source_identity=source_identity,
        )
        source_identity = dict(source_identity)
        source_identity.setdefault("original_filename", filename)
        source_identity["deterministic_filename"] = deterministic_filename
        filename = deterministic_filename
        if duplicate_policy not in {"skip", "allow"}:
            raise ValueError("duplicate_policy must be 'skip' or 'allow'")
        if duplicate_policy == "skip":
            duplicate_query = exact_source_duplicate_query(
                corpus_id=corpus_id,
                source_identity=source_identity,
            )
            if duplicate_query is not None and self._db is not None:
                existing = await self._db["documents"].find_one(
                    duplicate_query,
                    {
                        "_id": 0,
                        "doc_id": 1,
                        "filename": 1,
                        "source_tier": 1,
                        "source_key": 1,
                        "source_identity.content_sha256": 1,
                        "ingest_stage": 1,
                    },
                )
                if existing:
                    existing_doc_id = str(existing.get("doc_id") or "")
                    reason = (
                        "Exact source duplicate skipped: "
                        f"{filename!r} matches existing document "
                        f"{existing.get('filename') or existing_doc_id} "
                        f"({existing_doc_id}). Pass duplicate_policy='allow' "
                        "to intentionally ingest another copy."
                    )
                    await _call_ingest_callback(on_doc_id, existing_doc_id)
                    await _call_ingest_callback(
                        on_phase,
                        "skipped_duplicate",
                        {
                            "doc_id": existing_doc_id,
                            "corpus_id": corpus_id,
                            "duplicate_of": existing_doc_id,
                            "source_key": source_identity.get("source_key"),
                        },
                    )
                    return IngestJobResponse(
                        job_id=str(uuid.uuid4()),
                        doc_id=existing_doc_id,
                        corpus_id=corpus_id,
                        filename=filename,
                        source_tier=existing.get("source_tier"),
                        status="skipped_duplicate",
                        chunk_count=0,
                        parent_count=0,
                        error=reason,
                    )

        return await run_ingest_job(
            job_id=str(uuid.uuid4()),
            data=data,
            filename=filename,
            corpus_id=corpus_id,
            user_id=user_id,
            ingestion_config=ingestion_config,
            db=self._db,
            qdrant_client=self._qdrant,
            neo4j_driver=self._neo4j,
            model=model,
            ingest_overrides=ingest_overrides,
            source_url=source_url,
            source_identity=source_identity,
            on_doc_id=on_doc_id,
            on_phase=on_phase,
            target_stage=target_stage,
            extraction_endpoint_urls=extraction_endpoint_urls,
            defer_summaries=defer_summaries,
        )

    async def _embed_and_upsert_schema_terms(
        self,
        corpus_id: str,
        terms: list[str],
        kind: str,
        ingestion_config: IngestionConfig,
    ) -> int:
        """Phase 14.2 — embed schema vocabulary and upsert into polymath_schemas.
        Caller is responsible for first deleting stale terms when this is an update.
        """
        if not terms:
            return 0
        from services.embedder import embed_batch
        from services.storage.qdrant_writer import upsert_schema_terms

        vectors = await embed_batch(
            terms,
            mode=getattr(ingestion_config, "embed_mode", "local_st"),
            expected_dim=getattr(ingestion_config, "embedding_dimension", 1024),
            expected_model_id=getattr(ingestion_config, "embedding_model_id", None),
            api_pool=self._plaintext_model_pool(
                getattr(ingestion_config, "embedding_models", None)
            ),
        )
        return await upsert_schema_terms(self._qdrant, corpus_id, terms, kind, vectors)

    @staticmethod
    def _plaintext_model_pool(refs) -> list[dict]:
        """Return a pool with decrypted per-entry secret values for dispatch."""
        if not refs:
            return []
        from services.secrets import decrypt

        out: list[dict] = []
        for ref in refs:
            data = ref.model_dump() if hasattr(ref, "model_dump") else dict(ref)
            for secret_field in ("api_key", "lifecycle_api_key"):
                raw_key = data.get(secret_field)
                if raw_key:
                    plaintext = decrypt(raw_key)
                    data[secret_field] = plaintext if plaintext is not None else raw_key
            out.append(data)
        return out

    @staticmethod
    async def _apply_global_summary_defaults(
        *,
        user_id: str,
        ingestion_config: IngestionConfig,
    ) -> IngestionConfig:
        """Fill empty Ghost A defaults from Settings → Ingestion.

        Per-corpus settings remain authoritative when supplied. This only fills
        the common fresh-install/agent path where the client sends an otherwise
        valid config with an empty ``summary_models`` pool.
        """
        try:
            from services.settings import settings_service

            global_ingestion = await settings_service.get_runtime_ingestion_settings(
                user_id
            )
        except Exception as exc:  # noqa: BLE001 - settings defaults are best-effort
            logger.warning(
                "global summary defaults unavailable for user=%s: %s", user_id, exc
            )
            return ingestion_config

        summary = global_ingestion.summary
        patch: dict = {}
        runtime_summary_models = [
            m.model_dump() if hasattr(m, "model_dump") else dict(m)
            for m in (summary.summary_models or [])
        ]
        if not ingestion_config.summary_models and runtime_summary_models:
            patch["summary_models"] = runtime_summary_models
        elif ingestion_config.summary_models and runtime_summary_models:
            incoming_pool = [
                m.model_dump() if hasattr(m, "model_dump") else dict(m)
                for m in ingestion_config.summary_models
            ]

            def _matches(a: dict, b: dict) -> bool:
                return (
                    (a.get("model") or "") == (b.get("model") or "")
                    and (a.get("base_url") or None) == (b.get("base_url") or None)
                    and (a.get("provider_preset") or "")
                    == (b.get("provider_preset") or "")
                )

            changed = False
            for idx, entry in enumerate(incoming_pool):
                if not isinstance(entry, dict):
                    continue
                masked_fields = [
                    field
                    for field in ("api_key", "lifecycle_api_key")
                    if entry.get(field) == "[set]"
                ]
                if not masked_fields:
                    continue
                replacement = None
                if idx < len(runtime_summary_models) and _matches(
                    entry, runtime_summary_models[idx]
                ):
                    replacement = runtime_summary_models[idx]
                else:
                    replacement = next(
                        (
                            candidate
                            for candidate in runtime_summary_models
                            if _matches(entry, candidate)
                        ),
                        None,
                    )
                if replacement:
                    for field in masked_fields:
                        if replacement.get(field):
                            entry[field] = replacement[field]
                            changed = True
            if changed:
                patch["summary_models"] = incoming_pool

        default_tokens = IngestionConfig.model_fields["max_summary_tokens"].default
        if (
            summary.max_summary_tokens
            and ingestion_config.max_summary_tokens == default_tokens
            and summary.max_summary_tokens != default_tokens
        ):
            patch["max_summary_tokens"] = summary.max_summary_tokens

        if not patch:
            return ingestion_config
        return IngestionConfig(**{**ingestion_config.model_dump(), **patch})

    @staticmethod
    async def _materialize_ingestion_provider_refs(
        *,
        user_id: str,
        ingestion_config: IngestionConfig,
    ) -> IngestionConfig:
        """Resolve secret-free corpus profile refs from Settings.

        Corpus Manager stores a stable ``profile_id`` plus a per-corpus
        concurrency override. The worker-facing snapshot remains the existing
        ModelProfileRef shape, so every summary/extraction/indexing executor
        keeps one proven dispatch contract while credentials stay editable in
        Settings only.
        """
        if not user_id:
            return ingestion_config
        from services.settings import settings_service

        registry = await settings_service.get_ingestion_provider_registry_raw(user_id)
        by_id = {
            str(entry.get("profile_id")): entry
            for entry in registry
            if isinstance(entry, dict)
            and entry.get("profile_id")
            and entry.get("enabled", True)
        }
        if not by_id:
            return ingestion_config

        config = ingestion_config.model_dump()
        changed = False
        for field in ("summary_models", "extraction_models", "embedding_models"):
            resolved_pool: list[dict] = []
            for current in config.get(field) or []:
                if not isinstance(current, dict):
                    resolved_pool.append(current)
                    continue
                saved = by_id.get(str(current.get("profile_id") or ""))
                if not saved:
                    resolved_pool.append(current)
                    continue
                replacement = dict(saved)
                replacement["max_concurrent"] = current.get(
                    "max_concurrent", replacement.get("max_concurrent", 1)
                )
                resolved_pool.append(replacement)
                changed = True
            config[field] = resolved_pool
        return IngestionConfig(**config) if changed else ingestion_config

    async def create_corpus(
        self,
        name: str,
        description: Optional[str],
        user_id: str,
        ingestion_config: IngestionConfig,
    ) -> dict:
        from services.ghost_b import (
            UNIVERSAL_ENTITY_SCHEMA,
            UNIVERSAL_RELATION_SCHEMA,
        )
        from services.storage.mongo_writer import upsert_corpus

        ingestion_config = await self._apply_global_summary_defaults(
            user_id=user_id,
            ingestion_config=ingestion_config,
        )
        ingestion_config = await self._materialize_ingestion_provider_refs(
            user_id=user_id,
            ingestion_config=ingestion_config,
        )

        # Preset normalization: rewrite use_neo4j / chunk_summarization /
        # target_qdrant_collections to match the chosen preset. No-op for
        # preset='custom'.
        ingestion_config = apply_preset(ingestion_config)

        # Coerce embed_mode to "local_st" when Modal is disabled server-side,
        # so the frozen config reflects what will actually run.
        if (
            ingestion_config.embed_mode == "modal_tei"
            and not self._settings.MODAL_ENABLED
        ):
            ingestion_config = ingestion_config.model_copy(
                update={"embed_mode": "local_st"}
            )

        # Belt-and-suspenders: an old client may POST entity_schema=null /
        # relation_schema=null. Pydantic default_factory runs only on
        # omission, so an explicit null leaks through. Refill from the
        # universal schema so every corpus ingests with a consistent vocab.
        schema_patch: dict = {}
        if not ingestion_config.entity_schema:
            schema_patch["entity_schema"] = list(UNIVERSAL_ENTITY_SCHEMA)
        if not ingestion_config.relation_schema:
            schema_patch["relation_schema"] = list(UNIVERSAL_RELATION_SCHEMA)
        if schema_patch:
            ingestion_config = ingestion_config.model_copy(update=schema_patch)

        corpus_doc = {
            "corpus_id": str(uuid.uuid4()),
            "name": name,
            "description": description,
            "user_id": user_id,
            "default_ingestion_config": ingestion_config.model_dump(),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "doc_count": 0,
            "chunk_count": 0,
            "embedding_model_id": self._settings.EMBEDDER_MODEL_NAME,
        }

        # Phase 19.3 — encrypt per-ghost api keys before they land in Mongo.
        self._encrypt_ingestion_keys_in_place(corpus_doc["default_ingestion_config"])

        await upsert_corpus(self._db, corpus_doc)

        # Provision per-corpus retrieval storage up front so the first ingest
        # does not race collection/index creation. This uses the same readiness
        # contract as startup repair and the ingest worker.
        from services.retrieval_readiness import ensure_corpus_retrieval_ready

        try:
            readiness = await ensure_corpus_retrieval_ready(
                db=self._db,
                qdrant_client=self._qdrant,
                neo4j_driver=self._neo4j,
                corpus_id=corpus_doc["corpus_id"],
                corpus_doc=corpus_doc,
                corpus_name=corpus_doc.get("name"),
                ingestion_config=ingestion_config,
                neo4j_enabled=self._settings.NEO4J_ENABLED,
                default_dim=self._settings.EMBEDDING_DIMENSION,
            )
            if not readiness.ok:
                raise RuntimeError("; ".join(readiness.errors))
        except Exception as exc:
            logger.error(
                "Failed to prepare retrieval storage for corpus %s: %s",
                corpus_doc["corpus_id"],
                exc,
            )
            raise

        # Mask per-entry api_keys before the doc flows back to the API layer.
        # Otherwise the POST response leaks Fernet ciphertext (the encrypt
        # helper above mutates the dict in place).
        self._mask_ingestion_keys_in_place(corpus_doc["default_ingestion_config"])

        # Phase 14.2 — embed schema vocabularies if user populated them.
        corpus_id = corpus_doc["corpus_id"]
        if ingestion_config.entity_schema:
            try:
                await self._embed_and_upsert_schema_terms(
                    corpus_id,
                    ingestion_config.entity_schema,
                    kind="entity_type",
                    ingestion_config=ingestion_config,
                )
            except Exception as exc:
                logger.warning(
                    "Schema embedding failed for corpus %s (entity_type): %s",
                    corpus_id,
                    exc,
                )
        if ingestion_config.relation_schema:
            try:
                await self._embed_and_upsert_schema_terms(
                    corpus_id,
                    ingestion_config.relation_schema,
                    kind="relation",
                    ingestion_config=ingestion_config,
                )
            except Exception as exc:
                logger.warning(
                    "Schema embedding failed for corpus %s (relation): %s",
                    corpus_id,
                    exc,
                )

        await self._refresh_corpus_counts([corpus_doc])
        return corpus_doc

    async def list_corpora(self, user_id: Optional[str] = None) -> list[dict]:
        from services.storage.mongo_reader import list_corpora

        docs = await list_corpora(self._db, user_id=user_id)
        await self._refresh_corpus_counts(docs, refresh_readiness=False)
        for doc in docs:
            self._mask_ingestion_keys_in_place(doc.get("default_ingestion_config"))
        return docs

    async def get_corpus(self, corpus_id: str) -> Optional[dict]:
        from services.storage.mongo_reader import get_corpus

        doc = await get_corpus(self._db, corpus_id)
        if doc:
            await self._refresh_corpus_counts([doc], refresh_readiness=False)
            self._mask_ingestion_keys_in_place(doc.get("default_ingestion_config"))
        return doc

    async def _materialize_corpus_readiness_safely(self, corpus_id: str) -> dict | None:
        try:
            from services.ingestion.readiness import materialize_corpus_readiness

            return await materialize_corpus_readiness(self._db, corpus_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Corpus readiness materialization failed corpus=%s: %s",
                str(corpus_id)[:8],
                exc,
            )
            return None

    async def _compute_corpus_readiness_safely(self, corpus_id: str) -> dict | None:
        try:
            from services.ingestion.readiness import compute_corpus_readiness

            return await compute_corpus_readiness(self._db, corpus_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Corpus readiness pressure check failed corpus=%s: %s",
                str(corpus_id)[:8],
                exc,
            )
            return None

    async def _backpressure_pause_result(
        self,
        *,
        corpus_id: str,
        lane_key: str,
        operation: str,
        readiness: dict | None = None,
    ) -> dict | None:
        readiness = readiness or await self._compute_corpus_readiness_safely(corpus_id)
        pressure = (readiness or {}).get("pressure") or {}
        backpressure = pressure.get("backpressure") or {}
        if backpressure.get(lane_key) is not False:
            return None
        return {
            "corpus_id": corpus_id,
            "status": "paused_pressure",
            "operation": operation,
            "reason": f"{lane_key}=false",
            "pressure": pressure,
            "readiness": readiness,
        }

    async def _run_owned_repair_lane(
        self,
        *,
        corpus_id: str,
        lane: str,
        operation: str,
        runner: Any,
    ) -> dict[str, Any]:
        """Run one lane only when this controller owns its distributed lease."""

        from services.ingestion.job_leases import corpus_lane_lease

        owner = f"{operation}:{uuid.uuid4().hex}"
        async with corpus_lane_lease(
            self._db,
            corpus_id=corpus_id,
            lane=lane,
            owner=owner,
        ) as lease:
            if not lease:
                return {
                    "status": "lease_busy",
                    "corpus_id": corpus_id,
                    "lane": lane,
                    "operation": operation,
                    "claimed": 0,
                    "counts": {},
                }
            result = runner()
            if inspect.isawaitable(result):
                result = await result
            return result

    async def _refresh_corpus_counts(
        self,
        docs: list[dict],
        *,
        refresh_readiness: bool = True,
    ) -> None:
        """Repair corpus doc/chunk aggregate counters from Mongo truth.

        Document deletion cascades remove rows from `documents` and `chunks`.
        The header and frozen-field guard read `corpora.doc_count`, so stale
        aggregates make an emptied corpus look populated. This helper keeps
        the materialized counters honest without making the frontend derive
        counts from separate endpoints.
        """
        if not docs:
            return
        corpus_ids = [d.get("corpus_id") for d in docs if d.get("corpus_id")]
        if not corpus_ids:
            return
        from services.storage.record_status import with_active_records

        async def _counts(collection: str) -> dict[str, int]:
            pipeline = [
                {"$match": with_active_records({"corpus_id": {"$in": corpus_ids}})},
                {"$group": {"_id": "$corpus_id", "count": {"$sum": 1}}},
            ]
            rows = await self._db[collection].aggregate(pipeline).to_list(length=None)
            return {str(r["_id"]): int(r["count"]) for r in rows}

        doc_counts = await _counts("documents")
        chunk_counts = await _counts("chunks")
        # Owner 2026-07-06: doc_count alone misled — every file that STARTS
        # parsing gets a document row, so a churned batch showed "498" while
        # ~80 were actually complete. ready_doc_count = fully verified docs.
        ready_rows = (
            await self._db["documents"]
            .aggregate(
                [
                    {
                        "$match": with_active_records(
                            {
                                "corpus_id": {"$in": corpus_ids},
                                "write_state.verified": True,
                            }
                        )
                    },
                    {"$group": {"_id": "$corpus_id", "count": {"$sum": 1}}},
                ]
            )
            .to_list(length=None)
        )
        ready_counts = {str(r["_id"]): int(r["count"]) for r in ready_rows}
        cached_readiness: dict[str, dict] = {}
        if not refresh_readiness:
            rows = (
                await self._db["corpus_readiness"]
                .find(
                    {"_id": {"$in": corpus_ids}},
                    {
                        "_id": 1,
                        "corpus_id": 1,
                        "status": 1,
                        "blocking": 1,
                        "next_actions": 1,
                        "documents": 1,
                        "chunks": 1,
                        "summaries": 1,
                        "graph": 1,
                        "idempotency": 1,
                        "repair": 1,
                        "pressure": 1,
                        "schema_version": 1,
                        "computed_at": 1,
                        "source": 1,
                        "stale": 1,
                        "refresh_error": 1,
                    },
                )
                .to_list(length=len(corpus_ids))
            )
            cached_readiness = {
                str(row.get("corpus_id") or row.get("_id")): {
                    key: value for key, value in row.items() if key != "_id"
                }
                for row in rows
            }

        for doc in docs:
            cid = doc.get("corpus_id")
            if not cid:
                continue
            actual_docs = doc_counts.get(cid, 0)
            actual_chunks = chunk_counts.get(cid, 0)
            doc["ready_doc_count"] = ready_counts.get(cid, 0)
            if (
                doc.get("doc_count", 0) != actual_docs
                or doc.get("chunk_count", 0) != actual_chunks
            ):
                await self._db["corpora"].update_one(
                    {"corpus_id": cid},
                    {"$set": {"doc_count": actual_docs, "chunk_count": actual_chunks}},
                )
                logger.info(
                    "Repaired corpus counters corpus=%s docs=%s chunks=%s",
                    cid[:8],
                    actual_docs,
                    actual_chunks,
                )
            doc["doc_count"] = actual_docs
            doc["chunk_count"] = actual_chunks
            if not refresh_readiness:
                readiness = cached_readiness.get(str(cid))
                if readiness is not None:
                    doc["readiness"] = readiness
                else:
                    doc["readiness"] = {
                        "corpus_id": str(cid),
                        "status": "unknown",
                        "stale": True,
                        "blocking": ["readiness_snapshot_missing"],
                    }
                continue

            try:
                readiness = await self._materialize_corpus_readiness_safely(str(cid))
                if readiness is None:
                    raise RuntimeError(
                        "materialize_corpus_readiness returned no snapshot"
                    )
                doc["readiness"] = readiness
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Corpus readiness snapshot failed corpus=%s: %s",
                    str(cid)[:8],
                    exc,
                )
                cached = None
                try:
                    from services.ingestion.readiness import (
                        build_corpus_readiness_record,
                        get_materialized_corpus_readiness,
                    )

                    cached = await get_materialized_corpus_readiness(self._db, str(cid))
                    doc["readiness"] = build_corpus_readiness_record(
                        cached
                        or {
                            "corpus_id": str(cid),
                            "status": "unknown",
                            "blocking": ["readiness_refresh_failed"],
                        },
                        stale=True,
                        refresh_error=str(exc),
                    )
                except Exception:
                    doc["readiness"] = {
                        "corpus_id": str(cid),
                        "status": "unknown",
                        "stale": True,
                        "error": str(exc)[:300],
                        "blocking": ["readiness_refresh_failed"],
                    }

    async def _get_corpus_raw(self, corpus_id: str) -> Optional[dict]:
        """Unmasked read — used by update_corpus so `_encrypt_ingestion_keys_in_place`
        can diff the incoming patch against real stored ciphertext. NEVER return
        this to the API layer."""
        from services.storage.mongo_reader import get_corpus

        doc = await get_corpus(self._db, corpus_id)
        if doc:
            await self._refresh_corpus_counts([doc], refresh_readiness=False)
        return doc

    @staticmethod
    def _mask_ingestion_keys_in_place(config_dict: dict | None) -> None:
        """Walk pool entries and replace each ciphertext `api_key` with the
        masked sentinel "[set]" (or None). The frontend reads this to show
        'key present / not set' but never sees plaintext or ciphertext.
        On update, sending "[set]" back preserves the stored ciphertext
        (see _encrypt_ingestion_keys_in_place).

        Also tolerates legacy dicts with scalar `summary_api_key` /
        `extraction_api_key` — masks those in place too so an older reader
        path still gets a coherent payload.
        """
        if not config_dict:
            return

        for pool_field in ("summary_models", "extraction_models", "embedding_models"):
            pool = config_dict.get(pool_field)
            if not pool:
                continue
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                for secret_field in ("api_key", "lifecycle_api_key"):
                    raw = entry.get(secret_field)
                    entry[secret_field] = "[set]" if raw else None

        # Top-level mutable api keys (Phase 21 — embed provider wiring).
        if "embed_api_key" in config_dict:
            raw = config_dict.get("embed_api_key")
            config_dict["embed_api_key"] = "[set]" if raw else None

        # Legacy scalar fields — mask for older readers during the migration
        # window. The pre-validator will strip these when the config is loaded
        # through Pydantic.
        for legacy in ("summary_api_key", "extraction_api_key"):
            if legacy in config_dict:
                raw = config_dict.get(legacy)
                config_dict[legacy] = "[set]" if raw else None

    async def get_job_status(
        self,
        doc_id: str,
        *,
        corpus_id: str | None = None,
        user_id: str | None = None,
    ) -> Optional[dict]:
        query: dict = {"doc_id": doc_id}
        if corpus_id:
            query["corpus_id"] = corpus_id
        if user_id:
            query["user_id"] = user_id
        return await self._db["documents"].find_one(query)

    # Fields that must never change once any document has been ingested.
    # Changing them mid-corpus = silent zero-recall (different vector space).
    _LOCKED_EMBEDDING_FIELDS = frozenset(
        {"embedding_model", "embedding_dimension", "embedding_model_id"}
    )

    # Legacy scalar key field names — retained so migration code can find them
    # in old Mongo docs. New writes go through the pool walker below.
    _LEGACY_KEY_FIELDS: tuple[str, ...] = (
        "summary_api_key",
        "extraction_api_key",
    )

    @staticmethod
    def _encrypt_ingestion_keys_in_place(
        config_dict: dict, existing_config: dict | None = None
    ) -> None:
        """
        Walk summary_models and extraction_models; for each entry, ensure
        `api_key` / `lifecycle_api_key` hold Fernet ciphertext (or None)
        before they land in Mongo.

        Per-entry semantics (matched against the existing pool by index when
        available, so a user editing chip #3 doesn't wipe chip #1's key):
          - Value missing / None / "" / "[set]" → preserve existing ciphertext
            at that index if present, otherwise None.
          - Value already decrypts as a Fernet token → leave as-is.
          - Otherwise → treat as plaintext and encrypt.

        Also handles legacy top-level `summary_api_key` / `extraction_api_key`
        scalars if somehow present (shouldn't happen after the schema validator
        strips them, but defensive).
        """
        from services.secrets import decrypt, encrypt

        MASK_SENTINEL = "[set]"

        def _enc(new_val, existing_val):
            if not new_val or new_val == MASK_SENTINEL:
                return existing_val
            if isinstance(new_val, str) and decrypt(new_val) is not None:
                return new_val
            return encrypt(new_val)

        for pool_field in ("summary_models", "extraction_models", "embedding_models"):
            new_pool = config_dict.get(pool_field) or []
            existing_pool = (
                (existing_config or {}).get(pool_field) or [] if existing_config else []
            )
            for idx, entry in enumerate(new_pool):
                if not isinstance(entry, dict):
                    continue
                existing_entry = (
                    existing_pool[idx]
                    if idx < len(existing_pool) and isinstance(existing_pool[idx], dict)
                    else {}
                )
                for secret_field in ("api_key", "lifecycle_api_key"):
                    entry[secret_field] = _enc(
                        entry.get(secret_field),
                        existing_entry.get(secret_field),
                    )

        # Top-level embed_api_key — same "[set]" preserve + encrypt-plaintext
        # semantics as the per-entry pool keys.
        if "embed_api_key" in config_dict:
            config_dict["embed_api_key"] = _enc(
                config_dict.get("embed_api_key"),
                (existing_config or {}).get("embed_api_key"),
            )

        # Legacy scalar key fields — run the same resolution so mid-migration
        # writes don't drop the stored secret.
        for field in IngestionService._LEGACY_KEY_FIELDS:
            if field not in config_dict:
                continue
            existing_val = (
                (existing_config or {}).get(field) if existing_config else None
            )
            config_dict[field] = _enc(config_dict.get(field), existing_val)

    async def update_corpus(
        self, corpus_id: str, updates: dict, *, user_id: str | None = None
    ) -> Optional[dict]:
        """
        Partial update of corpus metadata (name, description, config).

        Raises ValueError if caller tries to change any of the 3 locked embedding
        fields after docs have been ingested. Router maps this to HTTP 409.

        Phase 14.2 — when entity_schema or relation_schema changes, the polymath_schemas
        Qdrant collection is updated in lockstep: stale terms deleted, new terms embedded
        and upserted.
        """
        from services.storage.mongo_writer import update_corpus
        from services.storage.qdrant_writer import delete_schema_terms

        # Guard: if doc_count > 0, reject changes to any FROZEN field. Mutable
        # fields (embed_*, model pools, concurrency knobs, models_linked,
        # entity_confidence_threshold) are always editable.
        new_config = updates.get("default_ingestion_config")
        existing = None
        if new_config is not None:
            # Use the raw (unmasked) fetch so we have real ciphertext to diff
            # incoming api_key values against. get_corpus() would have masked
            # api_key entries to "[set]", losing the stored ciphertext.
            existing = await self._get_corpus_raw(corpus_id)
            existing_config = (existing or {}).get("default_ingestion_config") or {}
            doc_count = (existing or {}).get("doc_count", 0) if existing else 0

            # Frozen-field lock runs BEFORE the server-side merge so the
            # check is evaluated against what the CALLER sent, not against
            # the post-merge state.
            if existing and doc_count > 0:
                changed_frozen = [
                    field
                    for field in FROZEN_CONFIG_FIELDS
                    if field in new_config
                    and field in existing_config
                    and new_config[field] != existing_config[field]
                ]
                if changed_frozen:
                    raise FrozenFieldError(changed_frozen, doc_count)

            # Server-side merge (Phase 21). With the router's exclude_unset
            # policy, the caller may send a partial config; Mongo's $set
            # would otherwise replace the whole subdocument and wipe every
            # untouched field. Merge the incoming patch over the existing
            # stored config before the write so frozen fields survive a
            # mutable-only patch, and vice-versa.
            merged_config = {**existing_config, **new_config}
            effective_user_id = user_id or str((existing or {}).get("user_id") or "")
            materialized = await self._materialize_ingestion_provider_refs(
                user_id=effective_user_id,
                ingestion_config=IngestionConfig(**merged_config),
            )
            new_config = materialized.model_dump()
            updates["default_ingestion_config"] = new_config

        # Preset normalization: rewrite toggles to match the chosen preset
        # before the schema-diff / Qdrant-sync block runs, so we diff against
        # the already-normalized config.
        if new_config is not None and "preset" in new_config:
            try:
                normalized = apply_preset(IngestionConfig(**new_config))
                new_config.update(normalized.model_dump())
                updates["default_ingestion_config"] = new_config
            except Exception as exc:
                logger.warning(
                    "apply_preset failed on update for corpus %s: %s — "
                    "leaving config unchanged",
                    corpus_id,
                    exc,
                )

        # Phase 14.2 — schema diff and Qdrant sync. Done BEFORE Mongo write so a
        # Qdrant failure aborts the whole update (caller sees the failure).
        if new_config is not None:
            if existing is None:
                existing = await self._get_corpus_raw(corpus_id)
            existing_config = (existing or {}).get("default_ingestion_config") or {}

            for kind, field_name in (
                ("entity_type", "entity_schema"),
                ("relation", "relation_schema"),
            ):
                old_terms = existing_config.get(field_name) or []
                new_terms = new_config.get(field_name) or []
                if list(old_terms) == list(new_terms):
                    continue  # unchanged — skip
                # Schema changed (added, removed, reordered, or cleared). Replace.
                try:
                    await delete_schema_terms(self._qdrant, corpus_id, kind=kind)
                except Exception as exc:
                    logger.warning(
                        "Failed to delete stale %s terms for corpus %s: %s",
                        kind,
                        corpus_id,
                        exc,
                    )
                if new_terms:
                    # Reuse the corpus's frozen ingestion_config for embedding params,
                    # falling back to the incoming new_config when fields are absent.
                    config_for_embed = IngestionConfig(
                        **{**existing_config, **new_config}
                    )
                    try:
                        await self._embed_and_upsert_schema_terms(
                            corpus_id,
                            new_terms,
                            kind=kind,
                            ingestion_config=config_for_embed,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Schema embedding failed on update for corpus %s (%s): %s",
                            corpus_id,
                            kind,
                            exc,
                        )

            # Phase 19.3 — encrypt per-ghost api keys in the incoming patch so
            # Mongo never sees plaintext. Existing ciphertext is preserved when
            # the field is blank/None (user didn't change the key).
            self._encrypt_ingestion_keys_in_place(
                new_config,
                existing_config=existing_config,
            )

        updated = await update_corpus(self._db, corpus_id, updates)
        # Mask api_keys in the returned doc so the PUT response matches GET.
        if updated:
            self._mask_ingestion_keys_in_place(updated.get("default_ingestion_config"))
            await self._refresh_corpus_counts([updated])

            # Phase 7.5 — if the corpus name changed, re-point Qdrant aliases.
            # Best-effort; failures are logged inside rename_corpus_aliases.
            if "name" in updates and updates["name"]:
                from services.storage.qdrant_writer import rename_corpus_aliases

                try:
                    await rename_corpus_aliases(
                        self._qdrant, corpus_id, updates["name"]
                    )
                except Exception as exc:
                    logger.warning(
                        "rename_corpus_aliases failed for %s: %s", corpus_id, exc
                    )
        return updated

    async def delete_corpus(self, corpus_id: str) -> bool:
        """
        Cascade delete: corpus → documents → chunks → Qdrant points → Neo4j.
        Returns True if the corpus existed and deletion was scheduled.

        FAST-RETURN DESIGN: the heavy parts (deleting ~570k chunks and ~1.7M
        Neo4j elements on a large corpus) used to run synchronously inside the
        request and blow past the 60s proxy timeout → DELETE 504'd, the UI
        looked broken, and re-clicks raced. We now do only the cheap, vanish-
        making work synchronously (drop Qdrant collections — O(1) collection
        drops; mark corpus/documents/support rows stale), then background the
        bulk chunk + graph cleanup. The corpus disappears from normal reads
        immediately because ``status=deleting`` is non-active; it is only marked
        ``deleted`` after projections are consistent.
        """
        from services.storage.mongo_writer import (
            delete_documents_by_corpus,
            mark_corpus_deleting,
        )
        from services.storage.qdrant_writer import drop_collections_for_corpus

        lease_until = datetime.utcnow() + timedelta(
            minutes=CORPUS_CLEANUP_LEASE_MINUTES
        )
        existed = await mark_corpus_deleting(
            self._db,
            corpus_id,
            cleanup_owner=self._cleanup_owner,
            cleanup_lease_until=lease_until,
        )

        # Phase 7.5 — atomically drop all 4 per-corpus collections (naive,
        # hrag, graph, schemas). A whole-collection drop is fast regardless of
        # point count, so it stays synchronous.
        try:
            await drop_collections_for_corpus(self._qdrant, corpus_id)
        except Exception:
            logger.warning(
                "Failed to drop per-corpus Qdrant collections for %s", corpus_id
            )

        try:
            from services.ingestion.tier0 import delete_corpus_doc_profiles

            await delete_corpus_doc_profiles(self._qdrant, corpus_id=corpus_id)
        except Exception:
            logger.warning(
                "Failed to delete shared Tier-0 profiles for corpus %s",
                corpus_id,
                exc_info=True,
            )

        # Mark document/support rows synchronously so evidence cannot be read
        # while the heavier chunk + graph projection cleanup runs.
        await delete_documents_by_corpus(self._db, corpus_id)

        try:
            from services.storage.mongo_writer import retire_corpus_derived_state

            await retire_corpus_derived_state(self._db, corpus_id=corpus_id)
        except Exception:
            logger.warning(
                "Failed to retire derived state for corpus %s",
                corpus_id,
                exc_info=True,
            )

        # The large chunk/graph deletion remains asynchronous for HTTP latency,
        # but it is now a leased durable task. A process restart can reclaim
        # the deleting corpus instead of abandoning an unreferenced coroutine.
        self._start_corpus_purge_task(corpus_id)

        return existed

    def _start_corpus_purge_task(self, corpus_id: str) -> None:
        task = asyncio.create_task(self._purge_corpus_bulk(corpus_id))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def recover_pending_corpus_purges(self, *, limit: int = 4) -> int:
        """Claim stale/partial corpus purges from durable corpus state."""
        if self._db is None:
            return 0
        recovered = 0
        for _ in range(max(0, int(limit))):
            now = datetime.utcnow()
            row = await self._db["corpora"].find_one_and_update(
                {
                    "$and": [
                        {
                            "$or": [
                                {"status": "deleting"},
                                {"cleanup_status": "partial"},
                            ]
                        },
                        {
                            "$or": [
                                {"cleanup_lease_until": {"$exists": False}},
                                {"cleanup_lease_until": {"$lte": now}},
                            ]
                        },
                        {
                            "$or": [
                                {"cleanup_retry_at": {"$exists": False}},
                                {"cleanup_retry_at": {"$lte": now}},
                            ]
                        },
                    ]
                },
                {
                    "$set": {
                        "cleanup_owner": self._cleanup_owner,
                        "cleanup_lease_until": now
                        + timedelta(minutes=CORPUS_CLEANUP_LEASE_MINUTES),
                        "cleanup_status": "running",
                        "updated_at": now,
                    },
                    "$unset": {"cleanup_retry_at": ""},
                },
                projection={"corpus_id": 1, "_id": 0},
                return_document=ReturnDocument.AFTER,
            )
            if not row:
                break
            corpus_id = str(row.get("corpus_id") or "").strip()
            if not corpus_id:
                break
            self._start_corpus_purge_task(corpus_id)
            recovered += 1
        return recovered

    async def _heartbeat_cleanup_lease(self, corpus_id: str) -> None:
        """Extend cleanup_lease_until while a long purge runs (P0.6).

        Owner-guarded: only the row this process claimed is renewed, so an
        expired-and-reclaimed corpus is never re-extended by the loser."""

        interval = max(20.0, CORPUS_CLEANUP_LEASE_MINUTES * 60 / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                await self._db["corpora"].update_one(
                    {
                        "corpus_id": corpus_id,
                        "cleanup_owner": self._cleanup_owner,
                    },
                    {
                        "$set": {
                            "cleanup_lease_until": datetime.utcnow()
                            + timedelta(minutes=CORPUS_CLEANUP_LEASE_MINUTES),
                            "cleanup_heartbeat_at": datetime.utcnow(),
                        }
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — heartbeat is best-effort
            logger.warning(
                "Cleanup lease heartbeat failed for corpus %s", corpus_id,
                exc_info=True,
            )

    async def _purge_corpus_bulk(self, corpus_id: str) -> None:
        """Background bulk cleanup for a deleted corpus: Mongo chunks +
        batched Neo4j graph. Best-effort — orphaned rows keyed by a dead
        corpus_id are harmless and re-runnable."""
        from services.storage.mongo_writer import delete_chunks_by_corpus, delete_corpus

        cleanup_warnings: list[dict[str, str]] = []
        heartbeat = asyncio.create_task(self._heartbeat_cleanup_lease(corpus_id))
        try:
            return await self._purge_corpus_bulk_stages(
                corpus_id, cleanup_warnings
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _purge_corpus_bulk_stages(
        self, corpus_id: str, cleanup_warnings: list[dict[str, str]]
    ) -> None:
        from services.storage.mongo_writer import delete_chunks_by_corpus, delete_corpus

        try:
            from services.ingestion.corpus_lexicon import delete_corpus_lexicon

            await delete_corpus_lexicon(self._db, corpus_id)
        except Exception as exc:
            logger.warning(
                "Background lexicon purge failed for corpus %s",
                corpus_id,
                exc_info=True,
            )
            cleanup_warnings.append(
                {
                    "stage": "mongo_lexicon",
                    "error": str(exc),
                    "at": datetime.utcnow().isoformat(),
                }
            )
        try:
            await delete_chunks_by_corpus(self._db, corpus_id)
        except Exception as exc:
            logger.warning(
                "Background chunk purge failed for corpus %s; finalizing tombstone anyway",
                corpus_id,
                exc_info=True,
            )
            cleanup_warnings.append(
                {
                    "stage": "mongo_chunks",
                    "error": str(exc),
                    "at": datetime.utcnow().isoformat(),
                }
            )
        if self._settings.NEO4J_ENABLED and self._neo4j:
            try:
                from services.graph.neo4j_writer import delete_corpus_graph

                await delete_corpus_graph(self._neo4j, corpus_id=corpus_id)
            except Exception as exc:
                logger.warning(
                    "Background Neo4j purge failed for corpus %s; finalizing tombstone anyway",
                    corpus_id,
                    exc_info=True,
                )
                cleanup_warnings.append(
                    {
                        "stage": "neo4j_graph",
                        "error": str(exc),
                        "at": datetime.utcnow().isoformat(),
                    }
                )
        try:
            await delete_corpus(self._db, corpus_id)
            cleanup_status = "partial" if cleanup_warnings else "complete"
            # Owner-guarded finalize (P0.6): if the lease expired mid-purge and
            # a replacement process reclaimed this corpus, the stale loser's
            # finalize becomes a no-op instead of clobbering the new owner's
            # state — the pair that makes competing-process and idempotent
            # replay behavior safe.
            await self._db["corpora"].update_one(
                {"corpus_id": corpus_id, "cleanup_owner": self._cleanup_owner},
                {
                    "$set": {
                        "cleanup_status": cleanup_status,
                        "cleanup_completed_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                        **(
                            {
                                "cleanup_retry_at": datetime.utcnow()
                                + timedelta(minutes=5)
                            }
                            if cleanup_warnings
                            else {}
                        ),
                        **(
                            {"cleanup_warnings": cleanup_warnings}
                            if cleanup_warnings
                            else {}
                        ),
                    },
                    "$unset": {
                        "cleanup_owner": "",
                        "cleanup_lease_until": "",
                        **({} if cleanup_warnings else {"cleanup_retry_at": ""}),
                    },
                },
            )
        except Exception:
            logger.warning(
                "Final corpus tombstone mark failed for corpus %s",
                corpus_id,
                exc_info=True,
            )
            return
        logger.info(
            "Background purge %s for corpus %s",
            "completed with warnings" if cleanup_warnings else "complete",
            corpus_id,
        )

    async def list_documents(
        self,
        corpus_id: str,
        user_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List all documents in a corpus."""
        from services.storage.mongo_reader import list_documents

        return await list_documents(
            self._db, corpus_id, user_id=user_id, limit=limit, offset=offset
        )

    async def delete_document(self, corpus_id: str, doc_id: str) -> bool:
        """Cascade delete a single document: Qdrant points → Neo4j nodes →
        Mongo chunks → Mongo doc. Corpus aggregate counts are repaired on
        subsequent get/list reads via `_refresh_corpus_counts()`.

        Returns True if the document row was removed from Mongo.
        """
        from services.storage.mongo_writer import (
            delete_chunks_by_doc,
            delete_document,
            retire_document_derived_state,
        )
        from services.storage.qdrant_writer import delete_points_by_doc

        # 1. Qdrant points across naive / hrag / graph (doc_id filter).
        try:
            await delete_points_by_doc(self._qdrant, corpus_id, doc_id)
        except Exception:
            logger.warning(
                "Qdrant per-doc delete failed for doc %s in corpus %s",
                doc_id[:12],
                corpus_id[:8],
            )

        # The universal Tier-0 collection is not part of the per-corpus
        # naive/hrag/graph collection family, so delete its deterministic
        # document routing card explicitly.
        try:
            from services.ingestion.tier0 import delete_doc_profile

            await delete_doc_profile(
                self._qdrant,
                corpus_id=corpus_id,
                doc_id=doc_id,
            )
        except Exception:
            logger.warning(
                "Tier-0 per-doc delete failed for doc %s in corpus %s",
                doc_id[:12],
                corpus_id[:8],
            )

        # 2. Neo4j — delete Entity and Mention nodes attached to this doc.
        if self._settings.NEO4J_ENABLED and self._neo4j:
            try:
                from services.graph.neo4j_writer import delete_document_graph

                await delete_document_graph(
                    self._neo4j,
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                )
            except Exception:
                logger.warning("Neo4j per-doc delete failed for doc %s", doc_id[:12])

        # 3. Reconcile the materialized vocabulary while source provenance is
        # still available. This is enrichment cleanup and must not prevent the
        # document tombstone when a vector provider is temporarily unavailable.
        try:
            from services.ingestion.corpus_lexicon import (
                index_affected_lexicon,
                remove_document_lexicon_sources,
            )

            lexicon_reconciliation = await remove_document_lexicon_sources(
                self._db,
                corpus_id=corpus_id,
                doc_id=doc_id,
            )
            await index_affected_lexicon(
                self._db,
                self._qdrant,
                corpus_id=corpus_id,
                entries=lexicon_reconciliation["entries"],
                stale_lexicon_ids=lexicon_reconciliation["stale_lexicon_ids"],
            )
        except Exception:
            logger.warning(
                "Lexicon per-doc delete reconciliation failed for doc %s",
                doc_id[:12],
                exc_info=True,
            )

        # 4. Remove derived hierarchy rows and retire every durable job for
        # this source identity before a same-content re-ingest can replan it.
        await retire_document_derived_state(
            self._db,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )

        # 5. Mongo chunks.
        await delete_chunks_by_doc(self._db, corpus_id, doc_id)

        # 6. Mongo document record and materialized corpus truth.
        deleted = await delete_document(self._db, corpus_id, doc_id)
        if deleted:
            await self._refresh_corpus_counts([{"corpus_id": corpus_id}])
        return deleted

    async def list_all_user_documents(
        self,
        user_id: str,
        limit: int = 100,
    ) -> list[dict]:
        """List all documents across all corpora for a user."""
        from services.storage.mongo_reader import list_all_user_documents

        return await list_all_user_documents(self._db, user_id=user_id, limit=limit)

    async def backfill_graph_failures(
        self,
        *,
        corpus_id: str,
        doc_id: str,
        user_id: str,
    ) -> dict:
        """Retry only failed Ghost B chunks and patch Neo4j incrementally."""
        from services.ingestion.graph_backfill import backfill_failed_graph_chunks

        result = await backfill_failed_graph_chunks(
            db=self._db,
            qdrant_client=self._qdrant,
            neo4j_driver=self._neo4j,
            corpus_id=corpus_id,
            doc_id=doc_id,
            user_id=user_id,
        )
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def plan_graph_promotion_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        apply: bool = False,
        limit: int = 100,
        max_chunks: int | None = None,
    ) -> dict:
        from services.ingestion.graph_promotion_jobs import plan_graph_promotion_jobs

        result = await plan_graph_promotion_jobs(
            self._db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=limit,
            max_chunks=max_chunks,
        )
        if apply:
            readiness = await self._materialize_corpus_readiness_safely(corpus_id)
            if readiness is not None:
                result["readiness"] = readiness
        return result

    async def list_graph_promotion_jobs(
        self,
        *,
        corpus_id: str,
        limit: int = 100,
        statuses: list[str] | None = None,
    ) -> dict:
        from services.ingestion.graph_promotion_jobs import list_graph_promotion_jobs

        return await list_graph_promotion_jobs(
            self._db,
            corpus_id=corpus_id,
            limit=limit,
            statuses=statuses,
        )

    async def run_graph_promotion_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str,
        limit: int = 5,
    ) -> dict:
        from services.ingestion.graph_promotion_jobs import run_graph_promotion_jobs

        paused = await self._backpressure_pause_result(
            corpus_id=corpus_id,
            lane_key="graph_promotion_allowed",
            operation="graph_promotion_jobs.run",
        )
        if paused is not None:
            paused["counts"] = {}
            return paused

        async def _execute_graph_lane() -> dict:
            return await run_graph_promotion_jobs(
                self._db,
                qdrant_client=self._qdrant,
                neo4j_driver=self._neo4j,
                corpus_id=corpus_id,
                user_id=user_id,
                limit=limit,
            )

        result = await self._run_owned_repair_lane(
            corpus_id=corpus_id,
            lane="graph_promotion",
            operation="graph_promotion_jobs.run",
            runner=_execute_graph_lane,
        )
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def plan_extraction_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str,
        apply: bool = False,
        limit: int = 500,
        include_succeeded: bool = False,
    ) -> dict:
        from services.ingestion.extraction_jobs import plan_extraction_jobs

        result = await plan_extraction_jobs(
            self._db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=limit,
            include_succeeded=include_succeeded,
        )
        if apply:
            readiness = await self._materialize_corpus_readiness_safely(corpus_id)
            if readiness is not None:
                result["readiness"] = readiness
        return result

    async def list_extraction_jobs(
        self,
        *,
        corpus_id: str,
        limit: int = 100,
        statuses: list[str] | None = None,
    ) -> dict:
        from services.ingestion.extraction_jobs import list_extraction_jobs

        return await list_extraction_jobs(
            self._db,
            corpus_id=corpus_id,
            limit=limit,
            statuses=statuses,
        )

    async def plan_source_parse_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        apply: bool = False,
        limit: int = 500,
    ) -> dict:
        from services.ingestion.source_parse_jobs import plan_source_parse_jobs

        result = await plan_source_parse_jobs(
            self._db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=limit,
        )
        if apply:
            readiness = await self._materialize_corpus_readiness_safely(corpus_id)
            if readiness is not None:
                result["readiness"] = readiness
        return result

    async def list_source_parse_jobs(
        self,
        *,
        corpus_id: str,
        limit: int = 100,
        statuses: list[str] | None = None,
    ) -> dict:
        from services.ingestion.source_parse_jobs import list_source_parse_jobs

        return await list_source_parse_jobs(
            self._db,
            corpus_id=corpus_id,
            limit=limit,
            statuses=statuses,
        )

    async def run_source_parse_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str,
        limit: int = 25,
        statuses: list[str] | None = None,
    ) -> dict:
        from services.ingestion.source_parse_jobs import run_source_parse_jobs

        start_runners = bool(get_settings().INGEST_RUNNERS_ENABLED)

        async def _execute_source_lane() -> dict:
            return await run_source_parse_jobs(
                self._db,
                corpus_id=corpus_id,
                user_id=user_id,
                ingestion_service=self if start_runners else None,
                limit=limit,
                statuses=statuses,
                start_runners=start_runners,
            )

        result = await self._run_owned_repair_lane(
            corpus_id=corpus_id,
            lane="source_parse",
            operation="source_parse_jobs.run",
            runner=_execute_source_lane,
        )
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def run_extraction_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str,
        limit: int = 25,
        statuses: list[str] | None = None,
    ) -> dict:
        from services.ingestion.extraction_jobs import run_extraction_jobs

        paused = await self._backpressure_pause_result(
            corpus_id=corpus_id,
            lane_key="extraction_backfill_allowed",
            operation="extraction_jobs.run",
        )
        if paused is not None:
            paused.update({"claimed": 0, "counts": {}})
            return paused

        async def _execute_extraction_lane() -> dict:
            return await run_extraction_jobs(
                self._db,
                qdrant_client=self._qdrant,
                corpus_id=corpus_id,
                user_id=user_id,
                limit=limit,
                statuses=statuses,
            )

        result = await self._run_owned_repair_lane(
            corpus_id=corpus_id,
            lane="extraction",
            operation="extraction_jobs.run",
            runner=_execute_extraction_lane,
        )
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def plan_document_pipeline_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        apply: bool = False,
        limit: int = 500,
        kinds: list[str] | None = None,
    ) -> dict:
        from services.ingestion.document_pipeline_jobs import (
            plan_document_pipeline_jobs,
        )

        result = await plan_document_pipeline_jobs(
            self._db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=limit,
            kinds=kinds,
        )
        if apply:
            readiness = await self._materialize_corpus_readiness_safely(corpus_id)
            if readiness is not None:
                result["readiness"] = readiness
        return result

    async def list_document_pipeline_jobs(
        self,
        *,
        corpus_id: str,
        limit: int = 100,
        statuses: list[str] | None = None,
        kinds: list[str] | None = None,
    ) -> dict:
        from services.ingestion.document_pipeline_jobs import (
            list_document_pipeline_jobs,
        )

        return await list_document_pipeline_jobs(
            self._db,
            corpus_id=corpus_id,
            limit=limit,
            statuses=statuses,
            kinds=kinds,
        )

    async def run_document_pipeline_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        limit: int = 25,
        statuses: list[str] | None = None,
        kinds: list[str] | None = None,
    ) -> dict:
        from services.ingestion.document_pipeline_jobs import run_document_pipeline_jobs

        paused = await self._backpressure_pause_result(
            corpus_id=corpus_id,
            lane_key="document_pipeline_allowed",
            operation="document_pipeline_jobs.run",
        )
        if paused is not None:
            paused.update(
                {
                    "claimed": 0,
                    "source_claimed": 0,
                    "source_requested": False,
                    "source_result": None,
                    "executor_missing_kinds": [],
                    "counts": {},
                    "jobs": [],
                }
            )
            return paused

        async def _source_runner(*, limit: int) -> dict:
            return await self.run_source_parse_jobs(
                corpus_id=corpus_id,
                user_id=user_id,
                limit=limit,
            )

        async def _persist_runner(*, doc_ids: list[str], limit: int) -> dict:
            from services.ingestion.document_pipeline_executors import (
                mark_documents_persisted_from_artifacts,
            )

            return await mark_documents_persisted_from_artifacts(
                self._db,
                corpus_id=corpus_id,
                doc_ids=doc_ids,
                limit=limit,
            )

        async def _embed_runner(*, doc_ids: list[str], limit: int) -> dict:
            from services.ingestion.document_pipeline_executors import (
                embed_documents_to_qdrant_from_artifacts,
            )

            return await embed_documents_to_qdrant_from_artifacts(
                self._db,
                qdrant_client=self._qdrant,
                neo4j_driver=self._neo4j,
                corpus_id=corpus_id,
                doc_ids=doc_ids,
                limit=limit,
            )

        async def _profile_runner(*, doc_ids: list[str], limit: int) -> dict:
            from services.ingestion.tier0 import embed_doc_profiles

            corpus = await self._db["corpora"].find_one(
                {"corpus_id": corpus_id},
                {"_id": 0, "default_ingestion_config.embedding_dimension": 1},
            )
            dimension = int(
                ((corpus or {}).get("default_ingestion_config") or {}).get(
                    "embedding_dimension", 1024
                )
            )
            result = await embed_doc_profiles(
                self._db,
                self._qdrant,
                corpus_id=corpus_id,
                doc_ids=doc_ids[: max(1, int(limit))],
                dim=dimension,
            )
            return {
                "status": "complete",
                "counts": {
                    "succeeded": int(result.get("embedded") or 0),
                    "failed": 0,
                },
                **result,
            }

        async def _summary_runner(*, doc_ids: list[str], limit: int) -> dict:
            from services.ingestion.document_pipeline_executors import (
                index_document_summaries_from_artifacts,
            )

            return await index_document_summaries_from_artifacts(
                self._db,
                qdrant_client=self._qdrant,
                neo4j_driver=self._neo4j,
                corpus_id=corpus_id,
                doc_ids=doc_ids,
                limit=limit,
            )

        async def _execute_document_lane() -> dict:
            return await run_document_pipeline_jobs(
                self._db,
                corpus_id=corpus_id,
                user_id=user_id,
                limit=limit,
                statuses=statuses,
                kinds=kinds,
                source_runner=_source_runner,
                persist_runner=_persist_runner,
                embed_runner=_embed_runner,
                summary_runner=_summary_runner,
                profile_runner=_profile_runner,
            )

        result = await self._run_owned_repair_lane(
            corpus_id=corpus_id,
            lane="document_pipeline",
            operation="document_pipeline_jobs.run",
            runner=_execute_document_lane,
        )
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def plan_summary_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        apply: bool = False,
        limit: int = 500,
        kinds: list[str] | None = None,
    ) -> dict:
        from services.ingestion.summary_jobs import plan_summary_jobs

        result = await plan_summary_jobs(
            self._db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=limit,
            kinds=kinds,
        )
        if apply:
            readiness = await self._materialize_corpus_readiness_safely(corpus_id)
            if readiness is not None:
                result["readiness"] = readiness
        return result

    async def list_summary_jobs(
        self,
        *,
        corpus_id: str,
        limit: int = 100,
        statuses: list[str] | None = None,
        kinds: list[str] | None = None,
    ) -> dict:
        from services.ingestion.summary_jobs import list_summary_jobs

        return await list_summary_jobs(
            self._db,
            corpus_id=corpus_id,
            limit=limit,
            statuses=statuses,
            kinds=kinds,
        )

    async def run_summary_jobs(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        limit: int = 25,
        statuses: list[str] | None = None,
        kinds: list[str] | None = None,
    ) -> dict:
        from services.ingestion.summary_jobs import run_summary_jobs

        pressure_readiness = await self._compute_corpus_readiness_safely(corpus_id)
        paused = await self._backpressure_pause_result(
            corpus_id=corpus_id,
            lane_key="summary_generation_allowed",
            operation="summary_jobs.run",
            readiness=pressure_readiness,
        )
        if paused is not None:
            paused.update(
                {
                    "claimed": 0,
                    "parent_claimed": 0,
                    "document_claimed": 0,
                    "counts": {},
                    "runner_results": {},
                    "jobs": [],
                }
            )
            return paused

        summary_backpressure = ((pressure_readiness or {}).get("pressure") or {}).get(
            "backpressure"
        ) or {}
        summary_indexing_allowed = (
            summary_backpressure.get("summary_indexing_allowed") is not False
        )

        async def _parent_runner(
            *, limit: int, doc_ids: list[str] | None = None
        ) -> dict:
            result = await self.backfill_parent_summaries(
                corpus_id,
                user_id=user_id,
                generate=True,
                index=summary_indexing_allowed,
                limit=limit,
                batch=min(max(int(limit or 1), 1), 32),
                doc_ids=doc_ids,
            )
            if not summary_indexing_allowed:
                result["index_scope"] = "paused_qdrant_pressure"
                result["index_deferred_by_pressure"] = True
            return result

        async def _document_runner(
            *, limit: int, doc_ids: list[str] | None = None
        ) -> dict:
            return await self.backfill_document_summaries(
                corpus_id=corpus_id,
                user_id=user_id,
                limit=limit,
                doc_ids=doc_ids,
            )

        async def _execute_summary_lane() -> dict:
            return await run_summary_jobs(
                self._db,
                corpus_id=corpus_id,
                user_id=user_id,
                limit=limit,
                statuses=statuses,
                kinds=kinds,
                parent_runner=_parent_runner,
                document_runner=_document_runner,
            )

        result = await self._run_owned_repair_lane(
            corpus_id=corpus_id,
            lane="summary",
            operation="summary_jobs.run",
            runner=_execute_summary_lane,
        )
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def run_bounded_corpus_repair_cycle(
        self,
        *,
        corpus_id: str,
        user_id: str,
        apply: bool = False,
        reconcile_failures: bool = True,
        failure_reconcile_limit: int = 5000,
        backfill_promoted_extraction_marks_rows: bool = True,
        promoted_extraction_marks_backfill_limit: int = 100,
        backfill_source_parse_stage_identity_rows: bool = True,
        source_parse_stage_identity_backfill_limit: int = 1000,
        backfill_ghost_b_stage_identity_rows: bool = True,
        ghost_b_stage_identity_backfill_limit: int = 1000,
        plan_source_parse_jobs: bool = True,
        source_parse_job_plan_limit: int = 500,
        run_source_parse_jobs: bool = False,
        source_parse_job_run_limit: int = 25,
        plan_document_pipeline_jobs: bool = True,
        document_pipeline_job_plan_limit: int = 500,
        run_document_pipeline_jobs: bool = False,
        document_pipeline_job_run_limit: int = 25,
        plan_graph_jobs: bool = True,
        graph_plan_limit: int = 100,
        graph_max_chunks: int | None = None,
        plan_extraction_jobs: bool = True,
        extraction_job_plan_limit: int = 500,
        run_extraction_jobs: bool = False,
        extraction_job_run_limit: int = 25,
        plan_summary_jobs: bool = True,
        summary_job_plan_limit: int = 500,
        backfill_summary_stage_identity_rows: bool = True,
        summary_stage_identity_backfill_limit: int = 1000,
        run_summary_jobs: bool = False,
        summary_job_run_limit: int = 25,
        run_document_summaries: bool = False,
        document_summary_limit: int = 10,
        run_graph_jobs: bool = False,
        graph_run_limit: int = 3,
        record_run: bool = True,
    ) -> dict:
        from services.ingestion.corpus_repair import run_bounded_corpus_repair_cycle

        return await run_bounded_corpus_repair_cycle(
            self._db,
            corpus_id=corpus_id,
            user_id=user_id,
            ingestion_service=self,
            qdrant_client=self._qdrant,
            neo4j_driver=self._neo4j,
            apply=apply,
            reconcile_failures=reconcile_failures,
            failure_reconcile_limit=failure_reconcile_limit,
            backfill_promoted_extraction_marks_rows=backfill_promoted_extraction_marks_rows,
            promoted_extraction_marks_backfill_limit=promoted_extraction_marks_backfill_limit,
            backfill_source_parse_stage_identity_rows=backfill_source_parse_stage_identity_rows,
            source_parse_stage_identity_backfill_limit=source_parse_stage_identity_backfill_limit,
            backfill_ghost_b_stage_identity_rows=backfill_ghost_b_stage_identity_rows,
            ghost_b_stage_identity_backfill_limit=ghost_b_stage_identity_backfill_limit,
            plan_source_parse_job_rows=plan_source_parse_jobs,
            source_parse_job_plan_limit=source_parse_job_plan_limit,
            run_source_parse_job_rows=run_source_parse_jobs,
            source_parse_job_run_limit=source_parse_job_run_limit,
            source_parse_start_runners=bool(get_settings().INGEST_RUNNERS_ENABLED),
            plan_document_pipeline_job_rows=plan_document_pipeline_jobs,
            document_pipeline_job_plan_limit=document_pipeline_job_plan_limit,
            run_document_pipeline_job_rows=run_document_pipeline_jobs,
            document_pipeline_job_run_limit=document_pipeline_job_run_limit,
            plan_graph_jobs=plan_graph_jobs,
            graph_plan_limit=graph_plan_limit,
            graph_max_chunks=graph_max_chunks,
            plan_extraction_job_rows=plan_extraction_jobs,
            extraction_job_plan_limit=extraction_job_plan_limit,
            run_extraction_job_rows=run_extraction_jobs,
            extraction_job_run_limit=extraction_job_run_limit,
            plan_summary_job_rows=plan_summary_jobs,
            summary_job_plan_limit=summary_job_plan_limit,
            backfill_summary_stage_identity_rows=backfill_summary_stage_identity_rows,
            summary_stage_identity_backfill_limit=summary_stage_identity_backfill_limit,
            run_summary_job_rows=run_summary_jobs,
            summary_job_run_limit=summary_job_run_limit,
            run_document_summaries=run_document_summaries,
            document_summary_limit=document_summary_limit,
            run_graph_jobs=run_graph_jobs,
            graph_run_limit=graph_run_limit,
            record_run=record_run,
        )

    async def run_auto_corpus_repair_tick(
        self,
        *,
        limit: int | None = None,
        _corpus_id: str | None = None,
        _fanout: bool = True,
    ) -> dict[str, Any]:
        """Plan safe repair queues for recently active corpora.

        This is the worker-owned maintenance loop that keeps corpus readiness
        and durable repair queues current without waiting for an operator to
        click a button. By default it plans/reconciles only. Provider-backed or
        write-heavy execution is gated by explicit settings flags.
        """

        settings = get_settings()
        if not bool(getattr(settings, "INGEST_AUTO_REPAIR_ENABLED", True)):
            return {"status": "disabled", "scanned": 0, "corpora": []}

        limit = max(
            1,
            min(
                int(
                    limit
                    or getattr(settings, "INGEST_AUTO_REPAIR_CORPUS_LIMIT", 5)
                    or 5
                ),
                100,
            ),
        )
        corpus_filter: dict[str, Any] = {}
        if _corpus_id:
            corpus_filter["corpus_id"] = _corpus_id
        rows = (
            await self._db["corpora"]
            .find(
                with_active_records(corpus_filter),
                {"_id": 0, "corpus_id": 1, "user_id": 1, "name": 1, "updated_at": 1},
            )
            .sort("updated_at", -1)
            .limit(limit)
            .to_list(length=limit)
        )

        # A long local pipeline repair in one corpus must not serialize cloud
        # extraction or summary work for another corpus. Fan out one-corpus
        # ticks under a small bounded semaphore; the normal durable lane
        # leases and provider credential semaphores still prevent duplicate or
        # over-budget execution inside each child tick.
        if _fanout and not _corpus_id and len(rows) > 1:
            corpus_concurrency = max(
                1,
                min(
                    int(
                        getattr(
                            settings,
                            "INGEST_AUTO_REPAIR_CORPUS_CONCURRENCY",
                            3,
                        )
                        or 3
                    ),
                    len(rows),
                ),
            )
            semaphore = asyncio.Semaphore(corpus_concurrency)

            async def _run_one(row: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    return await self.run_auto_corpus_repair_tick(
                        limit=1,
                        _corpus_id=str(row.get("corpus_id") or ""),
                        _fanout=False,
                    )

            child_results = await asyncio.gather(
                *(_run_one(row) for row in rows),
                return_exceptions=True,
            )
            corpora: list[dict[str, Any]] = []
            changed = 0
            for row, child in zip(rows, child_results, strict=True):
                if isinstance(child, Exception):
                    corpora.append(
                        {
                            "corpus_id": str(row.get("corpus_id") or ""),
                            "status": "failed",
                            "error": str(child)[:500],
                        }
                    )
                    changed += 1
                    continue
                corpora.extend(child.get("corpora") or [])
                changed += int(child.get("changed") or 0)
            return {
                "status": "complete",
                "scanned": len(corpora),
                "changed": changed,
                "corpora": corpora,
                "corpus_concurrency": corpus_concurrency,
            }

        results: list[dict[str, Any]] = []
        for row in rows:
            corpus_id = str(row.get("corpus_id") or "")
            if not corpus_id:
                continue
            user_id = str(row.get("user_id") or "")
            run_extraction_lane = bool(
                getattr(settings, "INGEST_AUTO_REPAIR_RUN_EXTRACTION", False)
            )
            run_summary_lane = bool(
                getattr(settings, "INGEST_AUTO_REPAIR_RUN_SUMMARIES", False)
            )
            run_document_lane = bool(
                getattr(settings, "INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE", False)
            )
            run_graph_lane = bool(
                getattr(settings, "INGEST_AUTO_REPAIR_RUN_GRAPH", True)
            )
            document_run_limit = int(
                getattr(settings, "INGEST_AUTO_REPAIR_DOCUMENT_RUN_LIMIT", 25) or 25
            )
            extraction_run_limit = int(
                getattr(settings, "INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT", 100) or 100
            )
            summary_run_limit = int(
                getattr(settings, "INGEST_AUTO_REPAIR_SUMMARY_RUN_LIMIT", 100) or 100
            )
            graph_run_limit = int(
                getattr(settings, "INGEST_AUTO_REPAIR_GRAPH_RUN_LIMIT", 5) or 5
            )
            scheduler_snapshot: dict[str, Any] | None = None
            try:
                from services.ingestion.repair_scheduler import (
                    backoff_decision,
                    load_scheduler_state,
                    quick_repair_gap_snapshot,
                    record_scheduler_outcome,
                )

                scheduler_snapshot = await quick_repair_gap_snapshot(
                    self._db,
                    corpus_id,
                )
                scheduler_state = await load_scheduler_state(self._db, corpus_id)
                scheduler_now = datetime.utcnow()
                decision = backoff_decision(
                    snapshot=scheduler_snapshot,
                    state=scheduler_state,
                    now=scheduler_now,
                )
                if not decision["should_run"]:
                    await record_scheduler_outcome(
                        self._db,
                        corpus_id=corpus_id,
                        snapshot=scheduler_snapshot,
                        changed=False,
                        now=scheduler_now,
                        base_seconds=int(
                            getattr(settings, "INGEST_AUTO_REPAIR_POLL_SECONDS", 120)
                            or 120
                        ),
                        max_seconds=int(
                            getattr(
                                settings,
                                "INGEST_AUTO_REPAIR_MAX_BACKOFF_SECONDS",
                                3600,
                            )
                            or 3600
                        ),
                    )
                    results.append(
                        {
                            "corpus_id": corpus_id,
                            "status": "idle",
                            "scheduler_reason": decision["reason"],
                            "gap_snapshot": scheduler_snapshot,
                            "provider_lanes": {},
                        }
                    )
                    continue
                result = await self.run_bounded_corpus_repair_cycle(
                    corpus_id=corpus_id,
                    user_id=user_id,
                    apply=True,
                    reconcile_failures=True,
                    failure_reconcile_limit=5000,
                    backfill_ghost_b_stage_identity_rows=(
                        int(
                            getattr(
                                settings,
                                "INGEST_AUTO_REPAIR_STAGE_IDENTITY_LIMIT",
                                1000,
                            )
                            or 0
                        )
                        > 0
                    ),
                    backfill_source_parse_stage_identity_rows=(
                        int(
                            getattr(
                                settings,
                                "INGEST_AUTO_REPAIR_STAGE_IDENTITY_LIMIT",
                                1000,
                            )
                            or 0
                        )
                        > 0
                    ),
                    source_parse_stage_identity_backfill_limit=int(
                        getattr(
                            settings, "INGEST_AUTO_REPAIR_STAGE_IDENTITY_LIMIT", 1000
                        )
                        or 0
                    ),
                    ghost_b_stage_identity_backfill_limit=int(
                        getattr(
                            settings, "INGEST_AUTO_REPAIR_STAGE_IDENTITY_LIMIT", 1000
                        )
                        or 0
                    ),
                    plan_source_parse_jobs=True,
                    source_parse_job_plan_limit=500,
                    run_source_parse_jobs=False,
                    plan_document_pipeline_jobs=True,
                    document_pipeline_job_plan_limit=500,
                    run_document_pipeline_jobs=False,
                    document_pipeline_job_run_limit=document_run_limit,
                    plan_graph_jobs=True,
                    graph_plan_limit=100,
                    plan_extraction_jobs=True,
                    extraction_job_plan_limit=500,
                    # Provider-backed lanes run independently below. Keeping
                    # them out of the sequential repair cycle lets extraction
                    # and summarization overlap without weakening their
                    # separate durable queues or pressure gates.
                    run_extraction_jobs=False,
                    extraction_job_run_limit=extraction_run_limit,
                    plan_summary_jobs=True,
                    summary_job_plan_limit=500,
                    backfill_summary_stage_identity_rows=(
                        int(
                            getattr(
                                settings,
                                "INGEST_AUTO_REPAIR_STAGE_IDENTITY_LIMIT",
                                1000,
                            )
                            or 0
                        )
                        > 0
                    ),
                    summary_stage_identity_backfill_limit=int(
                        getattr(
                            settings, "INGEST_AUTO_REPAIR_STAGE_IDENTITY_LIMIT", 1000
                        )
                        or 0
                    ),
                    run_summary_jobs=False,
                    summary_job_run_limit=summary_run_limit,
                    run_document_summaries=False,
                    run_graph_jobs=False,
                    graph_run_limit=graph_run_limit,
                    record_run=False,
                )

                lane_names: list[str] = []
                lane_coroutines: list[Any] = []
                if run_document_lane:
                    lane_names.append("document_pipeline")
                    lane_coroutines.append(
                        self.run_document_pipeline_jobs(
                            corpus_id=corpus_id,
                            user_id=user_id,
                            limit=document_run_limit,
                        )
                    )
                if run_summary_lane:
                    lane_names.append("summary")
                    lane_coroutines.append(
                        self.run_summary_jobs(
                            corpus_id=corpus_id,
                            user_id=user_id,
                            limit=summary_run_limit,
                        )
                    )
                if run_extraction_lane:
                    lane_names.append("extraction")
                    lane_coroutines.append(
                        self.run_extraction_jobs(
                            corpus_id=corpus_id,
                            user_id=user_id,
                            limit=extraction_run_limit,
                        )
                    )
                if run_graph_lane:
                    lane_names.append("graph_promotion")
                    lane_coroutines.append(
                        self.run_graph_promotion_jobs(
                            corpus_id=corpus_id,
                            user_id=user_id,
                            limit=graph_run_limit,
                        )
                    )

                provider_lanes: dict[str, dict[str, Any]] = {}
                if lane_coroutines:
                    lane_results = await asyncio.gather(
                        *lane_coroutines,
                        return_exceptions=True,
                    )
                    for lane_name, lane_result in zip(
                        lane_names,
                        lane_results,
                        strict=True,
                    ):
                        if isinstance(lane_result, Exception):
                            provider_lanes[lane_name] = {
                                "status": "failed",
                                "claimed": 0,
                                "error": str(lane_result)[:500],
                            }
                        else:
                            provider_lanes[lane_name] = lane_result
                    logger.info(
                        "Auto repair lanes corpus=%s document=%s summary=%s extraction=%s graph=%s",
                        corpus_id[:8],
                        {
                            key: provider_lanes.get("document_pipeline", {}).get(key)
                            for key in ("status", "claimed")
                        },
                        {
                            key: provider_lanes.get("summary", {}).get(key)
                            for key in ("status", "claimed")
                        },
                        {
                            key: provider_lanes.get("extraction", {}).get(key)
                            for key in ("status", "claimed")
                        },
                        {
                            key: provider_lanes.get("graph_promotion", {}).get(key)
                            for key in ("status", "claimed")
                        },
                    )
                result["provider_lanes"] = provider_lanes
                summary = result.get("summary") or {}
                result_row = {
                    "corpus_id": corpus_id,
                    "status": result.get("status"),
                    "readiness_status": summary.get("readiness_status"),
                    "queryable_docs": summary.get("queryable_docs"),
                    "total_docs": summary.get("total_docs"),
                    "failed_chunks": summary.get("failed_chunks"),
                    "graph_jobs_queued": summary.get("graph_jobs_queued"),
                    "extraction_jobs_queued": summary.get("extraction_jobs_queued"),
                    "summary_jobs_queued": summary.get("summary_jobs_queued"),
                    "document_pipeline_jobs_queued": summary.get(
                        "document_pipeline_jobs_queued"
                    ),
                    "ghost_b_stage_identity_backfilled": summary.get(
                        "ghost_b_stage_identity_backfilled"
                    ),
                    "source_parse_stage_identity_backfilled": summary.get(
                        "source_parse_stage_identity_backfilled"
                    ),
                    "summary_stage_identity_backfilled": summary.get(
                        "summary_stage_identity_backfilled"
                    ),
                    "provider_lanes": {
                        lane_name: {
                            "status": lane_result.get("status"),
                            "claimed": int(lane_result.get("claimed") or 0),
                            "counts": lane_result.get("counts") or {},
                        }
                        for lane_name, lane_result in provider_lanes.items()
                    },
                    "repair_changed": any(
                        bool(step.get("changed"))
                        for step in (result.get("steps") or [])
                    ),
                }
                results.append(result_row)
                if scheduler_snapshot is not None:
                    useful_change = bool(result_row["repair_changed"]) or any(
                        int(lane.get("claimed") or 0)
                        for lane in result_row["provider_lanes"].values()
                    )
                    await record_scheduler_outcome(
                        self._db,
                        corpus_id=corpus_id,
                        snapshot=scheduler_snapshot,
                        changed=useful_change,
                        base_seconds=int(
                            getattr(settings, "INGEST_AUTO_REPAIR_POLL_SECONDS", 120)
                            or 120
                        ),
                        max_seconds=int(
                            getattr(
                                settings,
                                "INGEST_AUTO_REPAIR_MAX_BACKOFF_SECONDS",
                                3600,
                            )
                            or 3600
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - maintenance must not kill worker
                logger.warning(
                    "Auto corpus repair tick failed corpus=%s: %s",
                    corpus_id[:8],
                    exc,
                )
                results.append(
                    {
                        "corpus_id": corpus_id,
                        "status": "failed",
                        "error": str(exc)[:500],
                    }
                )
        changed = [
            row
            for row in results
            if int(row.get("graph_jobs_queued") or 0)
            or int(row.get("extraction_jobs_queued") or 0)
            or int(row.get("summary_jobs_queued") or 0)
            or int(row.get("document_pipeline_jobs_queued") or 0)
            or int(row.get("source_parse_stage_identity_backfilled") or 0)
            or int(row.get("summary_stage_identity_backfilled") or 0)
            or int(row.get("failed_chunks") or 0)
            or bool(row.get("repair_changed"))
            or any(
                int(lane.get("claimed") or 0)
                for lane in (row.get("provider_lanes") or {}).values()
            )
            or row.get("status") == "failed"
        ]
        return {
            "status": "complete",
            "scanned": len(results),
            "changed": len(changed),
            "corpora": results,
        }

    async def backfill_document_summaries(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        limit: int = 25,
        doc_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        from services.ingestion.document_summaries import backfill_document_summaries

        paused = await self._backpressure_pause_result(
            corpus_id=corpus_id,
            lane_key="summary_generation_allowed",
            operation="document_summaries.backfill",
        )
        if paused is not None:
            paused.update({"attempted": 0, "built": 0, "skipped": 0, "failed": 0})
            return paused

        result = await backfill_document_summaries(
            self._db,
            corpus_id=corpus_id,
            qdrant_client=self._qdrant,
            user_id=user_id,
            limit=limit,
            doc_ids=doc_ids,
        )
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def audit_corpus_idempotency(
        self,
        *,
        corpus_id: str,
        group_limit: int = 25,
        missing_limit: int = 25,
    ) -> dict[str, Any]:
        """Return exact duplicate/source identity gaps for a corpus."""
        from services.ingestion.idempotency_audit import audit_corpus_idempotency

        return await audit_corpus_idempotency(
            self._db,
            corpus_id=corpus_id,
            group_limit=group_limit,
            missing_limit=missing_limit,
        )

    async def get_ingestion_audit(self, corpus_id: str) -> dict:
        """Aggregate corpus ingestion health for large-batch readiness."""
        docs = (
            await self._db["documents"]
            .find(
                {"corpus_id": corpus_id},
                {
                    "doc_id": 1,
                    "filename": 1,
                    "chunk_count": 1,
                    "ghost_b_failures": 1,
                    "ghost_b_staging": 1,
                    "ghost_b_failure_count": 1,
                    "ghost_b_staging_count": 1,
                    "ghost_b_metrics": 1,
                    "write_state": 1,
                    "_id": 0,
                },
            )
            .to_list(length=None)
        )
        total_chunks = await self._db["chunks"].count_documents(
            {"corpus_id": corpus_id}
        )
        totals = {
            "docs": len(docs),
            "chunks": total_chunks,
            "warning_docs": 0,
            "verify_failed_docs": 0,
            "graph_partial_docs": 0,
            "ghost_b_failed_chunks": 0,
            "ghost_b_extracted_chunks": 0,
            "ghost_b_tokens": 0,
            "ghost_b_attempts": 0,
            "relations": 0,
            "related_to": 0,
            "domain_range_remaps": 0,
            "domain_range_warns": 0,
            "endpoint_completions": 0,
            "evidence_cue_repairs": 0,
        }
        partial_docs: list[dict] = []
        for doc in docs:
            ws = doc.get("write_state") or {}
            warnings = ws.get("warnings") or []
            failures = doc.get("ghost_b_failures") or []
            failure_count = int(doc.get("ghost_b_failure_count") or len(failures))
            staged = doc.get("ghost_b_staging") or []
            staged_count = int(doc.get("ghost_b_staging_count") or len(staged))
            metrics = doc.get("ghost_b_metrics") or {}
            if warnings:
                totals["warning_docs"] += 1
            if ws.get("verified") is False:
                totals["verify_failed_docs"] += 1
            if failure_count:
                totals["graph_partial_docs"] += 1
                partial_docs.append(
                    {
                        "doc_id": doc.get("doc_id"),
                        "filename": doc.get("filename"),
                        "failed_chunks": failure_count,
                    }
                )
            totals["ghost_b_failed_chunks"] += failure_count
            totals["ghost_b_extracted_chunks"] += int(
                metrics.get("extracted_chunks") or staged_count
            )
            totals["ghost_b_tokens"] += int(metrics.get("total_tokens") or 0)
            totals["ghost_b_attempts"] += int(metrics.get("attempt_count") or 0)
            totals["relations"] += int(metrics.get("relation_count") or 0)
            totals["related_to"] += int(metrics.get("related_to_count") or 0)
            totals["domain_range_remaps"] += int(
                metrics.get("domain_range_remap_count") or 0
            )
            totals["domain_range_warns"] += int(
                metrics.get("domain_range_warn_count") or 0
            )
            totals["endpoint_completions"] += int(
                metrics.get("endpoint_completion_count") or 0
            )
            totals["evidence_cue_repairs"] += int(
                metrics.get("evidence_cue_repair_count") or 0
            )

        totals["ghost_b_success_rate"] = (
            round(totals["ghost_b_extracted_chunks"] / total_chunks, 4)
            if total_chunks
            else 1.0
        )
        totals["related_to_ratio"] = (
            round(totals["related_to"] / totals["relations"], 4)
            if totals["relations"]
            else 0.0
        )
        readiness = "ready"
        if totals["verify_failed_docs"] or totals["ghost_b_failed_chunks"]:
            readiness = "needs_backfill"
        if (
            totals["related_to_ratio"] > 0.35
            or totals["domain_range_remaps"] > totals["relations"] * 0.2
        ):
            readiness = "schema_review"
        try:
            idempotency = await self.audit_corpus_idempotency(
                corpus_id=corpus_id,
                group_limit=10,
                missing_limit=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "idempotency audit failed for corpus=%s: %s",
                str(corpus_id)[:8],
                exc,
            )
            idempotency = {"status": "audit_failed", "error": str(exc)[:500]}

        return {
            "corpus_id": corpus_id,
            "readiness": readiness,
            "totals": totals,
            "partial_docs": partial_docs[:50],
            "idempotency": idempotency,
            "recommendations": [
                (
                    "Run graph backfill for partial docs before large-batch graph analysis."
                    if totals["ghost_b_failed_chunks"]
                    else "No graph backfill needed."
                ),
                (
                    "Review relation schema if related_to ratio stays above 35%."
                    if totals["related_to_ratio"] > 0.35
                    else "Relation specificity is within the current target band."
                ),
            ],
        }

    async def backfill_parent_summaries(
        self,
        corpus_id: str,
        *,
        user_id: str | None = None,
        generate: bool = True,
        index: bool = True,
        limit: int | None = None,
        batch: int = 32,
        doc_ids: list[str] | None = None,
        index_existing_doc_summaries: bool = False,
    ) -> dict[str, Any]:
        """Repair parent-summary retrieval for an existing corpus.

        This intentionally does not mutate the frozen ``chunk_summarization``
        ingest flag. It fills missing retrieval parent summaries, idempotently
        indexes parent-summary text into Qdrant, and updates each document's
        ``write_state.summaries_indexed`` when required parent summaries are covered.
        Capped generate+index calls index only summaries generated in that
        call. A known post-batch document scope may explicitly request all
        existing summaries through ``index_existing_doc_summaries``. Explicit
        index-only calls still rebuild all existing summary points. This keeps
        a handful of failed durable jobs from re-embedding whole documents.
        """

        from pymongo import UpdateOne
        from qdrant_client import models

        from services.embedder import embed_batch
        from services.ghost_a import SummaryTask, summarize_parents
        from services.ingestion.summary_backfill import summary_index_text
        from services.settings import settings_service
        from services.storage.qdrant_writer import _col_for_corpus, upsert_summaries

        corpus = await self._get_corpus_raw(corpus_id)
        if not corpus:
            return {
                "corpus_id": corpus_id,
                "status": "not_found",
                "error": "corpus not found",
            }

        cfg = IngestionConfig(**(corpus.get("default_ingestion_config") or {}))
        effective_user_id = user_id or str(corpus.get("user_id") or "")
        batch = max(1, min(int(batch or 32), 128))
        if limit is not None:
            limit = max(0, int(limit))
        doc_id_filter = (
            sorted({str(doc_id) for doc_id in doc_ids if str(doc_id).strip()})
            if doc_ids is not None
            else None
        )

        summary_parent_clause = parent_summary_required_clause()
        summary_text_clause = {"summary": {"$exists": True, "$nin": [None, ""]}}
        missing_summary_clause = {
            "$or": [{"summary": {"$exists": False}}, {"summary": None}, {"summary": ""}]
        }

        def _parent_query(*clauses: dict) -> dict:
            query: dict[str, Any] = {
                "corpus_id": corpus_id,
                "$and": [summary_parent_clause, *clauses],
            }
            if doc_id_filter is not None:
                query["doc_id"] = {"$in": doc_id_filter}
            return query

        def _body_parent_query(*clauses: dict) -> dict:
            query: dict[str, Any] = {
                "corpus_id": corpus_id,
                "chunk_kind": "body",
            }
            if clauses:
                query["$and"] = list(clauses)
            if doc_id_filter is not None:
                query["doc_id"] = {"$in": doc_id_filter}
            return query

        async def _summary_health() -> dict[str, Any]:
            retrieval_parent_count = await self._db["parent_chunks"].count_documents(
                _parent_query()
            )
            body_parent_count = await self._db["parent_chunks"].count_documents(
                _body_parent_query()
            )
            with_summary_text = await self._db["parent_chunks"].count_documents(
                _parent_query(summary_text_clause)
            )
            body_with_summary_text = await self._db["parent_chunks"].count_documents(
                _body_parent_query(summary_text_clause)
            )
            indexed_summary_points = 0
            qdrant_error = None
            try:
                count_filter = models.Filter(
                    must=[
                        models.FieldCondition(
                            key="chunk_type",
                            match=models.MatchValue(value="summary"),
                        )
                    ]
                )
                indexed_summary_points = (
                    await self._qdrant.count(
                        collection_name=_col_for_corpus(corpus_id, "hrag"),
                        count_filter=count_filter,
                    )
                ).count
            except Exception as exc:  # noqa: BLE001
                qdrant_error = str(exc)[:300]

            coverage = (
                min(indexed_summary_points, retrieval_parent_count)
                / retrieval_parent_count
                if retrieval_parent_count
                else 1.0
            )
            if retrieval_parent_count == 0:
                status = "empty"
            elif (
                with_summary_text >= retrieval_parent_count
                and indexed_summary_points >= retrieval_parent_count
            ):
                status = "healthy"
            elif with_summary_text or indexed_summary_points:
                status = "partial"
            else:
                status = "degraded"
            health: dict[str, Any] = {
                "retrieval_parent_count": retrieval_parent_count,
                "body_parent_count": body_parent_count,
                "with_summary_text": with_summary_text,
                "missing_summary_text": max(
                    retrieval_parent_count - with_summary_text, 0
                ),
                "body_with_summary_text": body_with_summary_text,
                "body_missing_summary_text": max(
                    body_parent_count - body_with_summary_text, 0
                ),
                "indexed_summary_points": indexed_summary_points,
                "coverage": round(coverage, 4),
                "status": status,
            }
            if qdrant_error:
                health["qdrant_error"] = qdrant_error
            return health

        before = await _summary_health()
        pressure_readiness = await self._compute_corpus_readiness_safely(corpus_id)
        if generate:
            paused = await self._backpressure_pause_result(
                corpus_id=corpus_id,
                lane_key="summary_generation_allowed",
                operation="summaries.backfill",
                readiness=pressure_readiness,
            )
            if paused is not None:
                paused.update(
                    {
                        "doc_scope_count": (
                            len(doc_id_filter) if doc_id_filter is not None else None
                        ),
                        "generated": 0,
                        "attempted": 0,
                        "indexed": 0,
                        "index_scope": "paused_pressure",
                        "generation_batches": 0,
                        "generation_errors": [paused["reason"]],
                        "before": before,
                        "after": before,
                    }
                )
                return paused

        summary_backpressure = ((pressure_readiness or {}).get("pressure") or {}).get(
            "backpressure"
        ) or {}
        summary_indexing_allowed = (
            summary_backpressure.get("summary_indexing_allowed") is not False
        )
        index_requested = bool(index)
        index_deferred_by_pressure = False
        if index_requested and not summary_indexing_allowed:
            if generate:
                index = False
                index_deferred_by_pressure = True
            else:
                paused = await self._backpressure_pause_result(
                    corpus_id=corpus_id,
                    lane_key="summary_indexing_allowed",
                    operation="summaries.backfill",
                    readiness=pressure_readiness,
                )
                if paused is not None:
                    paused.update(
                        {
                            "doc_scope_count": (
                                len(doc_id_filter)
                                if doc_id_filter is not None
                                else None
                            ),
                            "generated": 0,
                            "attempted": 0,
                            "indexed": 0,
                            "index_scope": "paused_qdrant_pressure",
                            "index_deferred_by_pressure": True,
                            "generation_batches": 0,
                            "generation_errors": [],
                            "before": before,
                            "after": before,
                        }
                    )
                    return paused

        generated = 0
        attempted = 0
        generation_batches = 0
        generation_errors: list[str] = []
        generated_parent_ids: list[str] = []

        if generate and (limit is None or limit > 0):
            runtime_summary = (
                await settings_service.get_runtime_ingestion_settings(effective_user_id)
            ).summary
            corpus_summary_models = list(cfg.summary_models or [])
            use_corpus_summary_pool = bool(corpus_summary_models)
            pool_refs = (
                corpus_summary_models
                if use_corpus_summary_pool
                else (
                    runtime_summary.summary_models
                    if runtime_summary.enabled and runtime_summary.summary_models
                    else []
                )
            )
            from services.secrets import decrypt

            def _plaintext_key(value: str | None) -> str | None:
                if not value:
                    return None
                if isinstance(value, str) and value.startswith("gAAAAA"):
                    plaintext = decrypt(value)
                    return plaintext if plaintext is not None else value
                return value

            pool = [
                {
                    "model": (ref.model if hasattr(ref, "model") else ref.get("model")),
                    "base_url": (
                        ref.base_url
                        if hasattr(ref, "base_url")
                        else ref.get("base_url")
                    )
                    or None,
                    "api_key": _plaintext_key(
                        (ref.api_key if hasattr(ref, "api_key") else ref.get("api_key"))
                        or None
                    ),
                    "max_concurrent": int(
                        (
                            ref.max_concurrent
                            if hasattr(ref, "max_concurrent")
                            else ref.get("max_concurrent")
                        )
                        or 1
                    ),
                    "extra_params": (
                        ref.extra_params
                        if hasattr(ref, "extra_params")
                        else ref.get("extra_params")
                    )
                    or {},
                }
                for ref in (pool_refs or [])
            ]
            max_summary_tokens = cfg.max_summary_tokens
            default_tokens = IngestionConfig.model_fields["max_summary_tokens"].default
            if (
                runtime_summary.enabled
                and max_summary_tokens == default_tokens
                and runtime_summary.max_summary_tokens != default_tokens
            ):
                max_summary_tokens = runtime_summary.max_summary_tokens
            global_max_concurrent = (
                None
                if use_corpus_summary_pool
                else runtime_summary.max_concurrent if runtime_summary.enabled else None
            )

            if not pool:
                generation_errors.append("no summary model pool configured")
            else:
                logger.info(
                    "summary_backfill pool corpus=%s source=%s models=%s global_cap=%s",
                    corpus_id[:8],
                    "corpus" if use_corpus_summary_pool else "settings",
                    [str(entry.get("model") or "") for entry in pool],
                    global_max_concurrent or "-",
                )
                while limit is None or attempted < limit:
                    fetch = (
                        batch
                        if limit is None
                        else max(0, min(batch, limit - attempted))
                    )
                    if fetch <= 0:
                        break
                    rows = (
                        await self._db["parent_chunks"]
                        .find(
                            _parent_query(missing_summary_clause),
                            {
                                "_id": 0,
                                "parent_id": 1,
                                "doc_id": 1,
                                "corpus_id": 1,
                                "source_tier": 1,
                                "text": 1,
                            },
                        )
                        .limit(fetch)
                        .to_list(length=fetch)
                    )
                    rows = [r for r in rows if (r.get("text") or "").strip()]
                    if not rows:
                        break
                    from services.ingestion.summary_backfill import (
                        child_context_for_rows,
                        summary_result_fields,
                    )

                    child_context = await child_context_for_rows(
                        self._db,
                        corpus_id,
                        rows,
                    )
                    generation_batches += 1
                    attempted += len(rows)
                    tasks = [
                        SummaryTask(
                            parent_id=r["parent_id"],
                            doc_id=r.get("doc_id", ""),
                            corpus_id=corpus_id,
                            source_tier=r.get("source_tier") or "parent",
                            text=r["text"],
                            source_child_ids=child_context.get(r["parent_id"], {}).get(
                                "source_child_ids", []
                            ),
                            child_boundaries=child_context.get(r["parent_id"], {}).get(
                                "child_boundaries", ""
                            ),
                        )
                        for r in rows
                    ]
                    from services.ingestion.provider_call_telemetry import (
                        record_provider_call,
                    )

                    async def _summary_telemetry(event: dict) -> None:
                        await record_provider_call(self._db, event)

                    results = await summarize_parents(
                        tasks,
                        max_summary_tokens=max_summary_tokens,
                        pool=pool,
                        global_max_concurrent=global_max_concurrent,
                        telemetry_sink=_summary_telemetry,
                    )
                    results = [r for r in results if r and r.summary]
                    if not results:
                        generation_errors.append(
                            f"summary pool returned 0/{len(tasks)} summaries"
                        )
                        break

                    now = datetime.utcnow()
                    await self._db["parent_chunks"].bulk_write(
                        [
                            UpdateOne(
                                {"parent_id": r.parent_id, "corpus_id": corpus_id},
                                {
                                    "$set": summary_result_fields(
                                        r,
                                        updated_at=now,
                                    )
                                },
                            )
                            for r in results
                        ],
                        ordered=False,
                    )
                    generated_parent_ids.extend(str(r.parent_id) for r in results)
                    generated += len(results)
                    if len(results) < len(tasks):
                        generation_errors.append(
                            f"partial summary batch: {len(results)}/{len(tasks)}"
                        )

        indexed = 0
        index_scope = "skipped"
        if index:
            target_kinds = [
                kind
                for kind in (cfg.target_qdrant_collections or ["hrag"])
                if kind in ("naive", "hrag")
            ] or ["hrag"]
            index_scope, index_clauses = _summary_backfill_index_scope(
                generate=generate,
                limit=limit,
                generated_parent_ids=generated_parent_ids,
                summary_text_clause=summary_text_clause,
                bounded_by_doc_ids=(
                    doc_id_filter is not None and index_existing_doc_summaries
                ),
            )
            cursor = self._db["parent_chunks"].find(
                _parent_query(*index_clauses),
                {
                    "_id": 0,
                    "parent_id": 1,
                    "doc_id": 1,
                    "corpus_id": 1,
                    "source_tier": 1,
                    "summary": 1,
                    "retrieval_text": 1,
                    "text": 1,
                    "parent_text": 1,
                    "child_ids": 1,
                    "domain": 1,
                    "topics": 1,
                    "semantic_chunk_type": 1,
                    "key_terms": 1,
                    "mechanisms": 1,
                    "schema_version": 1,
                    "summary_type": 1,
                    "central_claim": 1,
                    "key_points": 1,
                    "main_mechanism": 1,
                    "concept_tags": 1,
                    "entity_hints": 1,
                    "retrieval_uses": 1,
                    "abstraction_level": 1,
                    "source_child_ids": 1,
                    "source_hash": 1,
                    "summary_model": 1,
                    "summary_created_at": 1,
                    "validation_status": 1,
                    "repair_status": 1,
                    "quality_score": 1,
                    "quality_flags": 1,
                    "heading_path": 1,
                    "filename": 1,
                    "doc_name": 1,
                    "metadata": 1,
                    "facet_ids": 1,
                    "facet_text": 1,
                    "content_facet_ids": 1,
                    "content_facet_text": 1,
                    "content_facet_source": 1,
                    "content_facet_confidence": 1,
                    "doc_facet_ids": 1,
                    "facet_schema_version": 1,
                    "chunk_kind": 1,
                    "language": 1,
                },
            )
            buf: list[dict[str, Any]] = []

            async def _flush() -> None:
                nonlocal indexed
                if not buf:
                    return
                vectors = await embed_batch(
                    [summary_index_text(p) for p in buf],
                    mode="local",
                    expected_dim=cfg.embedding_dimension,
                    expected_model_id=cfg.embedding_model_id,
                )
                payloads = [
                    {
                        **p,
                        "retrieval_text": summary_index_text(p),
                        "user_id": effective_user_id,
                        "source_tier": p.get("source_tier") or "parent",
                    }
                    for p in buf
                ]
                written = await upsert_summaries(
                    self._qdrant,
                    corpus_id,
                    payloads,
                    vectors,
                    target_kinds=target_kinds,
                )
                indexed += written
                buf.clear()

            async for parent in cursor:
                buf.append(parent)
                if len(buf) >= batch:
                    await _flush()
            await _flush()

            ready_by_doc: dict[str, tuple[bool, int]] = {}
            pipeline = [
                {"$match": _parent_query()},
                {
                    "$group": {
                        "_id": "$doc_id",
                        "retrieval_parent_count": {"$sum": 1},
                        "with_summary": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$ne": ["$summary", None]},
                                            {"$ne": ["$summary", ""]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },
            ]
            async for row in self._db["parent_chunks"].aggregate(pipeline):
                with_summary = int(row.get("with_summary") or 0)
                ready_by_doc[str(row["_id"])] = (
                    int(row.get("retrieval_parent_count") or 0) <= with_summary,
                    with_summary,
                )
            if ready_by_doc:
                now = datetime.utcnow()
                await self._db["documents"].bulk_write(
                    [
                        UpdateOne(
                            {"corpus_id": corpus_id, "doc_id": doc_id},
                            {
                                "$set": {
                                    "write_state.summaries_indexed": ready,
                                    "write_state.summary_points": summary_points,
                                    "write_state.summary_backfilled_at": now,
                                }
                            },
                        )
                        for doc_id, (ready, summary_points) in ready_by_doc.items()
                    ],
                    ordered=False,
                )

        after = await _summary_health()
        result = {
            "corpus_id": corpus_id,
            "status": after["status"],
            "doc_scope_count": (
                len(doc_id_filter) if doc_id_filter is not None else None
            ),
            "generated": generated,
            "attempted": attempted,
            "indexed": indexed,
            "index_scope": index_scope,
            "index_requested": index_requested,
            "index_deferred_by_pressure": index_deferred_by_pressure,
            "generation_batches": generation_batches,
            "generation_errors": generation_errors,
            "before": before,
            "after": after,
        }
        readiness = await self._materialize_corpus_readiness_safely(corpus_id)
        if readiness is not None:
            result["readiness"] = readiness
        return result

    async def preflight_document(
        self,
        *,
        data: bytes,
        filename: str,
        corpus_id: str,
        ingestion_config: IngestionConfig,
    ) -> dict:
        """Parse/chunk a document without writes to estimate ingestion cost/risk."""
        from services.ingestion import docling_adapter, tier_chunker

        source_mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        doc_id = hashlib.sha256(data).hexdigest()
        parse_result = await docling_adapter.parse_document(
            data,
            filename=filename,
            mime=source_mime,
            do_ocr=False,
        )
        parents, children, injected_headers = tier_chunker.chunk(
            parse_result,
            doc_id=doc_id,
            corpus_id=corpus_id,
            config=ingestion_config,
        )
        child_tokens = [int(getattr(c, "token_count", 0) or 0) for c in children]
        total_tokens = sum(child_tokens)
        graph_calls = len(children) if ingestion_config.use_neo4j else 0
        summary_calls = len(parents) if ingestion_config.chunk_summarization else 0
        settings = get_settings()
        extraction_pool = (
            ingestion_config.summary_models
            if ingestion_config.models_linked
            else ingestion_config.extraction_models
        )

        def _uses_managed_vllm(ref) -> bool:
            from services.extraction_provider_cards import (
                extraction_lane_uses_private_vllm,
            )

            return extraction_lane_uses_private_vllm(ref)

        managed_vllm = any(_uses_managed_vllm(m) for m in extraction_pool)
        active_extraction_docs = (
            settings.EXTRACTION_MANAGED_VLLM_MAX_ACTIVE_DOCS
            if managed_vllm
            else settings.EXTRACTION_MAX_ACTIVE_DOCS
        )
        model_phase_doc_concurrency = (
            settings.INGEST_MANAGED_VLLM_MODEL_PHASE_DOCS
            if managed_vllm
            else settings.INGEST_MAX_MODEL_PHASE_DOCS
        )
        extraction_concurrency = (
            sum(
                max(1, int(getattr(m, "max_concurrent", 1) or 1))
                for m in extraction_pool
            )
            or settings.EXTRACTION_MAX_CONCURRENT
        )
        foreground_calls_per_child = min(
            settings.EXTRACTION_JSONL_MAX_CALLS,
            settings.EXTRACTION_FOREGROUND_MAX_CALLS,
            2,
        )
        rescue_calls_per_child = max(foreground_calls_per_child - 1, 0)
        worst_case_extraction_calls = graph_calls * foreground_calls_per_child
        worst_case_completion_tokens = graph_calls * (
            settings.EXTRACTION_MAX_TOKENS
            + rescue_calls_per_child * settings.EXTRACTION_RESCUE_MAX_TOKENS
        )
        per_process_extraction_ceiling = min(
            settings.EXTRACTION_GLOBAL_MAX_CONCURRENT,
            extraction_concurrency,
        )
        warnings: list[str] = []
        if len(children) > 500:
            warnings.append(
                "High child-chunk count; ingest this file in a controlled batch."
            )
        if child_tokens and max(child_tokens) > settings.EXTRACTION_MAX_INPUT_TOKENS:
            warnings.append(
                "At least one child chunk exceeds the Ghost B extraction input cap."
            )
        return {
            "filename": filename,
            "doc_id": doc_id,
            "source_mime": source_mime,
            "source_tier": parse_result.source_tier.value,
            "parent_count": len(parents),
            "child_count": len(children),
            "total_child_tokens": total_tokens,
            "avg_child_tokens": (
                round(total_tokens / len(children), 1) if children else 0
            ),
            "max_child_tokens": max(child_tokens) if child_tokens else 0,
            "ghost_b_calls": graph_calls,
            "summary_calls": summary_calls,
            "estimated_llm_calls": graph_calls + summary_calls,
            "extraction_risk": {
                "foreground_facts_enabled": settings.EXTRACTION_ENABLE_FACTS,
                "facts_configured": settings.EXTRACTION_ENABLE_FACTS,
                "output_mode": "json_schema_for_structured_lanes",
                "configured_output_mode": settings.EXTRACTION_OUTPUT_MODE,
                "repair_strategy": "one_jsonl_repair_resume",
                "max_input_tokens": settings.EXTRACTION_MAX_INPUT_TOKENS,
                "normal_max_tokens": settings.EXTRACTION_MAX_TOKENS,
                "rescue_max_tokens": settings.EXTRACTION_RESCUE_MAX_TOKENS,
                "evidence_max_chars": settings.EXTRACTION_EVIDENCE_MAX_CHARS,
                "max_total_lines": settings.EXTRACTION_MAX_TOTAL_LINES,
                "rescue_max_total_lines": settings.EXTRACTION_RESCUE_MAX_TOTAL_LINES,
                "calls_per_child": foreground_calls_per_child,
                "extraction_concurrency": extraction_concurrency,
                "model_phase_doc_concurrency": model_phase_doc_concurrency,
                "active_extraction_docs": active_extraction_docs,
                "managed_vllm": managed_vllm,
                "global_max_concurrent": settings.EXTRACTION_GLOBAL_MAX_CONCURRENT,
                "per_process_extraction_ceiling": per_process_extraction_ceiling,
                "failure_pause_percent": settings.EXTRACTION_FAILURE_PAUSE_PERCENT,
                "failure_pause_min_chunks": settings.EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS,
                "worst_case_extraction_calls": worst_case_extraction_calls,
                "worst_case_completion_tokens": worst_case_completion_tokens,
            },
            "injected_headers": len(injected_headers),
            "chunking_config": tier_chunker.describe_chunking(
                parse_result, ingestion_config
            ),
            "recommended_batch_size": 25 if len(children) < 500 else 10,
            "warnings": warnings,
        }

    async def warm_graph_cache(self, *, corpus_id: str, user_id: str) -> dict:
        """Schedule corpus-scale graph analytics cache warmup after a batch."""
        from services.graph.orchestrator import schedule_graph_discovery_cache_warm

        schedule_graph_discovery_cache_warm(
            qdrant=self._qdrant,
            neo4j_driver=self._neo4j,
            db=self._db,
            corpus_id=corpus_id,
            user_id=user_id,
        )
        return {"status": "queued", "corpus_id": corpus_id}


ingestion_service = IngestionService()
