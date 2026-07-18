from __future__ import annotations

from scripts.run_two_lane_zero_provider_diagnosis import (
    _compare_stages,
    _stage_inputs,
)


def _surface(*, candidates=("a", "b"), budget=2, lanes=("anchor", "expansion")):
    selected = [
        {
            "candidate_id": candidate,
            "lane": lane,
            "side": "__all__",
            "matched_fields": ["title"] if lane == "anchor" else [],
        }
        for candidate, lane in zip(candidates, lanes, strict=True)
    ]
    return {
        "selected_identity": [
            {
                "seat": index,
                "candidate_id": row["candidate_id"],
                "lane": row["lane"],
                "side": row["side"],
            }
            for index, row in enumerate(selected, start=1)
        ],
        "diagnostics": {
            "budget": budget,
            "anchor_seats": 1,
            "expansion_seats": budget - 1,
            "groups": [
                {
                    "side": "__all__",
                    "budget": budget,
                    "anchor_quota": 1,
                    "expansion_quota": budget - 1,
                    "anchor_primary_filled": 1,
                    "expansion_primary_filled": budget - 1,
                    "anchor_candidate_ids": list(candidates),
                }
            ],
            "selected": selected,
        },
    }


def test_stage_inputs_preserve_candidate_order():
    receipt = _stage_inputs(_surface(candidates=("b", "a")))

    assert receipt["anchor_candidate_pool_order"][0]["candidate_ids"] == ["b", "a"]


def test_compare_stages_identifies_candidate_order_first():
    comparison = _compare_stages(
        _surface(candidates=("a", "b")),
        _surface(candidates=("b", "a")),
    )

    assert comparison["identical"] is False
    assert comparison["first_divergent_stage"] == "anchor_candidate_pool_order"
    assert comparison["comparisons"]["quota_math"] is True


def test_compare_stages_identifies_quota_math():
    comparison = _compare_stages(_surface(budget=2), _surface(budget=3))

    assert comparison["identical"] is False
    assert comparison["first_divergent_stage"] == "quota_math"


def test_compare_stages_green_for_identical_receipts():
    comparison = _compare_stages(_surface(), _surface())

    assert comparison["identical"] is True
    assert comparison["first_divergent_stage"] is None
