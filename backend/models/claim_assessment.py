"""Candidate-only negation and relation-signature assessment contracts.

These sidecars extend the claim compiler without changing the frozen
``LocalExtractionV1`` or ``ClaimRecordV1`` field sets.  They preserve exact
negation/evidence coordinates and annotate typed-signature compatibility;
they never promote, drop, or remap a relation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.local_extraction import EntityType, Polarity, PredicateType


CLAIM_ASSESSMENT_AUTHORITY = "executor-proposed, owner-ratifiable"
CLAIM_ASSESSMENT_OWNER_RATIFICATION_REQUIRED = True
CLAIM_ASSESSMENT_CHANGE_POLICY = "changes-require-new-schema-version"

NegationDerivation = Literal[
    "predicate_and_qualifier_agree",
    "predicate_only",
    "qualifier_only",
    "not_negated",
]
DependencyConflictReason = Literal[
    "predicate_not_compiled",
    "source_endpoint_disagrees",
    "target_endpoint_disagrees",
    "evidence_sentence_disagrees",
    "multiple_disagreements",
    "compiler_rejected_unspecified",
]
SignatureAssessmentReason = Literal[
    "predicate_mapping_unavailable",
    "source_type_mapping_unavailable",
    "target_type_mapping_unavailable",
    "signature_contract_unavailable",
    "subject_type_not_allowed",
    "target_type_not_allowed",
    "subject_and_target_type_not_allowed",
]
PolarityConflictReason = Literal[
    "predicate_flag_without_attached_cue",
    "attached_cue_missing_predicate_flag",
    "relation_disagrees_with_compiled_claim",
]


class StrictAssessmentModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "authority": CLAIM_ASSESSMENT_AUTHORITY,
            "owner_ratification_required": (
                CLAIM_ASSESSMENT_OWNER_RATIFICATION_REQUIRED
            ),
            "change_policy": CLAIM_ASSESSMENT_CHANGE_POLICY,
        },
    )


class AssessmentProvenanceV1(StrictAssessmentModel):
    corpus_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    engine: str = Field(min_length=1)


class EvidenceSentenceBoundaryV1(StrictAssessmentModel):
    evidence_sentence_id: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    quote_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_boundary(self) -> "EvidenceSentenceBoundaryV1":
        if self.end_char <= self.start_char:
            raise ValueError("evidence sentence boundary must be positive")
        return self


class NegationCueV1(StrictAssessmentModel):
    qualifier_observation_id: str = Field(min_length=1)
    cue: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    producer: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_boundary(self) -> "NegationCueV1":
        if self.end_char <= self.start_char:
            raise ValueError("negation cue boundary must be positive")
        return self


class ClaimNegationAssessmentV1(StrictAssessmentModel):
    schema_version: Literal["claim_negation_assessment.v1"]
    claim_id: str = Field(min_length=1)
    predicate_observation_id: str = Field(min_length=1)
    negated: bool
    negation_cues: list[NegationCueV1]
    evidence_sentences: list[EvidenceSentenceBoundaryV1]
    derivation: NegationDerivation
    knowledge_status: Literal["candidate"]
    validation_status: Literal["candidate"]

    @model_validator(mode="after")
    def validate_evidence(self) -> "ClaimNegationAssessmentV1":
        sentence_ids = [item.evidence_sentence_id for item in self.evidence_sentences]
        cue_ids = [item.qualifier_observation_id for item in self.negation_cues]
        if not sentence_ids:
            raise ValueError("claim negation assessment requires sentence evidence")
        if len(sentence_ids) != len(set(sentence_ids)):
            raise ValueError("claim evidence sentence boundaries must be unique")
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError("claim negation cues must be unique")
        if any(
            not any(
                sentence.start_char <= cue.start_char
                and cue.end_char <= sentence.end_char
                for sentence in self.evidence_sentences
            )
            for cue in self.negation_cues
        ):
            raise ValueError("claim negation cues must fall inside sentence evidence")
        return self


class RelationSemanticAssessmentV1(StrictAssessmentModel):
    schema_version: Literal["relation_semantic_assessment.v1"]
    relation_id: str = Field(min_length=1)
    predicate_id: str = Field(min_length=1)
    relation_type: PredicateType
    source_mention_id: str = Field(min_length=1)
    source_entity_type: EntityType
    target_mention_id: str = Field(min_length=1)
    target_entity_type: EntityType
    claim_id: str | None
    dependency_agrees: bool
    dependency_conflict_reason: DependencyConflictReason | None
    negated: bool
    negation_cues: list[NegationCueV1]
    evidence_sentences: list[EvidenceSentenceBoundaryV1]
    negation_derivation: NegationDerivation
    negation_source_agrees: bool
    claim_polarity: Polarity | None
    claim_polarity_agrees: bool | None
    polarity_conflict_reasons: list[PolarityConflictReason]
    signature_predicate: str | None
    signature_source_type: str | None
    signature_target_type: str | None
    signature_valid: bool | None
    signature_violation_reason: SignatureAssessmentReason | None
    signature_contract_id: Literal["relation_signature_adapter.v1"]
    signature_contract_hash: str = Field(min_length=1)
    observation_only: Literal[True]
    promotion_disposition: Literal["candidate_only", "owner_pending_negated"]
    knowledge_status: Literal["candidate"]
    validation_status: Literal["candidate"]

    @model_validator(mode="after")
    def validate_assessment(self) -> "RelationSemanticAssessmentV1":
        if self.dependency_agrees != (self.claim_id is not None):
            raise ValueError("only dependency-agreeing relations may attach to a claim")
        if self.dependency_agrees != (self.dependency_conflict_reason is None):
            raise ValueError("dependency disagreement requires exactly one reason")
        if self.dependency_agrees != (self.claim_polarity is not None):
            raise ValueError("only attached relations may carry claim polarity")
        if self.dependency_agrees != (self.claim_polarity_agrees is not None):
            raise ValueError("attached relations require a polarity comparison")
        if not self.evidence_sentences:
            raise ValueError("relation assessment requires sentence evidence")
        sentence_ids = [item.evidence_sentence_id for item in self.evidence_sentences]
        if len(sentence_ids) != len(set(sentence_ids)):
            raise ValueError("relation evidence sentence boundaries must be unique")
        cue_ids = [item.qualifier_observation_id for item in self.negation_cues]
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError("relation negation cues must be unique")
        if any(
            not any(
                sentence.start_char <= cue.start_char
                and cue.end_char <= sentence.end_char
                for sentence in self.evidence_sentences
            )
            for cue in self.negation_cues
        ):
            raise ValueError(
                "relation negation cues must fall inside sentence evidence"
            )
        if self.signature_valid is True and self.signature_violation_reason is not None:
            raise ValueError("valid signatures cannot carry a violation reason")
        if self.signature_valid is not True and self.signature_violation_reason is None:
            raise ValueError("invalid or unassessed signatures require a reason")
        unavailable_reasons = {
            "predicate_mapping_unavailable",
            "source_type_mapping_unavailable",
            "target_type_mapping_unavailable",
            "signature_contract_unavailable",
        }
        if self.signature_valid is None and (
            self.signature_violation_reason not in unavailable_reasons
        ):
            raise ValueError("unassessed signatures require an unavailable reason")
        if self.signature_valid is False and (
            self.signature_violation_reason in unavailable_reasons
        ):
            raise ValueError("invalid signatures require a compatibility violation")
        if self.signature_valid is not None and not all(
            (
                self.signature_predicate,
                self.signature_source_type,
                self.signature_target_type,
            )
        ):
            raise ValueError("assessed signatures require mapped predicate and types")
        expected_conflicts: set[str] = set()
        if self.negation_derivation == "predicate_only":
            expected_conflicts.add("predicate_flag_without_attached_cue")
        elif self.negation_derivation == "qualifier_only":
            expected_conflicts.add("attached_cue_missing_predicate_flag")
        expected_source_agreement = self.negation_derivation in {
            "predicate_and_qualifier_agree",
            "not_negated",
        }
        if self.negation_source_agrees != expected_source_agreement:
            raise ValueError("negation-source agreement must match derivation")
        if self.claim_polarity_agrees is False:
            expected_conflicts.add("relation_disagrees_with_compiled_claim")
        if set(self.polarity_conflict_reasons) != expected_conflicts:
            raise ValueError("polarity conflict reasons must close exactly")
        if len(self.polarity_conflict_reasons) != len(
            set(self.polarity_conflict_reasons)
        ):
            raise ValueError("polarity conflict reasons must be unique")
        expected_disposition = (
            "owner_pending_negated"
            if self.negated or self.polarity_conflict_reasons
            else "candidate_only"
        )
        if self.promotion_disposition != expected_disposition:
            raise ValueError("promotion disposition must preserve negation policy")
        return self


class ClaimSemanticAssessmentV1(StrictAssessmentModel):
    schema_version: Literal["claim_semantic_assessment.v1"]
    document_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    provenance: AssessmentProvenanceV1
    claim_negation_assessments: list[ClaimNegationAssessmentV1]
    relation_assessments: list[RelationSemanticAssessmentV1]
    signature_contract_id: Literal["relation_signature_adapter.v1"]
    signature_contract_hash: str = Field(min_length=1)
    assessment_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reference_uniqueness(self) -> "ClaimSemanticAssessmentV1":
        claim_ids = [item.claim_id for item in self.claim_negation_assessments]
        relation_ids = [item.relation_id for item in self.relation_assessments]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim negation assessment IDs must be unique")
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("relation semantic assessment IDs must be unique")
        if any(
            item.signature_contract_hash != self.signature_contract_hash
            for item in self.relation_assessments
        ):
            raise ValueError("relation assessments must share the active contract")
        return self

    def receipt(self) -> dict[str, Any]:
        """Return count-only assessment metrics with required rate dimensions."""

        by_predicate: dict[str, dict[str, int]] = {}
        for item in self.relation_assessments:
            counts = by_predicate.setdefault(
                item.relation_type,
                {
                    "relations": 0,
                    "negated": 0,
                    "polarity_conflict": 0,
                    "signature_valid": 0,
                    "signature_invalid": 0,
                    "signature_unassessed": 0,
                },
            )
            counts["relations"] += 1
            counts["negated"] += int(item.negated)
            counts["polarity_conflict"] += int(bool(item.polarity_conflict_reasons))
            if item.signature_valid is True:
                counts["signature_valid"] += 1
            elif item.signature_valid is False:
                counts["signature_invalid"] += 1
            else:
                counts["signature_unassessed"] += 1

        assessed = [
            item
            for item in self.relation_assessments
            if item.signature_valid is not None
        ]
        return {
            "assessment_recipe_hash": self.assessment_recipe_hash,
            "signature_contract_id": self.signature_contract_id,
            "signature_contract_hash": self.signature_contract_hash,
            "corpus_id": self.provenance.corpus_id,
            "provider": self.provenance.provider,
            "model": self.provenance.model,
            "engine": self.provenance.engine,
            "claim_count": len(self.claim_negation_assessments),
            "negated_claim_count": sum(
                item.negated for item in self.claim_negation_assessments
            ),
            "relation_count": len(self.relation_assessments),
            "negated_relation_count": sum(
                item.negated for item in self.relation_assessments
            ),
            "polarity_conflict_count": sum(
                bool(item.polarity_conflict_reasons)
                for item in self.relation_assessments
            ),
            "dependency_agree_count": sum(
                item.dependency_agrees for item in self.relation_assessments
            ),
            "dependency_conflict_count": sum(
                not item.dependency_agrees for item in self.relation_assessments
            ),
            "signature_assessed_count": len(assessed),
            "signature_valid_count": sum(
                item.signature_valid is True for item in assessed
            ),
            "signature_invalid_count": sum(
                item.signature_valid is False for item in assessed
            ),
            "signature_unassessed_count": (
                len(self.relation_assessments) - len(assessed)
            ),
            "relations_by_predicate": {
                key: by_predicate[key] for key in sorted(by_predicate)
            },
        }
