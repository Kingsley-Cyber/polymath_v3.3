"""Static contracts for the single final acceptance runner."""

from __future__ import annotations

import json

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
        "negv2_f5_figure_9_4",
        "negv2_f6_llc_guess",
    ]
    assert (
        runner._sha256_bytes(runner.MANIFEST_PATH.read_bytes())
        == runner.MANIFEST_SHA256
    )


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
