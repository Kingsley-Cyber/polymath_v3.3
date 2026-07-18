"""Static contracts for the single final acceptance runner."""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import run_final_acceptance_v1 as runner


def _source(name: str, *, kind: str = "body", tier: str = "tier_a") -> dict:
    return {
        "doc_name": name,
        "doc_id": name,
        "chunk_kind": kind,
        "source_tier": tier,
        "provenance": [],
    }


def test_manifest_is_exactly_the_preregistered_23_query_surface() -> None:
    manifest = json.loads(runner.MANIFEST_PATH.read_text())
    surface = runner._selection_surface(manifest)

    assert surface["query_count"] == 23
    assert surface["query_ids"][:5] == list(runner.DETERMINISM_IDS)
    assert surface["query_ids"][-5:] == [
        "negv2_f2_oscar_2026",
        "negv2_f3_deakins",
        "negv2_f3_visual_story",
        "negv2_f5_figure_34_1",
        "negv2_f6_llc_guess",
    ]
    assert (
        runner._sha256_bytes(runner.MANIFEST_PATH.read_bytes())
        == runner.MANIFEST_SHA256
    )


def test_refusal_state_uses_canonical_classifier_boolean() -> None:
    case = {"class": "refusal_f3"}

    assert runner._expected_state_receipt(
        case,
        {"state": "gate_blocked", "refused": True},
    ) == ("refused", True)
    assert runner._expected_state_receipt(
        case,
        {"state": "model_voiced_refusal", "refused": True},
    ) == ("refused", True)
    assert runner._expected_state_receipt(
        case,
        {"state": "answered", "refused": False},
    ) == ("refused", False)
    assert runner._expected_state_receipt(
        {"class": "direct_floor"},
        {"state": "answered", "refused": False},
    ) == ("answered", True)


def test_repeat_uses_same_librarian_and_refinement_flag() -> None:
    enabled = runner._repeat_librarian_controls(
        SimpleNamespace(LIBRARIAN_LLM_DECOMPOSER_ENABLED=True),
        user_id="owner-1",
    )
    disabled = runner._repeat_librarian_controls(
        SimpleNamespace(LIBRARIAN_LLM_DECOMPOSER_ENABLED=False),
        user_id="owner-1",
    )

    assert enabled == {
        "llm_decomposer_enabled": True,
        "librarian_refinement_enabled": True,
        "librarian_refinement_user_id": "owner-1",
    }
    assert disabled["llm_decomposer_enabled"] is False
    assert disabled["librarian_refinement_enabled"] is False


def test_expected_group_score_requires_one_document_from_each_side() -> None:
    case = {
        "expected_groups": [
            ["Animator.md"],
            ["Grammar.md", "Murch.md"],
        ]
    }
    one_side = {"items": [_source("Animator.md")]}
    both_sides = {
        "items": [
            _source("Animator.md"),
            _source("Murch.md"),
        ]
    }

    assert runner._expected_group_score(case, one_side)["all_groups_hit"] is False
    score = runner._expected_group_score(case, both_sides)
    assert score["all_groups_hit"] is True
    assert score["groups_hit"] == 2


def test_named_v3_guard_proof_requires_the_preregistered_blocking_reason() -> None:
    case = {
        "schema_proof": "named_v3_guard",
        "expected_guard": "artifact_absent",
    }
    traces = [
        {
            "title": "Answerability gate",
            "status": "warning",
            "metadata": {
                "corpus_scope_v3_guard": {
                    "blocking_reason_codes": ["artifact_absent"],
                }
            },
        }
    ]
    proof = runner._schema_proofs(
        case,
        traces=traces,
        sources={"items": []},
        expected={"all_groups_hit": True},
    )
    assert proof["proof_pass"] is True


def test_chunk_kind_proof_is_not_satisfied_by_body_hydration() -> None:
    case = {"schema_proof": "list_or_table_hydration"}
    traces = []
    expected = {"all_groups_hit": True}

    body = runner._schema_proofs(
        case,
        traces=traces,
        sources={"items": [_source("VES.md")]},
        expected=expected,
    )
    table = runner._schema_proofs(
        case,
        traces=traces,
        sources={"items": [_source("VES.md", kind="table")]},
        expected=expected,
    )

    assert body["proof_pass"] is False
    assert table["proof_pass"] is True


def test_profile_consumption_requires_shortlist_target_to_reach_final_sources() -> None:
    case = {"schema_proof": "doc_hit"}
    traces = [
        {
            "title": "Query plan",
            "status": "done",
            "metadata": {
                "librarian_query_plan": {
                    "plan": {
                        "shortlist": [
                            {
                                "doc_id": "story",
                                "summary": "Story structure and directing craft.",
                            }
                        ],
                        "subqueries": [{"target_doc_ids": ["story"]}],
                    },
                    "diagnostics": {
                        "shortlist": {
                            "mode": "librarian_plan_grounding_lanes_1_2",
                            "lanes": ["lexical", "semantic"],
                        }
                    },
                }
            },
        }
    ]
    consumed = runner._schema_proofs(
        case,
        traces=traces,
        sources={"items": [_source("Story.md") | {"doc_id": "story"}]},
        expected={"all_groups_hit": True},
    )
    missed = runner._schema_proofs(
        case,
        traces=traces,
        sources={"items": [_source("Other.md") | {"doc_id": "other"}]},
        expected={"all_groups_hit": True},
    )

    assert consumed["associative_profile"]["consumed"] is True
    assert consumed["associative_profile"]["consumed_shortlist_profile_doc_ids"] == [
        "story"
    ]
    assert missed["associative_profile"]["consumed"] is False


def test_runtime_contract_enables_bounded_refinement(monkeypatch) -> None:
    enabled = {
        "QUERY_PLAN_V2",
        "NEO4J_ENABLED",
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED",
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED",
        "ANSWERABILITY_CORPUS_SCOPE_V3_ENABLED",
        "TEMPORAL_QUERY_ROUTING_ENABLED",
        "ATOMIC_CLAIM_ANCHORS_ENABLED",
        "LIBRARIAN_PLANNER_ENABLED",
        "LIBRARIAN_LLM_DECOMPOSER_ENABLED",
        "SYNTHESIS_ROUTE_OVERRIDE_ENABLED",
        "CHAT_COST_TELEMETRY_ENABLED",
    }
    names = enabled | {
        "LIBRARIAN_PLANNER_SHADOW",
        "TWO_LANE_ANCHORING_ENABLED",
        "FOUR_LANE_TIER0_ROUTER_ENABLED",
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED",
        "WATERFALL_ASSEMBLY",
        "RERANK_EVIDENCE_SUPPORT",
        "PARENT_EXCERPT_ENABLED",
        "HYDE_ENABLED",
        "SHELF_RESERVE_ENABLED",
        "GROUNDED_QUERY_PLANNER_ENABLED",
        "AGENTIC_MODE_ENABLED",
    }
    settings = SimpleNamespace(**{name: name in enabled for name in names})
    monkeypatch.setattr(runner, "get_settings", lambda: settings)

    observed = runner._runtime_flags(expected_two_lane=False)

    assert observed["LIBRARIAN_LLM_DECOMPOSER_ENABLED"] is True
    assert observed["TWO_LANE_ANCHORING_ENABLED"] is False


def test_refinement_trace_is_recorded_from_retrieval_diagnostics() -> None:
    traces = [
        {
            "title": "Local RAG retrieval",
            "status": "done",
            "metadata": {
                "retrieval_diagnostics": {
                    "librarian_execution": {
                        "refinement": {
                            "enabled": True,
                            "fired": True,
                            "status": "built",
                            "round": 1,
                            "gaps": [{"lane_id": "subquery:0"}],
                            "second_pass": {
                                "attempted": True,
                                "improved_seating": True,
                                "remaining_gaps": [],
                            },
                        }
                    }
                }
            },
        }
    ]

    proof = runner._schema_proofs(
        {"schema_proof": "doc_hit"},
        traces=traces,
        sources={"items": []},
        expected={"all_groups_hit": True},
    )

    assert proof["refinement"] == {
        "enabled": True,
        "fired": True,
        "status": "built",
        "reason": None,
        "round": 1,
        "gap_count": 1,
        "second_pass_attempted": True,
        "improved_seating": True,
        "remaining_gap_count": 0,
        "planner_refinement_unavailable": None,
        "silent_fallback_count": 0,
    }


def test_refinement_acceptance_requires_depth_improvement_and_clean_nonfiring() -> None:
    def execution(ordinal: int, *, fired: bool, improved: bool) -> dict:
        return {
            "query_id": f"q{ordinal}",
            "schema_proofs": {
                "refinement": {
                    "enabled": True,
                    "fired": fired,
                    "status": "built" if fired else "not_needed",
                    "second_pass_attempted": fired,
                    "improved_seating": improved,
                }
            },
        }

    rows = {
        ordinal: execution(
            ordinal,
            fired=ordinal == runner.REFINEMENT_DEPTH_ORDINALS[0],
            improved=ordinal == runner.REFINEMENT_DEPTH_ORDINALS[0],
        )
        for ordinal in (
            *runner.REFINEMENT_DEPTH_ORDINALS,
            *runner.REFINEMENT_SIMPLE_ORDINALS,
        )
    }
    surface = runner._refinement_acceptance_surface(rows)
    assert surface["gap_firing_improved"] is True
    assert surface["simple_zero_firings"] is True

    rows[runner.REFINEMENT_SIMPLE_ORDINALS[0]] = execution(
        runner.REFINEMENT_SIMPLE_ORDINALS[0],
        fired=True,
        improved=True,
    )
    assert runner._refinement_acceptance_surface(rows)["simple_zero_firings"] is False

    rows[runner.REFINEMENT_SIMPLE_ORDINALS[0]] = execution(
        runner.REFINEMENT_SIMPLE_ORDINALS[0],
        fired=False,
        improved=False,
    )
    rows[runner.REFINEMENT_DEPTH_ORDINALS[0]] = execution(
        runner.REFINEMENT_DEPTH_ORDINALS[0],
        fired=True,
        improved=False,
    )
    assert runner._refinement_acceptance_surface(rows)["gap_firing_improved"] is False
