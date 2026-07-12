from types import SimpleNamespace

import pytest

from services.retriever.grounded_planner import (
    _validate_plan,
    filter_aligned_planner_lanes,
    grounded_planner_lanes,
    run_grounded_planner,
    should_run_grounded_planner,
)
from services.retriever.query_plan import build_query_plan_v2


def _resolution():
    return {
        "matches": [
            {
                "lexicon_id": "lex-facs",
                "term": "Facial Action Coding System",
                "aliases": ["FACS"],
                "retrieval_gloss": "A source-backed facial movement coding system.",
                "applicability": "source_term_overlap",
            }
        ],
        "rejected_expansions": [],
    }


def test_validator_rejects_uncited_domain_terms_and_accepts_scoped_terms():
    validated, rejected = _validate_plan(
        {
            "intent": "design an opening performance",
            "required_obligations": ["How should the opening work?"],
            "exploratory_obligations": [
                {
                    "question": "How can FACS guide the actor?",
                    "lexicon_entry_ids": ["lex-facs"],
                }
            ],
            "step_back_probes": [],
            "introduced_terms": [
                {
                    "term": "FACS",
                    "lexicon_entry_id": "lex-facs",
                },
                {
                    "term": "Invented Domain Method",
                    "lexicon_entry_id": "missing",
                },
            ],
            "confidence": 0.8,
        },
        _resolution(),
    )

    assert validated is not None
    assert validated["introduced_terms"] == [
        {"term": "FACS", "lexicon_entry_id": "lex-facs"}
    ]
    assert any(item["reason"] == "unsupported_domain_term" for item in rejected)
    assert "advisory" in validated["required_obligations_authority"]


def test_planner_lanes_are_always_optional():
    lanes, lane_lexicon_ids = grounded_planner_lanes(
        {
            "exploratory_obligations": [
                {
                    "question": "How can FACS guide facial performance?",
                    "lexicon_entry_ids": ["lex-facs"],
                }
            ],
            "step_back_probes": [
                {
                    "question": "What broader principles govern FACS?",
                    "lexicon_entry_ids": ["lex-facs"],
                }
            ],
        },
        _resolution(),
    )

    assert len(lanes) == 2
    assert all(lane.required is False for lane in lanes)
    assert all(lane.role == "core" for lane in lanes)
    assert set(lane_lexicon_ids) == {lane.lane_id for lane in lanes}
    assert all(value == ["lex-facs"] for value in lane_lexicon_ids.values())


def test_planner_decomposition_is_optional_and_semantically_gated():
    lanes, lane_lexicon_ids = grounded_planner_lanes(
        {
            "required_obligations": [
                "How should the opening ad guide the actor's facial performance?"
            ],
            "exploratory_obligations": [
                {
                    "question": "How can FACS guide facial performance?",
                    "lexicon_entry_ids": ["lex-facs"],
                }
            ],
        },
        _resolution(),
    )

    assert len(lanes) == 2
    assert all(lane.required is False for lane in lanes)
    accepted, vectors, accepted_ids, diagnostics = filter_aligned_planner_lanes(
        lanes,
        [[1.0, 0.0], [0.1, 0.995]],
        lane_lexicon_ids,
        original_vector=[1.0, 0.0],
        minimum_alignment=0.45,
        step_back_minimum_alignment=0.35,
    )

    assert [lane.lane_id for lane in accepted] == ["planner_decomposition_0"]
    assert vectors == [[1.0, 0.0]]
    assert accepted_ids == {}
    assert diagnostics["accepted"] == 1
    assert diagnostics["rejected"] == 1
    assert diagnostics["original_lane_protected"] is True


def test_conditional_planner_runs_for_compositional_grounded_queries():
    plan = build_query_plan_v2(
        "How should an actor's face move and how should the opening ad begin?"
    )
    assert should_run_grounded_planner(plan, _resolution()) is True


@pytest.mark.asyncio
async def test_unconfigured_planner_makes_no_provider_call(monkeypatch):
    monkeypatch.setattr(
        "services.retriever.grounded_planner.get_settings",
        lambda: SimpleNamespace(
            GROUNDED_QUERY_PLANNER_ENABLED=False,
            GROUNDED_QUERY_PLANNER_MODEL="",
            GROUNDED_QUERY_PLANNER_MAX_CALLS_TOTAL=0,
        ),
    )

    result = await run_grounded_planner(
        None,
        plan=build_query_plan_v2("How should an actor's face move?"),
        resolution=_resolution(),
        corpus_ids=["c1"],
    )

    assert result["status"] == "skipped"
    assert result["provider_calls"] == 0
