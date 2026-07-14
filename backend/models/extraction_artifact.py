"""Provider-neutral candidate extraction artifact contract (P2.6).

This is an additive, observation-only boundary.  Cloud, private/local,
legacy-local, and RunPod engines adapt into the same strict shape before
parity measurement.  The contract never promotes candidates or decides which
engine is correct; that authority remains with the existing validation and
graph-promotion gates.

Authority status is explicit because this schema is executor-proposed and
owner-ratifiable.  A later owner edit publishes a new version rather than
mutating this v1 contract in place.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from models.hash_taxonomy import namespace_hash


CANDIDATE_EXTRACTION_ARTIFACT_VERSION = "candidate_extraction_artifact.v1"
CANDIDATE_EXTRACTION_AUTHORITY = "executor_proposed_owner_ratifiable"

ExtractionEngine = Literal[
    "cloud",
    "local",
    "legacy_local",
    "runpod_flash",
]
ArtifactStatus = Literal["candidate", "failed", "skipped"]
OffsetStatus = Literal["exact", "unavailable"]
FieldMethodKind = Literal[
    "engine_model",
    "deterministic_python",
    "shared_backend_validation",
    "source_evidenced",
    "omitted_ungrounded",
    "unavailable_legacy_shape",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())


class OffsetSpan(StrictModel):
    """A source-text offset or an explicit record that no offset survived."""

    status: OffsetStatus
    char_start: int | None = None
    char_end: int | None = None

    @model_validator(mode="after")
    def validate_offset_pair(self) -> "OffsetSpan":
        if self.status == "unavailable":
            if self.char_start is not None or self.char_end is not None:
                raise ValueError("unavailable offsets must leave both positions null")
            return self
        if self.char_start is None or self.char_end is None:
            raise ValueError("exact offsets require both positions")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("exact offsets must form a positive source span")
        return self


class EvidenceRef(StrictModel):
    evidence_id: str
    text: str
    span: OffsetSpan
    method: Literal["exact_source_substring", "unavailable"]

    @model_validator(mode="after")
    def validate_method_matches_span(self) -> "EvidenceRef":
        if self.method == "exact_source_substring" and self.span.status != "exact":
            raise ValueError("exact evidence requires exact offsets")
        if self.method == "unavailable" and self.span.status != "unavailable":
            raise ValueError("unavailable evidence must not claim exact offsets")
        return self


class FieldMethod(StrictModel):
    field_path: str
    method: FieldMethodKind
    producer: str
    evidence_ids: list[str]


class CandidateEntity(StrictModel):
    entity_id: str
    canonical_name: str
    surface_form: str
    entity_type: str
    object_kind: str = ""
    confidence: float
    span: OffsetSpan
    query_aliases: list[str]
    definitional_phrase: str
    method: FieldMethodKind
    object_kind_evidence_ids: list[str]

    @model_validator(mode="after")
    def validate_entity(self) -> "CandidateEntity":
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("entity confidence must be between 0 and 1")
        if self.object_kind and not self.object_kind_evidence_ids:
            raise ValueError("object_kind requires exact source evidence")
        if not self.object_kind and self.object_kind_evidence_ids:
            raise ValueError("blank object_kind cannot cite object-kind evidence")
        return self


class CandidateRelation(StrictModel):
    relation_id: str
    subject: str
    predicate: str
    object: str
    object_kind: str
    confidence: float
    evidence_ids: list[str]
    relation_cue: str = ""
    relation_cue_evidence_ids: list[str]
    source_predicate: str | None = None
    validation_status: str | None = None
    graph_promotion_eligible: bool = False
    method: FieldMethodKind

    @model_validator(mode="after")
    def validate_relation(self) -> "CandidateRelation":
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("relation confidence must be between 0 and 1")
        if self.relation_cue and not self.relation_cue_evidence_ids:
            raise ValueError("relation_cue requires exact source evidence")
        if not self.relation_cue and self.relation_cue_evidence_ids:
            raise ValueError("blank relation_cue cannot cite cue evidence")
        if self.graph_promotion_eligible and not self.evidence_ids:
            raise ValueError("graph-promotion eligibility requires source evidence")
        return self


class CandidateFact(StrictModel):
    fact_id: str
    subject: str
    fact_type: str
    property_name: str
    value: str
    unit: str | None
    condition: str | None
    confidence: float
    evidence_ids: list[str]
    deterministic: bool
    method: FieldMethodKind

    @model_validator(mode="after")
    def validate_fact(self) -> "CandidateFact":
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("fact confidence must be between 0 and 1")
        if self.deterministic and self.method != "deterministic_python":
            raise ValueError("deterministic facts must name deterministic_python")
        return self


class EngineCapabilities(StrictModel):
    deterministic_facts_supported: bool
    facts_required_for_queryability: Literal[False] = False
    exact_entity_offsets_supported: bool
    exact_relation_evidence_supported: bool


class ExtractionProvenance(StrictModel):
    engine: ExtractionEngine
    engine_runtime_version: str
    model_id: str
    model_revision: str | None
    source_wire_contract_version: str
    source_contract_hash: str
    shared_contract_version: Literal["candidate_extraction_artifact.v1"]
    shared_contract_hash: str
    capabilities: EngineCapabilities
    field_methods: list[FieldMethod]
    lane: int | None
    attempts: int
    fallback_from: list[str]
    fallback_count: int
    failure_count: int

    @model_validator(mode="after")
    def validate_attempt_accounting(self) -> "ExtractionProvenance":
        if min(self.attempts, self.fallback_count, self.failure_count) < 0:
            raise ValueError("attempt/fallback/failure counters cannot be negative")
        if self.fallback_count != len(self.fallback_from):
            raise ValueError("fallback_count must equal the recorded fallback lanes")
        return self


class CandidateFailure(StrictModel):
    error_type: str
    error_message: str


class CandidateExtractionArtifact(StrictModel):
    schema_version: Literal["candidate_extraction_artifact.v1"]
    authority: Literal["executor_proposed_owner_ratifiable"]
    artifact_status: ArtifactStatus
    corpus_id: str
    doc_id: str
    chunk_id: str
    source_text_sha256: str
    entities: list[CandidateEntity]
    relations: list[CandidateRelation]
    facts: list[CandidateFact]
    evidence: list[EvidenceRef]
    provenance: ExtractionProvenance
    failure: CandidateFailure | None = None

    @model_validator(mode="after")
    def validate_reference_closure(self) -> "CandidateExtractionArtifact":
        if self.artifact_status == "failed" and self.failure is None:
            raise ValueError("failed artifacts require failure details")
        if self.artifact_status != "failed" and self.failure is not None:
            raise ValueError("only failed artifacts may carry failure details")
        if self.artifact_status != "candidate" and (
            self.entities or self.relations or self.facts
        ):
            raise ValueError("failed/skipped artifacts cannot carry candidates")

        id_groups = (
            [item.entity_id for item in self.entities],
            [item.relation_id for item in self.relations],
            [item.fact_id for item in self.facts],
            [item.evidence_id for item in self.evidence],
        )
        if any(len(values) != len(set(values)) for values in id_groups):
            raise ValueError("artifact-local IDs must be unique within each family")

        evidence_ids = set(id_groups[-1])
        references: set[str] = set()
        for entity in self.entities:
            references.update(entity.object_kind_evidence_ids)
        for relation in self.relations:
            references.update(relation.evidence_ids)
            references.update(relation.relation_cue_evidence_ids)
        for fact in self.facts:
            references.update(fact.evidence_ids)
        for method in self.provenance.field_methods:
            references.update(method.evidence_ids)
        if not references <= evidence_ids:
            raise ValueError("candidate fields reference unknown evidence IDs")
        return self

    @property
    def artifact_id(self) -> str:
        body = self.model_dump()
        digest = namespace_hash("work", body).split(":", 1)[1]
        return f"candidate-extraction:{digest}"


CANDIDATE_EXTRACTION_SCHEMA_HASH = namespace_hash(
    "schema", CandidateExtractionArtifact.model_json_schema()
)
