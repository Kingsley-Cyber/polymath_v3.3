"""Strict vNext parent packet carrying deterministic atomic claims."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.claim_record import (
    AssertionMode,
    ClaimArgumentV1,
    ClaimLinkV1,
    ClaimRecordV1,
    ClaimType,
    TypingStatus,
)
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.local_extraction import Modality, Polarity, PredicateType

ATOMIC_PACKET_SCHEMA_VERSION = "semantic_parent_packet.atomic_claims.v1"
BOUNDED_ATOMIC_PACKET_SCHEMA_VERSION = "semantic_parent_packet.atomic_claims.v2"
BOUNDED_SELECTION_RECIPE_VERSION = "atomic_claim_packet_selection.v2"
BOUNDED_PACKET_MAX_UTF8_BYTES = 20_000


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        protected_namespaces=(),
    )


class PacketEvidenceSentenceV1(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        protected_namespaces=(),
        str_strip_whitespace=False,
    )

    evidence_sentence_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class PacketAtomicClaimV1(StrictModel):
    schema_version: Literal["packet_atomic_claim.v1"]
    claim_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    canonical_proposition: str = Field(min_length=1)
    claim_type: ClaimType
    predicate_observation_id: str = Field(min_length=1)
    predicate_id: str | None
    predicate_surface: str = Field(min_length=1)
    predicate_lemma: str = Field(min_length=1)
    normalized_predicate: PredicateType | None
    typing_status: TypingStatus
    arguments: list[ClaimArgumentV1]
    polarity: Polarity
    modality: Modality
    assertion_mode: AssertionMode
    conditions: list[str]
    exceptions: list[str]
    temporal_cues: list[str]
    evidence_sentence_ids: list[str]
    source_relation_ids: list[str]
    scope_hash: str = Field(min_length=1)
    knowledge_status: Literal["candidate"]
    validation_status: Literal["candidate"]

    @classmethod
    def from_claim_record(cls, claim: ClaimRecordV1) -> "PacketAtomicClaimV1":
        values = claim.model_dump(mode="python")
        values.pop("proposition_text")
        values["schema_version"] = "packet_atomic_claim.v1"
        return cls.model_validate(values)

    def to_claim_record(self, *, proposition_text: str) -> ClaimRecordV1:
        values = self.model_dump(mode="python")
        values["schema_version"] = "claim_record.v1"
        values["proposition_text"] = proposition_text
        return ClaimRecordV1.model_validate(values)


class PacketExtractionEntityV1(StrictModel):
    canonical_name: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    surface_form: str | None = None
    object_kind: str | None = None
    definitional_phrase: str | None = None
    query_aliases: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PacketEvidenceContractV1(StrictModel):
    eligibility_schema_version: Literal["semantic_parent_eligibility.v2"]
    eligibility_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    packet_schema_version: Literal["semantic_parent_packet.atomic_claims.v1"]
    claim_record_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    claim_compilation_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_version: Literal["claim_compiler.v2"]
    compiler_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parser_id: Literal["en_core_web_sm"]
    parser_version: Literal["spacy:3.8.14;model:3.8.0"]
    source_child_ids: list[str]
    source_compilation_revision_ids: list[str]
    claims_interim: Literal[False]
    relation_disposition: Literal["relations_remain_observation_only"]

    @model_validator(mode="after")
    def validate_ordered_sources(self) -> "PacketEvidenceContractV1":
        for values, label in (
            (self.source_child_ids, "source child IDs"),
            (self.source_compilation_revision_ids, "source compilation revisions"),
        ):
            if not values or values != sorted(set(values)):
                raise ValueError(f"{label} must be nonempty, unique, and sorted")
        return self


class SemanticParentPacketAtomicClaimsV1(StrictModel):
    packet_schema_version: Literal["semantic_parent_packet.atomic_claims.v1"]
    corpus_id: str = Field(min_length=1)
    corpus_name: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    parent_text: str = Field(min_length=1)
    claims: list[PacketAtomicClaimV1]
    evidence_sentences: list[PacketEvidenceSentenceV1]
    claim_links: list[ClaimLinkV1]
    extraction_entities: list[PacketExtractionEntityV1]
    evidence_contract: PacketEvidenceContractV1

    @model_validator(mode="after")
    def validate_atomic_closure(self) -> "SemanticParentPacketAtomicClaimsV1":
        if not self.claims:
            raise ValueError("atomic parent packet requires at least one claim")
        if not self.extraction_entities:
            raise ValueError("atomic parent packet requires an extraction entity")
        if self.evidence_contract.packet_schema_version != self.packet_schema_version:
            raise ValueError("packet and evidence-contract versions disagree")

        claims = [item.claim_id for item in self.claims]
        evidence_ids = [item.evidence_sentence_id for item in self.evidence_sentences]
        links = [item.link_id for item in self.claim_links]
        if claims != sorted(set(claims)):
            raise ValueError("packet claims must be unique and sorted")
        if evidence_ids != sorted(set(evidence_ids)):
            raise ValueError("packet evidence must be unique and sorted")
        if links != sorted(set(links)):
            raise ValueError("packet claim links must be unique and sorted")

        source_children = set(self.evidence_contract.source_child_ids)
        evidence = {item.evidence_sentence_id: item for item in self.evidence_sentences}
        referenced_evidence: set[str] = set()
        for claim in self.claims:
            if (
                claim.document_id != self.doc_id
                or claim.child_id not in source_children
            ):
                raise ValueError("packet claim ownership escapes parent child closure")
            if len(claim.evidence_sentence_ids) != 1:
                raise ValueError(
                    "atomic packet v1 requires one evidence sentence per claim"
                )
            evidence_id = claim.evidence_sentence_ids[0]
            sentence = evidence.get(evidence_id)
            if sentence is None or sentence.child_id != claim.child_id:
                raise ValueError("packet claim evidence is missing or cross-child")
            restored = claim.to_claim_record(proposition_text=sentence.text)
            if restored.claim_id != claim.claim_id:
                raise ValueError("packet claim reconstruction changed identity")
            referenced_evidence.add(evidence_id)

        known_claims = set(claims)
        for link in self.claim_links:
            if {link.source_claim_id, link.target_claim_id} - known_claims:
                raise ValueError("packet claim link references an unknown claim")
            if set(link.evidence_sentence_ids) - set(evidence):
                raise ValueError("packet claim link references unknown evidence")
            referenced_evidence.update(link.evidence_sentence_ids)
        if referenced_evidence != set(evidence):
            raise ValueError("packet evidence must equal the referenced evidence set")
        return self


def semantic_parent_packet_atomic_schema_hash() -> str:
    return namespace_hash(
        "schema",
        SemanticParentPacketAtomicClaimsV1.model_json_schema(),
    )


class AtomicClaimPacketSelectionRecipeV2(StrictModel):
    schema_version: Literal["atomic_claim_packet_selection_recipe.v2"]
    recipe_version: Literal["atomic_claim_packet_selection.v2"]
    max_packet_utf8_bytes: Literal[20000]
    seed_rule: Literal["one_highest_priority_claim_per_source_child"]
    priority_lanes: list[Literal["typed", "negative", "nuanced", "ordinary"]]
    lane_iteration: Literal["round_robin_by_sorted_child_id"]
    within_child_order: list[Literal["claim_type", "claim_id"]]
    nuanced_if_any: list[
        Literal[
            "conditions_nonempty",
            "exceptions_nonempty",
            "temporal_cues_nonempty",
            "modality_not_asserted",
        ]
    ]
    oversize_policy: Literal["skip_and_continue"]
    claim_output_order: Literal["claim_id"]
    provider_claim_fields: list[
        Literal[
            "claim_id",
            "canonical_claim_text",
            "typing_status",
            "polarity",
            "evidence_sentence_id",
        ]
    ]
    parent_text_in_provider_packet: Literal[False]
    evidence_quote_bodies_in_provider_packet: Literal[False]
    excluded_claim_disposition: Literal["locally_durable_not_provider_visible"]

    @model_validator(mode="after")
    def validate_frozen_order(self) -> "AtomicClaimPacketSelectionRecipeV2":
        expected = {
            "priority_lanes": ["typed", "negative", "nuanced", "ordinary"],
            "within_child_order": ["claim_type", "claim_id"],
            "nuanced_if_any": [
                "conditions_nonempty",
                "exceptions_nonempty",
                "temporal_cues_nonempty",
                "modality_not_asserted",
            ],
            "provider_claim_fields": [
                "claim_id",
                "canonical_claim_text",
                "typing_status",
                "polarity",
                "evidence_sentence_id",
            ],
        }
        for field, values in expected.items():
            if getattr(self, field) != values:
                raise ValueError(f"{field} order is frozen")
        return self


class PacketAtomicClaimV2(StrictModel):
    claim_id: str = Field(min_length=1)
    canonical_claim_text: str = Field(min_length=1)
    typing_status: TypingStatus
    polarity: Polarity
    evidence_sentence_id: str = Field(min_length=1)

    @classmethod
    def from_claim_record(cls, claim: ClaimRecordV1) -> "PacketAtomicClaimV2":
        if len(claim.evidence_sentence_ids) != 1:
            raise ValueError("bounded packet claims require one evidence sentence")
        return cls(
            claim_id=claim.claim_id,
            canonical_claim_text=claim.canonical_proposition,
            typing_status=claim.typing_status,
            polarity=claim.polarity,
            evidence_sentence_id=claim.evidence_sentence_ids[0],
        )


class PacketClaimLinkV2(StrictModel):
    schema_version: Literal["packet_claim_link.v2"]
    link_id: str = Field(min_length=1)
    source_claim_id: str = Field(min_length=1)
    relation_type: Literal["RESULTS_IN"]
    target_claim_id: str = Field(min_length=1)
    evidence_sentence_ids: list[str]

    @classmethod
    def from_claim_link(cls, link: ClaimLinkV1) -> "PacketClaimLinkV2":
        return cls(
            schema_version="packet_claim_link.v2",
            link_id=link.link_id,
            source_claim_id=link.source_claim_id,
            relation_type=link.relation_type,
            target_claim_id=link.target_claim_id,
            evidence_sentence_ids=link.evidence_sentence_ids,
        )


class SelectionLaneAccountingV2(StrictModel):
    source_count: int = Field(ge=0)
    emitted_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_closed(self) -> "SelectionLaneAccountingV2":
        if self.emitted_count + self.excluded_count != self.source_count:
            raise ValueError("selection lane accounting does not close")
        return self


class PacketSelectionManifestV2(StrictModel):
    recipe_version: Literal["atomic_claim_packet_selection.v2"]
    recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    max_packet_utf8_bytes: Literal[20000]
    source_claim_count: int = Field(gt=0)
    emitted_claim_count: int = Field(gt=0)
    excluded_claim_count: int = Field(ge=0)
    source_claim_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    emitted_claim_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    excluded_claim_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_child_count: int = Field(gt=0)
    covered_source_child_count: int = Field(gt=0)
    source_child_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    covered_source_child_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_claim_link_count: int = Field(ge=0)
    emitted_claim_link_count: int = Field(ge=0)
    excluded_claim_link_count: int = Field(ge=0)
    source_claim_link_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    emitted_claim_link_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    excluded_claim_link_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    typed: SelectionLaneAccountingV2
    negative: SelectionLaneAccountingV2
    nuanced: SelectionLaneAccountingV2
    ordinary: SelectionLaneAccountingV2
    cap_applied: bool
    proposal_space_disposition: Literal[
        "bounded_to_emitted_claims_excluded_claims_remain_local"
    ]

    @model_validator(mode="after")
    def validate_manifest_closure(self) -> "PacketSelectionManifestV2":
        if (
            self.emitted_claim_count + self.excluded_claim_count
            != self.source_claim_count
        ):
            raise ValueError("claim selection accounting does not close")
        if (
            self.emitted_claim_link_count + self.excluded_claim_link_count
            != self.source_claim_link_count
        ):
            raise ValueError("claim-link selection accounting does not close")
        if self.covered_source_child_count != self.source_child_count:
            raise ValueError("bounded packet must cover every source child")
        if self.covered_source_child_set_hash != self.source_child_set_hash:
            raise ValueError("bounded packet child coverage hash does not close")
        if self.cap_applied != (self.excluded_claim_count > 0):
            raise ValueError("cap flag disagrees with excluded claim count")
        return self


class PacketEvidenceContractV2(StrictModel):
    eligibility_schema_version: Literal["semantic_parent_eligibility.v2"]
    eligibility_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    packet_schema_version: Literal["semantic_parent_packet.atomic_claims.v2"]
    claim_record_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    claim_compilation_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_version: Literal["claim_compiler.v2"]
    compiler_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parser_id: Literal["en_core_web_sm"]
    parser_version: Literal["spacy:3.8.14;model:3.8.0"]
    source_child_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_compilation_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    claims_interim: Literal[False]
    relation_disposition: Literal["relations_remain_observation_only"]


class SemanticParentPacketAtomicClaimsV2(StrictModel):
    packet_schema_version: Literal["semantic_parent_packet.atomic_claims.v2"]
    corpus_id: str = Field(min_length=1)
    corpus_name: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    claims: list[PacketAtomicClaimV2]
    claim_links: list[PacketClaimLinkV2]
    extraction_entities: list[PacketExtractionEntityV1]
    evidence_contract: PacketEvidenceContractV2
    selection_manifest: PacketSelectionManifestV2

    @model_validator(mode="after")
    def validate_bounded_atomic_closure(
        self,
    ) -> "SemanticParentPacketAtomicClaimsV2":
        if not self.claims:
            raise ValueError("bounded atomic packet requires at least one claim")
        if not self.extraction_entities:
            raise ValueError("bounded atomic packet requires an extraction entity")
        if self.evidence_contract.packet_schema_version != self.packet_schema_version:
            raise ValueError("packet and evidence-contract versions disagree")
        claim_ids = [item.claim_id for item in self.claims]
        link_ids = [item.link_id for item in self.claim_links]
        if claim_ids != sorted(set(claim_ids)):
            raise ValueError("bounded packet claims must be unique and sorted")
        if link_ids != sorted(set(link_ids)):
            raise ValueError("bounded packet links must be unique and sorted")
        known_claims = set(claim_ids)
        for link in self.claim_links:
            if {link.source_claim_id, link.target_claim_id} - known_claims:
                raise ValueError("bounded packet link references an unknown claim")
        manifest = self.selection_manifest
        if manifest.emitted_claim_count != len(claim_ids):
            raise ValueError("manifest emitted claim count disagrees with packet")
        if manifest.emitted_claim_set_hash != namespace_hash(
            "input-set", frozenset(claim_ids)
        ):
            raise ValueError("manifest emitted claim hash disagrees with packet")
        if manifest.emitted_claim_link_count != len(link_ids):
            raise ValueError("manifest emitted link count disagrees with packet")
        if manifest.emitted_claim_link_set_hash != namespace_hash(
            "input-set", frozenset(link_ids)
        ):
            raise ValueError("manifest emitted link hash disagrees with packet")
        size = len(canonical_json_v1(self.model_dump(mode="python")).encode("utf-8"))
        if size > manifest.max_packet_utf8_bytes:
            raise ValueError("bounded atomic packet exceeds byte maximum")
        return self


def semantic_parent_packet_bounded_schema_hash() -> str:
    return namespace_hash(
        "schema",
        SemanticParentPacketAtomicClaimsV2.model_json_schema(),
    )
