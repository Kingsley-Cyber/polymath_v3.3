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
import logging
import hashlib
import mimetypes
import uuid
from datetime import datetime
from typing import Any, Optional

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

FROZEN_CONFIG_FIELDS: frozenset[str] = frozenset({
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
})

MUTABLE_CONFIG_FIELDS: frozenset[str] = frozenset({
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
})


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
        return "generated_in_call", [*clauses, {"parent_id": {"$in": generated_parent_ids}}]
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
                [("source_identity.source_key", 1), ("corpus_id", 1)],
                name="documents_source_identity_key_corpus_idx",
                sparse=True,
                background=True,
            )
            await self._db["documents"].create_index(
                [("youtube_video_id", 1), ("corpus_id", 1)],
                name="documents_youtube_video_corpus_idx",
                sparse=True,
                background=True,
            )
        except Exception as exc:
            logger.warning("Source identity index setup skipped: %s", exc)

    async def disconnect(self) -> None:
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
            current = str(
                ((doc.get("default_ingestion_config") or {}).get("extraction_engine"))
                or ""
            ).strip().lower()
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
                        reasons.append(f"missing_universal_entities={len(missing_entities)}")
                if not existing_relations:
                    new_relations = universal_relations
                    reasons.append("null_relation_schema")
                elif all(rel in universal_relations for rel in existing_relations):
                    missing_relations = [
                        rel for rel in universal_relations if rel not in existing_relations
                    ]
                    if missing_relations:
                        body = [rel for rel in existing_relations if rel != "related_to"]
                        additions = [rel for rel in missing_relations if rel != "related_to"]
                        new_relations = [*body, *additions, "related_to"]
                        reasons.append(f"missing_universal_relations={len(missing_relations)}")
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
            logger.warning(
                "migrate_bare_model_names: DB not connected — skipping"
            )
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
                rewritten = _needs_rewrite(entry.get("provider_preset"), entry.get("model"))
                if rewritten is None:
                    continue
                rewrites_for_doc.append(
                    ("summary_models", idx, entry["model"], rewritten)
                )
                entry["model"] = rewritten

            for idx, entry in enumerate(new_extraction):
                if not isinstance(entry, dict):
                    continue
                rewritten = _needs_rewrite(entry.get("provider_preset"), entry.get("model"))
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
                    doc["corpus_id"], field, idx, old_model, new_model,
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
        return await upsert_schema_terms(
            self._qdrant, corpus_id, terms, kind, vectors
        )

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

            global_ingestion = await settings_service.get_runtime_ingestion_settings(user_id)
        except Exception as exc:  # noqa: BLE001 - settings defaults are best-effort
            logger.warning("global summary defaults unavailable for user=%s: %s", user_id, exc)
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
                    and (a.get("provider_preset") or "") == (b.get("provider_preset") or "")
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
                if idx < len(runtime_summary_models) and _matches(entry, runtime_summary_models[idx]):
                    replacement = runtime_summary_models[idx]
                else:
                    replacement = next(
                        (candidate for candidate in runtime_summary_models if _matches(entry, candidate)),
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
        ready_rows = await self._db["documents"].aggregate([
            {"$match": with_active_records({"corpus_id": {"$in": corpus_ids}, "write_state.verified": True})},
            {"$group": {"_id": "$corpus_id", "count": {"$sum": 1}}},
        ]).to_list(length=None)
        ready_counts = {str(r["_id"]): int(r["count"]) for r in ready_rows}
        for doc in docs:
            cid = doc.get("corpus_id")
            if not cid:
                continue
            actual_docs = doc_counts.get(cid, 0)
            actual_chunks = chunk_counts.get(cid, 0)
            doc["ready_doc_count"] = ready_counts.get(cid, 0)
            if doc.get("doc_count", 0) != actual_docs or doc.get("chunk_count", 0) != actual_chunks:
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
            existing_pool = (existing_config or {}).get(pool_field) or [] if existing_config else []
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
            existing_val = (existing_config or {}).get(field) if existing_config else None
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

            # Phase 7.5 — if the corpus name changed, re-point Qdrant aliases.
            # Best-effort; failures are logged inside rename_corpus_aliases.
            if "name" in updates and updates["name"]:
                from services.storage.qdrant_writer import rename_corpus_aliases
                try:
                    await rename_corpus_aliases(self._qdrant, corpus_id, updates["name"])
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

        existed = await mark_corpus_deleting(self._db, corpus_id)

        # Phase 7.5 — atomically drop all 4 per-corpus collections (naive,
        # hrag, graph, schemas). A whole-collection drop is fast regardless of
        # point count, so it stays synchronous.
        try:
            await drop_collections_for_corpus(self._qdrant, corpus_id)
        except Exception:
            logger.warning(
                "Failed to drop per-corpus Qdrant collections for %s", corpus_id
            )

        # Mark document/support rows synchronously so evidence cannot be read
        # while the heavier chunk + graph projection cleanup runs.
        await delete_documents_by_corpus(self._db, corpus_id)

        # Background the bulk deletes (chunks + Neo4j) — they no longer gate
        # the HTTP response.
        try:
            asyncio.create_task(self._purge_corpus_bulk(corpus_id))
        except RuntimeError:
            # No running loop (sync context) — fall back to inline so data is
            # still cleaned, just slower.
            await self._purge_corpus_bulk(corpus_id)

        return existed

    async def _purge_corpus_bulk(self, corpus_id: str) -> None:
        """Background bulk cleanup for a deleted corpus: Mongo chunks +
        batched Neo4j graph. Best-effort — orphaned rows keyed by a dead
        corpus_id are harmless and re-runnable."""
        from services.storage.mongo_writer import delete_chunks_by_corpus, delete_corpus

        try:
            await delete_chunks_by_corpus(self._db, corpus_id)
        except Exception:
            logger.warning("Background chunk purge failed for corpus %s", corpus_id)
            return
        if self._settings.NEO4J_ENABLED and self._neo4j:
            try:
                from services.graph.neo4j_writer import delete_corpus_graph

                await delete_corpus_graph(self._neo4j, corpus_id=corpus_id)
            except Exception:
                logger.warning("Background Neo4j purge failed for corpus %s", corpus_id)
                return
        try:
            await delete_corpus(self._db, corpus_id)
        except Exception:
            logger.warning("Final corpus tombstone mark failed for corpus %s", corpus_id)
        logger.info("Background purge complete for corpus %s", corpus_id)

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
                from services.graph.neo4j_writer import delete_document_graph

                await delete_document_graph(
                    self._neo4j,
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                )
            except Exception:
                logger.warning(
                    "Neo4j per-doc delete failed for doc %s", doc_id[:12]
                )

        # 3. Mongo chunks.
        await delete_chunks_by_doc(self._db, corpus_id, doc_id)

        # 4. Mongo document record.
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

    async def get_ingestion_audit(self, corpus_id: str) -> dict:
        """Aggregate corpus ingestion health for large-batch readiness."""
        docs = await self._db["documents"].find(
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
        ).to_list(length=None)
        total_chunks = await self._db["chunks"].count_documents({"corpus_id": corpus_id})
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
            totals["domain_range_remaps"] += int(metrics.get("domain_range_remap_count") or 0)
            totals["domain_range_warns"] += int(metrics.get("domain_range_warn_count") or 0)
            totals["endpoint_completions"] += int(metrics.get("endpoint_completion_count") or 0)
            totals["evidence_cue_repairs"] += int(metrics.get("evidence_cue_repair_count") or 0)

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
        if totals["related_to_ratio"] > 0.35 or totals["domain_range_remaps"] > totals["relations"] * 0.2:
            readiness = "schema_review"
        return {
            "corpus_id": corpus_id,
            "readiness": readiness,
            "totals": totals,
            "partial_docs": partial_docs[:50],
            "recommendations": [
                "Run graph backfill for partial docs before large-batch graph analysis."
                if totals["ghost_b_failed_chunks"]
                else "No graph backfill needed.",
                "Review relation schema if related_to ratio stays above 35%."
                if totals["related_to_ratio"] > 0.35
                else "Relation specificity is within the current target band.",
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
    ) -> dict[str, Any]:
        """Repair parent-summary retrieval for an existing corpus.

        This intentionally does not mutate the frozen ``chunk_summarization``
        ingest flag. It fills missing body-parent summaries, idempotently
        indexes parent-summary text into Qdrant, and updates each document's
        ``write_state.summaries_indexed`` when its body parents are covered.
        Capped generate+index calls index only the summaries generated in that
        call unless ``doc_ids`` scopes the run to a known batch. Explicit
        index-only calls still rebuild all existing summary points. That lets
        older balanced corpora gain the summary retrieval lane without
        delete/reingest churn or surprise full-corpus work.
        """

        from pymongo import UpdateOne
        from qdrant_client import models

        from services.embedder import embed_batch
        from services.ghost_a import SummaryTask, summarize_parents
        from services.settings import settings_service
        from services.storage.qdrant_writer import _col_for_corpus, upsert_summaries

        corpus = await self._get_corpus_raw(corpus_id)
        if not corpus:
            return {"corpus_id": corpus_id, "status": "not_found", "error": "corpus not found"}

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

        body_clause = {
            "$or": [
                {"chunk_kind": {"$exists": False}},
                {"chunk_kind": None},
                {"chunk_kind": "body"},
            ]
        }
        summary_text_clause = {"summary": {"$exists": True, "$nin": [None, ""]}}
        missing_summary_clause = {
            "$or": [{"summary": {"$exists": False}}, {"summary": None}, {"summary": ""}]
        }

        def _parent_query(*clauses: dict) -> dict:
            query: dict[str, Any] = {
                "corpus_id": corpus_id,
                "$and": [body_clause, *clauses],
            }
            if doc_id_filter is not None:
                query["doc_id"] = {"$in": doc_id_filter}
            return query

        async def _summary_health() -> dict[str, Any]:
            body_parent_count = await self._db["parent_chunks"].count_documents(_parent_query())
            with_summary_text = await self._db["parent_chunks"].count_documents(
                _parent_query(summary_text_clause)
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
                min(indexed_summary_points, body_parent_count) / body_parent_count
                if body_parent_count
                else 1.0
            )
            if body_parent_count == 0:
                status = "empty"
            elif with_summary_text >= body_parent_count and indexed_summary_points >= body_parent_count:
                status = "healthy"
            elif with_summary_text or indexed_summary_points:
                status = "partial"
            else:
                status = "degraded"
            health: dict[str, Any] = {
                "body_parent_count": body_parent_count,
                "with_summary_text": with_summary_text,
                "missing_summary_text": max(body_parent_count - with_summary_text, 0),
                "indexed_summary_points": indexed_summary_points,
                "coverage": round(coverage, 4),
                "status": status,
            }
            if qdrant_error:
                health["qdrant_error"] = qdrant_error
            return health

        before = await _summary_health()
        generated = 0
        attempted = 0
        generation_batches = 0
        generation_errors: list[str] = []
        generated_parent_ids: list[str] = []

        if generate and (limit is None or limit > 0):
            runtime_summary = (
                await settings_service.get_runtime_ingestion_settings(effective_user_id)
            ).summary
            pool_refs = (
                runtime_summary.summary_models
                if runtime_summary.enabled and runtime_summary.summary_models
                else cfg.summary_models
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
                    "base_url": (ref.base_url if hasattr(ref, "base_url") else ref.get("base_url")) or None,
                    "api_key": _plaintext_key(
                        (ref.api_key if hasattr(ref, "api_key") else ref.get("api_key"))
                        or None
                    ),
                    "max_concurrent": int(
                        (ref.max_concurrent if hasattr(ref, "max_concurrent") else ref.get("max_concurrent"))
                        or 1
                    ),
                    "extra_params": (
                        ref.extra_params if hasattr(ref, "extra_params") else ref.get("extra_params")
                    )
                    or {},
                }
                for ref in (pool_refs or [])
            ]
            max_summary_tokens = (
                runtime_summary.max_summary_tokens
                if runtime_summary.enabled
                else cfg.max_summary_tokens
            )
            global_max_concurrent = runtime_summary.max_concurrent if runtime_summary.enabled else None

            if not pool:
                generation_errors.append("no summary model pool configured")
            else:
                while limit is None or attempted < limit:
                    fetch = batch if limit is None else max(0, min(batch, limit - attempted))
                    if fetch <= 0:
                        break
                    rows = await self._db["parent_chunks"].find(
                        _parent_query(missing_summary_clause),
                        {
                            "_id": 0,
                            "parent_id": 1,
                            "doc_id": 1,
                            "corpus_id": 1,
                            "source_tier": 1,
                            "text": 1,
                        },
                    ).limit(fetch).to_list(length=fetch)
                    rows = [r for r in rows if (r.get("text") or "").strip()]
                    if not rows:
                        break
                    generation_batches += 1
                    attempted += len(rows)
                    tasks = [
                        SummaryTask(
                            parent_id=r["parent_id"],
                            doc_id=r.get("doc_id", ""),
                            corpus_id=corpus_id,
                            source_tier=r.get("source_tier") or "parent",
                            text=r["text"],
                        )
                        for r in rows
                    ]
                    results = await summarize_parents(
                        tasks,
                        max_summary_tokens=max_summary_tokens,
                        pool=pool,
                        global_max_concurrent=global_max_concurrent,
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
                                    "$set": {
                                        "summary": r.summary,
                                        "domain": r.domain,
                                        "topics": r.topics,
                                        "summary_updated_at": now,
                                    }
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
                bounded_by_doc_ids=doc_id_filter is not None,
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
                    [str(p.get("summary") or "") for p in buf],
                    mode="local",
                    expected_dim=cfg.embedding_dimension,
                    expected_model_id=cfg.embedding_model_id,
                )
                payloads = [
                    {
                        **p,
                        "user_id": effective_user_id,
                        "source_tier": p.get("source_tier") or "parent",
                    }
                    for p in buf
                ]
                await upsert_summaries(
                    self._qdrant,
                    corpus_id,
                    payloads,
                    vectors,
                    target_kinds=target_kinds,
                )
                indexed += len(buf)
                buf.clear()

            async for parent in cursor:
                buf.append(parent)
                if len(buf) >= batch:
                    await _flush()
            await _flush()

            ready_by_doc: dict[str, bool] = {}
            pipeline = [
                {"$match": _parent_query()},
                {
                    "$group": {
                        "_id": "$doc_id",
                        "body_parent_count": {"$sum": 1},
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
                ready_by_doc[str(row["_id"])] = int(row.get("body_parent_count") or 0) <= int(
                    row.get("with_summary") or 0
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
                                    "write_state.summary_backfilled_at": now,
                                }
                            },
                        )
                        for doc_id, ready in ready_by_doc.items()
                    ],
                    ordered=False,
                )

        after = await _summary_health()
        return {
            "corpus_id": corpus_id,
            "status": after["status"],
            "doc_scope_count": len(doc_id_filter) if doc_id_filter is not None else None,
            "generated": generated,
            "attempted": attempted,
            "indexed": indexed,
            "index_scope": index_scope,
            "generation_batches": generation_batches,
            "generation_errors": generation_errors,
            "before": before,
            "after": after,
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
        settings = get_settings()
        extraction_pool = (
            ingestion_config.summary_models
            if ingestion_config.models_linked
            else ingestion_config.extraction_models
        )

        def _uses_managed_vllm(ref) -> bool:
            from services.extraction_provider_cards import extraction_lane_uses_private_vllm

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
        extraction_concurrency = sum(
            max(1, int(getattr(m, "max_concurrent", 1) or 1))
            for m in extraction_pool
        ) or settings.EXTRACTION_MAX_CONCURRENT
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
            warnings.append("High child-chunk count; ingest this file in a controlled batch.")
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
            "avg_child_tokens": round(total_tokens / len(children), 1) if children else 0,
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
            "chunking_config": tier_chunker.describe_chunking(parse_result, ingestion_config),
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
