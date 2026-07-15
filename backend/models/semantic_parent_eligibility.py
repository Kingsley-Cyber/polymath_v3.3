"""Strict result contract for deterministic parent-content eligibility."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EligibilityReason = Literal[
    "heading_only",
    "below_substantive_byte_min",
    "eligible",
]


class ParentEligibilityDecisionV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["semantic_parent_eligibility.v2"]
    eligible: bool
    reason: EligibilityReason
    heading_only: bool
    substantive_bytes: int = Field(ge=0)
    recipe_version: Literal["v2"]
    recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_reason(self) -> "ParentEligibilityDecisionV2":
        if self.eligible != (self.reason == "eligible"):
            raise ValueError("eligibility boolean and reason disagree")
        if self.heading_only != (self.reason == "heading_only"):
            raise ValueError("heading-only flag and reason disagree")
        if self.heading_only and self.substantive_bytes != 0:
            raise ValueError("heading-only text must have zero substantive bytes")
        return self
