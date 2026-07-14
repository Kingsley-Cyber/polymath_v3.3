"""Strict candidate contracts for deterministic domain and frame resolution.

T9.1 is intentionally narrower than a semantic profile or FrameInstance:
domain assignments are exact registry candidates, predicate rules emit frame
candidates, and affinity data is exposed only through a non-identity serving
view. T9.2 owns role-bound FrameInstances and motif candidates.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.local_extraction import PredicateType
from models.semantic_digest import FrameId


DomainId = Literal[
    "D01",
    "D02",
    "D03",
    "D04",
    "D05",
    "D06",
    "D07",
    "D08",
    "D09",
    "D10",
    "D11",
    "D12",
    "D13",
    "D14",
    "D15",
    "D16",
]
DomainSignalKind = Literal["claim_concept", "section_heading"]
DomainAssignmentRole = Literal["dominant", "supporting"]
DOMAIN_RESOLUTION_AUTHORITY = "executor-proposed, owner-ratifiable"
DOMAIN_RESOLUTION_OWNER_RATIFICATION_REQUIRED = True
DOMAIN_RESOLUTION_CHANGE_POLICY = "changes-require-new-schema-version"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "authority": DOMAIN_RESOLUTION_AUTHORITY,
            "owner_ratification_required": (
                DOMAIN_RESOLUTION_OWNER_RATIFICATION_REQUIRED
            ),
            "change_policy": DOMAIN_RESOLUTION_CHANGE_POLICY,
        },
    )


def _validate_unique_nonempty(values: list[str], label: str) -> None:
    if not values or any(not value.strip() for value in values):
        raise ValueError(f"{label} must contain nonempty values")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class DomainSignalV1(StrictModel):
    schema_version: Literal["domain_signal.v1"]
    signal_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    signal_kind: DomainSignalKind
    evidence_ref_ids: list[str]
    supporting_claim_ids: list[str]

    @model_validator(mode="after")
    def validate_signal(self) -> "DomainSignalV1":
        if not self.signal_id.strip() or not self.label.strip():
            raise ValueError("domain signal id and label must be nonempty")
        _validate_unique_nonempty(self.evidence_ref_ids, "domain signal evidence")
        if len(self.supporting_claim_ids) != len(set(self.supporting_claim_ids)):
            raise ValueError("domain signal supporting claim IDs must be unique")
        if any(not item.strip() for item in self.supporting_claim_ids):
            raise ValueError("domain signal supporting claim IDs must be nonempty")
        if self.signal_kind == "claim_concept" and not self.supporting_claim_ids:
            raise ValueError("claim-concept signals require a supporting claim ID")
        return self


class DomainScoreComponentsV1(StrictModel):
    exact_claim_concept_matches: int = Field(ge=0)
    exact_heading_matches: int = Field(ge=0)
    claim_evidence_ref_count: int = Field(ge=0)
    context_evidence_ref_count: int = Field(ge=0)


class DomainAssignmentCandidateV1(StrictModel):
    schema_version: Literal["domain_assignment_candidate.v1"]
    assignment_id: str = Field(min_length=1)
    target_artifact_id: str = Field(min_length=1)
    domain_id: DomainId
    assignment_role: DomainAssignmentRole
    assignment_state: Literal["candidate"]
    derivation_method: Literal[
        "exact_claim_concept",
        "exact_section_heading",
        "exact_claim_concept_and_heading",
    ]
    matched_signal_ids: list[str]
    matched_normalized_terms: list[str]
    evidence_ref_ids: list[str]
    supporting_claim_ids: list[str]
    score_components: DomainScoreComponentsV1
    domain_registry: Literal["domain_registry"]
    domain_registry_version: Literal["v1"]
    domain_registry_hash: str = Field(min_length=1)
    resolution_policy: Literal["domain_resolution_policy"]
    resolution_policy_version: Literal["v1"]
    resolution_policy_hash: str = Field(min_length=1)
    resolution_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_assignment(self) -> "DomainAssignmentCandidateV1":
        for values, label in (
            (self.matched_signal_ids, "matched signal IDs"),
            (self.matched_normalized_terms, "matched normalized terms"),
            (self.evidence_ref_ids, "assignment evidence"),
        ):
            _validate_unique_nonempty(values, label)
            if values != sorted(values):
                raise ValueError(f"{label} must be sorted")
        if self.supporting_claim_ids != sorted(set(self.supporting_claim_ids)):
            raise ValueError("supporting claim IDs must be sorted and unique")
        dominant = self.score_components.exact_claim_concept_matches > 0
        if dominant != (self.assignment_role == "dominant"):
            raise ValueError("claim-local evidence must determine dominant role")
        expected_method = (
            "exact_claim_concept_and_heading"
            if self.score_components.exact_claim_concept_matches
            and self.score_components.exact_heading_matches
            else (
                "exact_claim_concept"
                if self.score_components.exact_claim_concept_matches
                else "exact_section_heading"
            )
        )
        if self.derivation_method != expected_method:
            raise ValueError("domain derivation method disagrees with score components")
        return self


class UnresolvedDomainSignalV1(StrictModel):
    schema_version: Literal["unresolved_domain_signal.v1"]
    unresolved_id: str = Field(min_length=1)
    target_artifact_id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    surface: str = Field(min_length=1)
    normalized_term: str
    signal_kind: DomainSignalKind
    evidence_ref_ids: list[str]
    supporting_claim_ids: list[str]
    assignment_state: Literal["unresolved"]
    reason: Literal["no_exact_domain_registry_match"]
    normalizer_id: Literal["corpus_lexicon.normalize_identity.v1"]

    @model_validator(mode="after")
    def validate_unresolved(self) -> "UnresolvedDomainSignalV1":
        if not self.surface.strip():
            raise ValueError("unresolved domain surface must be nonempty")
        _validate_unique_nonempty(self.evidence_ref_ids, "unresolved evidence")
        if self.evidence_ref_ids != sorted(self.evidence_ref_ids):
            raise ValueError("unresolved evidence must be sorted")
        if self.supporting_claim_ids != sorted(set(self.supporting_claim_ids)):
            raise ValueError(
                "unresolved supporting claim IDs must be sorted and unique"
            )
        return self


class DomainResolutionV1(StrictModel):
    schema_version: Literal["domain_resolution.v1"]
    target_artifact_id: str = Field(min_length=1)
    assignments: list[DomainAssignmentCandidateV1]
    unresolved_signals: list[UnresolvedDomainSignalV1]
    context_profile_ids: list[str]
    domain_registry_hash: str = Field(min_length=1)
    resolution_policy_hash: str = Field(min_length=1)
    resolution_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_resolution(self) -> "DomainResolutionV1":
        assignment_ids = [item.assignment_id for item in self.assignments]
        domain_ids = [item.domain_id for item in self.assignments]
        unresolved_ids = [item.unresolved_id for item in self.unresolved_signals]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("domain assignment IDs must be unique")
        if domain_ids != sorted(set(domain_ids)):
            raise ValueError("resolved domain IDs must be sorted and unique")
        if unresolved_ids != sorted(set(unresolved_ids)):
            raise ValueError("unresolved domain IDs must be sorted and unique")
        if self.context_profile_ids != sorted(set(self.context_profile_ids)):
            raise ValueError("context profile IDs must be sorted and unique")
        for assignment in self.assignments:
            if assignment.target_artifact_id != self.target_artifact_id:
                raise ValueError("domain assignment target ownership drifted")
            if (
                assignment.domain_registry_hash != self.domain_registry_hash
                or assignment.resolution_policy_hash != self.resolution_policy_hash
                or assignment.resolution_recipe_hash != self.resolution_recipe_hash
            ):
                raise ValueError("domain assignment recipe identity drifted")
        if any(
            item.target_artifact_id != self.target_artifact_id
            for item in self.unresolved_signals
        ):
            raise ValueError("unresolved domain target ownership drifted")
        return self

    def receipt(self, *, top_n: int = 10) -> dict[str, Any]:
        """Return assignment counts plus CP5 unresolved alias evidence."""

        if top_n < 0:
            raise ValueError("top_n must be nonnegative")
        terms = Counter(item.normalized_term for item in self.unresolved_signals)
        top_terms = sorted(terms.items(), key=lambda item: (-item[1], item[0]))[
            :top_n
        ]
        return {
            "resolution_recipe_hash": self.resolution_recipe_hash,
            "assignment_count": len(self.assignments),
            "dominant_count": sum(
                item.assignment_role == "dominant" for item in self.assignments
            ),
            "supporting_count": sum(
                item.assignment_role == "supporting" for item in self.assignments
            ),
            "unresolved_count": len(self.unresolved_signals),
            "top_unresolved_terms": [
                {"normalized_term": term, "count": count}
                for term, count in top_terms
            ],
            "unresolved_destination": "CP5_alias_registry_evidence",
            "unresolved_acted_on": False,
        }


class DomainAffinityPriorV1(StrictModel):
    domain_id: DomainId
    dominant_superframe_ids: list[FrameId]

    @model_validator(mode="after")
    def validate_prior(self) -> "DomainAffinityPriorV1":
        if not self.dominant_superframe_ids:
            raise ValueError("domain affinity prior cannot be empty")
        if len(self.dominant_superframe_ids) != len(
            set(self.dominant_superframe_ids)
        ):
            raise ValueError("domain affinity superframes must be unique")
        return self


class DomainAffinityServeViewV1(StrictModel):
    schema_version: Literal["domain_affinity_serve_view.v1"]
    target_artifact_id: str = Field(min_length=1)
    priors: list[DomainAffinityPriorV1]
    affinity_registry: Literal["domain_superframe_affinity"]
    affinity_registry_version: Literal["v1"]
    affinity_registry_hash: str = Field(min_length=1)
    serve_only: Literal[True]
    excluded_from_semantic_identity: Literal[True]
    excluded_from_acceptance: Literal[True]

    @model_validator(mode="after")
    def validate_view(self) -> "DomainAffinityServeViewV1":
        domain_ids = [item.domain_id for item in self.priors]
        if domain_ids != sorted(set(domain_ids)):
            raise ValueError("affinity view domains must be sorted and unique")
        return self


class SuperframeRuleMatchV1(StrictModel):
    schema_version: Literal["superframe_rule_match.v1"]
    match_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    frame_id: FrameId
    predicate_type: PredicateType
    assignment_state: Literal["candidate"]
    derivation_method: Literal["predicate_superframe_rule"]
    evidence_ref_ids: list[str]
    priority: int = Field(ge=0)
    terminal: Literal[True]
    owner_attention: bool
    rule_registry: Literal["superframe_rule_registry"]
    rule_registry_version: Literal["v1"]
    rule_registry_hash: str = Field(min_length=1)
    resolution_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_match(self) -> "SuperframeRuleMatchV1":
        _validate_unique_nonempty(self.evidence_ref_ids, "frame-match evidence")
        if self.evidence_ref_ids != sorted(self.evidence_ref_ids):
            raise ValueError("frame-match evidence must be sorted")
        return self


class SuperframeRuleResolutionV1(StrictModel):
    schema_version: Literal["superframe_rule_resolution.v1"]
    target_claim_id: str = Field(min_length=1)
    predicate_type: PredicateType | None
    matches: list[SuperframeRuleMatchV1]
    explicit_abstention_reason: Literal[
        "claim_is_untyped",
        "generic_association_is_not_a_mechanism",
    ] | None
    rule_registry_hash: str = Field(min_length=1)
    resolution_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rule_resolution(self) -> "SuperframeRuleResolutionV1":
        match_ids = [item.match_id for item in self.matches]
        if match_ids != sorted(set(match_ids)):
            raise ValueError("superframe match IDs must be sorted and unique")
        if len(self.matches) > 1:
            raise ValueError("terminal v1 superframe rules may emit at most one match")
        if bool(self.matches) == (self.explicit_abstention_reason is not None):
            raise ValueError("resolution requires either a match or an abstention")
        if (
            self.predicate_type is None
            and self.explicit_abstention_reason != "claim_is_untyped"
        ):
            raise ValueError("untyped claim must use the explicit untyped abstention")
        if (
            self.predicate_type is not None
            and self.explicit_abstention_reason == "claim_is_untyped"
        ):
            raise ValueError("typed claim cannot use the untyped abstention")
        for match in self.matches:
            if match.claim_id != self.target_claim_id:
                raise ValueError("superframe match claim ownership drifted")
            if match.predicate_type != self.predicate_type:
                raise ValueError("superframe match predicate drifted")
            if (
                match.rule_registry_hash != self.rule_registry_hash
                or match.resolution_recipe_hash != self.resolution_recipe_hash
            ):
                raise ValueError("superframe match recipe identity drifted")
        return self

    def receipt(self) -> dict[str, Any]:
        return {
            "resolution_recipe_hash": self.resolution_recipe_hash,
            "match_count": len(self.matches),
            "frame_ids": [item.frame_id for item in self.matches],
            "explicit_abstention_reason": self.explicit_abstention_reason,
            "owner_attention_count": sum(
                item.owner_attention for item in self.matches
            ),
        }
