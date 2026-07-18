from __future__ import annotations

import json
from types import SimpleNamespace

import scripts.run_two_lane_canonical_window as harness


def test_runtime_contract_matches_the_combined_pre_acceptance_stack(monkeypatch):
    enabled = {
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED",
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED",
        "TEMPORAL_QUERY_ROUTING_ENABLED",
        "ATOMIC_CLAIM_ANCHORS_ENABLED",
        "TWO_LANE_ANCHORING_ENABLED",
    }
    names = enabled | {
        "RERANK_EVIDENCE_SUPPORT",
        "PARENT_EXCERPT_ENABLED",
        "WATERFALL_ASSEMBLY",
        "TWO_LANE_ANCHORING",
        "HYDE_ENABLED",
        "SHELF_RESERVE_ENABLED",
        "GROUNDED_QUERY_PLANNER_ENABLED",
        "FOUR_LANE_TIER0_ROUTER_ENABLED",
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED",
        "AGENTIC_MODE_ENABLED",
    }
    settings = SimpleNamespace(**{name: name in enabled for name in names})
    monkeypatch.setattr(harness, "get_settings", lambda: settings)

    observed = harness._runtime_flags()

    assert observed["TEMPORAL_QUERY_ROUTING_ENABLED"] is True
    assert observed["ATOMIC_CLAIM_ANCHORS_ENABLED"] is True


def test_compact_selection_is_exact_and_role_partition_is_ten():
    frozen = [{"id": query_id} for query_id in reversed(harness.QUERY_IDS)]

    selected = harness.select_cases({"queries": frozen})

    assert [row["id"] for row in selected] == list(harness.QUERY_IDS)
    assert len(harness.ANCHOR_SURFACE_QUERY_IDS) == 6
    assert len(harness.RELATIONSHIP_SPOT_QUERY_IDS) == 2
    assert len(harness.DIRECT_SPOT_QUERY_IDS) == 2
    assert len(set(harness.QUERY_IDS)) == 10


def test_selection_surface_binds_ordered_seat_identity():
    selection = {
        "anchor_seats": 1,
        "expansion_seats": 1,
        "groups": [
            {
                "anchors_available": 1,
                "anchor_candidate_ids": ["corpus|anchor"],
            }
        ],
        "selected": [
            {
                "seat": 1,
                "candidate_id": "corpus|anchor",
                "side": "a",
                "lane": "anchor",
            },
            {
                "seat": 2,
                "candidate_id": "corpus|expansion",
                "side": "b",
                "lane": "expansion",
            },
        ],
    }

    surface = harness._selection_surface(selection)

    assert surface["pool_has_anchor"] is True
    assert surface["selected_has_anchor"] is True
    assert surface["allocation_fingerprint"] == [
        "corpus|anchor",
        "corpus|expansion",
    ]
    assert surface["selected_identity"][0] == {
        "seat": 1,
        "candidate_id": "corpus|anchor",
        "side": "a",
        "lane": "anchor",
    }
    assert len(surface["allocation_fingerprint_sha256"]) == 64


def test_trace_metadata_requires_completed_trace_and_uses_last():
    traces = [
        {"title": "Query plan", "status": "running", "metadata": {"value": 1}},
        {"title": "Query plan", "status": "done", "metadata": {"value": 2}},
        {"title": "Query plan", "status": "done", "metadata": {"value": 3}},
    ]

    assert harness._trace_metadata(traces, "Query plan") == {"value": 3}
    assert harness._trace_metadata(traces, "missing") == {}


def test_source_score_uses_exact_registered_document_names():
    case = {
        "expected_any": ["Book A.md", "Book B.md"],
        "expected_min_distinct": 2,
    }
    receipt = {
        "items": [
            {"doc_name": "Book A.md"},
            {"doc_name": "Book B.md"},
            {"doc_name": "Unrelated.md"},
        ]
    }

    score = harness._score_sources(case, receipt)

    assert score["doc_hit"] is True
    assert score["minimum_distinct_ok"] is True
    assert score["expected_hits"] == ["book a.md", "book b.md"]


def test_summary_gates_fingerprint_and_identity_not_answer_bytes():
    executions = []
    for index, query_id in enumerate(harness.QUERY_IDS):
        shape = (
            "relationship_multi_document"
            if "relationship_" in query_id
            else "lay_language"
            if query_id.startswith("lay_")
            else "direct_expert"
        )
        executions.append(
            {
                "query_id": query_id,
                "classification": {"state": "answered"},
                "technical": {"ok": True},
                "journal_complete": True,
                "sources": {"all_in_selected_corpus": True},
                "evaluation": {
                    "shape": shape,
                    "source_score": {
                        "doc_hit": True,
                        "minimum_distinct_ok": True,
                    },
                    "two_lane": {
                        "pool_has_anchor": query_id in harness.ANCHOR_SURFACE_QUERY_IDS,
                        "selected_has_anchor": query_id
                        in harness.ANCHOR_SURFACE_QUERY_IDS,
                    },
                },
            }
        )
    repeats = [
        {
            "query_id": query_id,
            "technical_ok": True,
            "fingerprint_identical": True,
            "selected_identity_identical": True,
        }
        for query_id in harness.QUERY_IDS
    ]

    summary = harness.summarize(executions, repeats)

    assert summary["all_green"] is True
    assert summary["anchor_coverage_rate"] == 1.0
    assert summary["fingerprint_determinism_rate"] == 1.0
    assert json.dumps(summary, sort_keys=True)
