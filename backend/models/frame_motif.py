"""Strict candidate contracts for T9.2 frame bindings and motif matching."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.local_extraction import Modality, Polarity, PredicateType
from models.semantic_digest import FrameId


FRAME_MOTIF_AUTHORITY = "executor-proposed, owner-ratifiable"
FRAME_MOTIF_OWNER_RATIFICATION_REQUIRED = True
FRAME_MOTIF_CHANGE_POLICY = "changes-require-new-schema-version"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "authority": FRAME_MOTIF_AUTHORITY,
            "owner_ratification_required": (
                FRAME_MOTIF_OWNER_RATIFICATION_REQUIRED
            ),
            "change_policy": FRAME_MOTIF_CHANGE_POLICY,
        },
    )


class FrameRoleBindingV1(StrictModel):
    schema_version: Literal["frame_role_binding.v1"]
    binding_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    claim_argument_role: Literal["subject", "object"]
    relation_direction_role: Literal["source", "target"]
    filler_kind: Literal["entity_mention", "span_observation"]
    filler_ref: str = Field(min_length=1)
    span_observation_id: str = Field(min_length=1)
    thread_key: str = Field(min_length=1)
    surface: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    evidence_sentence_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_binding(self) -> "FrameRoleBindingV1":
        expected = "source" if self.claim_argument_role == "subject" else "target"
        if self.relation_direction_role != expected:
            raise ValueError("claim argument role disagrees with relation direction")
        if self.end_char <= self.start_char:
            raise ValueError("frame role binding offsets must form a positive span")
        return self


class FrameInstanceCandidateV1(StrictModel):
    schema_version: Literal["frame_instance_candidate.v1"]
    frame_instance_id: str = Field(min_length=1)
    frame_id: FrameId
    instance_role: Literal["primary"]
    source_claim_id: str = Field(min_length=1)
    source_rule_match_id: str = Field(min_length=1)
    source_rule_id: str = Field(min_length=1)
    normalized_predicate: PredicateType
    role_bindings: list[FrameRoleBindingV1]
    unbound_argument_count: Literal[0]
    unbound_argument_refs: list[str]
    direction: Literal["source_to_target"]
    polarity: Polarity
    modality: Modality
    conditions: list[str]
    exceptions: list[str]
    temporal_cues: list[str]
    evidence_sentence_ids: list[str]
    source_relation_ids: list[str]
    assignment_state: Literal["candidate"]
    accepted_state_written: Literal[False]
    derivation_method: Literal["predicate_superframe_rule_role_binding"]
    frame_registry_hash: str = Field(min_length=1)
    superframe_rule_registry_hash: str = Field(min_length=1)
    frame_role_binding_policy_hash: str = Field(min_length=1)
    compilation_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_frame_instance(self) -> "FrameInstanceCandidateV1":
        if not self.role_bindings:
            raise ValueError("frame candidates require real claim argument bindings")
        binding_ids = [item.binding_id for item in self.role_bindings]
        argument_keys = [
            (
                item.claim_argument_role,
                item.span_observation_id,
                item.filler_ref,
            )
            for item in self.role_bindings
        ]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("frame role binding IDs must be unique")
        if len(argument_keys) != len(set(argument_keys)):
            raise ValueError("frame role bindings must map unique claim arguments")
        if any(item.claim_id != self.source_claim_id for item in self.role_bindings):
            raise ValueError("frame role binding claim ownership drifted")
        if not any(
            item.relation_direction_role == "source" for item in self.role_bindings
        ):
            raise ValueError("frame candidates require a source binding")
        if self.unbound_argument_refs:
            raise ValueError("ClaimRecordV1 admits no unbound argument roles")
        for values, label in (
            (self.evidence_sentence_ids, "frame evidence sentence IDs"),
            (self.source_relation_ids, "frame source relation IDs"),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"{label} must be sorted and unique")
        known_evidence = set(self.evidence_sentence_ids)
        if any(
            item.evidence_sentence_id not in known_evidence
            for item in self.role_bindings
        ):
            raise ValueError("frame binding references unknown sentence evidence")
        for values, label in (
            (self.conditions, "conditions"),
            (self.exceptions, "exceptions"),
            (self.temporal_cues, "temporal cues"),
        ):
            if len(values) != len(set(values)) or any(not item for item in values):
                raise ValueError(f"frame {label} must be nonempty and unique")
        return self


class FrameSequenceItemV1(StrictModel):
    schema_version: Literal["frame_sequence_item.v1"]
    sequence_index: int = Field(ge=0)
    frame_instance_id: str = Field(min_length=1)


class MotifStageMatchV1(StrictModel):
    schema_version: Literal["motif_stage_match.v1"]
    stage_index: int = Field(ge=0)
    stage: str = Field(min_length=1)
    frame_instance_id: str = Field(min_length=1)
    frame_id: FrameId
    match_tier: Literal["dominant", "admissible"]


class RoleThreadTransitionV1(StrictModel):
    schema_version: Literal["role_thread_transition.v1"]
    transition_index: int = Field(ge=0)
    prior_frame_instance_id: str = Field(min_length=1)
    next_frame_instance_id: str = Field(min_length=1)
    classification: Literal[
        "directional",
        "shared_participant",
        "disconnected",
    ]
    shared_thread_keys: list[str]

    @model_validator(mode="after")
    def validate_transition(self) -> "RoleThreadTransitionV1":
        if self.shared_thread_keys != sorted(set(self.shared_thread_keys)):
            raise ValueError("shared thread keys must be sorted and unique")
        connected = self.classification != "disconnected"
        if connected != bool(self.shared_thread_keys):
            raise ValueError("transition classification disagrees with shared keys")
        return self


class MotifScoreComponentsV1(StrictModel):
    canonical_stage_count: int = Field(gt=0)
    matched_stage_count: int = Field(ge=0)
    dominant_stage_count: int = Field(ge=0)
    admissible_stage_count: int = Field(ge=0)
    transition_count: int = Field(ge=0)
    directional_transition_count: int = Field(ge=0)
    shared_participant_transition_count: int = Field(ge=0)
    disconnected_transition_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "MotifScoreComponentsV1":
        if self.matched_stage_count != (
            self.dominant_stage_count + self.admissible_stage_count
        ):
            raise ValueError("motif stage-tier accounting must close")
        if self.transition_count != (
            self.directional_transition_count
            + self.shared_participant_transition_count
            + self.disconnected_transition_count
        ):
            raise ValueError("motif transition accounting must close")
        return self


MotifDisposition = Literal["confirmed_candidate", "provisional", "rejected"]
MotifRejectionReason = Literal[
    "no_role_continuity",
    "required_condition_missing",
]


class MotifCandidateV1(StrictModel):
    schema_version: Literal["motif_candidate.v1"]
    motif_candidate_id: str = Field(min_length=1)
    motif_id: str = Field(min_length=1)
    frame_instance_ids: list[str]
    source_claim_ids: list[str]
    stage_matches: list[MotifStageMatchV1]
    role_transitions: list[RoleThreadTransitionV1]
    score_components: MotifScoreComponentsV1
    sequence_alignment: float = Field(ge=0.0, le=1.0)
    sequence_alignment_interpretation: Literal[
        "definitional_under_strict_v1_not_quality_signal"
    ]
    role_continuity: float = Field(ge=0.0, le=1.0)
    matcher_disposition: MotifDisposition
    assignment_state: Literal["candidate", "rejected"]
    rejection_reasons: list[MotifRejectionReason]
    qualifier_status: Literal[
        "not_applicable",
        "satisfied",
        "required_condition_missing",
    ]
    evidence_sentence_ids: list[str]
    accepted_state_written: Literal[False]
    derivation_method: Literal["strict_sequence_and_exact_role_threading"]
    motif_registry_hash: str = Field(min_length=1)
    stage_binding_registry_hash: str = Field(min_length=1)
    motif_matching_policy_hash: str = Field(min_length=1)
    matcher_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate(self) -> "MotifCandidateV1":
        if self.frame_instance_ids != [
            item.frame_instance_id for item in self.stage_matches
        ]:
            raise ValueError("motif frame sequence must match stage bindings")
        if len(self.frame_instance_ids) != len(set(self.frame_instance_ids)):
            raise ValueError("motif frame instance IDs must be unique")
        if [item.stage_index for item in self.stage_matches] != list(
            range(len(self.stage_matches))
        ):
            raise ValueError("motif stage indices must be contiguous")
        if [item.transition_index for item in self.role_transitions] != list(
            range(len(self.role_transitions))
        ):
            raise ValueError("role transition indices must be contiguous")
        components = self.score_components
        if components.canonical_stage_count != len(self.stage_matches):
            raise ValueError("canonical motif stage count drifted")
        if components.matched_stage_count != len(self.stage_matches):
            raise ValueError("strict motif candidates must match every stage")
        if components.transition_count != len(self.role_transitions):
            raise ValueError("motif transition count drifted")
        if components.transition_count <= 0:
            raise ValueError("motif candidates require at least one transition")
        for index, transition in enumerate(self.role_transitions):
            if (
                transition.prior_frame_instance_id != self.frame_instance_ids[index]
                or transition.next_frame_instance_id
                != self.frame_instance_ids[index + 1]
            ):
                raise ValueError("role transition does not follow the frame sequence")
        expected_alignment = (
            components.matched_stage_count / components.canonical_stage_count
        )
        connected = (
            components.directional_transition_count
            + components.shared_participant_transition_count
        )
        expected_continuity = connected / components.transition_count
        if abs(self.sequence_alignment - expected_alignment) > 1e-12:
            raise ValueError("sequence alignment disagrees with raw components")
        if abs(self.role_continuity - expected_continuity) > 1e-12:
            raise ValueError("role continuity disagrees with raw components")
        if self.sequence_alignment != 1.0:
            raise ValueError("strict-v1 emitted windows have definitional alignment 1")

        expected_reasons: list[str] = []
        if connected == 0:
            expected_reasons.append("no_role_continuity")
        if self.qualifier_status == "required_condition_missing":
            expected_reasons.append("required_condition_missing")
        if self.rejection_reasons != sorted(expected_reasons):
            raise ValueError("motif rejection reasons must close exactly")
        if expected_reasons:
            expected_disposition = "rejected"
        elif connected == components.transition_count:
            expected_disposition = "confirmed_candidate"
        else:
            expected_disposition = "provisional"
        if self.matcher_disposition != expected_disposition:
            raise ValueError("motif disposition disagrees with exact components")
        expected_state = (
            "rejected" if expected_disposition == "rejected" else "candidate"
        )
        if self.assignment_state != expected_state:
            raise ValueError("motif assignment state disagrees with disposition")
        for values, label in (
            (self.source_claim_ids, "motif source claim IDs"),
            (self.evidence_sentence_ids, "motif evidence sentence IDs"),
        ):
            if values != sorted(set(values)):
                raise ValueError(f"{label} must be sorted and unique")
        return self


class MotifMatchResultV1(StrictModel):
    schema_version: Literal["motif_match_result.v1"]
    target_artifact_id: str = Field(min_length=1)
    sequence_items: list[FrameSequenceItemV1]
    candidates: list[MotifCandidateV1]
    windows_scanned: int = Field(ge=0)
    sequence_aligned_window_count: int = Field(ge=0)
    matcher_recipe_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> "MotifMatchResultV1":
        indices = [item.sequence_index for item in self.sequence_items]
        frame_ids = [item.frame_instance_id for item in self.sequence_items]
        if indices != list(range(len(indices))):
            raise ValueError("caller-supplied sequence indices must be contiguous")
        if len(frame_ids) != len(set(frame_ids)):
            raise ValueError("frame sequence instance IDs must be unique")
        candidate_ids = [item.motif_candidate_id for item in self.candidates]
        if candidate_ids != sorted(set(candidate_ids)):
            raise ValueError("motif candidate IDs must be sorted and unique")
        known_frames = set(frame_ids)
        if any(
            set(item.frame_instance_ids) - known_frames for item in self.candidates
        ):
            raise ValueError("motif candidate references frame outside the sequence")
        if self.sequence_aligned_window_count != len(self.candidates):
            raise ValueError("sequence-aligned window accounting must close")
        if any(
            item.matcher_recipe_hash != self.matcher_recipe_hash
            for item in self.candidates
        ):
            raise ValueError("motif candidate matcher recipe drifted")
        return self

    def receipt(self) -> dict[str, Any]:
        dispositions = Counter(item.matcher_disposition for item in self.candidates)
        motifs = Counter(item.motif_id for item in self.candidates)
        return {
            "matcher_recipe_hash": self.matcher_recipe_hash,
            "frame_sequence_count": len(self.sequence_items),
            "windows_scanned": self.windows_scanned,
            "sequence_aligned_window_count": self.sequence_aligned_window_count,
            "candidate_count": sum(
                item.assignment_state == "candidate" for item in self.candidates
            ),
            "rejected_count": sum(
                item.assignment_state == "rejected" for item in self.candidates
            ),
            "dispositions": dict(sorted(dispositions.items())),
            "motif_counts": dict(sorted(motifs.items())),
            "sequence_alignment_is_definitional": True,
            "accepted_state_count": 0,
        }
