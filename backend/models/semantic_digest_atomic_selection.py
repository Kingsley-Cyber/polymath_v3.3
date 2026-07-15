"""Strict B4 atomic-packet selection and review policy contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AtomicB4SizeBandV1(StrictModel):
    band_id: Literal[
        "q00_q25",
        "q25_q50",
        "q50_q75",
        "q75_q90",
        "top_decile_q90_q100",
    ]
    lower_basis_points_inclusive: int = Field(ge=0, lt=10_000)
    upper_basis_points_exclusive: int = Field(gt=0, le=10_000)
    selection_count: Literal[2]

    @model_validator(mode="after")
    def validate_bounds(self) -> "AtomicB4SizeBandV1":
        if self.lower_basis_points_inclusive >= self.upper_basis_points_exclusive:
            raise ValueError("B4 band bounds must be increasing")
        return self


class SummaryFaithfulnessReviewV1(StrictModel):
    review_count: Literal[10]
    summary_and_thesis_must_be_supported_by_emitted_claims: Literal[True]
    unsupported_synthesis_allowed: Literal[False]
    claim_list_prose_disposition: Literal[
        "surface_per_packet_and_require_senior_review"
    ]
    excluded_claims_may_be_required_for_faithfulness: Literal[False]
    provider_sparse_proposals_are_automatic_failure: Literal[False]


class AtomicB4SelectionRecipeV1(StrictModel):
    schema_version: Literal["polymath.semantic_digest_atomic_b4_selection.v1"]
    selection_name: Literal["mark-b4.atomic-claims-v2.parent-digest-v6.v1"]
    packet_schema_version: Literal["semantic_parent_packet.atomic_claims.v2"]
    prompt_version: Literal["parent-digest.v6"]
    target_count: Literal[10]
    population_rule: Literal["packet_ready_and_not_historically_purchased"]
    historical_purchase_statuses: list[Literal["dead_letter", "succeeded"]]
    population_order: list[Literal["packet_utf8_bytes", "parent_id"]]
    percentile_basis_points_formula: Literal[
        "floor(rank_zero_based * 10000 / population_count)"
    ]
    bands: list[AtomicB4SizeBandV1]
    within_band_order: Literal["namespace_hash_work_recipe_parent_packet"]
    unique_document_across_selection: Literal[True]
    oversize_policy: Literal["fail_closed"]
    summary_faithfulness_review: SummaryFaithfulnessReviewV1

    @model_validator(mode="after")
    def validate_frozen_recipe(self) -> "AtomicB4SelectionRecipeV1":
        if self.historical_purchase_statuses != ["dead_letter", "succeeded"]:
            raise ValueError("historical purchase statuses are frozen")
        if self.population_order != ["packet_utf8_bytes", "parent_id"]:
            raise ValueError("B4 population order is frozen")
        expected = [
            ("q00_q25", 0, 2500),
            ("q25_q50", 2500, 5000),
            ("q50_q75", 5000, 7500),
            ("q75_q90", 7500, 9000),
            ("top_decile_q90_q100", 9000, 10_000),
        ]
        actual = [
            (
                band.band_id,
                band.lower_basis_points_inclusive,
                band.upper_basis_points_exclusive,
            )
            for band in self.bands
        ]
        if actual != expected or sum(band.selection_count for band in self.bands) != 10:
            raise ValueError("B4 band definitions and counts are frozen")
        return self


def load_atomic_b4_selection_recipe(
    path: Path,
) -> AtomicB4SelectionRecipeV1:
    return AtomicB4SelectionRecipeV1.model_validate_json(
        path.read_text(encoding="utf-8")
    )
