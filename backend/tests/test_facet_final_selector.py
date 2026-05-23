from dataclasses import dataclass

from services.facets.final_selector import FacetCandidate, select_facet_final


@dataclass
class DummyChunk:
    chunk_id: str
    doc_id: str
    text: str = ""


def _candidate(
    chunk_id: str,
    *,
    score: float,
    lanes: set[str] | None = None,
    doc_id: str | None = None,
    junk: bool = False,
    order: int = 0,
) -> FacetCandidate:
    item = DummyChunk(chunk_id=chunk_id, doc_id=doc_id or f"doc-{chunk_id}")
    return FacetCandidate(
        item=item,
        score=score,
        lanes=lanes or set(),
        key=f"chunk:{chunk_id}",
        doc_id=item.doc_id,
        junk=junk,
        order=order,
    )


def test_facet_final_selector_reserves_missing_lanes_before_global_score_fill():
    selected, meta = select_facet_final(
        [
            _candidate("global-1", score=0.99, lanes={"measurement"}, order=1),
            _candidate("global-2", score=0.95, lanes={"measurement"}, order=2),
            _candidate("narrative", score=0.12, lanes={"identity_narrative"}, order=3),
            _candidate("cooperation", score=0.10, lanes={"cooperative"}, order=4),
        ],
        missing_lanes=["identity_narrative", "cooperative"],
        max_items=3,
        lane_budget=1,
    )

    ids = [item.chunk_id for item in selected]
    assert "narrative" in ids
    assert "cooperation" in ids
    assert meta["covered_lanes"] == ["identity_narrative", "cooperative"]


def test_facet_final_selector_reserves_priority_lanes_before_dynamic_fill():
    selected, meta = select_facet_final(
        [
            _candidate("dynamic-high", score=0.99, lanes={"cooperative"}, order=1),
            _candidate("psych-extra", score=0.90, lanes={"psychometrics"}, order=2),
            _candidate("knowledge-graph", score=0.25, lanes={"knowledge_graph"}, order=3),
            _candidate("user-modeling", score=0.20, lanes={"user_modeling"}, order=4),
        ],
        missing_lanes=[],
        priority_lanes=["knowledge_graph", "user_modeling", "psychometrics"],
        max_items=3,
        lane_budget=1,
    )

    ids = [item.chunk_id for item in selected]
    assert ids == ["knowledge-graph", "user-modeling", "psych-extra"]
    assert meta["covered_priority_lanes"] == [
        "knowledge_graph",
        "user_modeling",
        "psychometrics",
    ]


def test_facet_final_selector_filters_junk_when_clean_evidence_exists():
    selected, meta = select_facet_final(
        [
            _candidate("bibliography", score=1.0, lanes={"measurement"}, junk=True),
            _candidate("substantive", score=0.5, lanes={"measurement"}),
        ],
        missing_lanes=["measurement"],
        max_items=1,
    )

    assert [item.chunk_id for item in selected] == ["substantive"]
    assert meta["filtered_junk"] == 1
