from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


AssignmentState = Literal[
    "candidate", "corroborated", "validated", "unresolved", "rejected"
]
SemanticRole = Literal["dominant", "supporting", "adjacent", "exploratory"]
FrameId = Literal[
    "MF01",
    "MF02",
    "MF03",
    "MF04",
    "MF05",
    "MF06",
    "MF07",
    "MF08",
    "MF09",
    "MF10",
    "MF11",
    "MF12",
    "MF13",
    "MF14",
    "MF15",
    "MF16",
]


class SupportedStatement(StrictModel):
    text: str
    supporting_claim_ids: list[str] = Field()


class DomainProposal(StrictModel):
    registry_id: str
    proposed_label: str
    role: SemanticRole
    assignment_state: AssignmentState
    supporting_claim_ids: list[str] = Field()


class FrameProposal(StrictModel):
    frame_id: FrameId
    role: SemanticRole
    assignment_state: AssignmentState
    supporting_claim_ids: list[str] = Field()
    explanation: str


class LatentConceptProposal(StrictModel):
    preferred_label: str
    definition: str
    assignment_state: AssignmentState
    supporting_claim_ids: list[str] = Field()
    aliases: list[str] = Field()


class MotifProposal(StrictModel):
    proposed_label: str
    frame_sequence: list[FrameId] = Field()
    abstract_sequence: list[str] = Field()
    supporting_claim_ids: list[str] = Field()


class SemanticDigestV1(StrictModel):
    schema_version: Literal["semantic_digest.v1"]
    parent_id: str
    summary: str
    central_thesis: str
    underlying_meanings: list[SupportedStatement] = Field()
    domain_proposals: list[DomainProposal] = Field()
    frame_proposals: list[FrameProposal] = Field()
    latent_concepts: list[LatentConceptProposal] = Field()
    motif_proposals: list[MotifProposal] = Field()
    conditions: list[SupportedStatement] = Field()
    exceptions: list[SupportedStatement] = Field()
    unresolved_interpretations: list[str] = Field()
