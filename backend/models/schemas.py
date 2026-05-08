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
    child_chunk_tokens: _legacy.TokenBudget = Field(
        default_factory=lambda: _legacy.TokenBudget(
            min_tokens=128, target_tokens=500, max_tokens=700
        )
    )
    chunk_overlap: int = Field(default=200)
    max_summary_tokens: int = Field(default=175)
    child_chunk_algorithm: Literal["sentence_merge", "semantic_split"] = Field(
        default="sentence_merge"
    )
    semantic_split_threshold: float = Field(default=0.65, ge=0.0, le=1.0)

    summary_models: list[_legacy.ModelProfileRef] = Field(default_factory=list)
    extraction_models: list[_legacy.ModelProfileRef] = Field(default_factory=list)
    entity_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    models_linked: bool = True

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
            for suffix in ("model", "base_url", "api_key", "extra_params", "max_concurrent"):
                data.pop(f"{prefix}_{suffix}", None)
        return data


class WriteState(BaseModel):
    """Tracks per-store write completion and non-fatal ingest warnings."""

    mongo_written: bool = False
    qdrant_written: bool = False
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


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query_model_pool: list[QueryModelPoolEntry] = Field(default_factory=list)
    hyde: HydeConfig = Field(default_factory=HydeConfig)
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)


class OllamaBulkAddRequest(BaseModel):
    model_names: list[str] = Field(default_factory=list)


class ChatLLMSettings(_legacy.ChatLLMSettings):
    query_profile: Literal["fast", "balanced", "thorough", "custom"] = "balanced"


class RetrievalSettings(_legacy.RetrievalSettings):
    max_corpora_per_query: int = Field(default=32, ge=1, le=100)
    final_top_k: int = Field(default=8, ge=1, le=50)


class GlobalSettings(BaseModel):
    infrastructure: _legacy.InfrastructureSettings = Field(
        default_factory=_legacy.InfrastructureSettings
    )
    chat: ChatLLMSettings = Field(default_factory=ChatLLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    modal: _legacy.ModalDeploySettings = Field(default_factory=_legacy.ModalDeploySettings)
    models: ModelsConfig = Field(default_factory=ModelsConfig)


class GlobalSettingsResponse(BaseModel):
    settings: GlobalSettings


class GlobalSettingsUpdate(BaseModel):
    chat: ChatLLMSettings | None = None
    retrieval: RetrievalSettings | None = None
    modal: _legacy.ModalDeploySettings | None = None
    models: ModelsConfig | None = None


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

    headline: str = ""
    markdown: str = ""
    sources: list[SynthesisSource] = Field(default_factory=list)
    fallback: bool = False
    fallback_reason: str | None = None


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


class ContextGraphLink(BaseModel):
    source: str
    target: str
    kind: str = "context"
    role: str = "context"
    weight: float = 1.0
    suggested: bool = False
    evidence: str = ""


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


class GraphDiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    corpus_id: str
    query: str
    mode: Literal["auto", "connect", "gaps", "themes"] = "auto"
    session_id: str | None = None
    model: str | None = None
    agentic: bool = False


class GraphDiscoverResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str = ""
    corpus_id: str = ""
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
    graph: dict[str, Any] = Field(
        default_factory=lambda: {"nodes": [], "links": []}
    )
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
    insight_packet_summary: InsightPacketSummary = Field(default_factory=InsightPacketSummary)
    context_graph: ContextGraphPayload = Field(default_factory=ContextGraphPayload)


def _utcnow() -> datetime:
    return datetime.utcnow()


class GraphDiscoverSession(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    corpus_id: str
    title: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    turn_count: int = 0
    first_query: str | None = None


class GraphDiscoverTurn(BaseModel):
    model_config = ConfigDict(extra="allow")

    query: str
    mode: str = "auto"
    created_at: datetime = Field(default_factory=_utcnow)
    response: GraphDiscoverResponse | None = None


class GraphDiscoverSessionDetail(GraphDiscoverSession):
    turns: list[GraphDiscoverTurn] = Field(default_factory=list)


class GraphResumeCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    corpus_id: str
    query: str
    threshold: float | None = None


class GraphResumeCandidateResponse(BaseModel):
    session: GraphDiscoverSession | None = None
    score: float = 0.0


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


class GraphInsightPacketGap(BaseModel):
    gap_id: str
    cluster_a: str = ""
    cluster_b: str = ""
    cluster_a_label: str = ""
    cluster_b_label: str = ""
    question: str = ""


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
    this packet."""

    query: str
    corpus_id: str
    interpretation: str = ""
    headline: str = ""
    anchors: list[str] = Field(default_factory=list)
    entities: list[GraphInsightPacketEntity] = Field(default_factory=list)
    communities: list[GraphInsightPacketCommunity] = Field(default_factory=list)
    edges: list[GraphInsightPacketEdge] = Field(default_factory=list)
    gaps: list[GraphInsightPacketGap] = Field(default_factory=list)
    signals: list[GraphInsightPacketSignal] = Field(default_factory=list)
    weak_links: list[GraphInsightPacketWeakLink] = Field(default_factory=list)
    evidence: list[GraphInsightPacketEvidence] = Field(default_factory=list)
    graph_hint: dict[str, Any] = Field(default_factory=dict)
    trace_stages: list[GraphInsightPacketTraceStage] = Field(default_factory=list)
    sparse: bool = False
    temporal_support: bool = False


__all__ = [name for name in globals() if not name.startswith("_")]
