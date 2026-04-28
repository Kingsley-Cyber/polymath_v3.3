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
            min_tokens=128, target_tokens=350, max_tokens=512
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

    entity_schema: list[str] | None = None
    relation_schema: list[str] | None = None
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


class CorpusCreate(_legacy.CorpusCreate):
    default_ingestion_config: IngestionConfig = Field(default_factory=IngestionConfig)


class CorpusResponse(_legacy.CorpusResponse):
    default_ingestion_config: IngestionConfig


class AutoSynthesisItem(BaseModel):
    title: str = ""
    body: str = ""
    evidence: list[str] = Field(default_factory=list)
    related_ids: list[str] = Field(default_factory=list)


class AutoSynthesisPayload(BaseModel):
    headline: str = ""
    themes: list[AutoSynthesisItem] = Field(default_factory=list)
    bridges: list[AutoSynthesisItem] = Field(default_factory=list)
    gaps: list[AutoSynthesisItem] = Field(default_factory=list)
    emerging_signals: list[AutoSynthesisItem] = Field(default_factory=list)
    next_moves: list[AutoSynthesisItem] = Field(default_factory=list)
    evidence_notes: list[AutoSynthesisItem] = Field(default_factory=list)


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


class DiscoverTracePayload(_legacy.DiscoverTracePayload):
    stages: list[DiscoverTraceStage] = Field(default_factory=list)
    llm_context: dict[str, Any] = Field(default_factory=dict)
    graph_hint: dict[str, Any] = Field(default_factory=dict)


class GraphDiscoverResponse(_legacy.GraphDiscoverResponse):
    trace: DiscoverTracePayload = Field(default_factory=DiscoverTracePayload)
    auto_synthesis: AutoSynthesisPayload = Field(default_factory=AutoSynthesisPayload)
    insight_packet_summary: InsightPacketSummary = Field(default_factory=InsightPacketSummary)
    context_graph: ContextGraphPayload = Field(default_factory=ContextGraphPayload)


class GraphDiscoverTurn(BaseModel):
    query: str
    mode: str = "auto"
    created_at: datetime
    response: GraphDiscoverResponse | None = None


class GraphDiscoverSessionDetail(_legacy.GraphDiscoverSession):
    turns: list[GraphDiscoverTurn] = Field(default_factory=list)


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
