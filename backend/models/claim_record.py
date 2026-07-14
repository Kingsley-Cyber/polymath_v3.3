"""Strict candidate contract for deterministic atomic-claim compilation.

``ClaimRecordV1`` is the child-level bridge between provider-neutral
observations and the later canonical ``ClaimAssertion`` envelope.  It is
candidate-only: typing, evidence closure, and relation agreement can be
recorded here, but no extractor or compiler can mark a claim accepted.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.local_extraction import Modality, Polarity, PredicateType


ClaimType = Literal[
    "definition",
    "description_or_observation",
    "association",
    "causal",
    "comparison_or_contrast",
    "prediction",
    "recommendation_or_procedure",
    "normative",
    "argument_or_inference",
]
AssertionMode = Literal["reported", "attributed", "hypothetical"]
TypingStatus = Literal["typed", "untyped"]
CLAIM_RECORD_AUTHORITY = "executor-proposed, owner-ratifiable"
CLAIM_RECORD_OWNER_RATIFICATION_REQUIRED = True
CLAIM_RECORD_CHANGE_POLICY = "changes-require-new-schema-version"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "authority": CLAIM_RECORD_AUTHORITY,
            "owner_ratification_required": CLAIM_RECORD_OWNER_RATIFICATION_REQUIRED,
            "change_policy": CLAIM_RECORD_CHANGE_POLICY,
        },
    )


class ClaimArgumentV1(StrictModel):
    role: Literal["subject", "object"]
    filler_kind: Literal["entity_mention", "span_observation"]
    filler_ref: str = Field(min_length=1)
    span_observation_id: str = Field(min_length=1)
    surface: str = Field(min_length=1)
    start_char: int
    end_char: int
    evidence_sentence_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_span(self) -> "ClaimArgumentV1":
        if self.start_char < 0 or self.end_char <= self.start_char:
            raise ValueError("claim argument offsets must form a positive span")
        return self


class ClaimRecordV1(StrictModel):
    schema_version: Literal["claim_record.v1"]
    claim_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    proposition_text: str = Field(min_length=1)
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

    @model_validator(mode="after")
    def validate_candidate_contract(self) -> "ClaimRecordV1":
        typed = self.typing_status == "typed"
        if typed != (self.predicate_id is not None):
            raise ValueError("typed claims require exactly one predicate_id")
        if typed != (self.normalized_predicate is not None):
            raise ValueError("typed claims require a normalized predicate")
        if typed and not any(item.role == "subject" for item in self.arguments):
            raise ValueError("typed claims require a subject argument")
        if not self.evidence_sentence_ids:
            raise ValueError("claims require sentence evidence")
        if len(self.evidence_sentence_ids) != len(set(self.evidence_sentence_ids)):
            raise ValueError("claim evidence sentence IDs must be unique")
        if len(self.source_relation_ids) != len(set(self.source_relation_ids)):
            raise ValueError("source relation IDs must be unique")
        argument_keys = [
            (item.role, item.span_observation_id, item.filler_ref)
            for item in self.arguments
        ]
        if len(argument_keys) != len(set(argument_keys)):
            raise ValueError("claim arguments must be unique")
        evidence = set(self.evidence_sentence_ids)
        if any(item.evidence_sentence_id not in evidence for item in self.arguments):
            raise ValueError("claim argument references unknown sentence evidence")
        for values, label in (
            (self.conditions, "conditions"),
            (self.exceptions, "exceptions"),
            (self.temporal_cues, "temporal cues"),
        ):
            if len(values) != len(set(values)) or any(not value for value in values):
                raise ValueError(f"claim {label} must be nonempty and unique")
        return self


class ClaimLinkV1(StrictModel):
    schema_version: Literal["claim_link.v1"]
    link_id: str = Field(min_length=1)
    source_claim_id: str = Field(min_length=1)
    relation_type: Literal["RESULTS_IN"]
    target_claim_id: str = Field(min_length=1)
    evidence_sentence_ids: list[str]
    derivation_method: Literal["dependency_rule", "discourse_rule"]
    triggering_connective: str = Field(min_length=1)
    rule_id: Literal[
        "claim_results_in.explicit_dependency.v1",
        "claim_results_in.explicit_discourse_continuity.v1",
    ]
    knowledge_status: Literal["candidate"]
    validation_status: Literal["candidate"]

    @model_validator(mode="after")
    def validate_link(self) -> "ClaimLinkV1":
        if self.source_claim_id == self.target_claim_id:
            raise ValueError("claim links cannot be self-referential")
        if not self.evidence_sentence_ids:
            raise ValueError("claim links require sentence evidence")
        if len(self.evidence_sentence_ids) != len(set(self.evidence_sentence_ids)):
            raise ValueError("claim link evidence IDs must be unique")
        return self


class ClaimCompilationV1(StrictModel):
    schema_version: Literal["claim_compilation.v1"]
    document_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    claims: list[ClaimRecordV1]
    links: list[ClaimLinkV1]
    rejected_relation_ids: list[str]
    unresolved_coreference_spans: list[str]
    skipped_predicate_observation_ids: list[str]
    same_sentence_repeated_claim_count: int = Field(ge=0)
    cross_sentence_candidate_count: int = Field(ge=0)
    cross_sentence_rejected_count: int = Field(ge=0)
    compiler_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reference_closure(self) -> "ClaimCompilationV1":
        claim_ids = [item.claim_id for item in self.claims]
        link_ids = [item.link_id for item in self.links]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("compiled claim IDs must be unique")
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("compiled claim-link IDs must be unique")
        known_claims = set(claim_ids)
        for claim in self.claims:
            if claim.document_id != self.document_id or claim.child_id != self.child_id:
                raise ValueError("compiled claims must share compilation ownership")
        for link in self.links:
            if {link.source_claim_id, link.target_claim_id} - known_claims:
                raise ValueError("claim link references an unknown compiled claim")
        for values, label in (
            (self.rejected_relation_ids, "rejected relation IDs"),
            (self.unresolved_coreference_spans, "coreference spans"),
            (self.skipped_predicate_observation_ids, "skipped predicates"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        cross_sentence_accepted = sum(
            item.derivation_method == "discourse_rule" for item in self.links
        )
        if (
            cross_sentence_accepted + self.cross_sentence_rejected_count
            != self.cross_sentence_candidate_count
        ):
            raise ValueError("cross-sentence candidate accounting must close")
        return self

    def receipt(self) -> dict[str, Any]:
        """Return count-only accounting with no source text or claim payloads."""

        typed = sum(item.typing_status == "typed" for item in self.claims)
        untyped = sum(item.typing_status == "untyped" for item in self.claims)
        relation_agree = sum(len(item.source_relation_ids) for item in self.claims)
        dependency_links = sum(
            item.rule_id == "claim_results_in.explicit_dependency.v1"
            for item in self.links
        )
        discourse_links = sum(
            item.rule_id == "claim_results_in.explicit_discourse_continuity.v1"
            for item in self.links
        )
        return {
            "compiler_recipe_hash": self.compiler_recipe_hash,
            "claim_count": len(self.claims),
            "typed_claim_count": typed,
            "untyped_claim_count": untyped,
            "glirel_agree_count": relation_agree,
            "glirel_conflict_count": len(self.rejected_relation_ids),
            "link_count": len(self.links),
            "links_by_connective_family": {
                "explicit_result_phrase": dependency_links,
                "discourse_result": discourse_links,
            },
            "cross_sentence_candidate_count": self.cross_sentence_candidate_count,
            "cross_sentence_accepted_count": discourse_links,
            "cross_sentence_rejected_count": self.cross_sentence_rejected_count,
            "unresolved_coreference_count": len(self.unresolved_coreference_spans),
            "skipped_predicate_count": len(self.skipped_predicate_observation_ids),
            "same_sentence_repeated_claim_count": (
                self.same_sentence_repeated_claim_count
            ),
        }


class ClaimAssertionSourceV1(StrictModel):
    """Lossless compiler trace carried into the canonical assertion body."""

    source_claim_record_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    predicate_observation_id: str = Field(min_length=1)
    predicate_mention_id: str | None
    predicate_surface: str = Field(min_length=1)
    predicate_lemma: str = Field(min_length=1)
    normalized_predicate: PredicateType | None
    typing_status: TypingStatus
    local_polarity: Polarity
    local_modality: Modality
    source_relation_ids: list[str]


class ClaimAssertionTemporalV1(StrictModel):
    cues: list[str]
    valid_from: None
    valid_to: None
    reference_time: None
    temporal_status: Literal["unresolved"]


class ClaimAssertionV1(StrictModel):
    """Candidate projection shaped for the canonical ClaimAssertion envelope."""

    schema_version: Literal["polymath.claim_assertion.v1"]
    claim_id: str = Field(min_length=1)
    proposition_text: str = Field(min_length=1)
    canonical_proposition: str = Field(min_length=1)
    claim_type: ClaimType
    predicate_id: PredicateType | None
    arguments: list[ClaimArgumentV1]
    polarity: Literal["affirmed", "negated"]
    modal_force: Literal[
        "asserted",
        "possible",
        "probable",
        "recommended",
        "required",
    ]
    assertion_mode: AssertionMode
    conditions: list[str]
    exceptions: list[str]
    semantic_scope: dict[str, Any]
    scope_hash: str = Field(min_length=1)
    temporal: ClaimAssertionTemporalV1
    evidence_refs: list[str]
    evidence_episode_ids: list[str]
    domain_profile_id: None
    frame_instance_ids: list[str]
    derivation_parent_ids: list[str]
    source_compilation: ClaimAssertionSourceV1
    knowledge_status: Literal["candidate"]
    validation_status: Literal["candidate"]
