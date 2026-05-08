# backend/main.py
# FastAPI application entry point
# ALL routers registered here. Touch only to add routers.
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000 --reload

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
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from services.auth import auth_service
from services.conversation import conversation_service
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

    yield

    # Shutdown
    logger.info("Shutting down Polymath RAG API")
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@app.exception_handler(RequestValidationError)
async def _log_validation_error(request: Request, exc: RequestValidationError):
    logging.getLogger("validation").warning(
        "422 on %s %s — errors=%s",
        request.method,
        request.url.path,
        exc.errors(),
    )
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )

# CORS middleware configuration
# Allow frontend origins as specified in CLAUDE.md
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Local development
        "http://localhost:5173",  # Vite dev server default
        "https://kingsleylab.xyz",  # Production domain
        "https://app.kingsleylab.xyz",  # Production app subdomain
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
