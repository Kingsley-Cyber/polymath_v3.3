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

import logging
import uuid
from datetime import datetime
from typing import Optional

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


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
        self._qdrant = AsyncQdrantClient(url=self._settings.QDRANT_URL)
        logger.info("IngestionService: Qdrant connected (per-corpus collections)")

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

    @property
    def neo4j_driver(self):
        """Expose Neo4j async driver for graph router."""
        return self._neo4j

    async def disconnect(self) -> None:
        if self._qdrant:
            await self._qdrant.close()
        if self._neo4j:
            await self._neo4j.close()
        logger.info("IngestionService: clients closed")

    async def ingest(
        self,
        data: bytes,
        filename: str,
        corpus_id: str,
        user_id: str,
        ingestion_config: IngestionConfig,
        model: str,
    ) -> IngestJobResponse:
        """Run the full ingestion pipeline for one document."""
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
        )
        return await upsert_schema_terms(
            self._qdrant, corpus_id, terms, kind, vectors
        )

    async def create_corpus(
        self,
        name: str,
        description: Optional[str],
        user_id: str,
        ingestion_config: IngestionConfig,
    ) -> dict:
        from services.storage.mongo_writer import upsert_corpus

        # Coerce embed_mode to "local_st" when Modal is disabled server-side,
        # so the frozen config reflects what will actually run.
        if (
            ingestion_config.embed_mode == "modal_tei"
            and not self._settings.MODAL_ENABLED
        ):
            ingestion_config = ingestion_config.model_copy(
                update={"embed_mode": "local_st"}
            )

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

        # Phase 7.5 — provision the 4 per-corpus Qdrant collections up front
        # so the first ingest doesn't race with collection creation.
        from services.storage.qdrant_writer import ensure_collections_for_corpus

        try:
            await ensure_collections_for_corpus(
                self._qdrant,
                corpus_doc["corpus_id"],
                dim=self._settings.EMBEDDING_DIMENSION,
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
        for doc in docs:
            self._mask_ingestion_keys_in_place(doc.get("default_ingestion_config"))
        return docs

    async def get_corpus(self, corpus_id: str) -> Optional[dict]:
        from services.storage.mongo_reader import get_corpus

        doc = await get_corpus(self._db, corpus_id)
        if doc:
            self._mask_ingestion_keys_in_place(doc.get("default_ingestion_config"))
        return doc

    async def _get_corpus_raw(self, corpus_id: str) -> Optional[dict]:
        """Unmasked read — used by update_corpus so `_encrypt_ingestion_keys_in_place`
        can diff the incoming patch against real stored ciphertext. NEVER return
        this to the API layer."""
        from services.storage.mongo_reader import get_corpus

        return await get_corpus(self._db, corpus_id)

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

        for pool_field in ("summary_models", "extraction_models"):
            pool = config_dict.get(pool_field)
            if not pool:
                continue
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("api_key")
                entry["api_key"] = "[set]" if raw else None

        # Legacy scalar fields — mask for older readers during the migration
        # window. The pre-validator will strip these when the config is loaded
        # through Pydantic.
        for legacy in ("summary_api_key", "extraction_api_key"):
            if legacy in config_dict:
                raw = config_dict.get(legacy)
                config_dict[legacy] = "[set]" if raw else None

    async def get_job_status(self, doc_id: str) -> Optional[dict]:
        from services.storage.mongo_reader import get_document

        return await get_document(self._db, doc_id)

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

        for pool_field in ("summary_models", "extraction_models"):
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
                entry["api_key"] = _enc(entry.get("api_key"), existing_entry.get("api_key"))

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

        # Guard: if doc_count > 0 and the update touches default_ingestion_config,
        # reject changes to embedding_model / embedding_dimension / embedding_model_id.
        new_config = updates.get("default_ingestion_config")
        existing = None
        if new_config is not None:
            # Use the raw (unmasked) fetch so we have real ciphertext to diff
            # incoming api_key values against. get_corpus() would have masked
            # api_key entries to "[set]", losing the stored ciphertext.
            existing = await self._get_corpus_raw(corpus_id)
            if existing and existing.get("doc_count", 0) > 0:
                existing_config = existing.get("default_ingestion_config") or {}
                changed_locks = [
                    field
                    for field in self._LOCKED_EMBEDDING_FIELDS
                    if field in new_config
                    and field in existing_config
                    and new_config[field] != existing_config[field]
                ]
                if changed_locks:
                    raise ValueError(
                        "Cannot change frozen embedding fields after ingest: "
                        + ", ".join(sorted(changed_locks))
                        + ". Different embedding model or dimension = incompatible "
                        "vectors in Qdrant. Create a new corpus instead."
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

        # 5. Delete corpus record
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

    async def list_all_user_documents(
        self,
        user_id: str,
        limit: int = 100,
    ) -> list[dict]:
        """List all documents across all corpora for a user."""
        from services.storage.mongo_reader import list_all_user_documents

        return await list_all_user_documents(self._db, user_id=user_id, limit=limit)


ingestion_service = IngestionService()
