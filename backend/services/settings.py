"""
Settings service — per-user global app settings persisted to MongoDB.

Collection: `settings` (one document per user_id).
Bootstrap: on first read for a user, seeds defaults from config.py so the
frontend always gets a complete GlobalSettings object.

Architecture (see .Agent/Plan/SETTINGS_ARCHITECTURE.md):
  - Global settings = system-wide, mutable anytime (chat, retrieval, models,
    extraction endpoints, and ingestion defaults; infrastructure remains env-backed)
  - Per-corpus settings = IngestionConfig, frozen after first ingest (handled by ingestion_service)

Sensitive fields (API keys, service URLs) come from config.py env vars.
The API response masks them as "••••••••". The settings service stores
only the user-mutable sections (chat, retrieval) in MongoDB.
Infrastructure is always read from config.py at runtime.
"""

import logging
import os
import copy
from datetime import datetime
from typing import Any
from uuid import uuid4

from config import get_settings
from models.schemas import (
    AuthConfig,
    ChatLLMSettings,
    ExtractionEndpoint,
    ExtractionSettings,
    GlobalSettings,
    GlobalIngestionSettings,
    GlobalIngestionSummarySettings,
    RunpodFlashExtractionSettings,
    InfrastructureSettings,
    ModalDeploySettings,
    ModelsConfig,
    QueryModelPoolEntry,
    RetrievalSettings,
)
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

# Sentinel written on update payloads to mean "preserve existing ciphertext".
# Same semantics as ingestion pool keys in ingestion_service._enc.
_MASK_SENTINEL = "[set]"
_DEFAULT_CHAT_ENTRY_ID = "system-default-chat"

# Sections that users CAN modify via PUT /api/settings
_MUTABLE_SECTIONS = {"chat", "retrieval", "modal", "models", "extraction", "ingestion"}

# Sections that are always read from config.py (env vars) — never stored in MongoDB
_IMMUTABLE_SECTIONS = {"infrastructure"}


def _provider_from_model_id(model: str) -> str:
    """Infer the pool provider for a provider-prefixed LiteLLM model id."""
    raw = (model or "").strip()
    if "/" in raw:
        prefix = raw.split("/", 1)[0].lower()
        if prefix == "gemini":
            return "google"
        return prefix
    if ":" in raw:
        return "ollama"
    return "custom"


def _model_label(model: str) -> str:
    """Human-readable compact label for generated default pool entries."""
    tail = (model or "").split("/", 1)[-1]
    return tail.replace("_", " ").replace("-", " ").title() or "Model"


class SettingsService:
    """CRUD + bootstrap for per-user GlobalSettings documents."""

    # Phase 24 perf — short TTL cache on get_settings. Every chat turn calls
    # this twice (Custom profile resolver + Phase F resolver). Cache hit
    # avoids two Mongo round-trips per query (~30-60ms total). 60s TTL
    # balances freshness vs hit rate; cache is invalidated on update_*
    # methods so saves are still seen immediately.
    _CACHE_TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self._db: AsyncIOMotorDatabase | None = None
        self._config = get_settings()
        # cache: user_id -> (timestamp, GlobalSettings)
        self._settings_cache: dict[str, tuple[float, GlobalSettings]] = {}

    def attach(self, db: AsyncIOMotorDatabase) -> None:
        """Called from main.py lifespan after MongoDB connects."""
        self._db = db
        self._settings_cache.clear()

    def _invalidate_cache(self, user_id: str | None = None) -> None:
        """Drop cached settings for one user or all users. Called on writes."""
        if user_id is None:
            self._settings_cache.clear()
        else:
            self._settings_cache.pop(user_id, None)

    def _infrastructure_defaults(self) -> InfrastructureSettings:
        """
        Build infrastructure section from config.py env vars.
        Sensitive fields are MASKED in the response — raw values never leave the backend.
        """
        c = self._config
        # Mask userinfo in Modal URL (never round-trip credentials to frontend)
        import re as _re

        masked_modal_url = (
            _re.sub(r"://[^@/]+@", "://", c.MODAL_EMBEDDER_URL)
            if c.MODAL_EMBEDDER_URL
            else ""
        )
        return InfrastructureSettings(
            mongodb_url=c.MONGODB_URI,
            qdrant_url=c.QDRANT_URL,
            neo4j_uri=c.NEO4J_URI,
            neo4j_user=c.NEO4J_USER,
            neo4j_password="••••••••",
            litellm_base_url=c.LITELLM_URL,
            litellm_master_key="••••••••",
            ollama_base_url=c.OLLAMA_URL,
            redis_url=c.REDIS_URL,
            embedder_url=c.EMBEDDER_URL,
            reranker_url=c.RERANKER_URL,
            extraction_url=(os.environ.get("LOCAL_GHOST_B_EXTRACT_URL", "") or "").split(",")[0].strip(),
            modal_enabled=c.MODAL_ENABLED,
            modal_embedder_url=masked_modal_url,
            auth=AuthConfig(
                auth_secret_key="••••••••",
                auth_algorithm=c.AUTH_ALGORITHM,
                auth_token_expire_days=c.AUTH_TOKEN_EXPIRE_DAYS,
            ),
        )

    def _defaults(self) -> GlobalSettings:
        """Build a fresh GlobalSettings from config.py as the seed."""
        c = self._config
        return GlobalSettings(
            infrastructure=self._infrastructure_defaults(),
            chat=ChatLLMSettings(
                default_chat_model=c.DEFAULT_COMPLETION_MODEL,
                max_context_tokens=c.MAX_CONTEXT_TOKENS,
                max_completion_tokens=c.MAX_COMPLETION_TOKENS,
                temperature=0.7,
                top_p=0.9,
                agentic_mode_enabled=c.AGENTIC_MODE_ENABLED,
                agentic_model=c.AGENTIC_MODEL,
                hyde_model=c.HYDE_MODEL,
                query_profile="balanced",  # Phase 18 — speed preset default
            ),
            retrieval=RetrievalSettings(
                default_tier="qdrant_mongo",
                top_k_child=60,
                top_k_summary=20,
                reranker_model=c.RERANKER_MODEL,
                rerank_top_n=40,
                rerank_enabled=True,
                similarity_threshold=0.0,
                max_corpora_per_query=32,
                neo4j_expansion_cap=c.GRAPH_EXPANSION_LIMIT,
                final_top_k=8,
                fact_seed_limit=c.GRAPH_FACT_SEED_LIMIT,
                vector_child_chunks=70,
                vector_summaries=30,
                vector_final_sources=12,
                vector_reranker=True,
                hybrid_child_chunks=60,
                hybrid_summaries=20,
                hybrid_final_sources=8,
                hybrid_reranker=True,
                graph_child_chunks=40,
                graph_summaries=20,
                graph_fact_seeds=c.GRAPH_FACT_SEED_LIMIT,
                graph_expansion=c.GRAPH_EXPANSION_LIMIT,
                graph_final_sources=8,
                graph_reranker=True,
                graph_query_seed_entities=3,
                graph_query_max_hops=2,
                graph_query_node_limit=80,
            ),
            modal=ModalDeploySettings(),
            extraction=self._extraction_defaults_from_env(),
            ingestion=GlobalIngestionSettings(
                provider_models=[],
                summary=GlobalIngestionSummarySettings(
                    enabled=False,
                    max_summary_tokens=c.SUMMARY_MAX_TOKENS,
                    max_concurrent=c.SUMMARY_MAX_CONCURRENT,
                    summary_models=[],
                ),
                runpod_flash=RunpodFlashExtractionSettings(),
            ),
        )

    def _extraction_defaults_from_env(self) -> ExtractionSettings:
        """Seed extraction endpoints from LOCAL_GHOST_B_EXTRACT_URL so
        existing env-wired deployments see their current setup in the UI."""
        import os

        raw = os.environ.get(
            "LOCAL_GHOST_B_EXTRACT_URL", "http://host.docker.internal:8084"
        )
        endpoints: list[ExtractionEndpoint] = []
        for i, u in enumerate(x.strip().rstrip("/") for x in raw.split(",")):
            if not u:
                continue
            local = "host.docker.internal" in u or "localhost" in u or "127.0.0.1" in u
            endpoints.append(
                ExtractionEndpoint(
                    label="Local sidecar" if local else f"GPU box {i + 1}",
                    url=u,
                    enabled=True,
                )
            )
        return ExtractionSettings(endpoints=endpoints)

    async def get_system_extraction(self) -> ExtractionSettings:
        """Extraction endpoints for the ingestion worker. Reads the first
        settings doc (single-admin deployments) and falls back to env-seeded
        defaults — same pattern as get_system_modal, so UI toggles apply on
        the next ingest without a backend restart."""
        if self._db is not None:
            try:
                doc = await self._db["settings"].find_one(
                    {"extraction": {"$exists": True}}
                )
                if doc and doc.get("extraction"):
                    return ExtractionSettings(**doc["extraction"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_system_extraction fell back to env: %s", exc)
        return self._extraction_defaults_from_env()

    @staticmethod
    def _mask_ingestion_keys_in_place(ingestion_raw: dict | None) -> None:
        """Mask ingestion-registry and legacy Ghost A keys before UI return."""
        if not ingestion_raw:
            return
        pools = [
            ingestion_raw.get("provider_models") or [],
            (ingestion_raw.get("summary") or {}).get("summary_models") or [],
        ]
        for pool in pools:
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                for field in ("api_key", "lifecycle_api_key"):
                    entry[field] = _MASK_SENTINEL if entry.get(field) else None

    @staticmethod
    def _decrypt_ingestion_keys_in_place(ingestion_raw: dict | None) -> None:
        """Decrypt settings-level Ghost A API keys for backend runtime use."""
        if not ingestion_raw:
            return
        from services.secrets import decrypt

        pools = [
            ingestion_raw.get("provider_models") or [],
            (ingestion_raw.get("summary") or {}).get("summary_models") or [],
        ]
        for pool in pools:
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                for field in ("api_key", "lifecycle_api_key"):
                    raw_key = entry.get(field)
                    if raw_key:
                        plaintext = decrypt(raw_key)
                        entry[field] = plaintext if plaintext is not None else raw_key

    @staticmethod
    def _encrypt_ingestion_keys_in_place(
        ingestion_raw: dict,
        existing_ingestion_raw: dict | None = None,
    ) -> None:
        """Encrypt settings-level Ghost A pool keys before Mongo persistence.

        ``"[set]"`` and blank values preserve the existing ciphertext by index,
        matching corpus-level ``summary_models`` update semantics.
        """
        from services.secrets import decrypt, encrypt

        def _enc(new_val, existing_val, *, same_target: bool, provider: str):
            if provider in {"ollama", "ollama_chat", "local"} and not new_val:
                return None
            if not new_val or new_val == _MASK_SENTINEL:
                return existing_val if same_target else None
            if (
                isinstance(new_val, str)
                and new_val.startswith("gAAAAA")
                and decrypt(new_val) is not None
            ):
                return new_val
            return encrypt(new_val)

        registry = ingestion_raw.get("provider_models") or []
        existing_registry = (existing_ingestion_raw or {}).get("provider_models") or []
        existing_by_id = {
            str(entry.get("profile_id")): entry
            for entry in existing_registry
            if isinstance(entry, dict) and entry.get("profile_id")
        }
        for entry in registry:
            if not isinstance(entry, dict):
                continue
            entry["profile_id"] = str(entry.get("profile_id") or uuid4())
            entry.setdefault(
                "profile_label",
                str(entry.get("model") or entry.get("provider_preset") or "Ingestion route"),
            )
            existing_entry = existing_by_id.get(entry["profile_id"], {})
            same_target = all(
                (entry.get(key) or None) == (existing_entry.get(key) or None)
                for key in ("provider_preset", "model", "base_url")
            )
            provider = str(entry.get("provider_preset") or "").lower()
            for field in ("api_key", "lifecycle_api_key"):
                entry[field] = _enc(
                    entry.get(field),
                    existing_entry.get(field),
                    same_target=same_target,
                    provider=provider,
                )

        registry_by_id = {
            str(entry.get("profile_id")): entry
            for entry in registry
            if isinstance(entry, dict) and entry.get("profile_id")
        }
        summary = ingestion_raw.get("summary") or {}
        pool = summary.get("summary_models") or []
        existing_summary = (existing_ingestion_raw or {}).get("summary") or {}
        existing_pool = existing_summary.get("summary_models") or []

        def _same_summary_target(entry: dict, existing_entry: dict) -> bool:
            return all(
                (entry.get(key) or None) == (existing_entry.get(key) or None)
                for key in ("provider_preset", "model", "base_url")
            )

        for idx, entry in enumerate(pool):
            if not isinstance(entry, dict):
                continue
            profile_id = str(entry.get("profile_id") or "")
            registry_entry = registry_by_id.get(profile_id)
            if registry_entry:
                concurrency = entry.get("max_concurrent")
                entry.clear()
                entry.update(copy.deepcopy(registry_entry))
                if concurrency is not None:
                    entry["max_concurrent"] = concurrency
                continue
            existing_entry = (
                existing_pool[idx]
                if idx < len(existing_pool) and isinstance(existing_pool[idx], dict)
                else {}
            )
            entry["api_key"] = _enc(
                entry.get("api_key"),
                existing_entry.get("api_key"),
                same_target=_same_summary_target(entry, existing_entry),
                provider=str(entry.get("provider_preset") or "").lower(),
            )

    async def get_ingestion_provider_registry_raw(
        self, user_id: str
    ) -> list[dict[str, Any]]:
        """Return encrypted registry rows for server-side corpus materialization."""
        if self._db is None:
            return []
        doc = await self._db["settings"].find_one(
            {"user_id": user_id}, {"_id": 0, "ingestion.provider_models": 1}
        )
        return copy.deepcopy(((doc or {}).get("ingestion") or {}).get("provider_models") or [])

    async def _sync_ingestion_provider_snapshots(
        self, user_id: str, registry: list[dict[str, Any]]
    ) -> None:
        """Refresh secret-bearing corpus snapshots after a registry edit.

        Workers keep consuming the proven ModelProfileRef contract. Stable IDs
        make credential rotation and endpoint changes propagate without asking
        users to edit every corpus or exposing keys in Corpus Manager.
        """
        if self._db is None:
            return
        by_id = {
            str(entry.get("profile_id")): entry
            for entry in registry
            if isinstance(entry, dict) and entry.get("profile_id")
        }
        if not by_id:
            return
        cursor = self._db["corpora"].find(
            {"user_id": user_id},
            {"_id": 1, "default_ingestion_config": 1},
        )
        async for corpus in cursor:
            config = copy.deepcopy(corpus.get("default_ingestion_config") or {})
            changed = False
            for field in ("summary_models", "extraction_models", "embedding_models"):
                pool = config.get(field) or []
                for index, current in enumerate(pool):
                    if not isinstance(current, dict):
                        continue
                    saved = by_id.get(str(current.get("profile_id") or ""))
                    if not saved:
                        continue
                    replacement = copy.deepcopy(saved)
                    replacement["max_concurrent"] = current.get(
                        "max_concurrent", replacement.get("max_concurrent", 1)
                    )
                    pool[index] = replacement
                    changed = True
            if changed:
                await self._db["corpora"].update_one(
                    {"_id": corpus["_id"]},
                    {"$set": {"default_ingestion_config": config, "updated_at": datetime.utcnow()}},
                )

    async def get_runtime_ingestion_settings(
        self,
        user_id: str | None = None,
    ) -> GlobalIngestionSettings:
        """Return ingestion settings with plaintext keys for backend-only use.

        When ``user_id`` is omitted the worker reads the first persisted
        settings document that contains an ingestion section, which matches the
        existing single-admin pattern used by system Modal/extraction settings.
        """
        defaults = self._defaults().ingestion
        if self._db is None:
            return defaults

        try:
            query = {"user_id": user_id} if user_id else {"ingestion": {"$exists": True}}
            doc = await self._db["settings"].find_one(query)
            raw = copy.deepcopy((doc or {}).get("ingestion") or {})
            if not raw:
                return defaults
            self._decrypt_ingestion_keys_in_place(raw)
            return GlobalIngestionSettings(**raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_runtime_ingestion_settings fell back to defaults: %s", exc)
            return defaults

    async def get_system_runpod_flash(
        self,
        user_id: str | None = None,
    ) -> tuple[RunpodFlashExtractionSettings, str | None]:
        """Return safe Flash config plus the decrypted backend-only API key."""

        config = RunpodFlashExtractionSettings()
        api_key: str | None = None
        if self._db is None:
            return config, api_key
        try:
            query: dict[str, Any] = (
                {"user_id": user_id}
                if user_id
                else {
                    "$or": [
                        {"ingestion.runpod_flash": {"$exists": True}},
                        {"api_keys.runpod": {"$exists": True}},
                    ]
                }
            )
            doc = await self._db["settings"].find_one(query)
            raw = ((doc or {}).get("ingestion") or {}).get("runpod_flash") or {}
            if raw:
                config = RunpodFlashExtractionSettings(**raw)
            ciphertext = ((doc or {}).get("api_keys") or {}).get("runpod")
            if ciphertext:
                from services.secrets import decrypt

                api_key = decrypt(ciphertext)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_system_runpod_flash fell back to defaults: %s", exc)
        return config, api_key

    async def update_ingestion_settings(
        self,
        user_id: str,
        section_data: dict[str, Any],
    ) -> GlobalIngestionSettings:
        """Persist global ingestion defaults with encrypted summary API keys."""
        incoming = GlobalIngestionSettings(**section_data)
        doc = (
            await self._db["settings"].find_one({"user_id": user_id})
            if self._db is not None
            else None
        )
        existing = copy.deepcopy((doc or {}).get("ingestion") or {})
        to_write = incoming.model_dump()
        self._encrypt_ingestion_keys_in_place(to_write, existing)
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": {"ingestion": to_write}},
            upsert=True,
        )
        await self._sync_ingestion_provider_snapshots(
            user_id, to_write.get("provider_models") or []
        )
        masked = copy.deepcopy(to_write)
        self._mask_ingestion_keys_in_place(masked)
        return GlobalIngestionSettings(**masked)

    async def get_settings(self, user_id: str) -> GlobalSettings:
        """
        Get global settings for a user.

        1. Try loading from MongoDB
        2. If no document exists, seed from config.py defaults
        3. Always overlay infrastructure from config.py (env-sourced, never stale)

        Phase 24 perf: 60s TTL cache. Each chat turn calls this twice
        (Custom profile resolver + Phase F resolver) — caching saves ~30-60ms
        per query. Cache invalidation happens on update_*.
        """
        if self._db is None:
            return self._defaults()

        # Phase 24 — cache hit
        import time as _time

        now = _time.time()
        cached = self._settings_cache.get(user_id)
        if cached and (now - cached[0]) < self._CACHE_TTL_SECONDS:
            return cached[1]

        doc = await self._db["settings"].find_one({"user_id": user_id})
        if not doc:
            # First-time bootstrap: seed from config.py defaults
            defaults = self._defaults()
            await self._db["settings"].insert_one(
                {
                    "user_id": user_id,
                    "chat": defaults.chat.model_dump(),
                    "retrieval": defaults.retrieval.model_dump(),
                    "ingestion": defaults.ingestion.model_dump(),
                }
            )
            logger.info("Settings bootstrapped for user %s from config.py", user_id)
            return defaults

        # Sprint 3 — run the legacy-stores migration exactly once per user.
        # Check the flag BEFORE popping metadata so we know whether to run.
        migrated_flag = doc.get("models_migrated", False)
        if not migrated_flag:
            try:
                await self.migrate_legacy_model_stores(user_id)
                # Re-fetch so the new settings.models subdoc is included.
                doc = await self._db["settings"].find_one({"user_id": user_id}) or doc
            except Exception as exc:
                logger.error(
                    "Legacy model-stores migration failed for user %s: %s — "
                    "continuing with empty ModelsConfig",
                    user_id,
                    exc,
                )

        # Build from stored doc — only chat, retrieval, modal, models are persisted
        doc.pop("_id", None)
        doc.pop("user_id", None)
        doc.pop("models_migrated", None)

        chat = ChatLLMSettings(**doc.get("chat", {}))
        retrieval = RetrievalSettings(**doc.get("retrieval", {}))
        modal_cfg = ModalDeploySettings(**doc.get("modal", {}))
        models_raw = doc.get("models", {}) or {}
        models_raw = await self._ensure_default_chat_pool_entry(
            user_id=user_id,
            chat=chat,
            models_raw=models_raw,
        )
        # Mask api_key ciphertext on the out-path — the frontend NEVER sees
        # Fernet tokens. "[set]" sentinel signals "key present" to the UI.
        for entry in models_raw.get("query_model_pool", []) or []:
            if isinstance(entry, dict) and entry.get("api_key_ciphertext"):
                entry["api_key_ciphertext"] = _MASK_SENTINEL
        models_cfg = ModelsConfig(**models_raw)

        # Infrastructure is ALWAYS from config.py (env vars), never from MongoDB
        infrastructure = self._infrastructure_defaults()

        extraction_raw = doc.get("extraction")
        extraction_cfg = (
            ExtractionSettings(**extraction_raw)
            if extraction_raw
            else self._extraction_defaults_from_env()
        )

        ingestion_raw = copy.deepcopy(doc.get("ingestion") or {})
        self._mask_ingestion_keys_in_place(ingestion_raw)
        ingestion_cfg = (
            GlobalIngestionSettings(**ingestion_raw)
            if ingestion_raw
            else self._defaults().ingestion
        )

        result = GlobalSettings(
            infrastructure=infrastructure,
            chat=chat,
            retrieval=retrieval,
            modal=modal_cfg,
            models=models_cfg,
            extraction=extraction_cfg,
            ingestion=ingestion_cfg,
        )
        # Phase 24 perf — cache the assembled result
        self._settings_cache[user_id] = (now, result)
        return result

    async def _ensure_default_chat_pool_entry(
        self,
        *,
        user_id: str,
        chat: ChatLLMSettings,
        models_raw: dict[str, Any],
    ) -> dict[str, Any]:
        """Backfill a visible chat entry when the unified pool is empty.

        Older installs can still answer through `chat.default_chat_model` or
        DEFAULT_COMPLETION_MODEL while Settings -> Models shows no entries.
        This creates one editable pool row so frontend selection and backend
        resolution share the same source of truth again.
        """
        if "query_model_pool" in models_raw:
            # The pool exists, even if it is intentionally empty. Do not
            # resurrect the default chat model after a user deletes the last
            # pool entry; that makes Settings feel non-responsive and can
            # route graph synthesis back to a provider the user removed.
            pool = list(models_raw.get("query_model_pool") or [])
            if not pool:
                return models_raw
        else:
            pool = []
        if pool:
            return models_raw

        model = (chat.default_chat_model or self._config.DEFAULT_COMPLETION_MODEL or "").strip()
        if not model:
            return models_raw

        provider = _provider_from_model_id(model)
        entry = QueryModelPoolEntry(
            entry_id=_DEFAULT_CHAT_ENTRY_ID,
            label=f"Chat Default: {_model_label(model)}",
            provider=provider,
            base_url=None,
            api_key_ciphertext=None,
            model_name=model,
            source="ollama" if provider == "ollama" else "cloud",
            enabled=True,
        ).model_dump()

        next_models = {
            "query_model_pool": [entry],
            "hyde": dict(models_raw.get("hyde") or {}),
            "agentic": dict(models_raw.get("agentic") or {}),
            "reasoning": dict(models_raw.get("reasoning") or {}),
            "utility": dict(models_raw.get("utility") or {}),
        }
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": {"models": next_models, "models_migrated": True}},
            upsert=True,
        )
        logger.info(
            "models default chat entry backfilled user=%s model=%s",
            user_id,
            model,
        )
        return next_models

    async def update_system_modal(
        self,
        user_id: str,
        *,
        enabled: bool | None = None,
        embedder_url: str | None = None,
        workspace: str | None = None,
    ) -> None:
        """Phase 22 — persist Modal runtime wiring after a programmatic deploy.

        Uses dotted-path `$set` so untouched Modal fields (gpu_tier,
        max_containers, etc.) survive the write. `None` means 'leave
        unchanged' — callers should pass only the fields they want to
        rewrite. upsert=True because a fresh settings doc may not exist yet.
        """
        if self._db is None:
            return
        patch: dict[str, Any] = {}
        if enabled is not None:
            patch["modal.enabled"] = bool(enabled)
        if embedder_url is not None:
            patch["modal.embedder_url"] = embedder_url
        if workspace is not None:
            patch["modal.workspace"] = workspace
        if not patch:
            return
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": patch},
            upsert=True,
        )

    async def update_modal_workspace(self, user_id: str, workspace: str) -> None:
        """Persist the workspace name captured by `modal token info` into
        settings.modal.workspace. UI-only field — used to render the live URL
        preview (<workspace>--<app_name>-embed.modal.run)."""
        if self._db is None or not workspace:
            return
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": {"modal.workspace": workspace}},
            upsert=True,
        )

    # ── Sprint 3 — unified query_model_pool + models subdoc ───────────────

    async def get_models_raw(self, user_id: str) -> dict:
        """Return the stored models subdoc WITH ciphertext intact.
        Used by the query_model_resolver at chat-time to decrypt per-entry
        api_keys before injecting into LiteLLM. Never returned to the API
        layer — `get_settings()` masks ciphertext first."""
        if self._db is None:
            return {}
        doc = await self._db["settings"].find_one(
            {"user_id": user_id}, projection={"models": 1}
        )
        return (doc or {}).get("models") or {}

    async def update_models(
        self, user_id: str, models_patch: dict[str, Any]
    ) -> ModelsConfig:
        """Validate + persist the `models` section.

        Semantics:
          - `query_model_pool[]` entries have their `api_key_ciphertext`
            resolved: plaintext → encrypt(); "[set]" / None / "" → preserve
            the existing ciphertext at that entry_id (matched by id).
          - role `pool_entry_id` fields are validated against the POST-update
            pool — if the referenced entry_id does not exist in the new list,
            the write is rejected with ValueError (caller maps to HTTP 400).
          - Entire models subdoc is replaced atomically; callers send the
            full desired pool shape.
        """
        from services.secrets import decrypt, encrypt

        if self._db is None:
            return ModelsConfig()

        # Validate via Pydantic first (catches bad provider literals, etc.)
        incoming = ModelsConfig(**(models_patch or {}))

        # Fetch existing unmasked pool so we can preserve ciphertext on "[set]"
        existing_raw = await self.get_models_raw(user_id)
        existing_pool_by_id: dict[str, dict] = {}
        for e in (existing_raw.get("query_model_pool") or []):
            if isinstance(e, dict) and e.get("entry_id"):
                existing_pool_by_id[e["entry_id"]] = e

        resolved_pool: list[dict] = []
        for entry in incoming.query_model_pool:
            entry_dict = entry.model_dump()
            new_val = entry_dict.get("api_key_ciphertext")
            existing_ct = (
                existing_pool_by_id.get(entry.entry_id, {}).get(
                    "api_key_ciphertext"
                )
            )
            # Preserve existing ciphertext when caller signals "no change"
            if not new_val or new_val == _MASK_SENTINEL:
                entry_dict["api_key_ciphertext"] = existing_ct
            elif (
                isinstance(new_val, str)
                and new_val.startswith("gAAAAA")
                and decrypt(new_val) is not None
            ):
                # Already-encrypted — leave as-is (idempotent round-trip).
                pass
            else:
                # Fresh plaintext → encrypt before persistence.
                entry_dict["api_key_ciphertext"] = encrypt(new_val)
            resolved_pool.append(entry_dict)

        # Validate role references against the post-update pool.
        valid_ids = {e["entry_id"] for e in resolved_pool}
        for section_name, section_val in (
            ("hyde", incoming.hyde),
            ("agentic", incoming.agentic),
            ("reasoning", incoming.reasoning),
            ("utility", incoming.utility),
            ("graph_query", incoming.graph_query),
        ):
            pid = section_val.pool_entry_id
            if pid and pid not in valid_ids:
                raise ValueError(
                    f"{section_name}.pool_entry_id={pid!r} does not reference "
                    "any entry in query_model_pool."
                )

        doc_to_write = {
            "query_model_pool": resolved_pool,
            "hyde": incoming.hyde.model_dump(),
            "agentic": incoming.agentic.model_dump(),
            # Phase 24 — Reasoning Cascade target.
            "reasoning": incoming.reasoning.model_dump(),
            "utility": incoming.utility.model_dump(),
            "graph_query": incoming.graph_query.model_dump(),
        }
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": {"models": doc_to_write, "models_migrated": True}},
            upsert=True,
        )
        # Phase 24 perf — invalidate cache so next get_settings sees the write.
        self._invalidate_cache(user_id)
        logger.info(
            "models updated user=%s pool=%d hyde=%s agentic=%s reasoning=%s utility=%s graph_query=%s",
            user_id,
            len(resolved_pool),
            incoming.hyde.pool_entry_id or "-",
            incoming.agentic.pool_entry_id or "-",
            incoming.reasoning.pool_entry_id or "-",
            incoming.utility.pool_entry_id or "-",
            incoming.graph_query.pool_entry_id or "-",
        )

        # Return the masked view (same shape as GET) for router response
        stored_raw = await self.get_models_raw(user_id)
        for entry in (stored_raw.get("query_model_pool") or []):
            if isinstance(entry, dict) and entry.get("api_key_ciphertext"):
                entry["api_key_ciphertext"] = _MASK_SENTINEL
        return ModelsConfig(**stored_raw)

    async def add_ollama_entries(
        self, user_id: str, model_names: list[str]
    ) -> ModelsConfig:
        """Bulk-create ollama pool entries. No api_key, no base_url
        (worker uses settings.OLLAMA_URL at call time). Idempotent by
        (provider=ollama, model_name) — duplicates are silently skipped."""
        current = await self.get_models_raw(user_id)
        pool = list(current.get("query_model_pool") or [])
        existing_ollama_models = {
            e.get("model_name")
            for e in pool
            if isinstance(e, dict) and e.get("provider") == "ollama"
        }
        now = datetime.utcnow().isoformat()
        added = 0
        for name in model_names:
            name = (name or "").strip()
            if not name or name in existing_ollama_models:
                continue
            pool.append(
                QueryModelPoolEntry(
                    entry_id=str(uuid4()),
                    label=name,
                    provider="ollama",
                    base_url=None,
                    api_key_ciphertext=None,
                    model_name=name,
                    source="ollama",
                    enabled=True,
                    created_at=now,
                ).model_dump()
            )
            added += 1
        if added == 0:
            # Nothing to do — return current masked view
            for entry in pool:
                if isinstance(entry, dict) and entry.get("api_key_ciphertext"):
                    entry["api_key_ciphertext"] = _MASK_SENTINEL
            return ModelsConfig(**{
                "query_model_pool": pool,
                "hyde": current.get("hyde") or {},
                "agentic": current.get("agentic") or {},
                "reasoning": current.get("reasoning") or {},
                "utility": current.get("utility") or {},
                "graph_query": current.get("graph_query") or {},
            })
        # Write back (no re-encryption; we only appended ollama entries)
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": {
                "models.query_model_pool": pool,
                "models_migrated": True,
            }},
            upsert=True,
        )
        logger.info("ollama bulk-add user=%s added=%d skipped=%d",
                    user_id, added, len(model_names) - added)
        return await self._masked_models(user_id)

    async def delete_pool_entry(self, user_id: str, entry_id: str) -> ModelsConfig:
        """Remove one pool entry. If removal orphans a role-specific
        pool_entry_id, that field is nulled silently (the resolver will
        fall through to the legacy chain)."""
        current = await self.get_models_raw(user_id)
        pool = [
            e for e in (current.get("query_model_pool") or [])
            if not (isinstance(e, dict) and e.get("entry_id") == entry_id)
        ]
        hyde = dict(current.get("hyde") or {})
        agentic = dict(current.get("agentic") or {})
        reasoning = dict(current.get("reasoning") or {})
        utility = dict(current.get("utility") or {})
        graph_query = dict(current.get("graph_query") or {})
        if hyde.get("pool_entry_id") == entry_id:
            hyde["pool_entry_id"] = None
        if agentic.get("pool_entry_id") == entry_id:
            agentic["pool_entry_id"] = None
        if reasoning.get("pool_entry_id") == entry_id:
            reasoning["pool_entry_id"] = None
        if utility.get("pool_entry_id") == entry_id:
            utility["pool_entry_id"] = None
        if graph_query.get("pool_entry_id") == entry_id:
            graph_query["pool_entry_id"] = None
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": {
                "models.query_model_pool": pool,
                "models.hyde": hyde,
                "models.agentic": agentic,
                "models.reasoning": reasoning,
                "models.utility": utility,
                "models.graph_query": graph_query,
            }},
            upsert=True,
        )
        return await self._masked_models(user_id)

    async def _masked_models(self, user_id: str) -> ModelsConfig:
        """Reload the stored models subdoc and return with masked keys."""
        raw = await self.get_models_raw(user_id)
        for entry in (raw.get("query_model_pool") or []):
            if isinstance(entry, dict) and entry.get("api_key_ciphertext"):
                entry["api_key_ciphertext"] = _MASK_SENTINEL
        return ModelsConfig(**raw)

    # ── Sprint 3 — legacy-stores migration ────────────────────────────────

    async def migrate_legacy_model_stores(self, user_id: str) -> dict:
        """Collapse Phase 19.3 `model_profiles` + Phase E `model_pool`
        collections + Phase F `user_query_preferences` into
        settings.models.query_model_pool[] + role defaults.

        Idempotent. Keyed by `settings.models_migrated` flag — second call
        is a no-op. Legacy collections are NEVER deleted (guardrail).

        Key-preservation: old `profile_id` / old pool `entry_id` are carried
        through verbatim as the new `entry_id`. Any in-flight chat that
        says `profile:<pid>` or `pool:<eid>` keeps resolving after this.

        Returns an audit dict: {migrated, pool_entries, orphans}.
        """
        if self._db is None:
            return {"migrated": False, "pool_entries": 0, "orphans": []}

        existing = await self._db["settings"].find_one({"user_id": user_id})
        if existing and existing.get("models_migrated"):
            return {
                "migrated": True,
                "pool_entries": len(
                    ((existing.get("models") or {}).get("query_model_pool") or [])
                ),
                "orphans": [],
                "note": "already_migrated",
            }

        new_pool: list[dict] = []
        seen_ids: set[str] = set()
        now = datetime.utcnow().isoformat()

        # 1. Phase E `model_pool` → new pool (provider / api_key / all fields map)
        async for row in self._db["model_pool"].find({"user_id": user_id}):
            eid = row.get("entry_id") or str(uuid4())
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            # Migration never deals with raw plaintext — leave ciphertext verbatim.
            ct = row.get("api_key") or None
            new_pool.append({
                "entry_id": eid,
                "label": row.get("label") or row.get("model_name") or "pool-entry",
                "provider": row.get("provider") or "custom",
                "base_url": row.get("base_url") or None,
                "api_key_ciphertext": ct,
                "model_name": row.get("model_name") or "",
                "source": "ollama" if row.get("provider") == "ollama" else "cloud",
                "enabled": bool(row.get("enabled", True)),
                "created_at": row.get("created_at") or now,
            })

        # 2. Phase 19.3 `model_profiles` → new pool with provider="custom"
        async for row in self._db["model_profiles"].find({"user_id": user_id}):
            pid = row.get("profile_id") or str(uuid4())
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            ct = row.get("api_key") or None
            new_pool.append({
                "entry_id": pid,
                "label": row.get("label") or "profile",
                "provider": "custom",
                "base_url": row.get("base_url") or None,
                "api_key_ciphertext": ct,
                "model_name": row.get("model_name") or "",
                "source": "cloud",
                "enabled": True,
                "created_at": row.get("created_at") or now,
            })

        # 3. Phase F `user_query_preferences` → hyde/agentic pool_entry_ids
        valid_ids = {e["entry_id"] for e in new_pool}
        orphans: list[str] = []
        prefs = await self._db["user_query_preferences"].find_one({"user_id": user_id})
        hyde_pid = None
        agentic_pid = None
        if prefs:
            old_hyde = prefs.get("hyde_pool_id")
            old_agentic = prefs.get("agentic_pool_id")
            if old_hyde and old_hyde in valid_ids:
                hyde_pid = old_hyde
            elif old_hyde:
                orphans.append(f"hyde={old_hyde}")
                logger.warning(
                    "migrate: orphan hyde_pool_id=%s for user=%s — nulled",
                    old_hyde, user_id,
                )
            if old_agentic and old_agentic in valid_ids:
                agentic_pid = old_agentic
            elif old_agentic:
                orphans.append(f"agentic={old_agentic}")
                logger.warning(
                    "migrate: orphan agentic_pool_id=%s for user=%s — nulled",
                    old_agentic, user_id,
                )

        doc_to_write = {
            "query_model_pool": new_pool,
            "hyde": {"default_enabled": False, "pool_entry_id": hyde_pid},
            "agentic": {"default_enabled": False, "pool_entry_id": agentic_pid},
            # Phase 24 — Reasoning Cascade target. Migration leaves this empty;
            # user picks an entry in Settings → Models when they want it.
            "reasoning": {"default_enabled": False, "pool_entry_id": None},
            "utility": {"default_enabled": False, "pool_entry_id": None},
            "graph_query": {"pool_entry_id": None},
        }
        await self._db["settings"].update_one(
            {"user_id": user_id},
            {"$set": {"models": doc_to_write, "models_migrated": True}},
            upsert=True,
        )
        logger.info(
            "migrated user_id=%s pool_entries=%d orphans=%s",
            user_id, len(new_pool), orphans or [],
        )
        return {
            "migrated": True,
            "pool_entries": len(new_pool),
            "orphans": orphans,
        }

    async def update_settings(
        self, user_id: str, patch: dict[str, Any]
    ) -> GlobalSettings:
        """
        Partial update of global settings.

        Chat, retrieval, modal, models, extraction, and ingestion defaults are mutable.
        'infrastructure' is always read from config.py (env vars).
        """
        if self._db is None:
            return self._defaults()

        # Filter to mutable sections only
        safe_patch = {k: v for k, v in patch.items() if k in _MUTABLE_SECTIONS}
        if not safe_patch:
            logger.warning(
                "No mutable sections in settings update for user %s", user_id
            )
            return await self.get_settings(user_id)

        # Validate each section before writing
        for section_name, section_data in safe_patch.items():
            if section_name == "chat":
                ChatLLMSettings(**section_data)
            elif section_name == "retrieval":
                RetrievalSettings(**section_data)
            elif section_name == "modal":
                ModalDeploySettings(**section_data)
            elif section_name == "extraction":
                ExtractionSettings(**section_data)
            elif section_name == "ingestion":
                await self.update_ingestion_settings(user_id, section_data)
            elif section_name == "models":
                # Route through update_models so ciphertext handling + ref
                # validation run. Remove from safe_patch so the blind $set
                # below doesn't clobber the resolved ciphertext.
                await self.update_models(user_id, section_data)
        safe_patch.pop("models", None)
        safe_patch.pop("ingestion", None)

        if safe_patch:
            await self._db["settings"].update_one(
                {"user_id": user_id},
                {"$set": safe_patch},
                upsert=True,
            )
        # Phase 24 perf — invalidate cache so next get_settings sees the write.
        self._invalidate_cache(user_id)
        logger.info(
            "Settings updated for user %s: %s", user_id, list(safe_patch.keys())
        )
        return await self.get_settings(user_id)

    # ── Phase 19.3 — system-level Modal runtime (used by services/embedder.py)

    async def get_system_modal(self) -> ModalDeploySettings:
        """
        Return the Modal runtime config for the worker. Reads the first settings
        doc in Mongo (single-admin deployments) and falls back to .env defaults
        when nothing is persisted yet. Single source of truth for
        `enabled` / `embedder_url` so the embedder dispatcher sees UI changes
        without a backend restart.
        """
        if self._db is None:
            return self._modal_defaults_from_env()
        doc = await self._db["settings"].find_one(
            {"modal": {"$exists": True}},
            projection={"modal": 1},
        )
        if not doc or not doc.get("modal"):
            return self._modal_defaults_from_env()
        try:
            return ModalDeploySettings(**doc["modal"])
        except Exception as exc:  # defensive — corrupted doc
            logger.warning("Modal settings parse failed, falling back to env: %s", exc)
            return self._modal_defaults_from_env()

    def _modal_defaults_from_env(self) -> ModalDeploySettings:
        """Seed a ModalDeploySettings from config.py env vars."""
        c = self._config
        return ModalDeploySettings(
            enabled=bool(c.MODAL_ENABLED),
            embedder_url=c.MODAL_EMBEDDER_URL or "",
        )

    # ── Phase 19.2 — API key CRUD (Fernet-encrypted at rest) ────────────────

    async def get_api_keys_masked(self, user_id: str) -> dict[str, str]:
        """
        Return masked api_keys for the user. Missing providers map to "[not set]".
        Plaintext never leaves the backend.
        """
        from services.secrets import KNOWN_PROVIDERS, decrypt, mask

        result: dict[str, str] = {}
        stored: dict[str, Any] = {}
        if self._db is not None:
            doc = await self._db["settings"].find_one({"user_id": user_id})
            stored = (doc or {}).get("api_keys") or {}

        for provider in sorted(KNOWN_PROVIDERS):
            ciphertext = stored.get(provider)
            if not ciphertext:
                result[provider] = "[not set]"
                continue
            plaintext = decrypt(ciphertext)
            result[provider] = mask(plaintext)
        return result

    async def update_api_keys(
        self, user_id: str, plaintext_keys: dict[str, str]
    ) -> dict[str, str]:
        """
        Encrypt & store plaintext keys. Empty string value deletes the key for
        that provider. Unknown providers are rejected. Returns the fresh masked
        view.
        """
        from services.secrets import encrypt, validate_provider

        if self._db is None:
            logger.warning("update_api_keys called but DB not attached")
            return await self.get_api_keys_masked(user_id)

        # Validate provider names up front
        for provider in plaintext_keys.keys():
            validate_provider(provider)

        # Build $set / $unset atomic update
        set_ops: dict[str, str] = {}
        unset_ops: dict[str, str] = {}
        for provider, plaintext in plaintext_keys.items():
            if plaintext and plaintext.strip():
                set_ops[f"api_keys.{provider}"] = encrypt(plaintext.strip())
            else:
                # Empty value → delete the stored key
                unset_ops[f"api_keys.{provider}"] = ""

        update_doc: dict[str, Any] = {}
        if set_ops:
            update_doc["$set"] = set_ops
        if unset_ops:
            update_doc["$unset"] = unset_ops
        if not update_doc:
            return await self.get_api_keys_masked(user_id)

        await self._db["settings"].update_one(
            {"user_id": user_id},
            update_doc,
            upsert=True,
        )
        logger.info(
            "API keys updated for user %s: set=%s unset=%s",
            user_id,
            list(set_ops.keys()),
            list(unset_ops.keys()),
        )
        return await self.get_api_keys_masked(user_id)

    async def get_plaintext_keys_for_llm(self, user_id: str) -> dict[str, str]:
        """
        Decrypt every stored key for a user. Returns {provider: plaintext}.
        Called by the LLM client wrapper at request time to inject the right
        key. Never logged. Never returned to the frontend.
        """
        from services.secrets import decrypt_all

        if self._db is None:
            return {}
        doc = await self._db["settings"].find_one({"user_id": user_id})
        if not doc:
            return {}
        return decrypt_all(doc.get("api_keys"))

    async def get_plaintext_key_any_user(self, provider: str) -> str | None:
        """
        System-wide fallback: return the first non-empty decrypted key for this
        provider across all users. Used when an LLM call isn't scoped to a
        user (e.g. GHOST A summarization during ingestion). Falls back to env
        var if no user has set one.
        """
        from services.secrets import decrypt, validate_provider

        try:
            validate_provider(provider)
        except ValueError:
            return None
        if self._db is None:
            return None
        cursor = self._db["settings"].find(
            {f"api_keys.{provider}": {"$exists": True, "$ne": ""}},
            {f"api_keys.{provider}": 1, "_id": 0},
        ).limit(1)
        async for doc in cursor:
            ciphertext = (doc.get("api_keys") or {}).get(provider)
            if ciphertext:
                plaintext = decrypt(ciphertext)
                if plaintext:
                    return plaintext
        return None

    async def test_infrastructure(self) -> dict[str, Any]:
        """
        Test all service connectivity via health_service.

        Returns a dict of service_name → {status, latency_ms, error?}
        """
        from services.health_service import health_service

        response = await health_service.check_all_services()
        return {
            name: {
                "status": svc.status,
                "latency_ms": svc.latency_ms,
                "error": svc.error,
            }
            for name, svc in response.services.items()
        }


# Global instance — same pattern as other services (conversation_service, auth_service, etc.)
settings_service = SettingsService()
