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

    # === AUTOMATION ===
    N8N_URL: str = Field(
        default="http://n8n:5678", description="n8n automation workflow URL"
    )

    # === DEFAULT MODELS ===
    DEFAULT_COMPLETION_MODEL: str = Field(
        default="ollama/llama3.2:3b", description="Default model for chat completions"
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

    # === GHOST B — ONTOLOGY-LITE (Phase 14.2) ===
    SCHEMA_INLINE_LIMIT: int = Field(
        default=30,
        description=(
            "Threshold above which the ghost_b prompt switches from inlining "
            "the full schema to retrieving top-K terms per chunk via Qdrant."
        ),
    )
    SCHEMA_RETRIEVAL_TOP_K: int = Field(
        default=10,
        description="Number of schema terms to retrieve per chunk when over SCHEMA_INLINE_LIMIT.",
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
        default=1,
        description="Max concurrent LiteLLM calls for entity extraction (GHOST B)",
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
        default="ollama/llama3.2:3b",
        description="Model used when agentic mode is on. Must support function-calling (tools).",
    )

    # === HYDE (Phase 17) — dedicated cheap model for hypothetical answer generation ===
    HYDE_MODEL: str = Field(
        default="ollama/llama3.2:3b",
        description="Model used when HyDE is enabled per-request. Intentionally cheap — quality of the hypothetical matters less than its shape.",
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
                "secure random key (e.g. `python -c \"import secrets; print(secrets.token_urlsafe(64))\"`) "
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
