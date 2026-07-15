"""Frozen selection policy for the sentence-hybrid v3 canary."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from models.semantic_digest_atomic_selection import AtomicB4SizeBandV1


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SentenceHybridFaithfulnessReviewV1(StrictModel):
    review_count: Literal[10]
    summary_and_thesis_must_be_supported_by_citable_sentence_units: Literal[True]
    context_only_units_may_be_cited: Literal[False]
    unsupported_synthesis_allowed: Literal[False]
    atomic_expansion_disposition: Literal[
        "record_expansion_never_treat_as_unique_model_intent"
    ]


class SentenceHybridCanarySelectionRecipeV1(StrictModel):
    schema_version: Literal[
        "polymath.semantic_digest_sentence_hybrid_canary_selection.v1"
    ]
    selection_name: Literal["mark-sentence-hybrid-v3.parent-digest-v6.canary.v1"]
    packet_schema_version: Literal["semantic_parent_packet.sentence_hybrid.v3"]
    prompt_version: Literal["parent-digest.v6"]
    target_count: Literal[10]
    population_rule: Literal["packet_ready_and_parent_not_previously_purchased"]
    historical_purchase_statuses: list[
        Literal[
            "blocked_missing_cached_artifact",
            "blocked_unrecognized_status",
            "dead_letter",
            "dead_letter_unknown_outcome",
            "succeeded",
        ]
    ]
    population_order: list[Literal["packet_utf8_bytes", "parent_id"]]
    percentile_basis_points_formula: Literal[
        "floor(rank_zero_based * 10000 / population_count)"
    ]
    bands: list[AtomicB4SizeBandV1]
    within_band_order: Literal["namespace_hash_work_recipe_parent_packet"]
    unique_document_across_selection: Literal[True]
    long_packet_threshold_bytes_exclusive: Literal[20000]
    minimum_long_packet_selection_count: Literal[1]
    long_packet_reservation_order: list[Literal["packet_utf8_bytes_desc", "parent_id"]]
    oversize_policy: Literal["fail_closed_no_sentence_drops"]
    summary_faithfulness_review: SentenceHybridFaithfulnessReviewV1

    @model_validator(mode="after")
    def validate_frozen_recipe(self) -> "SentenceHybridCanarySelectionRecipeV1":
        if self.historical_purchase_statuses != [
            "blocked_missing_cached_artifact",
            "blocked_unrecognized_status",
            "dead_letter",
            "dead_letter_unknown_outcome",
            "succeeded",
        ]:
            raise ValueError("historical purchase statuses are frozen")
        if self.population_order != ["packet_utf8_bytes", "parent_id"]:
            raise ValueError("sentence-hybrid population order is frozen")
        if self.long_packet_reservation_order != [
            "packet_utf8_bytes_desc",
            "parent_id",
        ]:
            raise ValueError("long-packet reservation order is frozen")
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
            raise ValueError("sentence-hybrid band definitions and counts are frozen")
        return self


def load_sentence_hybrid_canary_selection_recipe(
    path: Path,
) -> SentenceHybridCanarySelectionRecipeV1:
    return SentenceHybridCanarySelectionRecipeV1.model_validate_json(
        path.read_text(encoding="utf-8")
    )
