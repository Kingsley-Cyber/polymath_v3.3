# backend/models/schemas.py
# ALL Pydantic request/response models
# One file, one source of truth. Never define schemas inline.
# Import from here: from models.schemas import ChatRequest, Conversation, etc.

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# NOTE: RetrievalTier and SourceChunk are defined in the INGESTION MODELS section
# below and are usable here because from __future__ import annotations defers
# annotation evaluation. The default=RetrievalTier.qdrant_mongo in ChatRequest
# is a runtime reference resolved after the full module loads.

# ============================================================================
# CHAT MODELS
# ============================================================================


class ModelOverrides(BaseModel):
    """Per-request overrides for model config and retrieval routing."""

    model: str | None = Field(default=None, description="Override default model")
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1, le=32000)
    hyde_enabled: bool | None = Field(default=None)
    graph_enabled: bool | None = Field(default=None)
    collection_ids: list[str] | None = Field(default=None)
    agentic_mode: bool | None = Field(
        default=None,
        description="Per-request agentic override. If None, falls back to server-side AGENTIC_MODE_ENABLED.",
    )
    agentic_model: str | None = Field(
        default=None,
        description="Per-request agentic model override. If None, falls back to server-side AGENTIC_MODEL.",
    )
    reasoning_mode: str | None = Field(
        default=None,
        description="Per-request reasoning mode (Phase 15). One of the 12 curated or 40 raw modes in services/reasoning.py. If None, falls back to ChatLLMSettings.default_reasoning_mode.",
    )
    reasoning_blend: list[str] | None = Field(
        default=None,
        description="Optional power-user blend — list of raw mode keys to concatenate into the prompt. If present, runs alongside reasoning_mode.",
    )
    hyde_model: str | None = Field(
        default=None,
        description="Per-request HyDE model override (Phase 17). When hyde_enabled, this model generates the hypothetical answer that replaces the query for Qdrant search. If None, falls back to ChatLLMSettings.hyde_model.",
    )
    # Phase 18 — Query Profile per-request overrides
    query_profile: str | None = Field(
        default=None,
        description="Per-request speed preset: 'fast' | 'balanced' | 'thorough'. Bundles retrieval_k + rerank + hyde defaults. Individual overrides below still win.",
    )
    retrieval_k: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="Per-request override for pre-rerank pool size (single-corpus). Takes precedence over profile preset and server default.",
    )
    rerank_enabled: bool | None = Field(
        default=None,
        description="Per-request reranker switch. When False, retriever skips the cross-encoder call and sorts by vector score. Fixes the previously-dead UI toggle.",
    )


class ChatRequest(BaseModel):
    """POST /api/chat request body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    conversation_id: str | None = Field(
        default=None, description="Existing conversation ID (null for new)"
    )
    message: str = Field(..., min_length=1, description="User message content")
    corpus_ids: list[str] | None = Field(
        default=None, description="Corpora to scope search (max 3)"
    )
    retrieval_tier: RetrievalTier = Field(
        default="qdrant_mongo", description="Strategy for retrieval"
    )
    collections: list[str] | None = Field(
        default=None, description="Target Qdrant collections"
    )
    overrides: ModelOverrides | None = Field(
        default=None, description="Per-request model overrides"
    )
    selected_tools: list[str] | None = Field(
        default=None, description="List of tool IDs to enable for this request"
    )

    @field_validator("corpus_ids")
    @classmethod
    def validate_corpus_ids(cls, v):
        """Hard cap: max 3 corpora per query (cross-corpus round-robin limit)."""
        if v is not None and len(v) > 3:
            raise ValueError("Maximum 3 corpora per query.")
        return v

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, v):
        """Validate conversation_id is a valid ObjectId if provided."""
        if v is not None and v.strip():
            if not ObjectId.is_valid(v):
                raise ValueError(f"Invalid ObjectId format: {v}")
        return v


class ChatChunk(BaseModel):
    """Individual SSE chunk streamed to frontend."""

    type: str = Field(
        ...,
        description="Chunk type: token, thinking, done, error, trimming, budget, sources, tool_call_start, tool_result, tier_downgraded",
    )
    content: str | None = Field(default=None)
    thinking: str | None = Field(
        default=None,
        description="Thinking/reasoning content from models that support it",
    )
    conversation_id: str | None = Field(default=None)
    model_used: str | None = Field(default=None)
    trimming_applied: bool | None = Field(default=None)
    trimming_details: str | None = Field(default=None)
    sources: list[SourceChunk] | None = Field(
        default=None, description="Retrieved sources"
    )
    # Token budget telemetry — emitted once per request as `type=budget`
    tokens_used: int | None = Field(default=None)
    tokens_max: int | None = Field(default=None)


# ============================================================================
# MESSAGE MODELS
# ============================================================================


class ChatMessage(BaseModel):
    """A single message in a conversation."""

    role: str = Field(..., description="Message role: user, assistant, system")
    content: str = Field(..., description="Message content")
    thinking: str | None = Field(
        default=None, description="Thinking/reasoning content from supported models"
    )
    model_used: str | None = Field(
        default=None, description="Model used for this response"
    )
    token_count: int | None = Field(default=None, description="Token count")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    trimming_applied: bool = Field(
        default=False, description="Whether history was trimmed"
    )
    collections_queried: list[str] = Field(
        default_factory=list, description="Collections queried for RAG"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Extensible metadata"
    )


# ============================================================================
# CONVERSATION MODELS
# ============================================================================


class ModelConfig(BaseModel):
    """Model configuration for a conversation."""

    model: str = Field(default="ollama/llama3.2:3b")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    max_tokens: int = Field(default=2048, ge=1, le=32000)


class Conversation(BaseModel):
    """Full conversation document (MongoDB schema)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str | None = Field(default=None, alias="_id")
    title: str = Field(default="New Conversation")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    model_config_conversation: ModelConfig = Field(
        default_factory=ModelConfig, alias="model_config"
    )
    messages: list[ChatMessage] = Field(default_factory=list)


class ConversationListItem(BaseModel):
    """Conversation summary for list endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(default=0)
    last_message_preview: str | None = Field(default=None)

    @field_validator("id")
    @classmethod
    def validate_object_id(cls, v):
        """Validate that id is a valid ObjectId."""
        if not ObjectId.is_valid(v):
            raise ValueError(f"Invalid ObjectId format: {v}")
        return v


class ConversationCreate(BaseModel):
    """POST /api/conversations request body."""

    title: str | None = Field(default="New Conversation")
    llm_config: ModelConfig | None = Field(default=None)


class ConversationUpdate(BaseModel):
    """PUT /api/conversations/{id} request body."""

    title: str | None = None
    llm_config: ModelConfig | None = None


# ============================================================================
# HEALTH MODELS
# ============================================================================


class ServiceStatus(BaseModel):
    """Individual service health status."""

    status: str = Field(..., description="ok, error, or degraded")
    latency_ms: float | None = Field(default=None)
    error: str | None = Field(default=None)


class HealthResponse(BaseModel):
    """GET /api/health response."""

    status: str = Field(..., description="Overall status: ok, degraded, error")
    services: dict[str, ServiceStatus] = Field(
        ..., description="Status of each service"
    )


# ============================================================================
# MODELS ENDPOINT
# ============================================================================


class ModelInfo(BaseModel):
    """Information about an available model."""

    id: str = Field(..., description="Model ID in provider/model format")
    name: str = Field(..., description="Display name")
    provider: str = Field(
        ..., description="Provider: ollama | openai | anthropic | local | tei"
    )
    source: str = Field(
        ..., description="Where discovered: ollama | litellm | download"
    )
    type: str = Field(default="chat", description="Model type: chat | embedding")
    context_length: int | None = Field(
        default=None, description="Max context window (chat models)"
    )
    dimension: int | None = Field(
        default=None, description="Embedding output dimension (embedding models only)"
    )


class ModelsResponse(BaseModel):
    """GET /api/models response."""

    chat_models: list[ModelInfo]
    embedding_models: list[ModelInfo]
    default_model: str
    default_embedding_model: str


# ============================================================================
# SETTINGS MODELS (Phase 2 - Placeholder)
# ============================================================================


# ============================================================================
# SETTINGS MODELS (Phase 10 — Global + Per-Corpus)
# ============================================================================


class TokenBudget(BaseModel):
    """Min/target/max token range for chunk sizes."""

    min_tokens: int = Field(default=500, ge=100)
    target_tokens: int = Field(default=1200, ge=200)
    max_tokens: int = Field(default=2000, ge=500)


class AuthConfig(BaseModel):
    """Auth settings — masked in API responses."""

    auth_secret_key: str = "••••••••"
    auth_algorithm: str = "HS256"
    auth_token_expire_days: int = 7


class InfrastructureSettings(BaseModel):
    """Service URLs + auth — loaded from config.py env vars."""

    mongodb_url: str = "mongodb://mongodb:27017"
    qdrant_url: str = "http://qdrant:6333"
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "••••••••"
    litellm_base_url: str = "http://litellm:4000"
    litellm_master_key: str = "••••••••"
    ollama_base_url: str = "http://ollama:11434"
    redis_url: str = "redis://redis:6379"
    embedder_url: str = "http://embedder:80"
    reranker_url: str = "http://reranker:8080"
    # Modal cloud GPU (Phase 14.3) — primary ingestion embed path
    modal_enabled: bool = False
    modal_embedder_url: str = ""  # masked/empty when not configured
    auth: AuthConfig = Field(default_factory=AuthConfig)


class ModalDeploySettings(BaseModel):
    """Phase M2 — user-tunable Modal deploy config.

    These values are read by modal_embedder.py as env vars at
    `modal deploy` time and baked into the deployed app. UI surfaces
    the ready-to-copy command so users don't have to juggle env vars.

    Phase 19.3 — two runtime fields added (`enabled`, `embedder_url`).
    Unlike the deploy-time knobs above, these are consulted by the live
    embedder dispatcher in services/embedder.py via
    settings_service.get_system_modal(). No backend restart required.
    """

    gpu_tier: Literal["T4", "L4", "A10G", "L40S", "A100", "H100"] = "T4"
    # Min/max container fleet. min_containers drives baseline cost even at
    # zero traffic (warm fleet). Max caps autoscale under load.
    min_containers: int = Field(
        default=0,
        ge=0,
        le=50,
        description="Warm fleet size. 0 = fully scale-to-zero. "
                    "Biggest cost lever — gpu_rate × min_containers × 730h/mo.",
    )
    max_containers: int = Field(default=10, ge=1, le=1000)
    idle_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    concurrency_per_container: int = Field(default=4, ge=1, le=64)
    app_name: str = Field(
        default="polymath-embedder",
        min_length=1,
        max_length=63,
        description="Modal App name. Determines the deployed URL: "
                    "https://<workspace>--<app_name>-embed.modal.run",
    )
    model_id: str = Field(
        default="Qwen/Qwen3-Embedding-0.6B",
        description="Any sentence-transformers-compatible HF model. "
                    "Must match the corpus's frozen embedding dimension.",
    )
    use_auth: bool = Field(
        default=False,
        description="If true, the deployed endpoint requires Bearer token matching MODAL_PROXY_KEY secret",
    )
    # Phase 19.3 — runtime connection (previously only in .env)
    enabled: bool = Field(
        default=False,
        description="Turn cloud-Modal embedding on/off without a backend restart.",
    )
    embedder_url: str = Field(
        default="",
        description="Deployed Modal app URL (e.g. https://<workspace>--<app>-<fn>.modal.run). "
                    "When set, new corpora default to embed_mode='modal_tei'.",
    )
    # Populated by the verify-token endpoint so the UI can render the live URL
    # preview (<workspace>--<app>-embed.modal.run). Not a secret — just a label.
    workspace: str = Field(
        default="",
        description="Modal workspace name captured by `modal token info`. "
                    "UI-only; display under URL preview.",
    )


class ChatLLMSettings(BaseModel):
    """Chat completion defaults — per-query overrides take precedence."""

    default_chat_model: str = "ollama/llama3.2:3b"
    max_context_tokens: int = 4096
    max_completion_tokens: int = 2048
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    # Agentic mode (Phase 14.1)
    agentic_mode_enabled: bool = False
    agentic_model: str = "ollama/llama3.2:3b"
    # Reasoning modes (Phase 15) — default used when ToggleBar selection is cleared
    default_reasoning_mode: str = "none"
    reasoning_blend: list[str] = Field(default_factory=list)
    # HyDE (Phase 17) — dedicated small/fast model for hypothetical answer
    # generation. Falls back to default_chat_model if the hyde model is
    # unavailable at runtime. Kept separate so cheap hyde + expensive chat
    # is the default posture.
    hyde_model: str = "ollama/llama3.2:3b"
    # Query Profile (Phase 18) — speed preset that bundles retrieval_k +
    # rerank + HyDE defaults. User picks a profile in ToggleBar; power users
    # override individual knobs via ModelOverrides.
    query_profile: Literal["fast", "balanced", "thorough"] = "balanced"


class RetrievalSettings(BaseModel):
    """Retrieval pipeline defaults — per-query overrides take precedence."""

    default_tier: Literal["qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"] = (
        "qdrant_mongo"
    )
    top_k_child: int = Field(default=30, ge=1, le=100)
    top_k_summary: int = Field(default=10, ge=1, le=50)
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L6-v2"
    rerank_top_n: int = Field(default=40, ge=1, le=200)
    rerank_enabled: bool = True
    similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_corpora_per_query: int = Field(default=3, ge=1, le=10)
    neo4j_expansion_cap: int = Field(default=20, ge=0, le=100)


class GlobalSettings(BaseModel):
    """Combined global settings — system-wide, mutable anytime."""

    infrastructure: InfrastructureSettings = Field(
        default_factory=InfrastructureSettings
    )
    chat: ChatLLMSettings = Field(default_factory=ChatLLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    modal: ModalDeploySettings = Field(default_factory=ModalDeploySettings)


class GlobalSettingsResponse(BaseModel):
    """GET /api/settings response."""

    settings: GlobalSettings


class GlobalSettingsUpdate(BaseModel):
    """PUT /api/settings request body — partial update, any section omitted = unchanged."""

    chat: ChatLLMSettings | None = None
    retrieval: RetrievalSettings | None = None
    modal: ModalDeploySettings | None = None


# ── Phase 19.2 — Cloud API key manager ──────────────────────────────────────


class ApiKeysPublic(BaseModel):
    """
    GET /api/settings/api-keys response — masked values only.
    Provider names are lowercase; value is either "[not set]" or a masked
    string like "sk-****abc4" showing only prefix + last 4 chars.
    """

    keys: dict[str, str] = Field(default_factory=dict)
    providers: list[str] = Field(
        default_factory=list,
        description="All known provider names for which keys can be set",
    )


class ApiKeysUpdate(BaseModel):
    """
    PUT /api/settings/api-keys request body — plaintext keys per provider.
    Only provided providers are updated; omitted providers are untouched.
    Empty-string value DELETES the stored key for that provider.
    """

    keys: dict[str, str] = Field(
        ...,
        description="Map of provider → plaintext API key (or empty string to clear)",
    )


# ============================================================================
# INGESTION MODELS
# ============================================================================


class SourceTier(str, Enum):
    tier_a = "tier_a"  # clean markdown — H1/H2 heading structure
    tier_b = "tier_b"  # semi-structured — HTML or normalized web content
    tier_b_plus = "tier_b_plus"  # recoverable structure — chapter/section markers injected as MD headers
    tier_c = "tier_c"  # flat prose — no headings, token-budget split only
    ocr_ast = "ocr_ast"  # multi-page PDF — page/column/block layout


class ModelProfileRef(BaseModel):
    """
    Single entry in a GHOST A / GHOST B model pool.
    Captures everything needed to dispatch a LiteLLM call:
      - model name (required)
      - optional base_url + api_key override
      - per-entry concurrency semaphore
      - extra params merged into the request body

    api_key is Fernet-encrypted at rest (see ingestion_service._encrypt_ingestion_keys_in_place).
    Masked as the sentinel "[set]" on GET so the UI can indicate presence without
    seeing ciphertext or plaintext.
    """

    provider_preset: str = Field(
        default="",
        description="UI label (openai / deepseek / ollama / …). Not runtime-authoritative.",
    )
    model: str = Field(..., min_length=1, description="LiteLLM model string, e.g. 'openai/gpt-4o'")
    base_url: str | None = Field(
        default=None, description="Per-entry api_base override. None = LITELLM default."
    )
    api_key: str | None = Field(
        default=None,
        description="Per-entry api_key (Fernet ciphertext at rest; masked as '[set]' on GET).",
    )
    max_concurrent: int = Field(
        default=1, ge=1, le=64, description="Max in-flight calls against this entry."
    )
    extra_params: dict = Field(
        default_factory=dict,
        description="Extra LiteLLM body params merged in. 'model', 'messages', 'response_format' reserved.",
    )


class IngestionConfig(BaseModel):
    """Per-corpus pipeline config. Frozen after first ingest."""

    # Embedding
    embedding_model: str = Field(
        default="Qwen/Qwen3-Embedding-0.6B", description="Embedding model name"
    )
    embedding_dimension: int = Field(
        default=1024, description="LOCKED — changing requires full re-index"
    )
    embedding_model_id: str = Field(
        default="qwen3-embedding-0.6b-v1",
        description="Stable ID for payloads/migrations",
    )
    embed_mode: Literal["local_st", "modal_tei", "siliconflow"] = Field(
        default="local_st",
        description="Embedding backend: local_st (local sentence-transformers), modal_tei (cloud, Modal), siliconflow (cloud, SiliconFlow API). All must serve the corpus's frozen embedding_model_id at embedding_dimension.",
    )

    # Chunking — tier-aware (see Plan_V3_2.md)
    parent_chunk_tokens: TokenBudget = Field(
        default_factory=lambda: TokenBudget(
            min_tokens=500, target_tokens=1200, max_tokens=2000
        ),
        description="Parent chunk token range",
    )
    child_chunk_tokens: TokenBudget = Field(
        default_factory=lambda: TokenBudget(
            min_tokens=128, target_tokens=500, max_tokens=700
        ),
        description=(
            "Child chunk token range. Cap at 700 to keep extraction prompts "
            "small and stay well under the Qwen3-Embedding-0.6B 1024-token "
            "ceiling. Raising max_tokens above 900 risks silent truncation."
        ),
    )
    chunk_overlap: int = Field(default=200, description="Overlap between parent chunks")
    max_summary_tokens: int = Field(
        default=175, description="Token cap per parent summary (GHOST A)"
    )
    child_chunk_algorithm: Literal["sentence_merge", "semantic_split"] = Field(
        default="sentence_merge",
        description="sentence_merge: split on sentence boundaries → merge to ~300-400 tok. semantic_split: embedding similarity (only if >max_tokens)",
    )
    semantic_split_threshold: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        description="Cosine similarity floor for semantic_split. Sentence pairs below this get split apart. Only applies when child_chunk_algorithm='semantic_split' AND a child exceeds max_tokens.",
    )

    # GHOST A — Summary Model Pool (multi-model; round-robin dispatch)
    summary_models: list[ModelProfileRef] = Field(
        default_factory=lambda: [
            ModelProfileRef(
                provider_preset="ollama",
                model="ollama/llama3.2:3b",
                max_concurrent=1,
            )
        ],
        description=(
            "One or more model profiles for parent summarization (GHOST A). "
            "Calls are round-robined across entries, each bounded by its own max_concurrent."
        ),
    )

    # GHOST B — Extraction Model Pool (multi-model; round-robin dispatch)
    extraction_models: list[ModelProfileRef] = Field(
        default_factory=list,
        description=(
            "Model profiles for entity extraction (GHOST B). Empty when models_linked=true, "
            "in which case the worker reuses summary_models."
        ),
    )
    entity_confidence_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Min confidence to keep extracted entity/relation",
    )

    # GHOST B — Ontology-Lite (Phase 14)
    entity_schema: list[str] | None = Field(
        default=None,
        description=(
            "Allowed entity types (categories). LLM creates instances freely under these types. "
            "None = open extraction (current 4-bucket enum). "
            "'other' is always implicitly available as a fallback."
        ),
    )
    relation_schema: list[str] | None = Field(
        default=None,
        description=(
            "Allowed relation predicates. None = open relations. "
            "'related_to' is always implicitly available as a fallback."
        ),
    )
    schema_strict: Literal["off", "soft", "hard"] = Field(
        default="soft",
        description=(
            "Out-of-schema enforcement: "
            "'off' = no enforcement (schema is a hint). "
            "'soft' = remap unknowns to sentinels ('other' / 'related_to'); preserves the edge/node. "
            "'hard' = drop unknowns entirely. "
            "Ignored when entity_schema and relation_schema are both None."
        ),
    )

    # Feature flags
    use_neo4j: bool = Field(
        default=False, description="Run GHOST B: entity extraction + Neo4j graph"
    )
    chunk_summarization: bool = Field(
        default=False, description="Run GHOST A: summarize parents + embed summaries"
    )
    target_qdrant_collections: list[str] = Field(
        default_factory=lambda: ["naive", "hrag"],
        description="Qdrant collections to write during ingest: naive | hrag | graph",
    )
    docling_ocr_enabled: bool = Field(
        default=True,
        description=(
            "Phase 7.6 — when True, the docling sidecar runs OCR on PDFs / "
            "images during parsing. Set False for text-only PDFs to cut "
            "ingest latency by 50-60%. Other formats ignore this flag."
        ),
    )

    # UI hint — also load-bearing at runtime for the extraction pool.
    # When True: worker reuses summary_models for GHOST B (extraction_models is
    # expected empty and ignored). The UI renders one combined "Summary +
    # Extraction" chip pool. When False: worker uses extraction_models as an
    # independent pool, UI renders two split chip pools.
    models_linked: bool = Field(
        default=True,
        description=(
            "When True, worker reuses summary_models for extraction (GHOST B). "
            "When False, extraction_models is required and used independently."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_single_model(cls, data):
        """
        Migrate legacy single-model config shape to the pool shape.

        Legacy (pre-pool) fields that may still be present on disk:
          summary_model, summary_base_url, summary_api_key,
          summary_extra_params, summary_max_concurrent
          extraction_model, extraction_base_url, extraction_api_key,
          extraction_extra_params, extraction_max_concurrent

        If the list-pool fields are absent but any legacy scalar is present,
        synthesize a one-entry pool from the scalars so existing corpora keep
        running. Also strips the legacy keys so they don't leak into the new
        schema.
        """
        if not isinstance(data, dict):
            return data

        def _build_entry(prefix: str) -> dict | None:
            model = data.get(f"{prefix}_model")
            if not model:
                return None
            entry = {
                "provider_preset": "",
                "model": model,
                "base_url": data.get(f"{prefix}_base_url"),
                "api_key": data.get(f"{prefix}_api_key"),
                "max_concurrent": data.get(f"{prefix}_max_concurrent") or 1,
                "extra_params": data.get(f"{prefix}_extra_params") or {},
            }
            return entry

        # Summary pool migration
        if "summary_models" not in data or not data.get("summary_models"):
            entry = _build_entry("summary")
            if entry is not None:
                data["summary_models"] = [entry]

        # Extraction pool migration
        if "extraction_models" not in data or data.get("extraction_models") is None:
            entry = _build_entry("extraction")
            if entry is not None:
                data["extraction_models"] = [entry]
            else:
                data["extraction_models"] = []

        # Strip legacy scalar fields so Pydantic v2 ignore-extra semantics work
        # whether or not the model is configured to forbid extras.
        for prefix in ("summary", "extraction"):
            for suffix in (
                "model",
                "base_url",
                "api_key",
                "extra_params",
                "max_concurrent",
            ):
                data.pop(f"{prefix}_{suffix}", None)

        return data

    @field_validator("summary_models")
    @classmethod
    def ensure_summary_pool_nonempty(cls, v):
        if not v:
            raise ValueError(
                "summary_models must contain at least one ModelProfileRef."
            )
        return v


class WriteState(BaseModel):
    """Tracks per-store write completion for idempotent resume."""

    mongo_written: bool = False
    qdrant_written: bool = False
    neo4j_written: bool = False


class CorpusCreate(BaseModel):
    """POST /api/corpora request body."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    default_ingestion_config: IngestionConfig = Field(default_factory=IngestionConfig)


class CorpusResponse(BaseModel):
    """Corpus document returned from API."""

    corpus_id: str
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    doc_count: int = 0
    chunk_count: int = 0
    embedding_model_id: str | None = None
    default_ingestion_config: IngestionConfig


class IngestJobResponse(BaseModel):
    """Response from POST /api/corpora/{corpus_id}/ingest."""

    job_id: str
    doc_id: str
    corpus_id: str
    filename: str
    source_tier: str | None = None
    status: str = Field(..., description="queued | processing | done | failed")
    write_state: WriteState = Field(default_factory=WriteState)
    chunk_count: int = 0
    parent_count: int = 0
    error: str | None = None


# ============================================================================
# FILE METADATA MODELS (Phase 10.14)
# ============================================================================


class FileMetadata(BaseModel):
    """Per-file analysis signals produced during quality assessment stage."""

    tier: Literal["tier_a", "tier_b", "tier_b_plus", "tier_c", "ocr_ast"] = Field(
        description="Document structure tier — drives chunking strategy"
    )
    structure_signals: dict[str, Any] = Field(
        default_factory=dict,
        description="Heading count, avg paragraph length, list density, code block count, etc.",
    )
    quality_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence in tier classification and structure detection",
    )
    conversion_method: str = Field(
        default="plain",
        description="How source was converted to markdown: marker | pymupdf4llm | pandoc | plain",
    )
    processing_time_ms: int = Field(
        default=0,
        description="Total processing time for this file in milliseconds",
    )
    page_count: int | None = Field(
        default=None,
        description="Page count for PDF sources, None for plain text / markdown",
    )


class RetrievalTier(str, Enum):
    """Strategy for RAG retrieval — controls which stores are consulted."""

    qdrant_only = "qdrant_only"
    qdrant_mongo = "qdrant_mongo"
    qdrant_mongo_graph = "qdrant_mongo_graph"


class SourceChunk(BaseModel):
    """A retrieved + hydrated chunk returned in chat response."""

    chunk_id: str
    parent_id: str
    doc_id: str
    corpus_id: str
    text: str
    summary: str | None = None
    score: float
    source_tier: str
    corpus_name: str | None = None  # populated during hydration from corpora collection
    doc_name: str | None = None  # basename of source_path, populated during hydration
    heading_path: list[str] | None = None  # populated from Qdrant payload / parent chunk
    # Phase 16.1 — graph expansion provenance: which Entity/predicate surfaced this
    # chunk, with the edge confidence. Only populated for Mode A / Mode B chunks.
    # Each entry: {"entity": str, "confidence": float, "predicate"?: str}
    # Pt 10a (Cluster 5) — entries now also carry surface_form, evidence_phrase,
    # domain_type, canonical_family, entity_type (Mode A) and predicate +
    # relation_family (Mode C only). Fields default to "" / None when unknown.
    provenance: list[dict] | None = None


class SourceFact(BaseModel):
    """A retrieved Fact node from Neo4j — pre-distilled answer unit.

    Pt 10a (Cluster 1) — chat retrieval returns Facts in parallel with chunks.
    Facts bypass the cross-encoder reranker (they're already pre-distilled by
    Ghost B with confidence + evidence_phrase) and feed a separate
    [Key Facts] section in the LLM prompt ahead of the chunk-text section.

    Fields mirror the Fact node's Neo4j shape (see
    services/graph/neo4j_writer.py:1586). `chunk_id` / `doc_id` point at the
    supporting source so citations can resolve to a normal SourceChunk.
    """

    fact_id: str
    subject: str
    fact_type: str
    property_name: str | None = None
    value: str | None = None
    unit: str | None = None
    condition: str | None = None
    confidence: float = 0.0
    evidence_phrase: str | None = None
    chunk_id: str | None = None
    doc_id: str | None = None
    corpus_id: str | None = None


class RetrievalResult(BaseModel):
    """Retriever output — chunks plus tier-intersection metadata for the orchestrator."""

    chunks: list[SourceChunk] = Field(default_factory=list)
    # Pt 10a (Cluster 1) — Facts retrieved in parallel with chunks, bypass
    # reranker, feed a separate [Key Facts] LLM prompt section. Empty when
    # Neo4j is disabled or no facts match the seed entities.
    facts: list[SourceFact] = Field(default_factory=list)
    requested_tier: RetrievalTier
    effective_tier: RetrievalTier
    downgrade_reason: str | None = Field(
        default=None,
        description="Populated when the requested tier was downgraded at strategy intersection time",
    )


class EntitySearchRequest(BaseModel):
    """POST /api/graph/entity-search — Mode B entity-first retrieval."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(..., min_length=1, description="Entity substring or free text")
    corpus_ids: list[str] | None = Field(
        default=None, description="Scope to these corpora (max 3)"
    )
    limit: int = Field(default=20, ge=1, le=100)
    hydrate: bool = Field(
        default=True,
        description="Fetch parent text + corpus_name + doc_name from MongoDB",
    )

    @field_validator("corpus_ids")
    @classmethod
    def cap_corpora(cls, v):
        if v is not None and len(v) > 3:
            raise ValueError("Maximum 3 corpora per entity search.")
        return v


class EntitySearchResponse(BaseModel):
    """Mode B entity-first search result."""

    chunks: list[SourceChunk] = Field(default_factory=list)
    neo4j_enabled: bool = True


# ============================================================================
# EXTRACTION MODELS (Phase 9)
# ============================================================================


class EntityResult(BaseModel):
    """Entity extracted from a chunk and stored in Neo4j."""

    entity_id: str
    normalized_name: str
    display_name: str
    entity_type: str
    confidence: float
    mention_count: int


class RelationEdge(BaseModel):
    """RELATES_TO edge between two entities."""

    subject_id: str
    subject_name: str
    predicate: str
    object_id: str
    object_name: str
    confidence: float


class ChunkExtractionResponse(BaseModel):
    """All entities and relations extracted from a single chunk."""

    chunk_id: str
    corpus_id: str
    entities: list[EntityResult]
    relations: list[RelationEdge]


class DocExtractionItem(BaseModel):
    """Per-chunk extraction count summary for a document."""

    chunk_id: str
    entity_count: int
    relation_count: int


# ── Phase 17 Wave 1 — Graph Discovery Query ─────────────────────────────────


class GraphQueryRequest(BaseModel):
    """POST /api/graph/query body — Agent Query backend call from GraphView."""

    corpus_id: str = Field(..., description="Corpus to scope the discovery to")
    query: str = Field(..., min_length=1, description="Free-text query; tokens matched against Entity names")
    max_hops: int = Field(default=2, ge=1, le=3, description="Entity→Entity traversal depth from seeds")
    limit: int = Field(default=50, ge=1, le=200, description="Max nodes in returned subgraph")


class GraphQueryNode(BaseModel):
    id: str
    display_name: str
    entity_type: str = "other"
    mention_count: int = 0
    is_seed: bool = False


class GraphQueryLink(BaseModel):
    source: str
    target: str
    predicate: str = "related_to"
    confidence: float = 0.5


class GraphBridge(BaseModel):
    entity_id: str
    display_name: str
    entity_type: str = "other"
    connected_seed_count: int
    connected_seeds: list[str] = Field(default_factory=list)


class GraphHub(BaseModel):
    entity_id: str
    display_name: str
    entity_type: str = "other"
    degree: int
    is_seed: bool = False


class GraphGap(BaseModel):
    entity_a_id: str
    entity_a_name: str
    entity_b_id: str
    entity_b_name: str


class GraphQueryResponse(BaseModel):
    """POST /api/graph/query response — the agent console's data payload."""

    nodes: list[GraphQueryNode]
    links: list[GraphQueryLink]
    bridges: list[GraphBridge] = Field(default_factory=list)
    hubs: list[GraphHub] = Field(default_factory=list)
    gaps: list[GraphGap] = Field(default_factory=list)
    seed_entities: list[GraphQueryNode] = Field(
        default_factory=list,
        description="Entities the query tokens matched as seeds (for UI highlight)",
    )


# ── Phase 17 Wave 2 — Discourse Graph ───────────────────────────────────────


class DiscourseNode(BaseModel):
    """A lexeme node in the corpus discourse co-occurrence graph."""

    id: str = Field(..., description="Lowercased term (node id + label)")
    label: str
    freq: int = Field(..., description="Corpus-wide term frequency")
    type: str = Field(default="lexeme", description="Always 'lexeme' — distinguishes from entity nodes in Split mode")
    cluster: int | None = Field(default=None, description="Cluster id from greedy modularity")


class DiscourseLink(BaseModel):
    """Weighted co-occurrence edge between two lexemes."""

    source: str
    target: str
    weight: int = Field(..., description="Number of chunks both terms appeared in")


class DiscourseGraph(BaseModel):
    nodes: list[DiscourseNode]
    links: list[DiscourseLink]


class DiscourseCluster(BaseModel):
    cluster_id: int
    size: int
    top_terms: list[str] = Field(default_factory=list, description="Up to 5 highest-frequency terms in the cluster")


class DiscourseBridge(BaseModel):
    """Lexeme with high betweenness centrality whose neighbors span ≥2 clusters."""

    term: str
    centrality: float
    connects_clusters: list[int] = Field(default_factory=list)
    degree: int


class DiscourseGap(BaseModel):
    """Cluster pair with missing (DISCONNECTED) or weak (THIN) bridging vocabulary."""

    cluster_a: int
    cluster_b: int
    bridging_words: list[str] = Field(default_factory=list)
    bridging_count: int
    severity: str = Field(..., description="DISCONNECTED | THIN")
    interpretation: str


class DiscourseShape(BaseModel):
    shape: str = Field(..., description="CONCENTRATED | SKEWED | DISPERSED | BALANCED | EMPTY")
    shape_description: str
    gini_coefficient: float
    cluster_proportions: dict[int, float] = Field(default_factory=dict)
    dominant_cluster: int | None = None
    dominant_percentage: float = 0.0
    top_words_by_degree: list[dict[str, Any]] = Field(default_factory=list)


class DiscourseGraphResponse(BaseModel):
    """GET /api/corpora/{corpus_id}/discourse response."""

    graph: DiscourseGraph
    chunk_count: int = Field(..., description="Chunks scanned to build the graph")
    clusters: list[DiscourseCluster] = Field(default_factory=list)
    bridges: list[DiscourseBridge] = Field(default_factory=list)
    gaps: list[DiscourseGap] = Field(default_factory=list)
    shape: DiscourseShape


# ── Phase 17 Wave 3 — Graph analyzer (LLM structural synthesis) ─────────────


class AnalyzerKnowledgeSnapshot(BaseModel):
    """Client-side snapshot of the knowledge canvas sent for analysis."""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    seed_ids: list[str] = Field(default_factory=list)


class AnalyzerDiscourseSnapshot(BaseModel):
    """Client-side snapshot of the discourse canvas sent for analysis."""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    bridges: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    shape: dict[str, Any] = Field(default_factory=dict)


class GraphAnalyzeRequest(BaseModel):
    """POST /api/graph/analyze body."""

    corpus_id: str
    mode: str = Field(..., description="knowledge | discourse | split")
    query: str | None = Field(
        default=None,
        description="Optional user query for knowledge/split mode narration",
    )
    model: str | None = Field(
        default=None,
        description="Override model for the synthesis call (defaults to server's default chat model)",
    )
    knowledge: AnalyzerKnowledgeSnapshot | None = None
    discourse: AnalyzerDiscourseSnapshot | None = None


class SplitOverlayAlignment(BaseModel):
    intersection: list[str] = Field(default_factory=list)
    intersection_size: int = 0
    union_size: int = 0
    score: float = 0.0
    entities_present_as_lexemes: list[str] = Field(default_factory=list)
    entities_absent_from_lexemes: list[str] = Field(default_factory=list)


class SplitOverlay(BaseModel):
    """Merged entity+lexeme canvas produced by `compute_split_overlay`."""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    alignment: SplitOverlayAlignment = Field(default_factory=SplitOverlayAlignment)
    crosslinks_count: int = 0


class GraphAnalyzeResponse(BaseModel):
    """POST /api/graph/analyze response."""

    mode: str
    markdown: str = Field(..., description="LLM narrative explaining the structure")
    structural_summary: dict[str, Any] = Field(
        default_factory=dict,
        description="Pre-computed structural features that fed the prompt",
    )
    overlay: SplitOverlay | None = Field(
        default=None,
        description="Only present for mode='split' — merged canvas + alignment",
    )
    handoff_prompt: str = Field(
        ...,
        description="Prose to inject into a new chat turn when the user clicks '→ Ask Chat'",
    )


# ============================================================================
# ERROR MODELS
# ============================================================================


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str = Field(..., description="Error message")
    detail: str | None = Field(default=None, description="Detailed error info")
    code: str | None = Field(default=None, description="Error code")


# ============================================================================
# TOOL MODELS
# ============================================================================


class ToolBase(BaseModel):
    """Base fields for a custom tool."""

    name: str = Field(..., description="Function name (e.g. get_weather)")
    description: str = Field(..., description="Description for the LLM")
    parameters: dict[str, Any] = Field(..., description="JSON Schema for parameters")
    code: str = Field(..., description="Raw Python script")
    enabled: bool = Field(default=True)


class ToolCreate(ToolBase):
    """POST /api/tools request body."""

    pass


class ToolUpdate(BaseModel):
    """PATCH /api/tools/{id} request body."""

    name: str | None = None
    description: str | None = None
    parameters: dict[str, Any] | None = None
    code: str | None = None
    enabled: bool | None = None


class Tool(ToolBase):
    """Tool response model."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")

    @field_validator("id", mode="before")
    @classmethod
    def convert_objectid_to_str(cls, v):
        if isinstance(v, ObjectId):
            return str(v)
        return str(v)


# ============================================================================
# AUTH MODELS
# ============================================================================


class LoginRequest(BaseModel):
    """POST /api/auth/login request body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(..., min_length=1, description="Username")
    password: str = Field(..., min_length=1, description="Password")


class UserPublic(BaseModel):
    """Public user data returned in responses (never includes password hash)."""

    id: str = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    created_at: datetime = Field(..., description="Account creation timestamp")

    @field_validator("id", mode="before")
    @classmethod
    def convert_objectid_to_str(cls, v):
        if isinstance(v, ObjectId):
            return str(v)
        return str(v)


class LoginResponse(BaseModel):
    """POST /api/auth/login response."""

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type")
    user: UserPublic = Field(..., description="Authenticated user info")


class UserMeResponse(BaseModel):
    """GET /api/auth/me response."""

    id: str = Field(..., description="User ID")
    username: str = Field(..., description="Username")
    created_at: datetime = Field(..., description="Account creation timestamp")

    @field_validator("id", mode="before")
    @classmethod
    def convert_objectid_to_str(cls, v):
        if isinstance(v, ObjectId):
            return str(v)
        return str(v)


class UpdateCredentialsRequest(BaseModel):
    """PATCH /api/auth/update request body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    current_password: str = Field(
        ..., min_length=1, description="Current password for verification"
    )
    new_username: str | None = Field(
        default=None, min_length=2, max_length=50, description="New username (optional)"
    )
    new_password: str | None = Field(
        default=None,
        min_length=6,
        max_length=128,
        description="New password (optional)",
    )

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v):
        """Ensure new password is not empty whitespace."""
        if v is not None and not v.strip():
            raise ValueError("New password cannot be blank")
        return v


class UpdateCredentialsResponse(BaseModel):
    """PATCH /api/auth/update response — returns fresh token if credentials changed."""

    success: bool = Field(..., description="Whether the update succeeded")
    access_token: str = Field(..., description="New JWT token (refreshed)")
    user: UserPublic = Field(..., description="Updated user info")


class TokenData(BaseModel):
    """JWT payload data extracted from token."""

    user_id: str = Field(..., description="User ID from token subject")
    username: str = Field(..., description="Username at time of token issuance")
    exp: datetime | None = Field(default=None, description="Token expiration")


# ── Phase 19.3 — Custom Model Profiles (Agent-Zero-style) ───────────────


class ModelProfileCreate(BaseModel):
    """POST /api/model-profiles body. Plaintext api_key is encrypted at rest."""

    label: str = Field(..., min_length=1, max_length=120)
    base_url: str = Field(..., min_length=8, max_length=500,
                          description="OpenAI-compatible base URL")
    model_name: str = Field(..., min_length=1, max_length=200)
    api_key: str = Field("", description="Plaintext key; encrypted before storage.")
    extra_params: dict = Field(
        default_factory=dict,
        description="Merged into LiteLLM request body (e.g. temperature, top_p).",
    )


class ModelProfileUpdate(BaseModel):
    """PUT /api/model-profiles/{id} partial patch. api_key '' or None = unchanged."""

    label: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=8, max_length=500)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(
        default=None,
        description="Plaintext key; '' or omitted = leave existing ciphertext unchanged.",
    )
    extra_params: dict | None = None


class ModelProfilePublic(BaseModel):
    """Masked view returned to the frontend. Never carries plaintext keys."""

    profile_id: str
    label: str
    base_url: str
    model_name: str
    api_key_masked: str
    extra_params: dict = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class ModelProfilesListResponse(BaseModel):
    """GET /api/model-profiles response."""

    profiles: list[ModelProfilePublic]


class ModelProfileTestResult(BaseModel):
    """POST /api/model-profiles/{id}/test response."""

    ok: bool
    status: int | None = None
    latency_ms: int | None = None
    error: str | None = None


# ── Phase E — Unified Model Pool ────────────────────────────────────────


class ModelPoolEntryCreate(BaseModel):
    """POST /api/model-pool body."""

    label: str = Field(..., min_length=1, max_length=140)
    provider: str = Field(
        ..., min_length=1, max_length=60,
        description="Lowercase provider key; matches API Keys registry slots."
    )
    base_url: str = Field(..., min_length=8, max_length=500)
    model_name: str = Field(..., min_length=1, max_length=200)
    api_key: str = Field(
        "", description="Plaintext; ignored when use_shared_key=True."
    )
    use_shared_key: bool = Field(
        default=False,
        description="Pull key from the API Keys registry by `provider` instead "
                    "of storing it on this entry.",
    )
    extra_params: dict = Field(default_factory=dict)
    context_length: int | None = None
    tags: list[str] = Field(
        default_factory=lambda: ["chat"],
        description="e.g. chat, embedding, reasoning, coding",
    )
    enabled: bool = True


class ModelPoolEntryUpdate(BaseModel):
    """PUT /api/model-pool/{id} — partial patch. api_key blank = unchanged."""

    label: str | None = Field(default=None, min_length=1, max_length=140)
    provider: str | None = Field(default=None, min_length=1, max_length=60)
    base_url: str | None = Field(default=None, min_length=8, max_length=500)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = None
    use_shared_key: bool | None = None
    extra_params: dict | None = None
    context_length: int | None = None
    tags: list[str] | None = None
    enabled: bool | None = None


class ModelPoolEntryPublic(BaseModel):
    """Masked public shape — frontend never sees plaintext or ciphertext."""

    entry_id: str
    label: str
    provider: str
    base_url: str
    model_name: str
    api_key_masked: str
    use_shared_key: bool = False
    extra_params: dict = Field(default_factory=dict)
    context_length: int | None = None
    tags: list[str] = Field(default_factory=lambda: ["chat"])
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class ModelPoolListResponse(BaseModel):
    entries: list[ModelPoolEntryPublic]


class ModelPoolTestResult(BaseModel):
    ok: bool
    status: int | None = None
    latency_ms: int | None = None
    error: str | None = None


# ── Phase F — User Query Preferences ──────────────────────────────────────
# One doc per user mapping the 3 query-time roles (HyDE / Agentic / default
# Query) to entries in the existing `model_pool` collection, plus per-user
# Ollama model exclusions. Chips themselves live in model_pool — this doc
# stores ONLY pool entry_id references.

class QueryPrefsResponse(BaseModel):
    user_id: str
    hyde_pool_id: str | None = None
    agentic_pool_id: str | None = None
    query_pool_id: str | None = None
    ollama_exclusions: list[str] = []
    updated_at: str | None = None


class QueryPrefsUpdate(BaseModel):
    """Partial update — unset fields are preserved. Pass an empty list to
    ollama_exclusions to clear; pass JSON `null` to a *_pool_id to unset.
    """
    hyde_pool_id: str | None = None
    agentic_pool_id: str | None = None
    query_pool_id: str | None = None
    ollama_exclusions: list[str] | None = None


# ── Forward-reference resolution ─────────────────────────────────────────
# ChatRequest (line ~72) references RetrievalTier (defined ~line 597) — Pydantic v2
# can't resolve the forward ref at class-body time. Rebuild after both are in scope.
ChatRequest.model_rebuild()
