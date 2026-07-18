# backend/main.py
# FastAPI application entry point
# ALL routers registered here. Touch only to add routers.
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000 --reload

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from config import get_settings
from db.indexes import create_all_indexes
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.auth import limiter as auth_limiter
from routers.auth import router as auth_router
from routers.chat import router as chat_router
from routers.conversations import router as conversations_router
from routers.discourse import router as discourse_router
from routers.health import router as health_router
from routers.graph import discovery_router as graph_discovery_router
from routers.graph import router as graph_router
from routers.ingestion import router as ingestion_router
from routers.model_pool import router as model_pool_router
from routers.model_profiles import router as model_profiles_router
from routers.models import router as models_router
from routers.query_prefs import router as query_prefs_router
from routers.modal_ops import router as modal_ops_router
from routers.portability import router as portability_router
from routers.settings import router as settings_router
from routers.tools import router as tools_router
from routers.skills import router as skills_router
from routers.mcp_info import router as mcp_info_router
from routers.cli_providers import router as cli_providers_router
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from services.auth import auth_service
from services.conversation import conversation_service
from services.ingestion import batches as ingest_batches
from services.ingestion_service import ingestion_service
from services.llm import llm_service
from services.model_pool import model_pool_service
from services.model_profiles import model_profiles_service
from services.query_prefs import query_prefs_service
from services.settings import settings_service

settings = get_settings()

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _safe_uri(uri: str) -> str:
    """
    Phase 17 W1.2 — return a URI with userinfo (user:password@) stripped.
    Keeps scheme + host + path + query so ops logs still identify the target.
    """
    if not uri:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(uri)
        if parsed.username or parsed.password:
            # Rebuild netloc without userinfo; preserve host + port
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunparse(
                (parsed.scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment)
            )
        return uri
    except Exception:
        # Never let log formatting crash startup — fall back to a safe placeholder
        return "<uri-unparseable>"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.
    Handles startup and shutdown tasks.

    Startup:
        - Connect to MongoDB
        - Log application start

    Shutdown:
        - Close MongoDB connection
        - Close LLM service HTTP client
    """
    # Startup
    logger.info(f"Starting Polymath RAG API (env: {settings.APP_ENV})")
    # Phase 17 W1.2 — strip userinfo from MongoDB URI before logging so
    # the password never lands in container logs.
    logger.info(f"MongoDB URI: {_safe_uri(settings.MONGODB_URI)}")
    logger.info(f"LiteLLM URL: {settings.LITELLM_URL}")
    logger.info(f"Ollama URL: {settings.OLLAMA_URL}")

    try:
        await conversation_service.connect()
        logger.info("MongoDB connected successfully")
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        raise

    # Phase 24 perf — pre-warm tiktoken so the first chat turn doesn't pay
    # the encoder's lazy-init cost on the assistant-message save (was 1-4s
    # cold). Non-fatal if tiktoken isn't available.
    try:
        from utils.tokens import prewarm as _prewarm_tokens

        _prewarm_tokens()
    except Exception as exc:
        logger.warning("tiktoken prewarm skipped: %s", exc)

    try:
        await create_all_indexes(conversation_service._db)
        logger.info("MongoDB indexes ensured")
    except Exception as e:
        logger.error(f"Failed to create indexes: {e}")
        raise

    # Auth service: connect to same DB, bootstrap default admin if zero users
    try:
        await auth_service.connect(conversation_service._db)
        await auth_service.bootstrap()
        logger.info("Auth service initialized")
    except Exception as e:
        logger.error(f"Failed to initialize auth service: {e}")
        raise

    # Ingestion service: Qdrant + optional Neo4j
    try:
        await ingestion_service.connect(conversation_service._db)
        logger.info("Ingestion service initialized")
    except Exception as e:
        logger.error(f"Failed to initialize ingestion service: {e}")
        raise

    # Embedder runtime-wiring self-check (NON-FATAL, ~8s budget). The embedder +
    # reranker are reached via env that comes from the compose OVERRIDE
    # (docker-compose.apple-mlx.yml / .override.yml). If the backend is recreated
    # WITHOUT the override (e.g. `docker compose -f docker-compose.yml up backend`
    # alone), EMBEDDER_URL silently reverts to the dead default `embedder:80` and
    # vector/hybrid/graph retrieval returns nothing while health stays green.
    # Probe a real embed at boot so a misw ired deploy SCREAMS in the logs with
    # the fix instead of failing silently. See scripts/verify_backend_runtime.sh
    # + CLAUDE.md. Never fatal — a transient embedder hiccup must not block boot.
    try:
        import asyncio as _asyncio
        from services.embedder import embed_query as _embed_query

        _dim = await _asyncio.wait_for(
            _embed_query("polymath embedder self-check"), timeout=8.0
        )
        if _dim:
            logger.info(
                "Embedder self-check OK (EMBEDDER_URL=%s, dim=%d)",
                settings.EMBEDDER_URL,
                len(_dim),
            )
        else:
            raise RuntimeError("embedder returned an empty vector")
    except Exception as exc:  # noqa: BLE001 — diagnostics only, never fatal
        logger.critical(
            "EMBEDDER SELF-CHECK FAILED (EMBEDDER_URL=%s): %s. Vector/Hybrid/Graph "
            "retrieval will return NO results while health stays green. The backend "
            "was likely recreated WITHOUT the compose override — redeploy with "
            "`docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml "
            "up -d --build backend` (or `bash scripts/setup_apple_mlx.sh`), NEVER "
            "`-f docker-compose.yml` alone, then run "
            "scripts/verify_backend_runtime.sh.",
            settings.EMBEDDER_URL,
            exc,
        )

    # Universal-schema migration: patch null/empty schemas (and coerce legacy
    # off/hard strict values) to the baked universal vocabulary. Idempotent.
    # FORCE_UNIVERSAL_SCHEMA=true overwrites every corpus.
    try:
        result = await ingestion_service.migrate_universal_schema(
            force=settings.FORCE_UNIVERSAL_SCHEMA,
        )
        logger.info(
            "Universal schema migration: scanned=%d patched=%d force=%s",
            result["scanned"],
            result["patched"],
            result["force"],
        )
    except Exception as e:
        logger.error(f"Universal schema migration failed: {e}")
        # Non-fatal: existing corpora still serve retrieval. New ingests on
        # un-patched corpora fall back to open extraction until fixed.

    # Bare-model-name migration — rewrite pool entries that stored the model
    # without the LiteLLM provider prefix (e.g. "deepseek-chat" →
    # "deepseek/deepseek-chat"). Idempotent: entries already containing "/"
    # or with an unknown provider_preset are skipped.
    try:
        result = await ingestion_service.migrate_bare_model_names()
        logger.info(
            "Bare model name migration: corpora_patched=%d pool_entries=%d "
            "settings_users=%d model_pool_entries=%d",
            result["corpora_patched"],
            result["pool_entries_patched"],
            result["settings_users_patched"],
            result["model_pool_entries_patched"],
        )
    except Exception as e:
        logger.error(f"Bare model name migration failed: {e}")
        # Non-fatal: new ingests with the fixed UI work, only pre-fix entries
        # remain broken until re-saved by the user.

    # Settings service: attach same DB handle (no network connect)
    settings_service.attach(conversation_service._db)
    logger.info("Settings service attached")

    # Extraction-engine migration (§13 correction) — stamp every corpus with
    # an EXPLICIT engine seeded from the current global Settings value, so the
    # extraction contract is per-corpus and deterministic. Idempotent; corpora
    # already explicit are untouched. Needs settings_service attached above.
    try:
        _ext = await settings_service.get_system_extraction()
        result = await ingestion_service.migrate_extraction_engine(
            str(getattr(_ext, "engine", "local") or "local")
        )
        logger.info(
            "Extraction engine migration: scanned=%d stamped=%d engine=%s",
            result["scanned"],
            result["stamped"],
            result["engine"],
        )
    except Exception as e:
        logger.error(f"Extraction engine migration failed: {e}")
        # Non-fatal: unstamped corpora resolve via 'inherit' -> global engine,
        # identical to pre-migration behavior.

    # Model Profiles service (Phase 19.3): attach same DB handle
    model_profiles_service.attach(conversation_service._db)
    logger.info("Model profiles service attached")

    # Model Pool service (Phase E — unified pool): attach same DB handle
    model_pool_service.attach(conversation_service._db)
    logger.info("Model pool service attached")

    # Query Prefs service (Phase F — per-user role→pool mappings + ollama exclusions)
    query_prefs_service.attach(conversation_service._db)
    logger.info("Query prefs service attached")

    # Phase 24 — strip legacy hardcoded model defaults from existing user
    # settings + conversation records. These values were baked into the
    # schema before Phase 24; they silently overrode user pool selections
    # at chat time and caused 60+ second cascade waits when the model
    # wasn't pulled. After this scrub, empty = "let the resolver decide."
    try:
        db = conversation_service._db
        if db is not None:
            LEGACY_VALUES = ["ollama/llama3.2:3b", "ollama/qwen3:1.7b"]
            settings_filter = {"$or": [
                {"chat.default_chat_model": {"$in": LEGACY_VALUES}},
                {"chat.agentic_model": {"$in": LEGACY_VALUES}},
                {"chat.hyde_model": {"$in": LEGACY_VALUES}},
            ]}
            r = await db["settings"].update_many(
                settings_filter,
                [
                    {"$set": {
                        "chat.default_chat_model": {
                            "$cond": [
                                {"$in": ["$chat.default_chat_model", LEGACY_VALUES]},
                                "",
                                "$chat.default_chat_model",
                            ]
                        },
                        "chat.agentic_model": {
                            "$cond": [
                                {"$in": ["$chat.agentic_model", LEGACY_VALUES]},
                                "",
                                "$chat.agentic_model",
                            ]
                        },
                        "chat.hyde_model": {
                            "$cond": [
                                {"$in": ["$chat.hyde_model", LEGACY_VALUES]},
                                "",
                                "$chat.hyde_model",
                            ]
                        },
                    }},
                ],
            )
            conv_r = await db["conversations"].update_many(
                {"model_config_conversation.model": {"$in": LEGACY_VALUES}},
                {"$set": {"model_config_conversation.model": ""}},
            )
            logger.info(
                "Legacy model scrub: settings_users=%d conversations=%d",
                r.modified_count,
                conv_r.modified_count,
            )
    except Exception as exc:
        logger.warning("Legacy model scrub failed (non-fatal): %s", exc)

    # PR 3 multi-corpus rollout — migrate graph_sessions documents from
    # the single-corpus shape ({corpus_id: "x", ...}) to the dual-field
    # shape ({corpus_id: "x", corpus_ids: ["x"], ...}). Idempotent: docs
    # that already have a non-empty corpus_ids array are skipped via the
    # filter. Pattern matches existing migrate_universal_schema and
    # migrate_bare_model_names migrations above.
    try:
        db = conversation_service._db
        if db is not None:
            r = await db["graph_sessions"].update_many(
                {
                    "corpus_id": {"$exists": True, "$ne": ""},
                    "$or": [
                        {"corpus_ids": {"$exists": False}},
                        {"corpus_ids": []},
                    ],
                },
                [
                    {"$set": {"corpus_ids": ["$corpus_id"]}},
                ],
            )
            if r.modified_count:
                logger.info(
                    "graph_sessions multi-corpus migration: corpus_id → corpus_ids: [corpus_id], "
                    "documents_patched=%d",
                    r.modified_count,
                )
    except Exception as exc:
        logger.warning("graph_sessions multi-corpus migration failed (non-fatal): %s", exc)

    ingest_poll_task: asyncio.Task | None = None
    startup_repair_task: asyncio.Task | None = None
    auto_repair_lock = asyncio.Lock()

    async def _stores_ready() -> bool:
        import httpx as _hx

        try:
            async with _hx.AsyncClient(timeout=2.0) as cli:
                q = await cli.get(f"{settings.QDRANT_URL}/readyz")
                if q.status_code != 200:
                    return False
            if ingestion_service.neo4j_driver is not None:
                await ingestion_service.neo4j_driver.verify_connectivity()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _recover_ingest_batches(reason: str) -> dict:
        result = await ingest_batches.recover_local_batch_runners(
            db=conversation_service._db,
            ingestion_service=ingestion_service,
            reclaim_active_running=(reason == "startup"),
        )
        if result["reclaimed_items"] or result["started_batches"]:
            logger.info(
                "Durable ingest %s recovery: reclaimed_items=%d "
                "candidate_batches=%d started_batches=%d",
                reason,
                result["reclaimed_items"],
                result["candidate_batches"],
                result["started_batches"],
            )
        return result

    async def _run_auto_corpus_repair_tick(reason: str) -> None:
        if auto_repair_lock.locked():
            logger.info("Auto corpus repair %s tick skipped: repair already running", reason)
            return
        try:
            async with auto_repair_lock:
                result = await ingestion_service.run_auto_corpus_repair_tick()
            logger.info(
                "Auto corpus repair %s tick: scanned=%d changed=%d",
                reason,
                int(result.get("scanned") or 0),
                int(result.get("changed") or 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto corpus repair %s tick failed: %s", reason, exc)

    async def _ingest_worker_poll_loop() -> None:
        interval = float(getattr(settings, "INGEST_RUNNER_POLL_SECONDS", 10.0) or 10.0)
        repair_interval = float(
            getattr(settings, "INGEST_AUTO_REPAIR_POLL_SECONDS", 300.0) or 300.0
        )
        purge_reclaim_interval = float(
            getattr(settings, "CORPUS_PURGE_RECLAIM_POLL_SECONDS", 60.0) or 60.0
        )
        last_repair_tick = 0.0
        last_purge_reclaim_tick = 0.0
        while True:
            await asyncio.sleep(interval)
            try:
                await _recover_ingest_batches("poll")
                # P0.6 — reclaim expired/partial corpus-cleanup leases during
                # normal uptime, not only at startup. The reclaim query already
                # honors cleanup_retry_at, so this same tick also auto-retries
                # partial cleanups without an operator-triggered restart.
                now = asyncio.get_running_loop().time()
                if now - last_purge_reclaim_tick >= purge_reclaim_interval:
                    last_purge_reclaim_tick = now
                    await ingestion_service.recover_pending_corpus_purges()
                if bool(getattr(settings, "INGEST_AUTO_REPAIR_ENABLED", True)):
                    if now - last_repair_tick >= repair_interval:
                        last_repair_tick = now
                        await _run_auto_corpus_repair_tick("poll")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Durable ingest poll recovery failed (non-fatal): %s", exc)

    if settings.INGEST_RUNNERS_ENABLED:
        try:
            # Readiness gate (2026-07-05): after a Docker VM restart the backend
            # boots FASTER than Qdrant/Neo4j accept connections. Resuming batches
            # immediately burned 81 docs on retrieval_setup connection-refused in
            # one storm. Wait (bounded) for both stores to answer before starting
            # batch runners — on timeout, resume anyway and let per-item retry
            # handle it (better late than never, never worse than before).
            for _attempt in range(24):  # up to ~120s
                if await _stores_ready():
                    if _attempt:
                        logger.info(
                            "Ingest recovery readiness gate: stores ready after %ds",
                            _attempt * 5,
                        )
                    break
                await asyncio.sleep(5)
            else:
                logger.warning(
                    "Ingest recovery readiness gate: stores not ready after 120s — "
                    "resuming anyway (per-item failures will surface)"
                )

            await _recover_ingest_batches("startup")
            if bool(getattr(settings, "INGEST_AUTO_REPAIR_ENABLED", True)):
                startup_repair_task = asyncio.create_task(
                    _run_auto_corpus_repair_tick("startup")
                )
            ingest_poll_task = asyncio.create_task(_ingest_worker_poll_loop())
            logger.info(
                "Durable ingest runners enabled; polling every %.1fs",
                float(getattr(settings, "INGEST_RUNNER_POLL_SECONDS", 10.0) or 10.0),
            )
        except Exception as exc:
            logger.warning("Durable ingest startup recovery failed (non-fatal): %s", exc)
    else:
        logger.info(
            "Durable ingest runners disabled for this process "
            "(INGEST_RUNNERS_ENABLED=false)"
        )

    # Optional chat-model warmup. The chat model is user-selected at request
    # time, so there is no reliable default to warm — this is opt-in via
    # OLLAMA_WARMUP_MODEL. When set, fire a tiny non-blocking completion so the
    # first real turn does not pay the multi-minute cold-load. keep_alive (see
    # config.OLLAMA_KEEP_ALIVE) then keeps it resident between turns.
    try:
        from config import get_settings as _get_settings

        warmup_model = (getattr(_get_settings(), "OLLAMA_WARMUP_MODEL", "") or "").strip()
        if warmup_model:
            async def _warmup() -> None:
                try:
                    async for _ in llm_service.stream_chat(
                        messages=[{"role": "user", "content": "ok"}],
                        model=warmup_model,
                        overrides=None,
                    ):
                        break  # one chunk proves the model is loaded
                    logger.info("Chat warmup complete: model=%s", warmup_model)
                except Exception as exc:  # never let warmup affect serving
                    logger.warning("Chat warmup failed (non-fatal): %s", exc)

            asyncio.create_task(_warmup())
    except Exception as exc:
        logger.warning("Chat warmup scheduling failed (non-fatal): %s", exc)

    yield

    # Shutdown
    logger.info("Shutting down Polymath RAG API")
    if startup_repair_task is not None:
        startup_repair_task.cancel()
        try:
            await startup_repair_task
        except asyncio.CancelledError:
            pass
    if ingest_poll_task is not None:
        ingest_poll_task.cancel()
        try:
            await ingest_poll_task
        except asyncio.CancelledError:
            pass
    await auth_service.disconnect()
    await ingestion_service.disconnect()
    await conversation_service.disconnect()
    await llm_service.close()
    logger.info("Cleanup complete")


# Initialize FastAPI application
app = FastAPI(
    title="Polymath RAG API",
    description="Hierarchical RAG System - ChatGPT-style interface with Qdrant + Neo4j + MongoDB + LiteLLM + Ollama",
    version="3.3.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Phase 17 W1.3 — slowapi rate limit state + 429 exception handler.
# The limiter instance itself is defined in routers/auth.py so the decorator
# and the app share one registry.
app.state.limiter = auth_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Phase 23 — log Pydantic 422 validation errors with full detail so silent
# "request rejected, no response" issues can be diagnosed. FastAPI's default
# handler returns the details in the response body but never logs server-side.
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@app.exception_handler(RequestValidationError)
async def _log_validation_error(request: Request, exc: RequestValidationError):
    # Pydantic v2 can embed a raw exception (e.g. ValueError) in an error's
    # `ctx`, which plain json.dumps cannot serialize. Run the errors through
    # jsonable_encoder first — the same thing FastAPI's default handler does —
    # so the handler never crashes (which previously masked the real 422 and
    # surfaced as a 500/blank response under load).
    safe_errors = jsonable_encoder(exc.errors())
    logging.getLogger("validation").warning(
        "422 on %s %s — errors=%s",
        request.method,
        request.url.path,
        safe_errors,
    )
    return JSONResponse(
        status_code=422,
        content={"detail": safe_errors},
    )

# Compress JSON responses — the UI's hottest payloads (batch detail ~585 KB,
# graph overview ~312 KB, corpora list ~35 KB) are highly repetitive JSON
# that gzips 5-10×. Browsers negotiate via Accept-Encoding automatically.
# NB: must NOT compress text/event-stream — gzip buffers the whole stream and
# collapses the live chat trace/answer into one end-of-stream blob. The
# stream-safe subclass skips SSE and gzips everything else. (see streaming_gzip)
from streaming_gzip import StreamSafeGZipMiddleware  # noqa: E402

app.add_middleware(StreamSafeGZipMiddleware, minimum_size=1024)

# CORS middleware configuration
# Allow frontend origins as specified in CLAUDE.md
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Local development
        "http://localhost:5173",  # Vite dev server default
        "https://kingsleylab.xyz",  # Production domain
        "https://app.kingsleylab.xyz",  # Production app subdomain
        "https://rag.kingsleylab.xyz",  # Production RAG frontend (the one actually used)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
# Each router handles a specific API domain
app.include_router(auth_router)
app.include_router(health_router)
app.include_router(models_router)
app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(tools_router)
app.include_router(skills_router)         # Phase 24 — Skills CRUD
app.include_router(mcp_info_router)       # Phase 24 — MCP server info for Settings tab
app.include_router(portability_router)    # Runtime archive download/upload for Settings
app.include_router(ingestion_router)
app.include_router(graph_router)
app.include_router(graph_discovery_router)  # Phase 17 Wave 1 — /api/graph/query
app.include_router(discourse_router)  # Phase 17 Wave 2 — /api/corpora/{id}/discourse
app.include_router(settings_router)
app.include_router(modal_ops_router)        # Phase 22 — programmatic Modal deploy
app.include_router(model_profiles_router)  # Phase 19.3 — custom model profiles
app.include_router(model_pool_router)      # Phase E — unified model pool
app.include_router(query_prefs_router)     # Phase F — per-user query prefs + ollama exclusions
app.include_router(cli_providers_router)   # T4 — CLI subscription lanes (status/login/sync)


@app.get("/", tags=["root"])
async def root() -> dict[str, str]:
    """
    Root endpoint - API welcome message.
    Use /api/health for service status checks.
    """
    return {
        "name": "Polymath RAG API",
        "version": "3.3.0",
        "docs": "/docs",
        "health": "/api/health",
    }


@app.get("/health", tags=["root"], include_in_schema=False)
async def health_alias():
    """Conventional /health alias for monitoring tools that ping the
    canonical path. The substantive health check lives at /api/health;
    this just forwards by re-using the same handler so dashboards that
    expect /health get a non-404."""
    from routers.health import health_check
    return await health_check()
