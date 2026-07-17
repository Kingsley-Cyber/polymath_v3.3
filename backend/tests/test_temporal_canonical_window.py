from __future__ import annotations

import scripts.run_temporal_canonical_window as harness


def test_compact_selection_is_exact_four_two_two_two():
    prereg = {
        "queries": [
            {"id": query_id, "shape": "direct_expert", "question": query_id}
            for query_id in (*harness.DIRECT_QUERY_IDS, *harness.LAY_QUERY_IDS)
        ]
    }
    negative = {
        "queries": [
            {"id": query_id, "shape": "negative_control", "question": query_id}
            for query_id in harness.NEGATIVE_QUERY_IDS
        ]
    }

    cases = harness.select_cases(prereg, negative)

    assert [row["id"] for row in cases] == list(harness.QUERY_IDS)
    assert len(harness.TEMPORAL_CASES) == 4
    assert len(harness.DIRECT_QUERY_IDS) == 2
    assert len(harness.LAY_QUERY_IDS) == 2
    assert len(harness.NEGATIVE_QUERY_IDS) == 2
    assert len(set(harness.QUERY_IDS)) == 10


def test_temporal_anchor_score_requires_expected_document_and_all_anchors():
    case = {
        "expected_any": ["Book.md"],
        "anchors": ["June 2006", "Nicole"],
    }
    sources = [
        {
            "doc_id": "doc-1",
            "doc_name": "Book.md",
            "text": "At the University in June 2006, the Nicole robot moved.",
        }
    ]

    score = harness.score_temporal_anchors(case, sources, {"doc-1": "Book.md"})

    assert score["expected_source_count"] == 1
    assert score["anchor_hits"] == ["June 2006", "Nicole"]
    assert score["all_anchors_hit"] is True


def test_temporal_anchor_score_does_not_credit_other_document():
    case = {
        "expected_any": ["Expected.md"],
        "anchors": ["1929", "dialog recording"],
    }
    sources = [
        {
            "doc_id": "other",
            "doc_name": "Other.md",
            "text": "1929 introduced dialog recording.",
        }
    ]

    score = harness.score_temporal_anchors(
        case,
        sources,
        {"other": "Other.md"},
    )

    assert score["expected_source_count"] == 0
    assert score["anchor_hits"] == []
    assert score["all_anchors_hit"] is False


def _execution(
    *,
    query_id: str,
    shape: str,
    doc_hit: bool = True,
    full_anchor: bool | None = None,
    state: str = "answered",
) -> dict:
    evaluation = {
        "shape": shape,
        "source_score": None if shape == "negative_control" else {"doc_hit": doc_hit},
        "temporal_routing": {"active": shape == "temporal"},
    }
    if shape == "temporal":
        evaluation["anchor_score"] = {"all_anchors_hit": bool(full_anchor)}
    return {
        "query_id": query_id,
        "technical": {"ok": True},
        "journal_complete": True,
        "classification": {"state": state},
        "sources": {"all_in_selected_corpus": True},
        "evaluation": evaluation,
    }


def test_summary_accepts_three_of_four_full_anchors_and_no_negative_regression():
    executions = [
        *[
            _execution(
                query_id=str(case["id"]),
                shape="temporal",
                full_anchor=index < 3,
            )
            for index, case in enumerate(harness.TEMPORAL_CASES)
        ],
        *[
            _execution(query_id=query_id, shape="direct_expert")
            for query_id in harness.DIRECT_QUERY_IDS
        ],
        *[
            _execution(query_id=query_id, shape="lay_language")
            for query_id in harness.LAY_QUERY_IDS
        ],
        _execution(
            query_id="negv2_f2_oscar_2026",
            shape="negative_control",
            state="answered",
        ),
        _execution(
            query_id="negv2_f1_crispr",
            shape="negative_control",
            state="gate_blocked",
        ),
    ]

    summary = harness.summarize(
        executions,
        {
            "negv2_f2_oscar_2026": "answered",
            "negv2_f1_crispr": "gate_blocked",
        },
    )

    assert summary["temporal_doc_hit_rate"] == 1.0
    assert summary["temporal_full_anchor_rate"] == 0.75
    assert summary["observed_negative_answered_count"] == 1
    assert summary["all_green"] is True


def test_summary_rejects_additional_answered_negative():
    executions = [
        *[
            _execution(
                query_id=str(case["id"]),
                shape="temporal",
                full_anchor=True,
            )
            for case in harness.TEMPORAL_CASES
        ],
        *[
            _execution(query_id=query_id, shape="direct_expert")
            for query_id in harness.DIRECT_QUERY_IDS
        ],
        *[
            _execution(query_id=query_id, shape="lay_language")
            for query_id in harness.LAY_QUERY_IDS
        ],
        *[
            _execution(
                query_id=query_id,
                shape="negative_control",
                state="answered",
            )
            for query_id in harness.NEGATIVE_QUERY_IDS
        ],
    ]

    summary = harness.summarize(
        executions,
        {
            "negv2_f2_oscar_2026": "answered",
            "negv2_f1_crispr": "gate_blocked",
        },
    )

    assert summary["gates"]["negative_non_degradation"] is False
    assert summary["all_green"] is False
