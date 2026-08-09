"""KnowledgeArtifactBundleV1 — compatibility view over post-gate extraction.

This is NOT a second extractor. It unifies fields already produced by
relex_local + corroboration_gate (+ optional alias decisions) so graph
projection, deterministic summaries, schemas, and readiness share one family.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

KNOWLEDGE_ARTIFACT_BUNDLE_VERSION = "knowledge_artifact_bundle.v1"
BUNDLE_AUTHORITY = "post_gate_compat_view"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class BundleIdentity(StrictModel):
    run_id: str = ""
    corpus_id: str
    corpus_generation: str = ""
    document_id: str
    chunk_id: str
    parent_id: str = ""
    section_id: str = ""


class BundleReleases(StrictModel):
    extraction_contract_hash: str = ""
    extractor_release: str = ""
    model_hash: str = ""
    alias_pipeline_release: str = ""
    entity_cluster_release: str = ""
    ontology_release: str = ""
    acceptance_policy_release: str = ""
    summary_release: str = ""
    projection_release: str = ""


class BundleSourceStructure(StrictModel):
    heading_path: list[str] = Field(default_factory=list)
    source_offsets: dict[str, Any] = Field(default_factory=dict)
    sentence_boundaries: list[dict[str, int]] = Field(default_factory=list)
    page_or_position_metadata: dict[str, Any] = Field(default_factory=dict)


class BundleEntityMention(StrictModel):
    surface: str
    canonical_name: str
    entity_type: str = ""
    confidence: float = 0.0
    query_aliases: list[str] = Field(default_factory=list)


class BundleRelation(StrictModel):
    subject: str
    predicate: str
    object: str
    confidence: float = 0.0
    evidence_text: str = ""
    validation_status: str = ""
    acceptance_lane: str = ""
    source_predicate: str = ""
    object_kind: str = "entity"
    negated: bool = False
    modal: bool = False
    attribution: str = ""


class BundleAudit(StrictModel):
    gate_decisions: dict[str, int] = Field(default_factory=dict)
    rejection_reasons: list[str] = Field(default_factory=list)
    unresolved_entities: list[str] = Field(default_factory=list)
    unresolved_predicates: list[str] = Field(default_factory=list)
    bundle_hash: str = ""
    source_adapter: str = "ghost_b_extraction_result"


class KnowledgeArtifactBundleV1(StrictModel):
    """Post-gate knowledge artifact consumed by projection/summary/schemas."""

    schema_version: Literal["knowledge_artifact_bundle.v1"] = (
        KNOWLEDGE_ARTIFACT_BUNDLE_VERSION
    )
    authority: Literal["post_gate_compat_view"] = BUNDLE_AUTHORITY
    identity: BundleIdentity
    releases: BundleReleases = Field(default_factory=BundleReleases)
    source_structure: BundleSourceStructure = Field(
        default_factory=BundleSourceStructure
    )
    entity_mentions: list[BundleEntityMention] = Field(default_factory=list)
    document_entities: list[BundleEntityMention] = Field(default_factory=list)
    corpus_entity_refs: list[str] = Field(default_factory=list)
    trusted_aliases: list[str] = Field(default_factory=list)
    retrieval_surface_variants: list[str] = Field(default_factory=list)
    descriptions: list[str] = Field(default_factory=list)
    relation_candidates: list[BundleRelation] = Field(default_factory=list)
    accepted_relation_assertions: list[BundleRelation] = Field(default_factory=list)
    open_relations: list[BundleRelation] = Field(default_factory=list)
    review_relations: list[BundleRelation] = Field(default_factory=list)
    rejected_relations: list[BundleRelation] = Field(default_factory=list)
    qualified_claims: list[dict[str, Any]] = Field(default_factory=list)
    qualified_facts: list[dict[str, Any]] = Field(default_factory=list)
    negation: list[dict[str, Any]] = Field(default_factory=list)
    modality: list[dict[str, Any]] = Field(default_factory=list)
    attribution: list[dict[str, Any]] = Field(default_factory=list)
    temporal_expressions: list[str] = Field(default_factory=list)
    quantities: list[str] = Field(default_factory=list)
    evidence_text: str = ""
    evidence_offsets: dict[str, Any] = Field(default_factory=dict)
    supporting_sentence_ids: list[str] = Field(default_factory=list)
    supporting_child_ids: list[str] = Field(default_factory=list)
    audit: BundleAudit = Field(default_factory=BundleAudit)

    def compute_bundle_hash(self) -> str:
        payload = self.model_dump()
        payload.get("audit", {}).pop("bundle_hash", None)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"sha256:{digest}"

    def with_hash(self) -> "KnowledgeArtifactBundleV1":
        h = self.compute_bundle_hash()
        audit = self.audit.model_copy(update={"bundle_hash": h})
        return self.model_copy(update={"audit": audit})
