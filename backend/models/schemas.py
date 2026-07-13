# backend/models/schemas.py
# Recovery shim plus additive Mission Control v2 response models.
#
# The working-tree schema module was newer than the Git baseline when this
# refactor started. To avoid regressing those models, load the preserved
# compiled module and then layer the new backward-compatible fields below.

from __future__ import annotations

import sys
import types
from datetime import datetime
from typing import Any, Literal

try:
    import bson as _bson  # noqa: F401
except ModuleNotFoundError:  # local tooling may not have pymongo installed
    _bson_stub = types.ModuleType("bson")

    class ObjectId(str):
        @staticmethod
        def is_valid(_value: object) -> bool:
            return True

    _bson_stub.ObjectId = ObjectId
    sys.modules["bson"] = _bson_stub

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import _schemas_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


class ModelOverrides(_legacy.ModelOverrides):
    """Current per-request override shape."""

    max_tokens: int | None = Field(default=None, ge=1, le=384000)
    disabled_lexicon_ids: list[str] | None = Field(
        default=None,
        max_length=64,
        description=(
            "Scoped corpus-vocabulary expansions to suppress for this turn. "
            "This changes query planning only and never edits corpus data."
        ),
    )
    web_search_enabled: bool | None = Field(
        default=None,
        description=(
            "When true, this chat turn may append live web results from the "
            "configured SearXNG instance. Off/None keeps chat corpus-only."
        ),
    )
    web_fetch_depth: Literal["snippets", "normal", "deep"] | None = Field(
        default=None,
        description=(
            "Per-turn live-web fetch depth. snippets keeps search snippets only; "
            "normal allows raw/static page extraction; deep also allows the "
            "Obscura JS-render fallback for allowlisted domains."
        ),
    )
    web_research_mode: bool | None = Field(
        default=None,
        description="When true, expand bounded web budgets for deeper research turns.",
    )
    web_youtube_transcripts: bool | None = Field(
        default=None,
        description=(
            "When true, YouTube URLs may be fetched as transcript evidence via "
            "yt-dlp without downloading video."
        ),
    )
    web_max_sources: int | None = Field(
        default=None,
        ge=3,
        le=20,
        description="Maximum web evidence sources requested for this turn.",
    )


class ModelConfig(_legacy.ModelConfig):
    """Current persisted conversation model config shape."""

    max_tokens: int = Field(default=16384, ge=1, le=384000)


class ChatRequest(_legacy.ChatRequest):
    """Current chat request shape.

    The preserved legacy schema is still the base contract, but the live
    orchestrator also reads Phase 24 fields. Keeping them here prevents a
    validated request from crashing later with AttributeError.
    """

    active_skill_ids: list[str] | None = Field(
        default=None,
        description="Skill IDs to inject for this chat turn.",
    )
    reasoning_cascade: bool | None = Field(
        default=None,
        description="Run the optional reasoning-cascade pre-digest for this turn.",
    )
    overrides: ModelOverrides | None = Field(
        default=None,
        description="Per-request model, retrieval, reasoning, and web-search overrides.",
    )


class ChatChunk(_legacy.ChatChunk):
    """Current SSE chunk shape emitted by the chat orchestrator."""

    trace_event: dict[str, Any] | None = Field(
        default=None,
        description="Structured execution trace lane event for durable UI logs.",
    )
    chunks_returned: int | None = None
    # Deterministic count of graph facts actually seeded into retrieval — the
    # real number from the retrieval result (len of the seeded facts), never an
    # LLM-authored value. Mirrors chunks_returned for a trustworthy UI counter.
    facts_seeded: int | None = None
    strategy_used: str | None = None
    query_profile_used: str | None = None
    reasoning_mode_used: str | None = None
    hyde_applied: bool | None = None
    agentic_mode_used: bool | None = None
    downgrade_reason: str | None = None
    collections_queried: list[str] | None = None
    skills_used: list[str] | None = None
    tools_used: list[str] | None = None
    reasoning_cascade_applied: bool | None = None


class ChatMessage(_legacy.ChatMessage):
    """Current persisted chat message shape."""

    trace_events: list[dict[str, Any]] = Field(default_factory=list)
    chunks_returned: int | None = None
    facts_seeded: int | None = None
    strategy_used: str | None = None
    query_profile_used: str | None = None
    reasoning_mode_used: str | None = None
    hyde_applied: bool | None = None
    agentic_mode_used: bool | None = None
    downgrade_reason: str | None = None
    skills_used: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    reasoning_cascade_applied: bool | None = None
    sources: list[dict[str, Any]] | None = None


class Conversation(_legacy.Conversation):
    """Current conversation response shape with current message fields."""

    model_config_conversation: ModelConfig = Field(
        default_factory=ModelConfig,
        alias="model_config",
    )
    messages: list[ChatMessage] = Field(default_factory=list)


def _universal_entity_schema() -> list[str]:
    """Lazy accessor for ghost_b.UNIVERSAL_ENTITY_SCHEMA. Importing ghost_b
    eagerly at module load would create a services↔models cycle, so the
    import lives inside the call site and the function is only invoked from
    the IngestionConfig field defaults at instantiation time."""
    from services.ghost_b import UNIVERSAL_ENTITY_SCHEMA

    return UNIVERSAL_ENTITY_SCHEMA


def _universal_relation_schema() -> list[str]:
    from services.ghost_b import UNIVERSAL_RELATION_SCHEMA

    return UNIVERSAL_RELATION_SCHEMA


class IngestionConfig(BaseModel):
    """Source-backed ingestion config for the current pipeline."""

    model_config = ConfigDict(extra="ignore")

    embedding_model: str = Field(default="Qwen/Qwen3-Embedding-0.6B")
    embedding_dimension: int = Field(default=1024)
    embedding_model_id: str = Field(default="qwen3-embedding-0.6b-v1")

    embed_mode: Literal[
        "local",
        "api",
        "modal",
        "local_st",
        "modal_tei",
        "siliconflow",
    ] = Field(default="local")
    embed_base_url: str | None = None
    embed_api_key: str | None = None
    embed_max_concurrent: int | None = Field(default=None, ge=1, le=64)
    embedding_models: list[_legacy.ModelProfileRef] = Field(default_factory=list)
    modal_containers: int | None = Field(default=None, ge=1, le=32)

    parent_chunk_tokens: _legacy.TokenBudget = Field(
        default_factory=lambda: _legacy.TokenBudget(
            min_tokens=500, target_tokens=1200, max_tokens=2000
        )
    )
    child_chunk_tokens: _legacy.ChildTokenBudget = Field(
        default_factory=lambda: _legacy.ChildTokenBudget(
            min_tokens=64, target_tokens=128, max_tokens=256
        )
    )
    chunk_overlap: int = Field(default=200)
    max_summary_tokens: int = Field(default=175)
    child_chunk_algorithm: Literal["sentence_merge", "semantic_split"] = Field(
        default="semantic_split",
        description=(
            "Child-chunk splitter. 'semantic_split' (default for NEW corpora): "
            "one child per paragraph/idea — finer, single-idea retrieval units. "
            "'sentence_merge': legacy paragraph-packing. Existing corpora keep "
            "their frozen value, so already-ingested data is unchanged."
        ),
    )
    semantic_split_threshold: float = Field(default=0.65, ge=0.0, le=1.0)

    summary_models: list[_legacy.ModelProfileRef] = Field(default_factory=list)
    extraction_models: list[_legacy.ModelProfileRef] = Field(default_factory=list)
    entity_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    models_linked: bool = False
    # Per-corpus extraction contract. New production extraction uses
    # provider-card LLM lanes in extraction_models/summary_models. "local" now
    # means a local/private OpenAI-compatible provider endpoint (RTX/vLLM),
    # while "legacy_local" is the deprecated GLiNER/GLiREL sidecar path.
    # "inherit" = legacy fallback to the global Settings engine; the lifespan
    # migration stamps existing corpora explicit so the resolved workflow is
    # deterministic per corpus. Resolution + fail-fast validation:
    # services/ingestion/extraction_contract.py.
    extraction_engine: Literal[
        "inherit",
        "off",
        "local",
        "cloud",
        "runpod_flash",
        "legacy_local",
        "dual",
        "local_then_cloud",
        "local_then_enrich",
    ] = Field(default="inherit")

    # Default to the universal vocab so freshly-instantiated configs match
    # what the lifespan migration patches existing corpora to. Ghost B's
    # UNIVERSAL_*_SCHEMA constants are imported lazily to avoid a circular
    # services↔models import at module load. `default_factory` clones the
    # list so per-corpus mutations never mutate the module-level vocab.
    entity_schema: list[str] | None = Field(
        default_factory=lambda: list(_universal_entity_schema())
    )
    relation_schema: list[str] | None = Field(
        default_factory=lambda: list(_universal_relation_schema())
    )
    schema_strict: Literal["off", "soft", "hard"] = "soft"

    use_neo4j: bool = True
    chunk_summarization: bool = False
    target_qdrant_collections: list[str] = Field(
        default_factory=lambda: ["naive", "hrag", "graph"]
    )
    docling_ocr_enabled: bool = Field(
        default=False,
        description="Deprecated policy flag. OCR is disabled; true values are ignored by the worker.",
    )
    preset: str = Field(default="balanced")

    @field_validator("embed_mode", mode="before")
    @classmethod
    def normalize_embed_mode(cls, value):
        aliases = {
            "local_st": "local",
            "modal_tei": "modal",
            "siliconflow": "api",
        }
        return aliases.get(value, value)

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_single_models(cls, data):
        if not isinstance(data, dict):
            return data

        def _build_entry(prefix: str) -> dict | None:
            model = data.get(f"{prefix}_model")
            if not model:
                return None
            return {
                "provider_preset": "",
                "model": model,
                "base_url": data.get(f"{prefix}_base_url"),
                "api_key": data.get(f"{prefix}_api_key"),
                "max_concurrent": data.get(f"{prefix}_max_concurrent") or 1,
                "extra_params": data.get(f"{prefix}_extra_params") or {},
            }

        if not data.get("summary_models"):
            entry = _build_entry("summary")
            if entry is not None:
                data["summary_models"] = [entry]
        if data.get("extraction_models") is None:
            data["extraction_models"] = []
        if not data.get("extraction_models"):
            entry = _build_entry("extraction")
            if entry is not None:
                data["extraction_models"] = [entry]

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


class WriteState(BaseModel):
    """Tracks per-store write completion and non-fatal ingest warnings."""

    mongo_written: bool = False
    qdrant_written: bool = False
    summaries_indexed: bool = False
    # Writer-intent stamp (2026-07-04): how many summary vectors THIS ingest
    # actually decided to write. The verifier prefers this over re-deriving
    # from Mongo parents — the summary-tree HEAL guard writes parent
    # summaries AFTER the qdrant phase, so Mongo-derived expectations drift
    # (tiny-doc receipt: successful ingest flagged "expected=1 summary
    # vectors but ... has 0"). None = legacy doc / write failed -> verifier
    # falls back to the strict Mongo derivation.
    summary_points: int | None = None
    neo4j_written: bool = False
    warnings: list[str] = Field(default_factory=list)
    verified: bool | None = None
    verify_errors: list[str] = Field(default_factory=list)


class IngestJobResponse(_legacy.IngestJobResponse):
    """Override the legacy class so the `write_state` annotation binds to the
    extended `WriteState` defined above (warnings / verified / verify_errors).
    Without this override, routers passing a new-style WriteState instance hit
    a Pydantic ValidationError because the legacy field expects the 3-field
    legacy WriteState class."""

    write_state: WriteState = Field(default_factory=WriteState)


class CorpusCreate(_legacy.CorpusCreate):
    default_ingestion_config: IngestionConfig = Field(default_factory=IngestionConfig)


class CorpusResponse(_legacy.CorpusResponse):
    default_ingestion_config: IngestionConfig
    readiness: dict[str, Any] | None = None


class ToolBase(BaseModel):
    """Source-backed custom tool model.

    The legacy source predates slash-command support. Define the current shape
    here so clean rebuilds do not depend on an ignored compiled schema module.
    """

    name: str = Field(..., description="Function name exposed to the LLM")
    description: str = Field(..., description="Description for the LLM")
    parameters: dict[str, Any] = Field(..., description="JSON schema parameters")
    code: str = Field(..., description="Sandboxed Python source")
    enabled: bool = True
    slash_command: str | None = None


class ToolCreate(ToolBase):
    pass


class ToolUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parameters: dict[str, Any] | None = None
    code: str | None = None
    enabled: bool | None = None
    slash_command: str | None = None


class Tool(ToolBase):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")

    @field_validator("id", mode="before")
    @classmethod
    def convert_objectid_to_str(cls, value):
        return str(value)


class SkillBase(BaseModel):
    """Source-backed custom skill model for Settings → Skills."""

    name: str = Field(..., description="Skill display name")
    description: str = Field(..., description="Short skill description")
    instructions: str = Field(..., description="Instructions injected into chat")
    enabled: bool = True
    slash_command: str | None = None


class SkillCreate(SkillBase):
    pass


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    enabled: bool | None = None
    slash_command: str | None = None


class Skill(SkillBase):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")

    @field_validator("id", mode="before")
    @classmethod
    def convert_objectid_to_str(cls, value):
        return str(value)


def _utc_iso() -> str:
    return datetime.utcnow().isoformat()


class QueryModelPoolEntry(BaseModel):
    """Unified chat/analysis model pool entry stored under settings.models."""

    model_config = ConfigDict(extra="allow")

    entry_id: str = ""
    label: str = ""
    provider: str = "custom"
    base_url: str | None = None
    api_key_ciphertext: str | None = None
    model_name: str = ""
    source: Literal["ollama", "cloud"] = "cloud"
    enabled: bool = True
    created_at: str = Field(default_factory=_utc_iso)


class HydeConfig(BaseModel):
    default_enabled: bool = False
    pool_entry_id: str | None = None


class AgenticConfig(BaseModel):
    default_enabled: bool = False
    pool_entry_id: str | None = None


class ReasoningConfig(BaseModel):
    default_enabled: bool = False
    pool_entry_id: str | None = None


class UtilityConfig(BaseModel):
    default_enabled: bool = False
    pool_entry_id: str | None = None


class GraphQueryConfig(BaseModel):
    pool_entry_id: str | None = None


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query_model_pool: list[QueryModelPoolEntry] = Field(default_factory=list)
    hyde: HydeConfig = Field(default_factory=HydeConfig)
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    utility: UtilityConfig = Field(default_factory=UtilityConfig)
    graph_query: GraphQueryConfig = Field(default_factory=GraphQueryConfig)


class UtilityModelTestResult(BaseModel):
    ok: bool
    status: str
    model: str | None = None
    latency_ms: int
    output_preview: str | None = None
    error: str | None = None


class OllamaBulkAddRequest(BaseModel):
    model_names: list[str] = Field(default_factory=list)


class ChatLLMSettings(_legacy.ChatLLMSettings):
    query_profile: Literal["fast", "balanced", "thorough", "custom"] = "balanced"


class RetrievalSettings(_legacy.RetrievalSettings):
    top_k_summary: int = Field(default=20, ge=0, le=100)
    max_corpora_per_query: int = Field(default=32, ge=1, le=100)
    final_top_k: int = Field(default=8, ge=1, le=50)
    source_cap: int = Field(
        default=8,
        ge=1,
        le=50,
        description=(
            "Max distinct source documents in the final context set "
            "(select_facet_final source_cap). Higher = broader cross-document "
            "coverage; bounded in practice by final_top_k (chunks sent to the "
            "LLM). Drives both the chat coverage cap and the graph/Mission-Control "
            "semantic-facet cap."
        ),
    )
    evidence_plan_llm_decompose: bool = Field(
        default=False,
        description=(
            "When on, a multi-document question that the deterministic concept "
            "detector and the no-LLM heuristic splitter both fail to decompose "
            "is sent to a small LLM (the HyDE route) to name its evidence sides. "
            "Adds one short model call to such queries; off by default. The "
            "grounded, no-LLM decomposition always runs regardless of this flag."
        ),
    )


class ExtractionEndpoint(BaseModel):
    """One Ghost B extraction sidecar a user can toggle on/off.

    The worker health-probes ENABLED endpoints per document (preference
    order = list order) and dispatches slices to the live ones — so a GPU
    box can be powered off without any config change: work just flows to
    whatever is on (e.g. the always-on local sidecar)."""

    label: str = Field(default="", max_length=60)
    url: str = Field(default="", max_length=300)
    enabled: bool = True


class ExtractionSettings(BaseModel):
    """Where local Ghost B extraction runs. Defaults seed from
    LOCAL_GHOST_B_EXTRACT_URL so existing deployments see their current
    wiring in the UI; edits persist in Mongo and apply on the next ingest
    without a backend restart (same pattern as Modal settings)."""

    engine: Literal[
        "off",
        "local",
        "cloud",
        "runpod_flash",
        "legacy_local",
        "local_then_cloud",
        "dual",
        "local_then_enrich",
    ] = Field(
        default="local",
        description=(
            "Which Ghost B engine runs extraction: 'local' = local/private "
            "provider-card LLM endpoint such as RTX/vLLM; 'cloud' = remote "
            "provider-card LLM pool; 'legacy_local' = deprecated "
            "GLiNER/GLiREL sidecars; 'dual' = legacy local plus provider LLM "
            "pool; 'local_then_cloud' and 'local_then_enrich' are "
            "transitional legacy-local modes. Use 'off' for vectors-only."
        ),
    )
    endpoints: list[ExtractionEndpoint] = Field(default_factory=list)


class RunpodFlashAccount(BaseModel):
    """One Runpod account for multi-account burst routing (P2.7c).

    Each API key is a distinct Runpod account with its own serverless
    endpoint, quota, and billing; batch-level routing across accounts makes
    combined burst throughput the sum of the accounts. The API key is
    deliberately absent here (same house rule as
    ``RunpodFlashExtractionSettings``): each account's key lives in the
    encrypted shared-key store under ``api_keys.runpod_accounts.<name>`` and
    is resolved only at dispatch time.
    """

    name: str = Field(max_length=60)
    endpoint_id: str = Field(max_length=120)
    embed_endpoint_id: str = Field(
        default="",
        max_length=120,
        description=(
            "Optional Runpod serverless endpoint id for the burst EMBEDDING "
            "worker (runpod_flash_embedder, P1.8). Accounts without it are "
            "skipped by embed mode 'runpod'; extraction routing ignores it."
        ),
    )
    enabled: bool = True
    max_workers: int = Field(default=8, ge=1, le=64)
    request_concurrency: int = Field(default=8, ge=1, le=64)
    weight: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="Least-in-flight tiebreaker: higher weight wins ties.",
    )


class RunpodFlashExtractionSettings(BaseModel):
    """Runpod Flash burst lane for joint GLiNER-Relex extraction.

    The API key is deliberately absent. It lives in the encrypted shared-key
    store under ``api_keys.runpod`` and is resolved only at dispatch time.
    Flash workers are stateless inference workers: they never receive database
    credentials and never write MongoDB, Qdrant, or Neo4j directly.
    """

    enabled: bool = False
    endpoint_id: str = Field(default="", max_length=120)
    accounts: list[RunpodFlashAccount] = Field(
        default_factory=list,
        description=(
            "Additive multi-account routing rows (P2.7c). Empty keeps the "
            "legacy single endpoint_id + api_keys.runpod pair as the implicit "
            "'default' account."
        ),
    )
    endpoint_name: str = Field(default="polymath-gliner-relex", max_length=120)
    model_id: str = Field(
        default="knowledgator/gliner-relex-large-v0.5",
        max_length=240,
    )
    model_revision: str = Field(
        default="9c4171ae1e690fc29b87f33579e50bcd65faf2cc",
        max_length=80,
        description="Pinned Hugging Face revision; empty opts into the repository default.",
    )
    spacy_pipeline: str = Field(default="blank:en", max_length=80)
    min_workers: int = Field(default=0, ge=0, le=64)
    max_workers: int = Field(default=8, ge=1, le=64)
    worker_max_concurrency: int = Field(default=1, ge=1, le=8)
    # Auto-repair polls every 120 seconds by default. Keep burst workers warm
    # across adjacent durable slices while still scaling to zero promptly.
    idle_timeout_seconds: int = Field(default=180, ge=5, le=3600)
    scaler_value: int = Field(
        default=1,
        ge=1,
        le=100,
        description=(
            "REQUEST_COUNT jobs-per-worker target used when the Flash endpoint "
            "is deployed. Lower values scale out more aggressively."
        ),
    )
    request_batch_size: int = Field(default=32, ge=1, le=128)
    request_concurrency: int = Field(default=8, ge=1, le=64)
    timeout_seconds: int = Field(default=1800, ge=30, le=7200)
    poll_interval_seconds: float = Field(default=1.0, ge=0.25, le=10.0)
    entity_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    adjacency_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    relation_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    entity_lens_enabled: bool = True
    entity_lens_max_labels: int = Field(default=6, ge=2, le=14)
    model_batch_size: int = Field(default=32, ge=1, le=256)
    max_window_words: int = Field(default=260, ge=80, le=800)
    benchmark_chunks: int = Field(default=5000, ge=100, le=50000)
    target_speedup: float = Field(default=100.0, ge=1.0, le=1000.0)
    budget_cap_usd: float = Field(default=40.0, ge=0.0, le=10000.0)
    estimated_gpu_rate_per_second_usd: float = Field(
        default=0.00031,
        ge=0.0,
        le=1.0,
        description=(
            "Operator-supplied conservative Flex rate used only for estimates. "
            "Actual billing remains authoritative in Runpod."
        ),
    )
    cost_overhead_multiplier: float = Field(
        default=1.5,
        ge=1.0,
        le=10.0,
        description="Estimate allowance for worker startup and idle time.",
    )

    @model_validator(mode="after")
    def validate_worker_bounds(self):
        if self.min_workers > self.max_workers:
            raise ValueError("runpod_flash.min_workers cannot exceed max_workers")
        return self


class GlobalIngestionSummarySettings(BaseModel):
    """Global defaults for Ghost A parent summarization.

    Per-corpus ``summary_models`` still win when explicitly configured. This
    section exists so a fresh install can define one local/cloud summary pool
    once, have new corpora inherit it, and let the worker apply a system-wide
    concurrency budget without hardcoding provider details in the frontend.
    """

    enabled: bool = Field(
        default=False,
        description="Default value for chunk_summarization on newly created corpora.",
    )
    max_summary_tokens: int = Field(
        default=175,
        ge=32,
        le=1024,
        description="Default Ghost A output token cap for new corpora.",
    )
    max_concurrent: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Global cap on concurrent Ghost A summary calls.",
    )
    summary_models: list[_legacy.ModelProfileRef] = Field(
        default_factory=list,
        description=(
            "Default Ghost A model pool. Entries may target local or cloud "
            "OpenAI-compatible providers and are encrypted at rest."
        ),
    )


class GlobalIngestionSettings(BaseModel):
    """Mutable global ingestion defaults."""

    provider_models: list[_legacy.ModelProfileRef] = Field(
        default_factory=list,
        description=(
            "Dedicated ingestion provider registry. Corpus configs reference these "
            "rows by profile_id; chat model routing remains separate."
        ),
    )
    summary: GlobalIngestionSummarySettings = Field(
        default_factory=GlobalIngestionSummarySettings
    )
    runpod_flash: RunpodFlashExtractionSettings = Field(
        default_factory=RunpodFlashExtractionSettings
    )


class GlobalSettings(BaseModel):
    infrastructure: _legacy.InfrastructureSettings = Field(
        default_factory=_legacy.InfrastructureSettings
    )
    chat: ChatLLMSettings = Field(default_factory=ChatLLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    modal: _legacy.ModalDeploySettings = Field(
        default_factory=_legacy.ModalDeploySettings
    )
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    ingestion: GlobalIngestionSettings = Field(default_factory=GlobalIngestionSettings)


class GlobalSettingsResponse(BaseModel):
    settings: GlobalSettings


class GlobalSettingsUpdate(BaseModel):
    chat: ChatLLMSettings | None = None
    retrieval: RetrievalSettings | None = None
    modal: _legacy.ModalDeploySettings | None = None
    models: ModelsConfig | None = None
    extraction: ExtractionSettings | None = None
    ingestion: GlobalIngestionSettings | None = None


class SynthesisSource(BaseModel):
    """Source receipt that backs a [n] inline citation in `markdown`."""

    index: int = 0
    evidence_id: str = ""
    source_label: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    snippet: str = ""


class AutoSynthesisPayload(BaseModel):
    """Polymath prose synthesis: one woven analysis with inline [n] citations.

    `markdown` carries the entire synthesis as ADHD-encyclopedia-newspaper prose.
    `sources` lists the receipts each [n] citation refers to. No card schema.
    """

    model_config = ConfigDict(protected_namespaces=())

    headline: str = ""
    markdown: str = ""
    sources: list[SynthesisSource] = Field(default_factory=list)
    fallback: bool = False
    fallback_reason: str | None = None
    model_used: str | None = None
    model_source: str | None = None
    web_evidence: dict[str, Any] = Field(default_factory=dict)


class InsightPacketSummary(BaseModel):
    sparse: bool = False
    temporal_support: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    evidence_sources: dict[str, int] = Field(default_factory=dict)
    fallback_reason: str | None = None


class ContextGraphJumpTarget(BaseModel):
    section: str
    label: str
    detail: str = ""
    target_id: str | None = None


class ContextGraphNode(BaseModel):
    id: str
    label: str
    kind: str = "concept"
    role: str = "context"
    topic_id: str | None = None
    size: float = 1.0
    weight: float = 0.0
    evidence_count: int = 0
    top_entities: list[str] = Field(default_factory=list)
    jump_targets: list[ContextGraphJumpTarget] = Field(default_factory=list)
    # Multi-corpus PR 1 — provenance for cross-corpus rendering.
    # source_corpus = primary corpus the node came from; source_corpora =
    # full set of corpora that contributed (populated by PR 3's merger when
    # the same entity_id appears in multiple corpora).
    source_corpus: str = ""
    source_corpora: list[str] = Field(default_factory=list)


class ContextGraphLink(BaseModel):
    source: str
    target: str
    kind: str = "context"
    role: str = "context"
    weight: float = 1.0
    suggested: bool = False
    evidence: str = ""
    # Multi-corpus PR 1 — edge provenance. `dangling=True` is set by PR 2's
    # graph merger when the target node is absent from the loaded corpora set.
    source_corpus: str = ""
    source_corpora: list[str] = Field(default_factory=list)
    dangling: bool = False


class ContextGraphPayload(BaseModel):
    nodes: list[ContextGraphNode] = Field(default_factory=list)
    links: list[ContextGraphLink] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class DiscoverTraceStage(BaseModel):
    stage: str
    label: str = ""
    count: int = 0
    status: str = "ok"
    detail: str = ""


class DiscoverTracePayload(BaseModel):
    """Source-backed trace payload for Mission Control discovery.

    The legacy pyc used to carry this shape, but clean Docker/GitHub rebuilds
    only have tracked source. Keep the model intentionally permissive because
    the graph orchestrator emits diagnostic dictionaries that evolve faster
    than the stable API envelope.
    """

    model_config = ConfigDict(extra="allow")

    anchor_terms: list[str] = Field(default_factory=list)
    latent_terms: list[str] = Field(default_factory=list)
    vector_neighbors: list[dict[str, Any]] = Field(default_factory=list)
    graph_expansion: dict[str, Any] = Field(default_factory=dict)
    working_entities: list[dict[str, Any]] = Field(default_factory=list)
    selected_edges: list[dict[str, Any]] = Field(default_factory=list)
    source_docs: list[dict[str, Any]] = Field(default_factory=list)
    evidence_filter: dict[str, Any] | None = None
    retrieval_evidence: dict[str, Any] | None = None
    stages: list[DiscoverTraceStage] = Field(default_factory=list)
    llm_context: dict[str, Any] = Field(default_factory=dict)
    graph_hint: dict[str, Any] = Field(default_factory=dict)
    gap_profile: dict[str, Any] = Field(default_factory=dict)


class GraphDiscoverRequest(BaseModel):
    """Mission Control discover request.

    Multi-corpus PR 1 — `corpus_id: str` is preserved as a deprecated alias.
    Callers should use `corpus_ids: list[str]` going forward. The
    model_validator below normalizes legacy single-id payloads into the
    canonical list at validation time.
    """

    model_config = ConfigDict(extra="ignore")

    corpus_id: str | None = Field(
        default=None,
        description="DEPRECATED — use corpus_ids. Wrapped into corpus_ids=[corpus_id] when present.",
    )
    corpus_ids: list[str] = Field(default_factory=list)
    query: str
    mode: Literal["auto", "connect", "gaps", "themes"] = "auto"
    # Phase 3 — synthesis-mode selector. "research" (default) gives the
    # concrete-claim research synthesis. "ideation" gives the build-advisor
    # output with [BUILD IDEA] blocks. "nuance" gives conceptual exploration
    # of gaps, analogies, transfers, and bridges. "gap" gives a structural
    # gap-analysis map — what the corpus does NOT yet connect — foregrounding
    # candidate gaps, fragile bridges, and weak links. Retrieval + packet are
    # the same shape; the packet caps and system prompt differ per mode.
    synthesis_mode: Literal["research", "ideation", "nuance", "gap"] = "research"
    # Sprint #2 — opt-in critique + revise loop. When True, the synthesis
    # pipeline runs a second LLM call to audit the draft (flag fabricated
    # terms, missing citations, shell sentences, label leaks) and a third
    # call to revise. Cost: 2-3× tokens. Default False to keep the hot
    # path single-call.
    validate_synthesis: bool = False
    web_search_enabled: bool = Field(
        default=False,
        description=(
            "When true, graph synthesis may add a bounded live-web evidence "
            "lane before the final LLM synthesis. Off keeps discover corpus-only."
        ),
    )
    web_fetch_depth: Literal["snippets", "normal", "deep"] = Field(
        default="normal",
        description=(
            "Live-web fetch depth for graph synthesis. snippets keeps search "
            "snippets only; normal fetches static page text; deep permits the "
            "configured JS-render fallback for allowlisted domains."
        ),
    )
    web_max_results: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum reranked live-web sources added to graph synthesis.",
    )
    session_id: str | None = None
    model: str | None = None
    agentic: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_corpus_ids(cls, values):
        if not isinstance(values, dict):
            return values
        ids = values.get("corpus_ids")
        single = values.get("corpus_id")
        if not ids and isinstance(single, str) and single:
            values["corpus_ids"] = [single]
        return values


class GraphDiscoverResponse(BaseModel):
    """Mission Control discover response. Multi-corpus PR 1 adds `corpus_ids`.

    `corpus_id` is preserved as a deprecated alias (set to corpus_ids[0] for
    legacy clients). New consumers should read `corpus_ids` and the
    `source_corpus` attribution on graph nodes/edges.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str = ""
    corpus_id: str = ""
    corpus_ids: list[str] = Field(default_factory=list)
    query: str = ""
    mode: str = "auto"
    interpretation: str = ""
    frontier: list[dict[str, Any]] = Field(default_factory=list)
    analogies: list[dict[str, Any]] = Field(default_factory=list)
    bridges: list[dict[str, Any]] = Field(default_factory=list)
    weak_links: list[dict[str, Any]] = Field(default_factory=list)
    transfers: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)
    strategic_read: dict[str, Any] | None = None
    intent_profile: dict[str, Any] | None = None
    atomic_trace: list[dict[str, Any]] = Field(default_factory=list)
    socratic_prompts: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    domain_map_summary: list[dict[str, Any]] = Field(default_factory=list)
    graph: dict[str, Any] = Field(default_factory=lambda: {"nodes": [], "links": []})
    anchors: list[dict[str, Any]] = Field(default_factory=list)
    concept_communities: list[dict[str, Any]] = Field(default_factory=list)
    entity_concept_map: dict[str, dict[str, Any]] = Field(default_factory=dict)
    headline: dict[str, Any] | None = None
    themes: list[dict[str, Any]] = Field(default_factory=list)
    bridges_v2: list[dict[str, Any]] = Field(default_factory=list)
    gaps_v2: list[dict[str, Any]] = Field(default_factory=list)
    latent_topics: list[dict[str, Any]] = Field(default_factory=list)
    tensions: list[dict[str, Any]] = Field(default_factory=list)
    trace: DiscoverTracePayload = Field(default_factory=DiscoverTracePayload)
    auto_synthesis: AutoSynthesisPayload = Field(default_factory=AutoSynthesisPayload)
    insight_packet_summary: InsightPacketSummary = Field(
        default_factory=InsightPacketSummary
    )
    context_graph: ContextGraphPayload = Field(default_factory=ContextGraphPayload)
    web_evidence: dict[str, Any] = Field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.utcnow()


class GraphDiscoverSession(BaseModel):
    """Mission Control session metadata.

    Multi-corpus PR 1 adds `corpus_ids: list[str]`. `corpus_id` is preserved
    as a deprecated alias for backward compat with sessions stored before
    PR 3's lifespan migration. The validator below populates one from the
    other so the dict shape works regardless of which field is set.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    corpus_id: str = ""
    corpus_ids: list[str] = Field(default_factory=list)
    title: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    turn_count: int = 0
    first_query: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _sync_corpus_fields(cls, values):
        if not isinstance(values, dict):
            return values
        ids = values.get("corpus_ids")
        single = values.get("corpus_id")
        if not ids and isinstance(single, str) and single:
            values["corpus_ids"] = [single]
        elif ids and not single:
            # Echo first id into legacy field so old clients keep working.
            values["corpus_id"] = ids[0]
        return values


class GraphDiscoverTurn(BaseModel):
    model_config = ConfigDict(extra="allow")

    query: str
    mode: str = "auto"
    created_at: datetime = Field(default_factory=_utcnow)
    response: GraphDiscoverResponse | None = None


class GraphDiscoverSessionDetail(GraphDiscoverSession):
    turns: list[GraphDiscoverTurn] = Field(default_factory=list)


class GraphResumeCandidateRequest(BaseModel):
    """Find an existing Mission Control session by query similarity.

    Multi-corpus PR 1 — `corpus_id` deprecated, use `corpus_ids`. Same
    normalization pattern as GraphDiscoverRequest.
    """

    model_config = ConfigDict(extra="ignore")

    corpus_id: str | None = Field(
        default=None,
        description="DEPRECATED — use corpus_ids.",
    )
    corpus_ids: list[str] = Field(default_factory=list)
    query: str
    threshold: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_corpus_ids(cls, values):
        if not isinstance(values, dict):
            return values
        ids = values.get("corpus_ids")
        single = values.get("corpus_id")
        if not ids and isinstance(single, str) and single:
            values["corpus_ids"] = [single]
        return values


class GraphResumeCandidateResponse(BaseModel):
    session: GraphDiscoverSession | None = None
    score: float = 0.0


class GraphQueryRequest(BaseModel):
    """POST /api/graph/query body — Agent Query backend call from GraphView.

    Multi-corpus PR 1 — overrides _legacy.GraphQueryRequest to add
    `corpus_ids: list[str]`. `corpus_id` is preserved as a deprecated alias;
    the model_validator below wraps a legacy single id into the new list.
    """

    model_config = ConfigDict(extra="ignore")

    corpus_id: str | None = Field(
        default=None,
        description="DEPRECATED — use corpus_ids. Wrapped into corpus_ids=[corpus_id] when present.",
    )
    corpus_ids: list[str] = Field(default_factory=list)
    query: str = Field(
        ...,
        min_length=1,
        description="Free-text query; tokens matched against Entity names",
    )
    max_hops: int = Field(
        default=2, ge=1, le=3, description="Entity→Entity traversal depth from seeds"
    )
    limit: int = Field(
        default=50, ge=1, le=200, description="Max nodes in returned subgraph"
    )
    seed_limit_per_token: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Seed entity budget per query token before graph expansion",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_corpus_ids(cls, values):
        if not isinstance(values, dict):
            return values
        ids = values.get("corpus_ids")
        single = values.get("corpus_id")
        if not ids and isinstance(single, str) and single:
            values["corpus_ids"] = [single]
        return values


class GraphNodeInsightRequest(BaseModel):
    """POST /api/graph/node-insight body.

    Read-only semantic neighborhood lookup for a clicked graph node. This is
    intentionally analysis-only: vector neighbors and provenance are returned
    to the UI, but no candidate edge is written back to Neo4j.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    corpus_ids: list[str] = Field(default_factory=list)
    node_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    entity_type: str | None = None
    node_kind: str | None = None
    top_entities: list[str] = Field(default_factory=list)
    limit: int = Field(default=8, ge=1, le=16)


class GraphNodeInsightDocument(BaseModel):
    doc_id: str = ""
    doc_name: str = ""
    corpus_id: str = ""
    corpus_name: str = ""
    count: int = 0
    best_score: float = 0.0


class GraphNodeInsightRelatedEntity(BaseModel):
    name: str
    predicate: str = ""
    relation_family: str = ""
    confidence: float = 0.0
    count: int = 0


class GraphNodeInsightResponse(BaseModel):
    query: str
    chunks: list[SourceChunk] = Field(default_factory=list)
    documents: list[GraphNodeInsightDocument] = Field(default_factory=list)
    related_entities: list[GraphNodeInsightRelatedEntity] = Field(default_factory=list)
    effective_tier: str = ""
    downgrade_reason: str | None = None


class GraphSuggestionItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str = ""
    kind: str = "query"
    entities: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class GraphSuggestionsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    corpus_id: str
    domain_map_summary: list[dict[str, Any]] = Field(default_factory=list)
    suggestions: list[GraphSuggestionItem] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# GraphInsightPacket — bounded LLM input shape.
#
# Built from cached metrics / scoped entities / concept communities / evidence
# chunks / context edges / gaps / latent topics / weak links / trace stages.
# No Louvain / PageRank / betweenness / gap discovery runs at packet build
# time; everything is pulled from data the legacy orchestrator already cached.
# ────────────────────────────────────────────────────────────────────────────


class GraphInsightPacketEntity(BaseModel):
    entity_id: str
    canonical_name: str
    domain: str = ""
    domain_type: str = ""
    object_kind: str = ""
    canonical_family: str = ""
    degree: int = 0
    role: str = "working"
    # Multi-corpus PR 1 — provenance. PR 3's merger populates these when the
    # same entity is seen across multiple corpora.
    source_corpus: str = ""
    source_corpora: list[str] = Field(default_factory=list)


class GraphInsightPacketCommunity(BaseModel):
    concept_id: str
    label: str
    size: int = 0
    scope_count: int = 0
    bridge_count: int = 0
    top_entities: list[str] = Field(default_factory=list)


class GraphInsightPacketEdge(BaseModel):
    source: str
    target: str
    source_name: str = ""
    target_name: str = ""
    predicate: str = ""
    relation_family: str = ""
    confidence: float = 0.0
    role: str = "context"
    # Multi-corpus PR 1 — edge provenance. RELATES_TO edges already carry
    # r.corpus_ids: list[str] in Neo4j, so this can be populated directly
    # from that field.
    source_corpus: str = ""
    source_corpora: list[str] = Field(default_factory=list)
    dangling: bool = False


class GraphInsightPacketGap(BaseModel):
    gap_id: str
    cluster_a: str = ""
    cluster_b: str = ""
    cluster_a_label: str = ""
    cluster_b_label: str = ""
    question: str = ""
    gap_type: str = "missing_edge"
    source_domain: str = ""
    target_domain: str = ""
    topology_sim: float | None = None
    neighbor_jaccard: float | None = None
    cd_pagerank: float | None = None


class GraphInsightPacketSignal(BaseModel):
    entity_id: str
    canonical_name: str
    domain: str = ""
    mention_count: int = 0
    doc_count: int = 0
    degree: int = 0
    rationale: str = ""


class GraphInsightPacketWeakLink(BaseModel):
    source: str
    target: str
    source_name: str = ""
    target_name: str = ""
    weakness_type: str = ""
    severity: str = "medium"
    rationale: str = ""


class GraphInsightPacketEvidence(BaseModel):
    chunk_id: str
    doc_id: str = ""
    text: str = ""
    source_label: str = ""
    has_temporal: bool = False


class GraphInsightPacketTraceStage(BaseModel):
    stage: str
    label: str = ""
    count: int = 0


class GraphInsightPacket(BaseModel):
    """Bounded packet handed to the synthesis LLM. Hard caps applied at build
    time; if the legacy result has more data, the assembler keeps the highest-
    weight items and drops the rest. The LLM never sees raw corpus text beyond
    this packet.

    Multi-corpus PR 1 — `corpus_ids: list[str]` is the new canonical scope.
    `corpus_id` stays as the deprecated single-value alias so the legacy
    packet builder in services.graph.orchestrator (PR 3 wraps this) keeps
    working unchanged.
    """

    query: str
    corpus_id: str = ""
    corpus_ids: list[str] = Field(default_factory=list)
    interpretation: str = ""
    headline: str = ""
    anchors: list[str] = Field(default_factory=list)
    entities: list[GraphInsightPacketEntity] = Field(default_factory=list)
    communities: list[GraphInsightPacketCommunity] = Field(default_factory=list)
    edges: list[GraphInsightPacketEdge] = Field(default_factory=list)
    gaps: list[GraphInsightPacketGap] = Field(default_factory=list)
    signals: list[GraphInsightPacketSignal] = Field(default_factory=list)
    analogies: list[dict[str, Any]] = Field(default_factory=list)
    transfers: list[dict[str, Any]] = Field(default_factory=list)
    bridges: list[dict[str, Any]] = Field(default_factory=list)
    fragile_bridges: list[dict[str, Any]] = Field(default_factory=list)
    weak_links: list[GraphInsightPacketWeakLink] = Field(default_factory=list)
    evidence: list[GraphInsightPacketEvidence] = Field(default_factory=list)
    graph_hint: dict[str, Any] = Field(default_factory=dict)
    gap_profile: dict[str, Any] = Field(default_factory=dict)
    trace_stages: list[GraphInsightPacketTraceStage] = Field(default_factory=list)
    sparse: bool = False
    temporal_support: bool = False


__all__ = [name for name in globals() if not name.startswith("_")]
