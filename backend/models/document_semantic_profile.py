"""Strict, corpus-local document ontology profile for Tier-0 routing.

The profile is an additive serving artifact.  It compacts already-durable
LocalExtractionV1/ClaimCompilationV1 and hierarchy metadata; it is never an
accepted semantic assertion and never mutates any source row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.hash_taxonomy import namespace_hash
from models.semantic_digest import FrameId
from models.semantic_resolution import DomainId


PROFILE_SCHEMA_VERSION = "t91_document_profile.v1"
PROFILE_COLLECTION = "t91_document_profiles"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProfileSourceSliceV1(StrictModel):
    source_kind: Literal["document", "parent_chunks", "extraction_rows"]
    row_count: int = Field(ge=0)
    artifact_ids: list[str]
    slice_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_slice(self) -> "ProfileSourceSliceV1":
        if self.artifact_ids != sorted(set(self.artifact_ids)):
            raise ValueError("profile source artifact IDs must be sorted and unique")
        if self.row_count != len(self.artifact_ids):
            raise ValueError("profile source row count must equal artifact ID count")
        return self


class ProfileRegistryClosureV1(StrictModel):
    domain_registry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    domain_resolution_policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    superframe_registry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    superframe_rule_registry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    frame_role_binding_policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    motif_registry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    motif_stage_binding_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    motif_matching_policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ProfileDomainEvidenceV1(StrictModel):
    domain_id: DomainId
    assignment_role: Literal["dominant", "supporting"]
    derivation_method: Literal[
        "exact_claim_concept",
        "exact_section_heading",
        "exact_claim_concept_and_heading",
    ]
    matched_normalized_terms: list[str]
    evidence_ref_ids: list[str]
    supporting_claim_ids: list[str]

    @model_validator(mode="after")
    def validate_evidence(self) -> "ProfileDomainEvidenceV1":
        for values, label in (
            (self.matched_normalized_terms, "domain terms"),
            (self.evidence_ref_ids, "domain evidence"),
            (self.supporting_claim_ids, "domain claims"),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"profile {label} must be sorted and unique")
        if not self.matched_normalized_terms or not self.evidence_ref_ids:
            raise ValueError("profile domain evidence cannot be empty")
        return self


class ProfileFrameEvidenceV1(StrictModel):
    frame_id: FrameId
    source_claim_id: str = Field(min_length=1)
    source_rule_id: str = Field(min_length=1)
    evidence_sentence_ids: list[str]

    @model_validator(mode="after")
    def validate_evidence(self) -> "ProfileFrameEvidenceV1":
        if self.evidence_sentence_ids != sorted(set(self.evidence_sentence_ids)):
            raise ValueError("profile frame evidence must be sorted and unique")
        if not self.evidence_sentence_ids:
            raise ValueError("profile frame evidence cannot be empty")
        return self


class ProfileMotifEvidenceV1(StrictModel):
    motif_id: str = Field(min_length=1)
    motif_candidate_id: str = Field(min_length=1)
    frame_instance_ids: list[str]
    frame_ids: list[FrameId]
    source_claim_ids: list[str]
    evidence_sentence_ids: list[str]
    matcher_disposition: Literal["confirmed_candidate", "provisional"]

    @model_validator(mode="after")
    def validate_evidence(self) -> "ProfileMotifEvidenceV1":
        for values, label in (
            (self.frame_instance_ids, "motif frames"),
            (self.frame_ids, "motif frame IDs"),
            (self.source_claim_ids, "motif claims"),
            (self.evidence_sentence_ids, "motif evidence"),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"profile {label} must be sorted and unique")
            if not values:
                raise ValueError(f"profile {label} cannot be empty")
        return self


class ProfileConceptEvidenceV1(StrictModel):
    concept_term: str = Field(min_length=2)
    evidence_ref_ids: list[str]
    supporting_claim_ids: list[str]

    @model_validator(mode="after")
    def validate_evidence(self) -> "ProfileConceptEvidenceV1":
        if self.concept_term != self.concept_term.strip().lower():
            raise ValueError("profile concept terms must be normalized")
        for values, label in (
            (self.evidence_ref_ids, "concept evidence"),
            (self.supporting_claim_ids, "concept claims"),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"profile {label} must be sorted and unique")
        if not self.evidence_ref_ids:
            raise ValueError("profile concept evidence cannot be empty")
        return self


def profile_logical_hash(
    *,
    corpus_id: str,
    doc_id: str,
    source_version_id: str,
) -> str:
    return namespace_hash(
        "logical-artifact",
        {
            "artifact_kind": PROFILE_SCHEMA_VERSION,
            "natural_keys": {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "source_version_id": source_version_id,
            },
        },
    )


class T91DocumentProfileV1(StrictModel):
    schema_version: Literal["t91_document_profile.v1"]
    profile_id: str = Field(pattern=r"^t91-doc-profile:[0-9a-f]{64}$")
    corpus_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    source_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    domain_ids: list[DomainId]
    superframe_ids: list[FrameId]
    motif_ids: list[str]
    concept_terms: list[str]
    domain_evidence: list[ProfileDomainEvidenceV1]
    frame_evidence: list[ProfileFrameEvidenceV1]
    motif_evidence: list[ProfileMotifEvidenceV1]
    concept_evidence: list[ProfileConceptEvidenceV1]
    source_slices: list[ProfileSourceSliceV1]
    registry_closure: ProfileRegistryClosureV1
    input_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    body_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    assignment_state: Literal["candidate"]
    canonical_write: Literal[False]
    llm_call_count: Literal[0]
    provider_spend_usd: Literal[0.0]

    def identity_body(self) -> dict:
        return self.model_dump(
            mode="python",
            exclude={"body_hash", "profile_hash"},
        )

    @model_validator(mode="after")
    def validate_profile(self) -> "T91DocumentProfileV1":
        logical_hash = profile_logical_hash(
            corpus_id=self.corpus_id,
            doc_id=self.doc_id,
            source_version_id=self.source_version_id,
        )
        if self.profile_id != f"t91-doc-profile:{logical_hash.split(':', 1)[1]}":
            raise ValueError("T9.1 profile logical identity drifted")
        if self.source_key != self.source_content_sha256:
            raise ValueError("T9.1 profile source key/content hash closure drifted")
        expected_kinds = ["document", "extraction_rows", "parent_chunks"]
        if [item.source_kind for item in self.source_slices] != expected_kinds:
            raise ValueError("T9.1 profile source slices must be complete and sorted")
        for values, label in (
            (self.domain_ids, "domain IDs"),
            (self.superframe_ids, "superframe IDs"),
            (self.motif_ids, "motif IDs"),
            (self.concept_terms, "concept terms"),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"T9.1 profile {label} must be sorted and unique")
        if self.domain_ids != sorted({item.domain_id for item in self.domain_evidence}):
            raise ValueError("T9.1 profile domain evidence does not close")
        if self.superframe_ids != sorted(
            {item.frame_id for item in self.frame_evidence}
        ):
            raise ValueError("T9.1 profile frame evidence does not close")
        if self.motif_ids != sorted({item.motif_id for item in self.motif_evidence}):
            raise ValueError("T9.1 profile motif evidence does not close")
        if self.concept_terms != sorted(
            {item.concept_term for item in self.concept_evidence}
        ):
            raise ValueError("T9.1 profile concept evidence does not close")
        expected_input_hash = namespace_hash(
            "input-set",
            sorted(item.slice_hash for item in self.source_slices),
        )
        if self.input_set_hash != expected_input_hash:
            raise ValueError("T9.1 profile input-set identity drifted")
        expected_body_hash = namespace_hash("body", self.identity_body())
        if self.body_hash != expected_body_hash:
            raise ValueError("T9.1 profile body hash drifted")
        expected_profile_hash = namespace_hash(
            "revision",
            {
                "logical_artifact_hash": logical_hash,
                "body_hash": self.body_hash,
                "supersedes": None,
            },
        )
        if self.profile_hash != expected_profile_hash:
            raise ValueError("T9.1 profile revision identity drifted")
        return self
