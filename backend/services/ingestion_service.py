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

import hashlib
import logging
import mimetypes
import uuid
from datetime import datetime
from typing import Optional

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


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
        "extraction_repair_models",
        "entity_confidence_threshold",
        "models_linked",
        "large_doc_child_threshold",
        "full_extract_max_children",
        "compact_mode_max_entities",
        "compact_mode_max_relations",
        "deep_pass_enabled",
        "deep_pass_max_chunks",
        "graph_per_chunk_max_attempts",
        "graph_per_doc_max_failed_chunks_before_pause",
        "graph_per_lane_max_consecutive_failures",
        "graph_per_lane_cooldown_seconds",
        "graph_backfill_max_chunks",
        "graph_backfill_max_attempts_per_chunk",
        "deferred_graph_repair_enabled",
        "graph_repair_max_attempts",
        "graph_extraction_engine",
        "llm_fallback_enabled",
        "llm_fallback_max_percent",
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
    entity_confidence_threshold, models_linked, modal_containers) are
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
    # Older document snapshots may contain a full IngestionConfig, not just
    # frozen fields. Keep those mutable values as a compatibility fallback,
    # then let the live corpus record override them below.
    for k in MUTABLE_CONFIG_FIELDS:
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
    return isolate_local_model_routing(IngestionConfig(**merged))


def _canonical_embed_mode(value: object) -> str:
    aliases = {
        "local_st": "local",
        "modal_tei": "modal",
        "siliconflow": "api",
    }
    key = str(value or "local")
    return aliases.get(key, key)


def isolate_local_model_routing(config: IngestionConfig) -> IngestionConfig:
    """Keep local-EMBEDDING ingestion runs pinned to local embedder dispatch.

    Embedder routing (`embed_mode`, `embed_*_url`, `embedding_models`,
    `modal_containers`) is a separate concern from Ghost A / Ghost B
    extraction routing (`summary_models`, `extraction_models`,
    `extraction_repair_models`). The legacy version of this helper reset
    BOTH on local-embed corpora, which silently dropped any cloud overflow
    lanes the user added to Ghost A/B. Now we only normalize the embedder
    fields — Ghost pools are untouched so a corpus can run local
    embeddings + cloud-overflow extraction simultaneously.
    """
    if _canonical_embed_mode(getattr(config, "embed_mode", "local")) != "local":
        return config

    return config.model_copy(
        update={
            "embed_mode": "local",
            "embed_base_url": None,
            "embed_api_key": None,
            "embed_max_concurrent": None,
            "embedding_models": [],
            "modal_containers": None,
        }
    )


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
        self._batch_manager = None
        self._repair_worker = None
        self._backfill_worker = None

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

        # Phase 7.5 alias backfill + repair — ensure every existing corpus has
        # its per-corpus Qdrant collections before aliasing. This self-heals a
        # corpus row that was written to Mongo before Qdrant collection
        # provisioning timed out.
        try:
            from services.storage.qdrant_writer import ensure_collections_for_corpus

            cursor = db["corpora"].find({}, {"corpus_id": 1, "name": 1, "_id": 0})
            async for row in cursor:
                cid = row.get("corpus_id")
                nm = row.get("name")
                if cid and nm:
                    try:
                        await ensure_collections_for_corpus(
                            self._qdrant,
                            cid,
                            corpus_name=nm,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Qdrant collection repair skipped for corpus %s: %s",
                            cid,
                            exc,
                        )
        except Exception as exc:
            logger.warning("Qdrant collection repair sweep skipped: %s", exc)

        if self._settings.NEO4J_ENABLED:
            from neo4j import AsyncGraphDatabase

            self._neo4j = AsyncGraphDatabase.driver(
                self._settings.NEO4J_URI,
                auth=(self._settings.NEO4J_USER, self._settings.NEO4J_PASSWORD),
            )
            from services.graph.schema import initialize_schema

            # initialize_schema(driver) opens its own session internally.
            await initialize_schema(self._neo4j)
            logger.info("IngestionService: Neo4j connected + schema initialized")

        try:
            from services.ingestion.batch_queue import batch_ingestion_manager

            self._batch_manager = batch_ingestion_manager
            self._batch_manager.attach(
                db=db,
                ingest_callable=self.ingest,
                warm_graph_cache_callable=self.warm_graph_cache,
            )
            await self._batch_manager.start_resume()
            logger.info("IngestionService: batch ingestion manager attached")
        except Exception as exc:
            logger.warning("Batch ingestion manager did not start: %s", exc)

        if self._settings.GRAPH_REPAIR_WORKER_ENABLED and self._neo4j is not None:
            try:
                from services.ingestion.repair_worker import GraphRepairWorker

                self._repair_worker = GraphRepairWorker(
                    db=db,
                    qdrant_client=self._qdrant,
                    neo4j_driver=self._neo4j,
                )
                self._repair_worker.start()
                logger.info("IngestionService: graph repair worker started")
            except Exception as exc:
                logger.warning("Graph repair worker did not start: %s", exc)

            try:
                from services.ingestion.needs_backfill_worker import (
                    NeedsBackfillWorker,
                )

                self._backfill_worker = NeedsBackfillWorker(
                    db=db,
                    qdrant_client=self._qdrant,
                    neo4j_driver=self._neo4j,
                )
                self._backfill_worker.start()
                logger.info("IngestionService: needs-backfill worker started")
            except Exception as exc:
                logger.warning("Needs-backfill worker did not start: %s", exc)

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

    async def disconnect(self) -> None:
        if self._backfill_worker:
            await self._backfill_worker.stop()
        if self._repair_worker:
            await self._repair_worker.stop()
        if self._batch_manager:
            await self._batch_manager.disconnect()
        if self._qdrant:
            await self._qdrant.close()
        if self._neo4j:
            await self._neo4j.close()
        logger.info("IngestionService: clients closed")

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
        """Lifespan migration — rewrite bare model strings to include the
        LiteLLM provider prefix.

        Motivation: prior UI presets auto-filled `model = "deepseek-chat"` (no
        prefix). LiteLLM's wildcard router can't match that to `deepseek/*`
        and returns 400, cascading into Ghost A + Ghost B failures on every
        ingest through such a corpus.

        Scope:
          • `corpora.default_ingestion_config.{summary_models,extraction_models,extraction_repair_models}`
            (per-corpus ingestion pools — `ModelProfileRef` with `provider_preset`).
          • `settings.models.query_model_pool` (per-user unified chat pool —
            `QueryModelPoolEntry` with `provider` + `model_name`).
          • `model_pool` collection (Phase E unified pool — same shape as
            the settings subdoc).

        Rules per entry:
          • If the stored model contains "/", assume it's already prefixed →
            skip (idempotent).
          • Else, look up the entry's preset id in the backend registry. If
            unknown, leave alone (user-authored custom config).
          • Else, rewrite `model = f"{litellm_provider}/{old_model}"`. Log
            an audit line per rewrite.

        Returns: {"corpora_patched", "pool_entries_patched", "corpus_ids",
                  "settings_users_patched", "model_pool_entries_patched"}.
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
                "default_ingestion_config.extraction_repair_models": 1,
            },
        )
        async for doc in cursor:
            cfg = doc.get("default_ingestion_config") or {}
            rewrites_for_doc: list[tuple[str, int, str, str]] = []
            new_summary = cfg.get("summary_models") or []
            new_extraction = cfg.get("extraction_models") or []
            new_repair = cfg.get("extraction_repair_models") or []

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

            for idx, entry in enumerate(new_repair):
                if not isinstance(entry, dict):
                    continue
                rewritten = _needs_rewrite(
                    entry.get("provider_preset"), entry.get("model")
                )
                if rewritten is None:
                    continue
                rewrites_for_doc.append(
                    ("extraction_repair_models", idx, entry["model"], rewritten)
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
                        "default_ingestion_config.extraction_repair_models": new_repair,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            corpus_ids.append(doc["corpus_id"])
            pool_entries_patched += len(rewrites_for_doc)
            for field, idx, old_model, new_model in rewrites_for_doc:
                logger.info(
                    "migrate_bare_model_names: corpus=%s field=%s idx=%d old=%r new=%r",
                    doc["corpus_id"],
                    field,
                    idx,
                    old_model,
                    new_model,
                )

        # ── 2. Per-user settings.models.query_model_pool.
        settings_users_patched = 0
        scursor = self._db["settings"].find(
            {}, projection={"user_id": 1, "models.query_model_pool": 1}
        )
        async for sdoc in scursor:
            models_subdoc = sdoc.get("models") or {}
            pool = models_subdoc.get("query_model_pool") or []
            if not pool:
                continue
            rewrites: list[tuple[int, str, str]] = []
            for idx, entry in enumerate(pool):
                if not isinstance(entry, dict):
                    continue
                # QueryModelPoolEntry uses `provider` (not `provider_preset`)
                # and `model_name` (not `model`).
                rewritten = _needs_rewrite(
                    entry.get("provider"), entry.get("model_name")
                )
                if rewritten is None:
                    continue
                rewrites.append((idx, entry["model_name"], rewritten))
                entry["model_name"] = rewritten
            if not rewrites:
                continue
            await self._db["settings"].update_one(
                {"_id": sdoc["_id"]},
                {"$set": {"models.query_model_pool": pool}},
            )
            settings_users_patched += 1
            for idx, old_model, new_model in rewrites:
                logger.info(
                    "migrate_bare_model_names: settings user=%s idx=%d old=%r new=%r",
                    sdoc.get("user_id", "?"),
                    idx,
                    old_model,
                    new_model,
                )

        # ── 3. model_pool collection (Phase E unified pool).
        model_pool_entries_patched = 0
        try:
            mpcursor = self._db["model_pool"].find(
                {},
                projection={
                    "entry_id": 1,
                    "user_id": 1,
                    "provider": 1,
                    "model_name": 1,
                },
            )
            async for mdoc in mpcursor:
                rewritten = _needs_rewrite(mdoc.get("provider"), mdoc.get("model_name"))
                if rewritten is None:
                    continue
                await self._db["model_pool"].update_one(
                    {"_id": mdoc["_id"]},
                    {
                        "$set": {
                            "model_name": rewritten,
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                model_pool_entries_patched += 1
                logger.info(
                    "migrate_bare_model_names: model_pool entry=%s user=%s "
                    "old=%r new=%r",
                    mdoc.get("entry_id", "?"),
                    mdoc.get("user_id", "?"),
                    mdoc.get("model_name"),
                    rewritten,
                )
        except Exception as exc:
            # model_pool collection may not exist on fresh installs.
            logger.debug("migrate_bare_model_names: model_pool scan skipped: %s", exc)

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
        source_mime: str | None = None,
        cancel_check: "Any | None" = None,
        on_doc_id: "Any | None" = None,
    ) -> IngestJobResponse:
        """Run the full ingestion pipeline for one document.

        `ingest_overrides` (Phase 21) carries ephemeral per-request overrides
        — embed wiring, synthesized ghost pools — that shadow the corpus's
        mutable defaults for this ingest only. Never persisted.

        `on_doc_id` (Phase K) is invoked with the resolved doc_id as soon as
        docling parse completes — the HTTP endpoint uses this to return a
        response before the long tail of ghost/embed/write runs.
        """
        from services.ingestion.worker import run_ingest_job

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
            source_mime=source_mime,
            cancel_check=cancel_check,
            on_doc_id=on_doc_id,
        )

    async def create_batch_ingest(
        self,
        *,
        corpus_id: str,
        user_id: str,
        uploads: list,
        ingestion_config: IngestionConfig,
        model: str = "",
        ingest_overrides: dict | None = None,
        warnings: list[str] | None = None,
        preflight: dict | None = None,
    ) -> dict:
        if self._batch_manager is None:
            raise RuntimeError("Batch ingestion manager is unavailable")
        return await self._batch_manager.create_batch(
            corpus_id=corpus_id,
            user_id=user_id,
            uploads=uploads,
            ingestion_config=ingestion_config,
            model=model,
            ingest_overrides=ingest_overrides,
            warnings=warnings,
            preflight=preflight,
        )

    async def preflight_ingest(
        self,
        *,
        corpus: dict | None,
        corpus_id: str,
        ingestion_config: IngestionConfig,
        ingest_overrides: dict | None = None,
    ) -> dict:
        """Check vector/graph dependencies before costly parse or summary work."""
        from services.ingestion.preflight import run_ingest_preflight

        live_corpus_cfg = (corpus or {}).get("default_ingestion_config")
        if live_corpus_cfg is None:
            loaded = await self._get_corpus_raw(corpus_id)
            live_corpus_cfg = (loaded or {}).get("default_ingestion_config") or {}
        effective_config = build_effective_config(
            frozen_base=ingestion_config.model_dump(),
            live_corpus=live_corpus_cfg or {},
            ingest_overrides=ingest_overrides,
        )
        return await run_ingest_preflight(
            config=effective_config,
            qdrant_client=self._qdrant,
        )

    async def get_ingestion_batch(self, batch_id: str, *, user_id: str) -> dict | None:
        if self._batch_manager is None:
            return None
        return await self._batch_manager.get_batch(batch_id, user_id=user_id)

    async def get_ingestion_batch_summary(
        self, batch_id: str, *, user_id: str
    ) -> dict | None:
        if self._batch_manager is None:
            return None
        return await self._batch_manager.get_batch_summary(batch_id, user_id=user_id)

    async def pause_ingestion_batch(self, batch_id: str, *, user_id: str) -> dict:
        if self._batch_manager is None:
            raise RuntimeError("Batch ingestion manager is unavailable")
        return await self._batch_manager.pause(batch_id, user_id=user_id)

    async def resume_ingestion_batch(self, batch_id: str, *, user_id: str) -> dict:
        if self._batch_manager is None:
            raise RuntimeError("Batch ingestion manager is unavailable")
        return await self._batch_manager.resume(batch_id, user_id=user_id)

    async def cancel_ingestion_batch(self, batch_id: str, *, user_id: str) -> dict:
        if self._batch_manager is None:
            raise RuntimeError("Batch ingestion manager is unavailable")
        return await self._batch_manager.cancel(batch_id, user_id=user_id)

    async def retry_failed_ingestion_batch(
        self, batch_id: str, *, user_id: str
    ) -> dict:
        if self._batch_manager is None:
            raise RuntimeError("Batch ingestion manager is unavailable")
        return await self._batch_manager.retry_failed(batch_id, user_id=user_id)

    async def get_ingestion_resource_profile(self) -> dict:
        if self._batch_manager is None:
            from services.ingestion.batch_queue import detect_resource_profile

            return detect_resource_profile(self._settings.INGEST_SPOOL_DIR)
        profile = self._batch_manager.resource_profile()
        profile["queue_metrics"] = await self._batch_manager.queue_metrics()
        return profile

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
        """Return a pool with decrypted api_key values for embedder dispatch."""
        if not refs:
            return []
        from services.secrets import decrypt

        out: list[dict] = []
        for ref in refs:
            data = ref.model_dump() if hasattr(ref, "model_dump") else dict(ref)
            raw_key = data.get("api_key")
            if raw_key:
                plaintext = decrypt(raw_key)
                data["api_key"] = plaintext if plaintext is not None else raw_key
            out.append(data)
        return out

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
        ingestion_config = isolate_local_model_routing(ingestion_config)

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

        # Phase 21 — freeze each pool entry's context_length from the user's
        # model_pool so the Ghost A/B token-budget guards can size against
        # the real local-model context window (lfm2-summary 12288, etc.)
        # instead of the static registry default of 4096.
        await self._resolve_pool_context_lengths(
            corpus_doc["default_ingestion_config"], user_id
        )

        await upsert_corpus(self._db, corpus_doc)

        # Phase 7.5 — provision the 4 per-corpus Qdrant collections up front
        # so the first ingest doesn't race with collection creation.
        from services.storage.qdrant_writer import ensure_collections_for_corpus

        try:
            await ensure_collections_for_corpus(
                self._qdrant,
                corpus_doc["corpus_id"],
                dim=self._settings.EMBEDDING_DIMENSION,
                corpus_name=corpus_doc.get("name"),
            )
        except Exception as exc:
            logger.error(
                "Failed to create Qdrant collections for corpus %s: %s",
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

        return corpus_doc

    async def list_corpora(self, user_id: Optional[str] = None) -> list[dict]:
        from services.storage.mongo_reader import list_corpora

        docs = await list_corpora(self._db, user_id=user_id)
        await self._refresh_corpus_counts(docs)
        for doc in docs:
            self._mask_ingestion_keys_in_place(doc.get("default_ingestion_config"))
        return docs

    async def get_corpus(self, corpus_id: str) -> Optional[dict]:
        from services.storage.mongo_reader import get_corpus

        doc = await get_corpus(self._db, corpus_id)
        if doc:
            await self._refresh_corpus_counts([doc])
            self._mask_ingestion_keys_in_place(doc.get("default_ingestion_config"))
        return doc

    async def _refresh_corpus_counts(self, docs: list[dict]) -> None:
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

        async def _counts(collection: str) -> dict[str, int]:
            pipeline = [
                {"$match": {"corpus_id": {"$in": corpus_ids}}},
                {"$group": {"_id": "$corpus_id", "count": {"$sum": 1}}},
            ]
            rows = await self._db[collection].aggregate(pipeline).to_list(length=None)
            return {str(r["_id"]): int(r["count"]) for r in rows}

        doc_counts = await _counts("documents")
        chunk_counts = await _counts("chunks")
        for doc in docs:
            cid = doc.get("corpus_id")
            if not cid:
                continue
            actual_docs = doc_counts.get(cid, 0)
            actual_chunks = chunk_counts.get(cid, 0)
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

    async def _get_corpus_raw(self, corpus_id: str) -> Optional[dict]:
        """Unmasked read — used by update_corpus so `_encrypt_ingestion_keys_in_place`
        can diff the incoming patch against real stored ciphertext. NEVER return
        this to the API layer."""
        from services.storage.mongo_reader import get_corpus

        doc = await get_corpus(self._db, corpus_id)
        if doc:
            await self._refresh_corpus_counts([doc])
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

        for pool_field in (
            "summary_models",
            "extraction_models",
            "extraction_repair_models",
            "embedding_models",
        ):
            pool = config_dict.get(pool_field)
            if not pool:
                continue
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("api_key")
                entry["api_key"] = "[set]" if raw else None

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

    async def _resolve_pool_context_lengths(
        self, config_dict: dict, user_id: str
    ) -> None:
        """Look up each ghost-pool entry's authoritative context_length from
        the user's model_pool collection and freeze it onto the corpus's
        ingestion config. Without this, ModelProfileRef.context_length stays
        None and the Ghost A/B token-budget guards fall back to the static
        utils.tokens registry — which only knows public/cloud models.

        Local fine-tunes (lfm2-summary @ 12288, lfm2-extract @ 12288, etc.)
        get a 4096 default from the registry, which is wrong, and the
        budget guard ends up skipping more parents than necessary because
        it thinks the model is smaller than it is.

        Match strategy: strip the LiteLLM provider prefix (`openai/`, etc.)
        from the corpus entry's `model` field and compare against
        `model_pool.model_name`. If multiple pool entries match the same
        bare name (rare — different providers, same fine-tune name), prefer
        the one whose base_url matches the corpus entry's base_url.

        Idempotent: when the corpus entry already has an explicit
        `context_length`, leave it alone (user intent wins).
        """
        from services.model_pool import model_pool_service

        if not user_id:
            return
        pool_entries: list[dict] | None = None  # lazy fetched on first hit

        def _bare_name(litellm_model: str) -> str:
            return litellm_model.split("/", 1)[1] if "/" in litellm_model else litellm_model

        for pool_field in ("summary_models", "extraction_models", "extraction_repair_models"):
            pool = config_dict.get(pool_field) or []
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                if entry.get("context_length") is not None:
                    continue
                entry_model = str(entry.get("model") or "").strip()
                if not entry_model:
                    continue
                if pool_entries is None:
                    pool_entries = await model_pool_service.list_for_user(user_id)
                bare = _bare_name(entry_model)
                entry_base = (entry.get("base_url") or "").rstrip("/").lower()
                matches = [
                    p for p in pool_entries
                    if str(p.get("model_name") or "").strip() == bare
                ]
                if not matches:
                    continue
                if len(matches) > 1 and entry_base:
                    base_match = [
                        p for p in matches
                        if (p.get("base_url") or "").rstrip("/").lower() == entry_base
                    ]
                    if base_match:
                        matches = base_match
                ctx_len = matches[0].get("context_length")
                if ctx_len:
                    try:
                        entry["context_length"] = int(ctx_len)
                        logger.info(
                            "Resolved context_length=%d for corpus pool entry "
                            "model=%s (pool_field=%s)",
                            int(ctx_len), entry_model, pool_field,
                        )
                    except (TypeError, ValueError):
                        pass

    @staticmethod
    def _encrypt_ingestion_keys_in_place(
        config_dict: dict, existing_config: dict | None = None
    ) -> None:
        """
        Walk summary_models and extraction_models; for each entry, ensure
        `api_key` holds Fernet ciphertext (or None) before it lands in Mongo.

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

        for pool_field in (
            "summary_models",
            "extraction_models",
            "extraction_repair_models",
            "embedding_models",
        ):
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
                entry["api_key"] = _enc(
                    entry.get("api_key"), existing_entry.get("api_key")
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

    async def update_corpus(self, corpus_id: str, updates: dict) -> Optional[dict]:
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
            new_config = merged_config
            updates["default_ingestion_config"] = merged_config

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

        if new_config is not None:
            try:
                isolated = isolate_local_model_routing(IngestionConfig(**new_config))
                new_config.update(isolated.model_dump())
                updates["default_ingestion_config"] = new_config
            except Exception as exc:
                logger.warning(
                    "local model routing isolation failed on update for corpus %s: %s",
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

            # Phase 21 — refresh frozen context_length on each pool entry from
            # the corpus owner's model_pool. Re-runs on every update so the
            # value tracks model_pool changes (user reconfigured a local model
            # to a higher context window → next corpus update propagates it).
            owner_id = (existing or {}).get("user_id") if existing else None
            if owner_id:
                await self._resolve_pool_context_lengths(new_config, owner_id)

        updated = await update_corpus(self._db, corpus_id, updates)
        # Mask api_keys in the returned doc so the PUT response matches GET.
        if updated:
            self._mask_ingestion_keys_in_place(updated.get("default_ingestion_config"))

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
        Cascade delete: corpus → documents → chunks → Qdrant points.
        Returns True if corpus existed and was deleted.
        """
        from services.storage.mongo_writer import (
            delete_chunks_by_corpus,
            delete_corpus,
            delete_documents_by_corpus,
        )
        from services.storage.qdrant_writer import drop_collections_for_corpus

        if self._batch_manager is not None:
            try:
                await self._batch_manager.cancel_corpus_work(
                    corpus_id=corpus_id,
                    reason="corpus_deleted_from_corpus_manager",
                )
            except Exception:
                logger.warning("Failed to cancel active ingest work for corpus %s", corpus_id)

        # Phase 7.5 — atomically drop all 4 per-corpus collections (naive,
        # hrag, graph, schemas). Replaces the old filter-delete cascade.
        try:
            await drop_collections_for_corpus(self._qdrant, corpus_id)
        except Exception:
            logger.warning(
                "Failed to drop per-corpus Qdrant collections for %s", corpus_id
            )

        # 2. Delete Neo4j nodes if enabled
        if self._settings.NEO4J_ENABLED and self._neo4j:
            try:
                async with self._neo4j.session() as session:
                    await session.run(
                        "MATCH (n {corpus_id: $corpus_id}) DETACH DELETE n",
                        corpus_id=corpus_id,
                    )
            except Exception:
                logger.warning("Failed to delete Neo4j nodes for corpus %s", corpus_id)

        # 3. Delete chunks
        await delete_chunks_by_corpus(self._db, corpus_id)

        # 4. Delete documents
        await delete_documents_by_corpus(self._db, corpus_id)

        # 5. Delete durable relation repair jobs
        await self._db["graph_repair_queue"].delete_many({"corpus_id": corpus_id})

        # 6. Delete corpus record
        return await delete_corpus(self._db, corpus_id)

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
        )
        from services.storage.qdrant_writer import delete_points_by_doc

        doc = await self._db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "content_hash": 1, "user_id": 1},
        )
        if self._batch_manager is not None:
            try:
                await self._batch_manager.cancel_document_work(
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    content_hash=(doc or {}).get("content_hash"),
                    user_id=(doc or {}).get("user_id"),
                    reason="document_deleted_from_corpus_manager",
                )
            except Exception:
                logger.warning(
                    "Failed to cancel active ingest work for doc %s in corpus %s",
                    doc_id[:12],
                    corpus_id[:8],
                )

        # 1. Qdrant points across naive / hrag / graph (doc_id filter).
        try:
            await delete_points_by_doc(self._qdrant, corpus_id, doc_id)
        except Exception:
            logger.warning(
                "Qdrant per-doc delete failed for doc %s in corpus %s",
                doc_id[:12],
                corpus_id[:8],
            )

        # 2. Neo4j — delete Entity and Mention nodes attached to this doc.
        if self._settings.NEO4J_ENABLED and self._neo4j:
            try:
                async with self._neo4j.session() as session:
                    await session.run(
                        "MATCH (n {doc_id: $doc_id, corpus_id: $corpus_id}) "
                        "DETACH DELETE n",
                        doc_id=doc_id,
                        corpus_id=corpus_id,
                    )
            except Exception:
                logger.warning("Neo4j per-doc delete failed for doc %s", doc_id[:12])

        # 3. Mongo chunks.
        await delete_chunks_by_doc(self._db, corpus_id, doc_id)

        # 4. Durable relation repair jobs for this document.
        await self._db["graph_repair_queue"].delete_many(
            {"corpus_id": corpus_id, "doc_id": doc_id}
        )

        # 5. Mongo document record.
        return await delete_document(self._db, corpus_id, doc_id)

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

        return await backfill_failed_graph_chunks(
            db=self._db,
            qdrant_client=self._qdrant,
            neo4j_driver=self._neo4j,
            corpus_id=corpus_id,
            doc_id=doc_id,
            user_id=user_id,
        )

    async def drain_graph_repairs(
        self,
        *,
        corpus_id: str,
        doc_id: str | None = None,
        limit: int = 32,
    ) -> dict:
        """Drain durable relation-level repair jobs for a corpus/document."""
        from services.ingestion.repair_worker import drain_repair_queue_once

        return await drain_repair_queue_once(
            db=self._db,
            qdrant_client=self._qdrant,
            neo4j_driver=self._neo4j,
            corpus_id=corpus_id,
            doc_id=doc_id,
            limit=limit,
        )

    async def recover_document_vectors(
        self,
        *,
        corpus_id: str,
        doc_id: str,
        user_id: str,
    ) -> dict:
        """Finish Qdrant/vector readiness for a Mongo-only document."""
        from services.ingestion.worker import recover_vector_from_mongo

        return await recover_vector_from_mongo(
            db=self._db,
            qdrant_client=self._qdrant,
            corpus_id=corpus_id,
            doc_id=doc_id,
            user_id=user_id,
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
                    "ghost_b_metrics": 1,
                    "write_state": 1,
                    "decision_trace": 1,
                    "decision_trace_summary": 1,
                    "chunking_config": 1,
                    "source_mime": 1,
                    "source_tier": 1,
                    "_id": 0,
                },
            )
            .to_list(length=None)
        )
        total_chunks = await self._db["chunks"].count_documents(
            {"corpus_id": corpus_id}
        )
        try:
            graph_cache_doc = await self._db["graph_metrics_cache"].find_one(
                {"corpus_id": corpus_id},
                {
                    "schema_version": 1,
                    "computed_at": 1,
                    "last_graph_refresh_at": 1,
                    "graph_cache_stale": 1,
                    "stale_reason": 1,
                    "stale_at": 1,
                    "entity_quality_version": 1,
                    "entity_quality_counts": 1,
                    "_id": 0,
                },
            )
        except Exception:
            graph_cache_doc = None
        try:
            from services.graph.entity_quality import (
                ENTITY_QUALITY_VERSION,
                entity_quality_stats,
            )

            quality_audit = await entity_quality_stats(self._neo4j, corpus_id)
        except Exception:
            quality_audit = {}
        totals = {
            "docs": len(docs),
            "chunks": total_chunks,
            "warning_docs": 0,
            "verify_failed_docs": 0,
            "vector_ready_docs": 0,
            "graph_pending_docs": 0,
            "graph_extracting_docs": 0,
            "graph_partial_docs": 0,
            "graph_retry_scheduled_docs": 0,
            "graph_ready_docs": 0,
            "needs_backfill_docs": 0,
            "graph_skipped_docs": 0,
            "vector_recovery_available_docs": 0,
            "ghost_b_failed_chunks": 0,
            "failed_chunk_count": 0,
            "graph_retryable_failed_chunks": 0,
            "relation_repair_jobs": 0,
            "relation_repair_pending": 0,
            "relation_repair_failed": 0,
            "relation_repair_succeeded": 0,
            "retry_budget_exhausted_count": 0,
            "all_lanes_exhausted_count": 0,
            "lane_cooling_down_count": 0,
            "provider_error_count": 0,
            "rate_limited_count": 0,
            "timeout_count": 0,
            "ghost_b_requested_chunks": 0,
            "ghost_b_extracted_chunks": 0,
            "ghost_b_tokens": 0,
            "ghost_b_attempts": 0,
            "json_recovery_count": 0,
            "estimated_cost_tokens": 0,
            "prompt_tokens": 0,
            "llm_graph_calls": 0,
            "summary_llm_calls": 0,
            "llm_fallback_chunks": 0,
            "llm_fallback_tokens": 0,
            "per_gpu_chunks_processed": {},
            "per_gpu_oom_count": {},
            "compact_extraction_chunks": 0,
            "deep_extraction_chunks": 0,
            "full_extraction_chunks": 0,
            "skipped_low_value_chunks": 0,
            "relations": 0,
            "related_to": 0,
            "domain_range_remaps": 0,
            "domain_range_remap_count": 0,
            "domain_range_warns": 0,
            "domain_range_warn_count": 0,
            "endpoint_completions": 0,
            "evidence_cue_repairs": 0,
            "evidence_cue_repair_count": 0,
            "direction_repair_count": 0,
            "predicate_confidence_avg": 0.0,
            "avg_prompt_tokens_per_chunk": 0.0,
            "json_recovery_rate": 0.0,
            "entity_quality": quality_audit,
            "entity_quality_total": int(
                (quality_audit or {}).get("total_entities") or 0
            ),
            "noisy_entity_count": int(
                (quality_audit or {}).get("noisy_entity_count") or 0
            ),
            "claim_like_count": int((quality_audit or {}).get("claim_like_count") or 0),
            "generic_role_count": int(
                (quality_audit or {}).get("generic_role_count") or 0
            ),
            "joined_list_count": int(
                (quality_audit or {}).get("joined_list_count") or 0
            ),
            "topic_label_eligible_pct": float(
                (quality_audit or {}).get("topic_eligible_pct") or 0.0
            ),
            "synthesis_eligible_pct": float(
                (quality_audit or {}).get("synthesis_eligible_pct") or 0.0
            ),
            "graph_cache": {
                "stale": bool((graph_cache_doc or {}).get("graph_cache_stale")),
                "stale_reason": (graph_cache_doc or {}).get("stale_reason"),
                "stale_at": (graph_cache_doc or {}).get("stale_at"),
                "last_graph_refresh_at": (
                    (graph_cache_doc or {}).get("last_graph_refresh_at")
                    or (graph_cache_doc or {}).get("computed_at")
                ),
                "entity_quality_version": (graph_cache_doc or {}).get(
                    "entity_quality_version"
                )
                or (quality_audit or {}).get("entity_quality_version")
                or (
                    ENTITY_QUALITY_VERSION
                    if "ENTITY_QUALITY_VERSION" in locals()
                    else ""
                ),
            },
        }
        partial_docs: list[dict] = []
        document_metrics: list[dict] = []
        predicate_confidence_weighted_sum = 0.0
        predicate_confidence_weight = 0

        def _doc_readiness(metrics: dict, ws: dict, failures: list) -> str:
            graph_status = str(ws.get("graph_status") or "")
            if graph_status == "needs_backfill":
                return "needs_backfill"
            if graph_status == "graph_retry_scheduled":
                return "graph_retry_scheduled"
            if graph_status == "graph_partial":
                return "needs_backfill"
            if graph_status in {"graph_pending", "graph_extracting"}:
                return graph_status
            relation_count = int(metrics.get("relation_count") or 0)
            success_rate = float(
                metrics.get("ghost_b_success_rate", metrics.get("success_rate", 1.0))
                or 0.0
            )
            failed_chunks = int(
                metrics.get("failed_chunk_count") or metrics.get("failed_chunks") or 0
            )
            related_ratio = float(metrics.get("related_to_ratio") or 0.0)
            predicate_avg = float(metrics.get("predicate_confidence_avg") or 0.0)
            remaps = int(metrics.get("domain_range_remap_count") or 0)
            warns = int(metrics.get("domain_range_warn_count") or 0)
            recoveries = int(metrics.get("json_recovery_count") or 0)
            attempts = int(metrics.get("attempt_count") or 0)
            if failures or failed_chunks or ws.get("verified") is False:
                return "needs_backfill"
            if success_rate < 0.95 or (
                recoveries and attempts and recoveries / max(attempts, 1) > 0.20
            ):
                return "extraction_unstable"
            if (
                related_ratio > 0.35
                or (relation_count and remaps > relation_count * 0.20)
                or (relation_count and warns > relation_count * 0.30)
                or (relation_count and predicate_avg and predicate_avg < 0.70)
            ):
                return "schema_review"
            return "ready"

        def _fallback_decision_trace(doc: dict, ws: dict, metrics: dict) -> dict:
            chunking = doc.get("chunking_config") or {}
            budgets = chunking.get("token_budgets") or {}
            vector_ready = bool(
                ws.get("vector_ready")
                or (ws.get("mongo_written") and ws.get("qdrant_written"))
            )
            graph_status = str(
                ws.get("graph_status")
                or ("graph_ready" if ws.get("neo4j_written") else "graph_pending")
            )
            skipped = int(metrics.get("skipped_low_value_chunks") or 0)
            reasons = []
            parent_strategy = str(chunking.get("parent_strategy") or "unknown")
            if parent_strategy == "pdf_page_grouped":
                reasons.append("PDF pages were grouped into token-sized parents.")
            elif parent_strategy.startswith("heading_bound"):
                reasons.append("Document headings were preserved as parent boundaries.")
            elif parent_strategy == "token_window":
                reasons.append("Weak document structure used token-window chunking.")
            if skipped:
                reasons.append(
                    f"{skipped} low-value chunk(s) were skipped for graph extraction."
                )
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
                "graph_strategy": str(metrics.get("extraction_strategy") or "unknown"),
                "graph_mode": str(metrics.get("extraction_mode") or "unknown"),
                "graph_extraction_engine": str(
                    metrics.get("graph_extraction_engine_used") or "unknown"
                ),
                "graph_completeness": str(metrics.get("graph_completeness") or ""),
                "reasons": reasons
                or [
                    "Legacy document; decision trace was derived from stored metadata."
                ],
                "warnings": [
                    "Derived fallback trace for a document ingested before decision traces existed."
                ],
            }

        for doc in docs:
            ws = doc.get("write_state") or {}
            warnings = ws.get("warnings") or []
            failures = doc.get("ghost_b_failures") or []
            staged = doc.get("ghost_b_staging") or []
            metrics = doc.get("ghost_b_metrics") or {}
            vector_ready = bool(
                ws.get("vector_ready")
                or (ws.get("mongo_written") and ws.get("qdrant_written"))
            )
            graph_status = str(ws.get("graph_status") or "")
            if not graph_status:
                if ws.get("neo4j_written"):
                    graph_status = "graph_ready"
                elif vector_ready:
                    graph_status = "graph_pending"
                else:
                    graph_status = "graph_pending"
            doc_failed_chunks = int(
                metrics.get("failed_chunk_count")
                or metrics.get("failed_chunks")
                or len(failures)
            )
            doc_extracted_chunks = int(metrics.get("extracted_chunks") or len(staged))
            doc_requested_chunks = int(
                metrics.get("requested_chunks")
                or (doc_extracted_chunks + doc_failed_chunks)
            )
            doc_relation_count = int(metrics.get("relation_count") or 0)
            doc_predicate_avg = float(metrics.get("predicate_confidence_avg") or 0.0)
            doc_readiness = _doc_readiness(metrics, ws, failures)
            decision_trace = doc.get("decision_trace") or _fallback_decision_trace(
                doc, ws, metrics
            )
            decision_trace_summary = str(
                doc.get("decision_trace_summary")
                or " - ".join(
                    [
                        str(
                            decision_trace.get("chunking_strategy") or "auto chunking"
                        ).replace("_", " "),
                        str(
                            decision_trace.get("graph_strategy") or "graph policy"
                        ).replace("_", " "),
                    ]
                )
            )
            graph_completeness = str(
                metrics.get("graph_completeness")
                or (
                    "needs-backfill"
                    if failures or doc_failed_chunks
                    else "graph-complete"
                )
            )
            if warnings:
                totals["warning_docs"] += 1
            if ws.get("verified") is False:
                totals["verify_failed_docs"] += 1
            if vector_ready:
                totals["vector_ready_docs"] += 1
            if ws.get("mongo_written") and not ws.get("qdrant_written"):
                totals["vector_recovery_available_docs"] += 1
            if graph_status == "graph_pending":
                totals["graph_pending_docs"] += 1
            elif graph_status == "graph_extracting":
                totals["graph_extracting_docs"] += 1
            elif graph_status == "graph_partial":
                totals["graph_partial_docs"] += 1
            elif graph_status == "graph_retry_scheduled":
                totals["graph_retry_scheduled_docs"] += 1
            elif graph_status == "graph_ready":
                totals["graph_ready_docs"] += 1
            elif graph_status == "needs_backfill":
                totals["needs_backfill_docs"] += 1
            elif graph_status == "graph_skipped":
                totals["graph_skipped_docs"] += 1
            if failures or doc_failed_chunks:
                if graph_status not in {"graph_partial", "graph_retry_scheduled"}:
                    totals["graph_partial_docs"] += 1
                partial_docs.append(
                    {
                        "doc_id": doc.get("doc_id"),
                        "filename": doc.get("filename"),
                        "failed_chunks": doc_failed_chunks,
                        "readiness": doc_readiness,
                    }
                )
            totals["relation_repair_jobs"] += int(ws.get("repair_total") or 0)
            totals["relation_repair_pending"] += int(ws.get("repair_pending") or 0)
            totals["relation_repair_failed"] += int(ws.get("repair_failed") or 0)
            totals["relation_repair_succeeded"] += int(ws.get("repair_succeeded") or 0)
            totals["ghost_b_failed_chunks"] += doc_failed_chunks
            totals["failed_chunk_count"] += doc_failed_chunks
            totals["graph_retryable_failed_chunks"] += int(
                metrics.get("retryable_failed_chunks") or 0
            )
            totals["retry_budget_exhausted_count"] += int(
                metrics.get("retry_budget_exhausted_count") or 0
            )
            totals["all_lanes_exhausted_count"] += int(
                metrics.get("all_lanes_exhausted_count") or 0
            )
            totals["lane_cooling_down_count"] += int(
                metrics.get("lane_cooling_down_count") or 0
            )
            totals["provider_error_count"] += int(
                metrics.get("provider_error_count") or 0
            )
            totals["rate_limited_count"] += int(metrics.get("rate_limited_count") or 0)
            totals["timeout_count"] += int(metrics.get("timeout_count") or 0)
            totals["ghost_b_requested_chunks"] += doc_requested_chunks
            totals["ghost_b_extracted_chunks"] += doc_extracted_chunks
            totals["ghost_b_tokens"] += int(metrics.get("total_tokens") or 0)
            totals["estimated_cost_tokens"] += int(
                metrics.get("estimated_cost_tokens") or metrics.get("total_tokens") or 0
            )
            totals["prompt_tokens"] += int(metrics.get("prompt_tokens") or 0)
            totals["llm_graph_calls"] += int(metrics.get("llm_graph_calls") or 0)
            totals["summary_llm_calls"] += int(metrics.get("summary_llm_calls") or 0)
            totals["llm_fallback_chunks"] += int(
                metrics.get("llm_fallback_chunks") or 0
            )
            totals["llm_fallback_tokens"] += int(
                metrics.get("llm_fallback_tokens") or 0
            )
            for gpu_name, count in (
                metrics.get("per_gpu_chunks_processed") or {}
            ).items():
                totals["per_gpu_chunks_processed"][gpu_name] = int(
                    totals["per_gpu_chunks_processed"].get(gpu_name) or 0
                ) + int(count or 0)
            for gpu_name, count in (metrics.get("per_gpu_oom_count") or {}).items():
                totals["per_gpu_oom_count"][gpu_name] = int(
                    totals["per_gpu_oom_count"].get(gpu_name) or 0
                ) + int(count or 0)
            totals["ghost_b_attempts"] += int(metrics.get("attempt_count") or 0)
            totals["json_recovery_count"] += int(
                metrics.get("json_recovery_count") or 0
            )
            totals["compact_extraction_chunks"] += int(
                metrics.get("compact_extraction_chunks") or 0
            )
            totals["deep_extraction_chunks"] += int(
                metrics.get("deep_extraction_chunks") or 0
            )
            totals["full_extraction_chunks"] += int(
                metrics.get("full_extraction_chunks") or 0
            )
            totals["skipped_low_value_chunks"] += int(
                metrics.get("skipped_low_value_chunks") or 0
            )
            totals["relations"] += doc_relation_count
            totals["related_to"] += int(metrics.get("related_to_count") or 0)
            remaps = int(metrics.get("domain_range_remap_count") or 0)
            warns = int(metrics.get("domain_range_warn_count") or 0)
            evidence_repairs = int(metrics.get("evidence_cue_repair_count") or 0)
            direction_repairs = int(metrics.get("direction_repair_count") or 0)
            totals["domain_range_remaps"] += remaps
            totals["domain_range_remap_count"] += remaps
            totals["domain_range_warns"] += warns
            totals["domain_range_warn_count"] += warns
            totals["endpoint_completions"] += int(
                metrics.get("endpoint_completion_count") or 0
            )
            totals["evidence_cue_repairs"] += evidence_repairs
            totals["evidence_cue_repair_count"] += evidence_repairs
            totals["direction_repair_count"] += direction_repairs
            if doc_relation_count and doc_predicate_avg:
                predicate_confidence_weighted_sum += (
                    doc_predicate_avg * doc_relation_count
                )
                predicate_confidence_weight += doc_relation_count
            document_metrics.append(
                {
                    "doc_id": doc.get("doc_id"),
                    "filename": doc.get("filename"),
                    "readiness": doc_readiness,
                    "vector_ready": vector_ready,
                    "graph_status": graph_status,
                    "decision_trace": decision_trace,
                    "decision_trace_summary": decision_trace_summary,
                    "decision_reasons": list(decision_trace.get("reasons") or []),
                    "chunking_strategy": str(
                        decision_trace.get("chunking_strategy") or "unknown"
                    ),
                    "graph_strategy": str(
                        decision_trace.get("graph_strategy") or "unknown"
                    ),
                    "graph_extraction_engine": str(
                        metrics.get("graph_extraction_engine_used")
                        or decision_trace.get("graph_extraction_engine")
                        or "unknown"
                    ),
                    "graph_completeness": graph_completeness,
                    "graph_retry_after": ws.get("graph_retry_after")
                    or metrics.get("graph_retry_after"),
                    "graph_backfill_attempt_count": int(
                        ws.get("graph_backfill_attempt_count") or 0
                    ),
                    "graph_retryable_failed_chunks": int(
                        metrics.get("retryable_failed_chunks") or 0
                    ),
                    "repair_status": ws.get("repair_status"),
                    "repair_total": int(ws.get("repair_total") or 0),
                    "repair_pending": int(ws.get("repair_pending") or 0),
                    "repair_failed": int(ws.get("repair_failed") or 0),
                    "vector_recovery_available": bool(
                        ws.get("mongo_written") and not ws.get("qdrant_written")
                    ),
                    "extraction_strategy": str(
                        metrics.get("extraction_strategy") or "unknown"
                    ),
                    "skipped_low_value_chunks": int(
                        metrics.get("skipped_low_value_chunks") or 0
                    ),
                    "compact_extraction_chunks": int(
                        metrics.get("compact_extraction_chunks") or 0
                    ),
                    "deep_extraction_chunks": int(
                        metrics.get("deep_extraction_chunks") or 0
                    ),
                    "full_extraction_chunks": int(
                        metrics.get("full_extraction_chunks") or 0
                    ),
                    "avg_prompt_tokens_per_chunk": float(
                        metrics.get("avg_prompt_tokens_per_chunk") or 0.0
                    ),
                    "ghost_b_success_rate": float(
                        metrics.get(
                            "ghost_b_success_rate", metrics.get("success_rate", 1.0)
                        )
                        or 0.0
                    ),
                    "failed_chunk_count": doc_failed_chunks,
                    "related_to_ratio": float(metrics.get("related_to_ratio") or 0.0),
                    "predicate_confidence_avg": doc_predicate_avg,
                    "domain_range_remap_count": remaps,
                    "domain_range_warn_count": warns,
                    "direction_repair_count": direction_repairs,
                    "evidence_cue_repair_count": evidence_repairs,
                    "json_recovery_count": int(metrics.get("json_recovery_count") or 0),
                    "llm_graph_calls": int(metrics.get("llm_graph_calls") or 0),
                    "summary_llm_calls": int(metrics.get("summary_llm_calls") or 0),
                    "llm_fallback_chunks": int(metrics.get("llm_fallback_chunks") or 0),
                    "per_gpu_graph_metrics": metrics.get("per_gpu_graph_metrics") or {},
                }
            )

        totals["ghost_b_success_rate"] = (
            round(
                totals["ghost_b_extracted_chunks"] / totals["ghost_b_requested_chunks"],
                4,
            )
            if totals["ghost_b_requested_chunks"]
            else round(totals["ghost_b_extracted_chunks"] / total_chunks, 4)
            if total_chunks
            else 1.0
        )
        totals["related_to_ratio"] = (
            round(totals["related_to"] / totals["relations"], 4)
            if totals["relations"]
            else 0.0
        )
        totals["predicate_confidence_avg"] = (
            round(predicate_confidence_weighted_sum / predicate_confidence_weight, 4)
            if predicate_confidence_weight
            else 0.0
        )
        totals["avg_prompt_tokens_per_chunk"] = (
            round(totals["prompt_tokens"] / totals["ghost_b_requested_chunks"], 2)
            if totals["ghost_b_requested_chunks"]
            else 0.0
        )
        totals["json_recovery_rate"] = (
            round(totals["json_recovery_count"] / totals["ghost_b_requested_chunks"], 4)
            if totals["ghost_b_requested_chunks"]
            else 0.0
        )
        readinesses = {doc.get("readiness") for doc in document_metrics}
        if "graph_retry_scheduled" in readinesses:
            readiness = "graph_retry_scheduled"
        elif (
            totals["verify_failed_docs"]
            or totals["failed_chunk_count"]
            or "needs_backfill" in readinesses
        ):
            readiness = "needs_backfill"
        elif "graph_extracting" in readinesses or "graph_pending" in readinesses:
            readiness = "graph_enrichment_pending"
        elif "extraction_unstable" in readinesses:
            readiness = "extraction_unstable"
        elif (
            "schema_review" in readinesses
            or totals["related_to_ratio"] > 0.35
            or totals["domain_range_remap_count"] > totals["relations"] * 0.2
        ):
            readiness = "schema_review"
        else:
            readiness = "ready"
        return {
            "corpus_id": corpus_id,
            "readiness": readiness,
            "totals": totals,
            "partial_docs": partial_docs,
            "document_metrics": document_metrics,
            "recommendations": [
                "Durable relation repairs are queued; vector RAG is usable while Gemma backfills the graph."
                if totals["relation_repair_pending"]
                else "No queued relation repairs.",
                "Run graph backfill for partial docs before large-batch graph analysis."
                if totals["failed_chunk_count"]
                else "No graph backfill needed.",
                "Inspect Ghost B model/prompt stability; JSON recovery or chunk success rate is outside target."
                if readiness == "extraction_unstable"
                else "Extraction stability is within the current target band.",
                "Review relation schema if related_to ratio stays above 35%."
                if totals["related_to_ratio"] > 0.35
                else "Relation specificity is within the current target band.",
            ],
        }

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
        warnings: list[str] = []
        if len(children) > 500:
            warnings.append(
                "High child-chunk count; ingest this file in a controlled batch."
            )
        if child_tokens and max(child_tokens) > 900:
            warnings.append(
                "At least one child chunk is unusually large and may stress extraction."
            )
        return {
            "filename": filename,
            "doc_id": doc_id,
            "source_mime": source_mime,
            "source_tier": parse_result.source_tier.value,
            "parent_count": len(parents),
            "child_count": len(children),
            "total_child_tokens": total_tokens,
            "avg_child_tokens": round(total_tokens / len(children), 1)
            if children
            else 0,
            "max_child_tokens": max(child_tokens) if child_tokens else 0,
            "ghost_b_calls": graph_calls,
            "summary_calls": summary_calls,
            "estimated_llm_calls": graph_calls + summary_calls,
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

    async def backfill_entity_quality(
        self,
        *,
        corpus_id: str,
        user_id: str,
        batch_size: int = 500,
        force: bool = False,
    ) -> dict:
        """Classify existing Neo4j entity labels without deleting graph data."""
        from services.graph.entity_quality import backfill_entity_quality
        from services.graph.orchestrator import schedule_graph_discovery_cache_warm

        result = await backfill_entity_quality(
            self._neo4j,
            self._db,
            corpus_id=corpus_id,
            batch_size=batch_size,
            force=force,
        )
        schedule_graph_discovery_cache_warm(
            qdrant=self._qdrant,
            neo4j_driver=self._neo4j,
            db=self._db,
            corpus_id=corpus_id,
            user_id=user_id,
        )
        result["graph_cache_warm"] = "queued"
        return result


ingestion_service = IngestionService()
