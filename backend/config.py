# backend/config.py
# Pydantic Settings - Single source of truth for all configuration
# NEVER hardcode values elsewhere - always import from here

from functools import lru_cache
from typing import Literal

from pydantic import Field, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # === DATABASES ===
    MONGODB_URI: str = Field(
        default="mongodb://mongodb:27017/polymath", description="MongoDB connection URI"
    )
    MONGODB_DATABASE: str = Field(
        default="polymath", description="MongoDB database name"
    )

    # === VECTOR DATABASE ===
    QDRANT_URL: str = Field(
        default="http://qdrant:6333", description="Qdrant vector database URL"
    )
    QDRANT_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="HTTP timeout for Qdrant operations such as per-corpus collection provisioning",
    )
    QDRANT_COLLECTION: str = Field(
        default="polymath_chunks", description="Default Qdrant collection name"
    )

    # === GRAPH DATABASE ===
    NEO4J_ENABLED: bool = Field(
        default=False, description="Enable Neo4j graph database (optional)"
    )
    NEO4J_URI: str = Field(
        default="bolt://neo4j:7687", description="Neo4j bolt connection URI"
    )
    NEO4J_USER: str = Field(default="neo4j", description="Neo4j username")
    NEO4J_PASSWORD: str = Field(default="", description="Neo4j password")

    # === LLM GATEWAY ===
    LITELLM_URL: str = Field(
        default="http://litellm:4000", description="LiteLLM proxy URL"
    )
    LITELLM_MASTER_KEY: str = Field(
        default=..., description="LiteLLM master key for authentication"
    )

    # === OLLAMA ===
    OLLAMA_URL: str = Field(default="http://ollama:11434", description="Ollama API URL")

    # === REDIS ===
    REDIS_URL: str = Field(
        default="redis://redis:6379", description="Redis connection URL"
    )

    # === EMBEDDER (sentence-transformers GPU service) ===
    EMBEDDER_URL: str = Field(
        default="http://embedder:80",
        description="Embedder service URL — GPU-accelerated, OpenAI-compatible /embeddings",
    )
    EMBEDDER_MODEL_NAME: str = Field(
        default="Qwen3-Embedding-0.6B",
        description="Display name of the loaded embedding model — must match MODEL_NAME env var in embedder container",
    )
    LOCAL_EMBEDDER_ENABLED: bool = Field(
        default=False,
        description="Whether the local Docker embedder profile is expected to be running.",
    )

    # === MODAL CLOUD GPU (primary ingestion embed path) ===
    MODAL_ENABLED: bool = Field(
        default=False,
        description="Enable Modal cloud GPU embedding. Modal is the primary production path; local is fallback.",
    )
    MODAL_EMBEDDER_URL: str = Field(
        default="",
        description="Modal webhook URL for embedding — OpenAI-compatible /embeddings endpoint",
    )
    MODAL_API_KEY: str = Field(
        default="",
        description="Modal auth token. Server-side only; never round-tripped to frontend.",
    )
    MODAL_TIMEOUT_SECONDS: float = Field(
        default=300.0,
        description="HTTP timeout for Modal embed calls (cold starts can be 10-30s)",
    )

    # === SILICONFLOW CLOUD API (alternate cloud embedding provider) ===
    SILICONFLOW_ENABLED: bool = Field(
        default=False,
        description="Enable SiliconFlow cloud API for embedding. Primary production alternative to Modal.",
    )
    SILICONFLOW_EMBEDDER_URL: str = Field(
        default="https://api.siliconflow.com/v1/embeddings",
        description="SiliconFlow OpenAI-compatible /embeddings endpoint",
    )
    SILICONFLOW_API_KEY: str = Field(
        default="",
        description="SiliconFlow auth token (Bearer). Server-side only; never round-tripped to frontend.",
    )
    SILICONFLOW_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="HTTP timeout for SiliconFlow embed calls (no cold-start penalty)",
    )
    EMBED_ALLOW_LOCAL_FALLBACK: bool = Field(
        default=False,
        description=(
            "When False, cloud/API embedding failures fail closed instead of "
            "silently falling back to the local GPU embedder."
        ),
    )
    INGEST_MAX_PARSE_JOBS: int = Field(
        default=4,
        ge=1,
        le=64,
        description=(
            "Process-local cap for concurrent parse/chunk phases. le bumped "
            "from 16 to 64 — high-VRAM rigs (RTX Pro 6000 / H100) need to "
            "feed graph extraction faster than 16 parses/window."
        ),
    )
    INGEST_MAX_MODEL_PHASE_DOCS: int = Field(
        default=3,
        ge=1,
        le=64,
        description=(
            "Process-local cap for local documents concurrently running "
            "pre-vector model phases. Per-entry model concurrency still "
            "applies inside a slot. le bumped from 8 to 64 for high-VRAM "
            "rigs that can saturate vllm with more parallel docs."
        ),
    )
    INGEST_MAX_CLOUD_MODEL_PHASE_DOCS: int = Field(
        default=2,
        ge=1,
        le=64,
        description=(
            "Process-local cap for cloud/API documents concurrently running "
            "pre-vector model phases. Kept separate from local routing. "
            "le bumped to 64 to match local cap."
        ),
    )
    INGEST_MAX_GRAPH_MODEL_PHASE_DOCS: int = Field(
        default=2,
        ge=1,
        le=64,
        description=(
            "Process-local cap for graph extraction model phases. Graph work "
            "must not block vector readiness for new local documents. "
            "le bumped from 8 to 64 — at the legacy 8 cap a 6-book batch "
            "could only graph_extract 2 in parallel, leaving vllm at <20%% "
            "utilization on 97 GB cards."
        ),
    )
    INGEST_MAX_ACTIVE_JOBS: int = Field(
        default=16,
        ge=1,
        le=256,
        description=(
            "Process-local cap for active background ingest jobs retained in memory. "
            "Requests over this cap fail fast with 429 instead of holding uploaded bytes."
        ),
    )
    INGEST_SPOOL_DIR: str = Field(
        default="/app/ingest-spool",
        description=(
            "Durable directory for multi-file batch upload spooling. Files are "
            "streamed here before queued ingestion so 500-file uploads do not "
            "live in process RAM."
        ),
    )
    INGEST_MAX_SPOOLED_BYTES: int = Field(
        default=250 * 1024 * 1024 * 1024,
        ge=1,
        description="Admission cap for queued batch-upload bytes on disk.",
    )
    INGEST_MIN_FREE_DISK_GB: float = Field(
        default=20.0,
        ge=1.0,
        description=(
            "Minimum free disk on the spool volume before a new batch is "
            "admitted. Below this floor the batch endpoint returns 507 "
            "(Insufficient Storage) and the worker refuses to claim new "
            "items until the operator clears space. Independent from the "
            "spool byte cap — this guards against catastrophic ENOSPC "
            "during long ingest runs where intermediate writes "
            "(qdrant, mongo, neo4j journals) can swallow free disk faster "
            "than the spool itself."
        ),
    )
    INGEST_MIN_FREE_VRAM_MB: int = Field(
        default=1024,
        ge=0,
        description=(
            "Backend scheduler floor for embedder GPU free VRAM. Below this "
            "the queue refuses to claim new items. Closes GOTCHAS §65.1 — "
            "during a 500-file ingest the embedder, vllm-summary, and "
            "vllm-extract share one GPU; without backpressure the embedder "
            "OOM-kills mid-batch. Set to 0 to disable (not recommended)."
        ),
    )
    EMBEDDER_HEALTH_URL: str = Field(
        default="http://embedder:80/health",
        description=(
            "URL the scheduler probes for adaptive backpressure. Returns "
            "{gpu_free_mb, gpu_total_mb}. Failure to reach falls open "
            "(no backpressure) so a flaky probe doesn't stall the queue."
        ),
    )
    INGEST_CIRCUIT_BREAKER_CONSECUTIVE_FAILS: int = Field(
        default=5,
        ge=2,
        le=100,
        description=(
            "Pause the batch when N consecutive items fail with the same "
            "error_kind (token_budget, lane_disabled, mongo_overflow, "
            "etc.). Prevents a 500-file batch from chewing through "
            "every doc with the same configuration error before anyone "
            "notices. Operator un-pauses after fixing config."
        ),
    )
    INGEST_BATCH_POLL_SECONDS: float = Field(
        default=1.0,
        ge=0.1,
        le=30.0,
        description="Scheduler polling cadence for durable batch ingestion.",
    )
    INGEST_BATCH_MAX_ACTIVE_DOCS: int = Field(
        default=0,
        ge=0,
        le=32,
        description=(
            "Optional hard override for active batch document workers. "
            "0 lets the scheduler size pre-vector plus graph headroom automatically."
        ),
    )
    INGEST_BATCH_RETAIN_SPOOL_ON_FAILURE: bool = Field(
        default=True,
        description=(
            "Keep spooled files when a queued document fails so retry-failed "
            "can run without the user re-uploading."
        ),
    )
    GRAPH_REPAIR_WORKER_ENABLED: bool = Field(
        default=True,
        description="Run the durable Gemma graph repair queue worker when Neo4j is enabled.",
    )
    GRAPH_REPAIR_WORKER_CONCURRENCY: int = Field(
        default=2,
        ge=1,
        le=16,
        description="Maximum concurrent durable graph repair jobs.",
    )
    GRAPH_REPAIR_WORKER_POLL_SECONDS: float = Field(
        default=2.0,
        ge=0.1,
        le=60.0,
        description="Polling cadence for the durable graph repair worker.",
    )
    GRAPH_REPAIR_LEASE_SECONDS: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Visibility timeout for leased graph repair jobs.",
    )

    # === LOCAL MODELS DIR ===
    MODELS_DIR: str = Field(
        default="/models",
        description="Path to local HF model downloads (Docker volume → ./download)",
    )

    # === RERANKER ===
    RERANKER_URL: str = Field(
        default="http://reranker:8080",
        description="Reranker service URL (sentence-transformers cross-encoder)",
    )
    LOCAL_RERANKER_ENABLED: bool = Field(
        default=False,
        description="Whether the local Docker reranker profile is expected to be running.",
    )

    # === AUTOMATION ===
    N8N_URL: str = Field(
        default="http://n8n:5678", description="n8n automation workflow URL"
    )

    # === DEFAULT MODELS ===
    # Phase 24 — empty by default. CLAUDE.md "never hardcode model names"
    # rule. The user's per-pool entries (Settings → Models) supply real
    # values; resolution falls back to the active chat model for HyDE /
    # Reasoning Cascade and raises a clear error when nothing is configured.
    DEFAULT_COMPLETION_MODEL: str = Field(
        default="",
        description=(
            "Last-resort fallback when neither the user's pool entry nor the "
            "active chat model is set. Leave blank — the resolver should never "
            "land here unless config is genuinely incomplete."
        ),
    )
    DEFAULT_EMBEDDING_MODEL: str = Field(
        default="tei/qwen3-embedding",
        description="Default embedding model — routes through LiteLLM to TEI embedder",
    )

    # === EMBEDDING SETTINGS ===
    EMBEDDING_DIMENSION: int = Field(
        default=1024,
        description="Embedding vector dimension — must match model output. Qwen3-Embedding-0.6B=1024. Changing requires full Qdrant re-index.",
    )

    # === CHUNKING SETTINGS ===
    CHUNK_SIZE: int = Field(default=1000, description="Parent chunk size in characters")
    CHUNK_OVERLAP: int = Field(
        default=200, description="Overlap between chunks in characters"
    )
    CHILD_CHUNK_SIZE: int = Field(
        default=300, description="Child chunk size for fine-grained retrieval"
    )

    # === TOKEN LIMITS ===
    MAX_CONTEXT_TOKENS: int = Field(
        default=4096, description="Maximum context window tokens"
    )
    MAX_COMPLETION_TOKENS: int = Field(
        default=2048, description="Maximum completion tokens"
    )
    RESERVE_TOKENS: int = Field(
        default=500, description="Tokens reserved for system prompt and response"
    )

    # === RETRIEVAL SETTINGS ===
    DEFAULT_RETRIEVAL_K: int = Field(
        default=5, description="Default number of chunks to retrieve"
    )
    SIMILARITY_THRESHOLD: float = Field(
        default=0.7, description="Minimum similarity score for retrieval"
    )

    # === QDRANT COLLECTION NAMES ===
    QDRANT_NAIVE: str = Field(
        default="polymath_naive",
        description="Qdrant collection for all-tier child + summary vectors",
    )
    QDRANT_HRAG: str = Field(
        default="polymath_hrag",
        description="Qdrant collection for Tier A/B heading-aware vectors only",
    )
    QDRANT_GRAPH: str = Field(
        default="polymath_graph",
        description="Qdrant collection for graph-aligned vectors (use_neo4j=True path)",
    )
    QDRANT_SCHEMAS: str = Field(
        default="polymath_schemas",
        description=(
            "Phase 14.2 — single collection for both entity-type and "
            "relation-predicate schema-term embeddings. payload.kind ∈ "
            "{entity_type, relation} discriminates."
        ),
    )
    QDRANT_COLLECTION_PREFIX: str = Field(
        default="corpus_",
        description=(
            "Phase 7.5 — per-corpus collection naming. Final name shape: "
            "f'{prefix}{corpus_id[:8]}_{kind}' where kind ∈ "
            "{naive,hrag,graph,schemas}. The legacy QDRANT_* names above are "
            "retained for the one-shot migration script's source-side reads."
        ),
    )

    # === GHOST B — UNIVERSAL SCHEMA ===
    FORCE_UNIVERSAL_SCHEMA: bool = Field(
        default=False,
        description=(
            "Lifespan admin lever. When False (default), corpora with a "
            "null/empty schema are patched to the universal schema; corpora "
            "with a legacy custom schema are preserved untouched. When True, "
            "ALL corpora are overwritten with the universal schema on startup."
        ),
    )

    # === GHOST B — ONTOLOGY-LITE (Phase 14.2) ===
    SCHEMA_INLINE_LIMIT: int = Field(
        default=64,
        description=(
            "Threshold above which the ghost_b prompt switches from inlining "
            "the full schema to retrieving top-K terms per chunk via Qdrant."
        ),
    )
    SCHEMA_RETRIEVAL_TOP_K: int = Field(
        default=10,
        description="Number of schema terms to retrieve per chunk when over SCHEMA_INLINE_LIMIT.",
    )
    SCHEMA_LENS_LLM_ENABLED: bool = Field(
        default=True,
        description=(
            "When True, the first Ghost B run for a corpus makes one bounded "
            "LLM call to profile a soft schema lens. The lens is cached on the "
            "corpus and still clamps all suggestions to the approved schema."
        ),
    )
    SCHEMA_LENS_SAMPLE_CHUNKS: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Max child chunks sampled when creating the auto schema lens.",
    )
    SCHEMA_LENS_SAMPLE_CHARS: int = Field(
        default=6000,
        ge=1000,
        le=20000,
        description="Max text characters sent to the schema lens profiler.",
    )

    # === MCP — Phase 8 Integration ===
    MCP_HOST: str = Field(
        default="0.0.0.0",
        description="Bind host for the MCP sidecar's streamable-HTTP server.",
    )
    MCP_PORT: int = Field(
        default=8765,
        description="Port for the MCP sidecar (exposed inside docker-compose network).",
    )
    MCP_TRANSPORT: Literal["streamable-http", "stdio"] = Field(
        default="streamable-http",
        description=(
            "MCP transport selector. 'streamable-http' runs the ASGI server on "
            "MCP_HOST:MCP_PORT; 'stdio' attaches to stdin/stdout for local Claude "
            "Desktop integration via a host-side proxy."
        ),
    )
    MCP_DEFAULT_TOP_K: int = Field(
        default=5,
        description="Default top_k for polymath_search when client omits the param.",
    )
    MCP_REQUIRE_AUTH: bool = Field(
        default=True,
        description=(
            "When True, MCP tools reject requests without a valid JWT or API key. "
            "Set False only for trusted single-user local development."
        ),
    )
    MCP_API_KEY: str | None = Field(
        default=None,
        description=(
            "Static bearer token for system-level MCP access (no per-user corpus "
            "scoping — sees all corpora). When set, the auth middleware tries it "
            "BEFORE JWT validation via constant-time compare. Use for trusted "
            "agents (openclaw, cron jobs, internal tools) that don't manage user "
            "JWTs. Leave unset to require JWT for every request. Never commit; "
            "set via .env. Recommended: 32+ random bytes (`openssl rand -hex 32`)."
        ),
    )

    # === GHOST A — PARENT SUMMARIZATION ===
    SUMMARY_MAX_CONCURRENT: int = Field(
        default=1,
        description="Max concurrent LiteLLM calls for parent summarization (GHOST A)",
    )
    SUMMARY_MAX_TOKENS: int = Field(
        default=175,
        description="Token cap per parent summary output (GHOST A)",
    )

    # === GHOST B — ENTITY EXTRACTION ===
    EXTRACTION_MAX_CONCURRENT: int = Field(
        default=2,
        description="Max concurrent LiteLLM calls for entity extraction (GHOST B)",
    )
    EXTRACTION_MAX_TOKENS: int = Field(
        default=8192,
        ge=256,
        le=8192,
        description="Maximum completion tokens for each entity extraction call (GHOST B)",
    )
    LOCAL_VLLM_COMPACT_MAX_CONCURRENT: int = Field(
        default=64,
        ge=1,
        le=512,
        description=(
            "Per-document concurrency cap for local vllm extraction lanes "
            "in compact mode. Bumped from the legacy 8 to 64 — the legacy "
            "value starved high-VRAM GPUs (RTX Pro 6000, H100) where vllm "
            "schedules max_num_seqs=256 natively. Lower if you see OOM on "
            "an under-provisioned GPU."
        ),
    )
    LOCAL_VLLM_NORMAL_MAX_CONCURRENT: int = Field(
        default=128,
        ge=1,
        le=512,
        description=(
            "Per-document concurrency cap for local vllm extraction lanes "
            "in normal (non-compact) mode. Bumped from the legacy 16 — same "
            "reasoning as LOCAL_VLLM_COMPACT_MAX_CONCURRENT."
        ),
    )
    INGEST_PRE_VECTOR_DOC_CAP: int = Field(
        default=4,
        ge=1,
        le=32,
        description=(
            "Max concurrent doc workers in the pre-vector phase (parse + "
            "chunk + ghost_a + mongo + embed + qdrant). Legacy hard cap was "
            "4. On a 97 GB GPU + 64 GB RAM rig this can safely go to 8-16 "
            "to feed graph extraction without idle slots. Lower if you OOM "
            "the WSL VM or see Mongo connection-pool exhaustion."
        ),
    )
    INGEST_GRAPH_DOC_CAP: int = Field(
        default=4,
        ge=1,
        le=32,
        description=(
            "Max concurrent doc workers in the graph_extracting phase. "
            "Legacy hard cap was 4. Combined with INGEST_MAX_GRAPH_MODEL_"
            "PHASE_DOCS as the floor; this is the ceiling. On high-VRAM "
            "boxes raise to 6-8 to fully saturate vllm-extract."
        ),
    )
    EXTRACTION_MAX_ENTITIES_PER_CHUNK: int = Field(
        default=14,
        ge=1,
        le=64,
        description="Maximum entities Ghost B should return for a single child chunk",
    )
    EXTRACTION_MAX_RELATIONS_PER_CHUNK: int = Field(
        default=14,
        ge=0,
        le=64,
        description="Maximum relations Ghost B should return for a single child chunk",
    )
    ENTITY_CONFIDENCE_THRESHOLD: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Min confidence to keep an extracted entity or relation (GHOST B)",
    )

    # === AGENTIC MODE ===
    AGENTIC_MODE_ENABLED: bool = Field(
        default=False,
        description="Global default for agentic mode. When True, queries route through AGENTIC_MODEL and tools can execute. User can override per-request via ModelOverrides.",
    )
    AGENTIC_MODEL: str = Field(
        default="",
        description=(
            "Phase 24: empty by default. Use Settings → Models → Tool-Capable "
            "Fallback to pick an entry. When unset and the active chat model "
            "can't tool-call, the resolver raises a clear error rather than "
            "silently degrading to a slow Ollama default."
        ),
    )

    # === HYDE (Phase 17) — dedicated cheap model for hypothetical answer generation ===
    HYDE_MODEL: str = Field(
        default="",
        description=(
            "Phase 24: empty by default. Use Settings → Models → HyDE to pick "
            "an entry. When unset, HyDE reuses the active chat model rather "
            "than silently degrading to a hardcoded default."
        ),
    )
    HYDE_TIMEOUT_SECONDS: float = Field(
        default=8.0,
        ge=1.0,
        le=60.0,
        description=(
            "Hard wall for the HyDE helper call. Keep bounded so retrieval can "
            "fall back to the raw query, but allow cloud models like Mistral "
            "enough room to succeed."
        ),
    )
    HYDE_MAX_TOKENS: int = Field(
        default=192,
        ge=32,
        le=1024,
        description="Maximum tokens for the short hypothetical answer used by HyDE.",
    )

    # === REASONING CASCADE (Phase 24) — analyst that digests retrieved chunks ===
    REASONING_MODEL: str = Field(
        default="",
        description=(
            "Model used for the reasoning cascade (opt-in per-request). "
            "Should be a strong reasoning/analysis model (DeepSeek R1, o1, "
            "claude-sonnet, etc). Empty falls back to DEFAULT_COMPLETION_MODEL. "
            "Cost ~20× of a Balanced query — use sparingly via per-request flag."
        ),
    )

    # === AUTHENTICATION ===
    # SECURITY (Phase 17 W1.1): both AUTH_SECRET_KEY and DEFAULT_ADMIN_PASSWORD
    # are now REQUIRED env vars with no default. Startup validators reject the
    # legacy sentinel values so that an insecure .env can never deploy silently.
    AUTH_SECRET_KEY: str = Field(
        default=...,
        description="Secret key for JWT signing. MUST be set in .env; startup rejects the legacy 'polymath-dev-secret-key-change-in-production' sentinel.",
    )
    AUTH_ALGORITHM: str = Field(default="HS256", description="JWT signing algorithm")
    AUTH_TOKEN_EXPIRE_DAYS: int = Field(
        default=7, description="JWT token expiration in days"
    )
    DEFAULT_ADMIN_USERNAME: str = Field(
        default="admin", description="Default admin username for zero-user bootstrap"
    )
    DEFAULT_ADMIN_PASSWORD: str = Field(
        default=...,
        description="Initial admin password for zero-user bootstrap. MUST be set in .env; startup rejects empty and the legacy 'changeme' sentinel.",
    )

    # === APPLICATION ===
    APP_ENV: str = Field(default="development", description="Application environment")
    LOG_LEVEL: str = Field(default="info", description="Logging level")

    class Config:
        """Pydantic config."""

        env_file = "../.env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"

    @validator("LITELLM_MASTER_KEY")
    def validate_litellm_key(cls, v):
        """Validate that LITELLM_MASTER_KEY is not empty."""
        if not v or not v.strip():
            raise ValueError("LITELLM_MASTER_KEY is required and cannot be empty")
        return v

    @validator("AUTH_SECRET_KEY")
    def validate_auth_secret_key(cls, v):
        """
        Phase 17 W1.1 — reject empty or the legacy dev sentinel.

        JWT tokens signed with the sentinel are forgeable by anyone who has
        seen the source code. Hard fail at startup if it's still in place.
        """
        if not v or not v.strip():
            raise ValueError("AUTH_SECRET_KEY is required and cannot be empty")
        if v.strip() == "polymath-dev-secret-key-change-in-production":
            raise ValueError(
                "AUTH_SECRET_KEY is still the legacy dev sentinel. Generate a "
                'secure random key (e.g. `python -c "import secrets; print(secrets.token_urlsafe(64))"`) '
                "and set it in .env before starting."
            )
        return v

    @validator("DEFAULT_ADMIN_PASSWORD")
    def validate_default_admin_password(cls, v):
        """
        Phase 17 W1.1 — reject empty or the legacy 'changeme' sentinel.
        The bootstrap admin account would otherwise be publicly guessable.
        """
        if not v or not v.strip():
            raise ValueError("DEFAULT_ADMIN_PASSWORD is required and cannot be empty")
        if v.strip().lower() == "changeme":
            raise ValueError(
                "DEFAULT_ADMIN_PASSWORD is still the legacy 'changeme' sentinel. "
                "Set a strong password in .env before starting."
            )
        return v


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached application settings.
    Usage: settings = get_settings()
    Then: settings.MONGODB_URI, settings.QDRANT_URL, etc.
    """
    return Settings()
