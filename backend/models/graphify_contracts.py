"""Versioned contracts for the deterministic Graphify extraction pipeline."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GRAPHIFY_CONTRACT_RELEASE = "graphify-contracts-v1"
FROZEN_ENTITY_TYPES: frozenset[str] = frozenset({
    "person", "organization", "location", "event", "concept", "method",
    "product", "software", "document", "standard", "rule", "law",
    "artifact", "timereference", "unknown",
})


def stable_digest(value: Any) -> str:
    """Return a canonical SHA-256 digest for JSON-compatible data."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}:{stable_digest(list(parts))[:24]}"


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MentionTerminalState(StrEnum):
    ALIGNED = "aligned"
    ALIGNMENT_FAILURE = "alignment_failure"


class EntityTerminalState(StrEnum):
    PROMOTED = "promoted"
    DOCUMENT_LOCAL = "document_local"
    REVIEW = "review"
    SUPPRESSED = "suppressed"


class RelationTerminalState(StrEnum):
    ACCEPTED = "accepted"
    QUALIFIED = "qualified"
    OPEN = "open"
    REVIEW = "review"
    REJECTED = "rejected"
    ALIGNMENT_FAILURE = "alignment_failure"


class OpenIEArgumentKind(StrEnum):
    ENTITY = "ENTITY"
    LITERAL = "LITERAL"
    DESCRIPTION = "DESCRIPTION"
    EMBEDDED_CLAUSE = "EMBEDDED_CLAUSE"
    UNRESOLVED = "UNRESOLVED"


class PipelineStage(StrEnum):
    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    SURVEY_COMPLETE = "SURVEY_COMPLETE"
    ENTITY_CENSUS_COMPLETE = "ENTITY_CENSUS_COMPLETE"
    ENTITY_REDUCTION_COMPLETE = "ENTITY_REDUCTION_COMPLETE"
    MENTION_COMPLETION_COMPLETE = "MENTION_COMPLETION_COMPLETE"
    RELATION_ELIGIBILITY_COMPLETE = "RELATION_ELIGIBILITY_COMPLETE"
    OPENIE_EXTRACTION_COMPLETE = "OPENIE_EXTRACTION_COMPLETE"
    OPENIE_ARGUMENT_ADAPTATION_COMPLETE = "OPENIE_ARGUMENT_ADAPTATION_COMPLETE"
    OPENIE_PROPOSITION_REDUCTION_COMPLETE = "OPENIE_PROPOSITION_REDUCTION_COMPLETE"
    OPENIE_PREDICATE_COMPILATION_COMPLETE = "OPENIE_PREDICATE_COMPILATION_COMPLETE"
    OPENIE_ASSERTION_ASSEMBLY_COMPLETE = "OPENIE_ASSERTION_ASSEMBLY_COMPLETE"
    RELATION_COMPILATION_COMPLETE = "RELATION_COMPILATION_COMPLETE"
    ASSERTION_VALIDATION_COMPLETE = "ASSERTION_VALIDATION_COMPLETE"
    TEST_PROJECTION_COMPLETE = "TEST_PROJECTION_COMPLETE"
    E2E_VERIFIED = "E2E_VERIFIED"


class NormalizedDocumentV1(StrictFrozenModel):
    schema_version: Literal["polymath.normalized_document.v1"] = "polymath.normalized_document.v1"
    document_id: str = Field(min_length=1)
    source_uri: str = ""
    original_text: str
    normalized_text: str
    normalized_to_original: tuple[int, ...]
    normalization_release: str
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_mapping(self) -> "NormalizedDocumentV1":
        if len(self.normalized_to_original) != len(self.normalized_text) + 1:
            raise ValueError("normalized_to_original must contain every character boundary")
        if not self.normalized_to_original or self.normalized_to_original[0] != 0:
            raise ValueError("offset map must begin at original boundary 0")
        if self.normalized_to_original[-1] != len(self.original_text):
            raise ValueError("offset map must end at the original text length")
        if any(a > b for a, b in zip(self.normalized_to_original, self.normalized_to_original[1:])):
            raise ValueError("offset map must be monotonic")
        return self


class ExtractionWindowV1(StrictFrozenModel):
    schema_version: Literal["polymath.extraction_window.v1"] = "polymath.extraction_window.v1"
    window_id: str
    document_id: str
    sequence: int = Field(ge=0)
    normalized_start: int = Field(ge=0)
    normalized_end: int = Field(gt=0)
    original_start: int | None = Field(default=None, ge=0)
    original_end: int | None = Field(default=None, ge=0)
    text: str = Field(min_length=1)
    heading_path: tuple[str, ...] = ()
    token_count: int = Field(ge=1)
    window_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_span(self) -> "ExtractionWindowV1":
        if self.normalized_end <= self.normalized_start:
            raise ValueError("window normalized span must be non-empty")
        if (self.original_start is None) != (self.original_end is None):
            raise ValueError("original window offsets are both present or both absent")
        return self


class RawMentionV1(StrictFrozenModel):
    schema_version: Literal["polymath.raw_mention.v1"] = "polymath.raw_mention.v1"
    mention_id: str
    document_id: str
    window_id: str
    sequence: int = Field(ge=0)
    surface: str
    entity_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    local_start: int = Field(ge=0)
    local_end: int = Field(gt=0)
    normalized_start: int | None = Field(default=None, ge=0)
    normalized_end: int | None = Field(default=None, ge=0)
    original_start: int | None = Field(default=None, ge=0)
    original_end: int | None = Field(default=None, ge=0)
    terminal_state: MentionTerminalState
    alignment_error: str = ""
    provider_release: str

    @field_validator("entity_type")
    @classmethod
    def frozen_entity_type(cls, value: str) -> str:
        canonical = value.strip().lower()
        if canonical not in FROZEN_ENTITY_TYPES:
            raise ValueError(f"entity type {value!r} is outside the frozen inventory")
        return canonical

    @model_validator(mode="after")
    def validate_terminal_offsets(self) -> "RawMentionV1":
        aligned = self.terminal_state == MentionTerminalState.ALIGNED
        normalized = self.normalized_start is not None and self.normalized_end is not None
        if aligned and not normalized:
            raise ValueError("aligned raw mentions require normalized offsets")
        if aligned and (self.original_start is None or self.original_end is None):
            raise ValueError("aligned raw mentions require original offsets")
        if not aligned and not self.alignment_error:
            raise ValueError("alignment failures require an explicit reason")
        return self


class DocumentEntityV1(StrictFrozenModel):
    schema_version: Literal["polymath.document_entity.v1"] = "polymath.document_entity.v1"
    entity_id: str
    document_id: str
    canonical_name: str
    entity_type: str
    aliases: tuple[str, ...] = ()
    mention_ids: tuple[str, ...]
    state: EntityTerminalState
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: tuple[str, ...]
    reducer_release: str

    @field_validator("entity_type")
    @classmethod
    def frozen_entity_type(cls, value: str) -> str:
        canonical = value.strip().lower()
        if canonical not in FROZEN_ENTITY_TYPES:
            raise ValueError(f"entity type {value!r} is outside the frozen inventory")
        return canonical


class CompletedMentionV1(StrictFrozenModel):
    schema_version: Literal["polymath.completed_mention.v1"] = "polymath.completed_mention.v1"
    mention_id: str
    entity_id: str
    document_id: str
    surface: str
    normalized_start: int = Field(ge=0)
    normalized_end: int = Field(gt=0)
    original_start: int | None = Field(default=None, ge=0)
    original_end: int | None = Field(default=None, ge=0)
    source: Literal["raw", "exact_name", "alias", "acronym", "variant"]
    context_rule: str = ""
    completion_release: str


class OpenIEAsserterLinkV1(StrictFrozenModel):
    asserter: str
    verb: str
    construction: str
    speech_act: bool
    negated: bool
    cluster: int | None = None
    cluster_canonical: str | None = None


class OpenIERawPropositionV1(StrictFrozenModel):
    schema_version: Literal["polymath.openie_raw_proposition.v1"] = "polymath.openie_raw_proposition.v1"
    proposition_id: str
    document_id: str
    unit_id: str
    evidence_text: str
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    rendering_sequence: int = Field(ge=0)
    subject: str
    relation: str
    object: str
    confidence: float = Field(ge=0.0, le=1.0)
    from_clause_split: bool = False
    from_entailment: bool = False
    entailment_score: float = Field(ge=0.0, le=1.0)
    asserter_chain: tuple[str, ...] = ()
    asserter_links: tuple[OpenIEAsserterLinkV1, ...] = ()
    extractor_release: str

    @model_validator(mode="after")
    def validate_evidence(self) -> "OpenIERawPropositionV1":
        if self.evidence_end <= self.evidence_start:
            raise ValueError("OpenIE evidence span must be non-empty")
        return self


class AdaptedOpenIEArgumentV1(StrictFrozenModel):
    schema_version: Literal["polymath.adapted_openie_argument.v1"] = "polymath.adapted_openie_argument.v1"
    argument_id: str
    proposition_id: str
    document_id: str
    unit_id: str
    role: Literal["subject", "object"]
    surface: str
    kind: OpenIEArgumentKind
    normalized_start: int | None = Field(default=None, ge=0)
    normalized_end: int | None = Field(default=None, ge=0)
    mention_id: str | None = None
    entity_id: str | None = None
    entity_type: str | None = None
    entity_state: EntityTerminalState | None = None
    literal_type: str = ""
    normalized_value: str = ""
    reasons: tuple[str, ...]
    adapter_release: str

    @model_validator(mode="after")
    def validate_classification(self) -> "AdaptedOpenIEArgumentV1":
        has_span = self.normalized_start is not None and self.normalized_end is not None
        if (self.normalized_start is None) != (self.normalized_end is None):
            raise ValueError("adapted argument offsets are both present or both absent")
        if self.kind == OpenIEArgumentKind.ENTITY:
            if not has_span or not self.mention_id or not self.entity_id or not self.entity_type:
                raise ValueError("ENTITY arguments require exact mention identity and offsets")
        elif self.mention_id or self.entity_id or self.entity_type or self.entity_state:
            raise ValueError("non-ENTITY arguments cannot carry entity identity")
        if self.kind == OpenIEArgumentKind.LITERAL and not self.literal_type:
            raise ValueError("LITERAL arguments require a literal type")
        return self


class OpenIEPropositionFamilyV1(StrictFrozenModel):
    schema_version: Literal["polymath.openie_proposition_family.v1"] = "polymath.openie_proposition_family.v1"
    family_id: str
    document_id: str
    unit_id: str
    subject_key: str
    subject_kind: OpenIEArgumentKind
    relation_lemma: str
    object_key: str
    object_kind: OpenIEArgumentKind
    polarity: str
    modality: str
    attribution: str
    representative_proposition_id: str
    rendering_ids: tuple[str, ...]
    surface_relations: tuple[str, ...]
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    max_confidence: float = Field(ge=0.0, le=1.0)
    family_release: str

    @model_validator(mode="after")
    def validate_family(self) -> "OpenIEPropositionFamilyV1":
        if not self.rendering_ids or self.representative_proposition_id not in self.rendering_ids:
            raise ValueError("proposition family must retain its representative rendering")
        if len(set(self.rendering_ids)) != len(self.rendering_ids):
            raise ValueError("proposition family rendering ids must be unique")
        return self


class OpenIEPredicateCandidateV1(StrictFrozenModel):
    schema_version: Literal["polymath.openie_predicate_candidate.v1"] = "polymath.openie_predicate_candidate.v1"
    candidate_id: str
    family_id: str
    document_id: str
    unit_id: str
    subject_argument_id: str
    object_argument_id: str
    subject_key: str
    subject_kind: OpenIEArgumentKind
    object_key: str
    object_kind: OpenIEArgumentKind
    surface_relation: str
    relation_lemma: str
    canonical_predicate: str | None = None
    mapping_status: Literal["MAPPED", "STORE_UNMAPPED_SURFACE_RELATION", "REVIEW"]
    mapping_rule: str
    direction_rule: str
    polarity: str
    modality: str
    attribution: str
    representative_proposition_id: str
    rendering_ids: tuple[str, ...]
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    compiler_release: str

    @model_validator(mode="after")
    def validate_mapping(self) -> "OpenIEPredicateCandidateV1":
        if self.mapping_status == "MAPPED" and not self.canonical_predicate:
            raise ValueError("mapped OpenIE candidates require a canonical predicate")
        declared_related_normalization = self.mapping_rule.startswith(
            "mapped:openie:declared_closed_ontology:occurred_"
        )
        if (
            self.canonical_predicate == "related_to"
            and "related" not in self.surface_relation.casefold()
            and not declared_related_normalization
        ):
            raise ValueError("related_to cannot be used as a forced fallback")
        return self


class OpenIEAssertionV1(StrictFrozenModel):
    schema_version: Literal["polymath.openie_assertion.v1"] = "polymath.openie_assertion.v1"
    assertion_id: str
    candidate_id: str
    family_id: str
    document_id: str
    lane: Literal["FACT", "QUALIFIED_CLAIM", "OPEN_RELATION", "REVIEW", "REJECT"]
    canonical_predicate: str | None = None
    surface_relation: str
    subject_argument_id: str
    object_argument_id: str
    subject_entity_id: str | None = None
    object_entity_id: str | None = None
    subject_mention_id: str | None = None
    object_mention_id: str | None = None
    value_kind: OpenIEArgumentKind | None = None
    value: str = ""
    polarity: str
    modality: str
    attribution: str
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    score: float = Field(ge=0.0, le=1.0)
    reasons: tuple[str, ...]
    policy_release: str

    @model_validator(mode="after")
    def validate_lane(self) -> "OpenIEAssertionV1":
        if self.lane == "FACT":
            if not self.canonical_predicate:
                raise ValueError("FACT assertions require a canonical predicate")
            if not self.subject_entity_id:
                raise ValueError("FACT assertions require an entity subject")
            if not self.object_entity_id and self.value_kind not in {
                OpenIEArgumentKind.LITERAL, OpenIEArgumentKind.DESCRIPTION,
            }:
                raise ValueError("FACT assertions require an entity or value object")
            if self.polarity != "positive" or self.modality != "asserted" or self.attribution != "direct":
                raise ValueError("qualified propositions cannot enter the FACT lane")
        return self


class SurfaceRelationV1(StrictFrozenModel):
    schema_version: Literal["polymath.surface_relation.v1"] = "polymath.surface_relation.v1"
    relation_id: str
    document_id: str
    evidence_text: str
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    subject_mention_id: str
    object_mention_id: str
    surface_predicate: str
    lemma: str = ""
    particle: str = ""
    preposition: str = ""
    dependency_frame: str = ""
    dependency_path: str = ""
    voice: str = ""
    polarity: str = "positive"
    modality: str = "asserted"
    attribution: str = ""
    canonical_candidate: str | None = None
    mapping_rule: str = ""
    mapping_release: str
    terminal_state: RelationTerminalState
    reasons: tuple[str, ...] = ()


class AssertionDecisionV1(StrictFrozenModel):
    schema_version: Literal["polymath.assertion_decision.v1"] = "polymath.assertion_decision.v1"
    decision_id: str
    relation_id: str
    document_id: str
    status: RelationTerminalState
    canonical_predicate: str | None = None
    score: float = Field(ge=0.0, le=1.0)
    reasons: tuple[str, ...]
    policy_release: str


class StageReceiptV1(StrictFrozenModel):
    schema_version: Literal["polymath.graphify_stage_receipt.v1"] = "polymath.graphify_stage_receipt.v1"
    run_id: str
    document_id: str
    stage: PipelineStage
    status: Literal["passed", "failed"]
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_pins: dict[str, str]
    elapsed_seconds: float = Field(ge=0.0)
    input_count: int = Field(ge=0)
    output_count: int = Field(ge=0)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    retry_count: int = Field(default=0, ge=0)


class BenchmarkResultV1(StrictFrozenModel):
    schema_version: Literal["polymath.graphify_benchmark.v1"] = "polymath.graphify_benchmark.v1"
    run_id: str
    status: Literal["passed", "failed"]
    fixtures: tuple[str, ...]
    metrics: dict[str, int | float | bool | str]
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_pins: dict[str, str]


def contract_schema_hash() -> str:
    models = (
        NormalizedDocumentV1,
        ExtractionWindowV1,
        RawMentionV1,
        DocumentEntityV1,
        CompletedMentionV1,
        OpenIEAsserterLinkV1,
        OpenIERawPropositionV1,
        AdaptedOpenIEArgumentV1,
        OpenIEPropositionFamilyV1,
        OpenIEPredicateCandidateV1,
        OpenIEAssertionV1,
        SurfaceRelationV1,
        AssertionDecisionV1,
        StageReceiptV1,
        BenchmarkResultV1,
    )
    return stable_digest({model.__name__: model.model_json_schema() for model in models})
