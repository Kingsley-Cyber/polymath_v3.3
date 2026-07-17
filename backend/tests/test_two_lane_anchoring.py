"""Deterministic final evidence-seat anchoring contracts (Agent T)."""

from __future__ import annotations

from dataclasses import dataclass, field

from config import Settings
from models.schemas import RetrievalTier, SourceChunk
from services.retriever.evidence_allocation import (
    allocate_two_lane_seats,
    classify_metadata_anchor,
)
from services.retriever.intent_policy import infer_retrieval_intent
from services.retriever.ranking_policy import select_with_diversity


@dataclass
class Candidate:
    candidate_id: str
    score: float = 0.8
    side: str = "__all__"
    doc_name: str = ""
    heading_path: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    provenance: list[dict] = field(default_factory=list)


def _id(candidate: Candidate) -> str:
    return candidate.candidate_id


def _score(candidate: Candidate) -> float:
    return candidate.score


def _allocate(
    selected: list[Candidate],
    pool: list[Candidate],
    *,
    query: str = "What does The Art of Seduction say about charm?",
    budget: int | None = None,
    ratio: float = 0.60,
    anchor_threshold: float = 0.10,
    expansion_threshold: float = 0.10,
    side_fn=None,
    protected_ids=None,
):
    return allocate_two_lane_seats(
        selected,
        pool,
        query=query,
        budget=budget if budget is not None else len(selected),
        anchor_ratio=ratio,
        anchor_threshold=anchor_threshold,
        expansion_threshold=expansion_threshold,
        candidate_id_fn=_id,
        score_fn=_score,
        side_fn=side_fn,
        protected_ids=protected_ids,
    )


def _anchor(candidate_id: str, *, score: float = 0.8, side: str = "__all__"):
    return Candidate(
        candidate_id,
        score=score,
        side=side,
        metadata={"source_book": "The Art of Seduction"},
    )


def _expansion(candidate_id: str, *, score: float = 0.8, side: str = "__all__"):
    return Candidate(candidate_id, score=score, side=side)


def test_metadata_classifier_matches_exact_case_normalized_title():
    match = classify_metadata_anchor(
        "How does ART OF SEDUCTION describe charm?",
        Candidate("a", metadata={"source_book": "The Art of Seduction"}),
    )
    assert match.is_anchor
    assert match.matched_fields == ("title",)
    assert match.matched_terms == ("art of seduction",)


def test_metadata_classifier_covers_author_heading_entity_and_bibliography():
    candidate = Candidate(
        "a",
        heading_path=["The Charismatic"],
        metadata={
            "author": "Robert Greene",
            "entities": [{"canonical_name": "Coquette"}],
            "cited_titles": ["The Presentation of Self"],
        },
    )
    match = classify_metadata_anchor(
        "Compare Robert Greene's Coquette with The Charismatic and "
        "The Presentation of Self.",
        candidate,
    )
    assert match.is_anchor
    assert set(match.matched_fields) == {
        "author",
        "bibliographic",
        "entity",
        "heading_path",
    }


def test_metadata_classifier_rejects_generic_or_partial_terms():
    generic = Candidate(
        "generic",
        heading_path=["Introduction"],
        metadata={"source_book": "The Book", "author": "Li"},
    )
    assert not classify_metadata_anchor(
        "Give an introduction to the book and its author.", generic
    ).is_anchor
    partial = Candidate("partial", metadata={"source_book": "The Art of Seduction"})
    assert not classify_metadata_anchor(
        "What makes an art persuasive?", partial
    ).is_anchor


def test_quota_reserves_ceil_sixty_percent_anchors():
    selected = [_expansion(f"e{i}") for i in range(5)]
    anchors = [_anchor(f"a{i}") for i in range(4)]
    allocation = _allocate(selected, [*anchors, *selected])
    assert allocation.diagnostics["anchor_seats"] == 3
    assert allocation.diagnostics["expansion_seats"] == 2
    assert len(allocation.candidates) == 5
    assert allocation.diagnostics["groups"][0]["anchor_candidate_ids"] == [
        "a0",
        "a1",
        "a2",
        "a3",
    ]


def test_no_anchor_collapses_to_byte_ordered_single_lane():
    selected = [_expansion(f"e{i}") for i in range(5)]
    allocation = _allocate(
        selected,
        list(reversed(selected)),
        query="Explain this general framework.",
    )
    assert list(allocation.candidates) == selected
    assert allocation.diagnostics["groups"][0]["collapsed_single_lane"] is True


def test_anchor_shortfall_spills_to_expansion():
    selected = [_anchor("a0"), *[_expansion(f"e{i}") for i in range(4)]]
    allocation = _allocate(selected, selected)
    report = allocation.diagnostics["groups"][0]
    assert len(allocation.candidates) == 5
    assert report["anchors_selected"] == 1
    assert report["expansions_selected"] == 4
    assert report["spill_anchor_to_expansion"] == 2


def test_expansion_shortfall_spills_to_anchor():
    selected = [_anchor(f"a{i}") for i in range(5)]
    allocation = _allocate(selected, selected)
    report = allocation.diagnostics["groups"][0]
    assert len(allocation.candidates) == 5
    assert report["anchors_selected"] == 5
    assert report["expansions_selected"] == 0
    assert report["spill_expansion_to_anchor"] == 2


def test_admission_threshold_releases_weak_anchor_seat():
    selected = [_expansion(f"e{i}") for i in range(5)]
    weak_anchor = _anchor("weak", score=0.09)
    allocation = _allocate(
        selected,
        [weak_anchor, *selected],
        anchor_threshold=0.10,
    )
    assert "weak" not in {_id(item) for item in allocation.candidates}
    assert allocation.diagnostics["anchor_seats"] == 0


def test_protected_seat_is_never_replaced():
    selected = [_expansion(f"e{i}") for i in range(5)]
    anchors = [_anchor(f"a{i}") for i in range(4)]
    allocation = _allocate(
        selected,
        [*anchors, *selected],
        protected_ids={"e4"},
    )
    assert "e4" in {_id(item) for item in allocation.candidates}


def test_relationship_composition_preserves_per_side_counts_before_lane_quota():
    selected = [
        *[_expansion(f"left-e{i}", side="left") for i in range(3)],
        *[_expansion(f"right-e{i}", side="right") for i in range(3)],
    ]
    anchors = [
        *[_anchor(f"left-a{i}", side="left") for i in range(3)],
        *[_anchor(f"right-a{i}", side="right") for i in range(3)],
    ]
    allocation = _allocate(
        selected,
        [*anchors, *selected],
        budget=6,
        side_fn=lambda candidate: candidate.side,
    )
    sides = [candidate.side for candidate in allocation.candidates]
    assert sides.count("left") == 3
    assert sides.count("right") == 3
    for report in allocation.diagnostics["groups"]:
        assert report["anchor_quota"] == 2
        assert report["anchors_selected"] == 2
        assert report["expansions_selected"] == 1
    assert allocation.diagnostics["relationship_precedence"] is True


def _source(
    chunk_id: str,
    *,
    score: float,
    title: str | None = None,
    text: str | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"p-{chunk_id}",
        doc_id=f"d-{chunk_id}",
        corpus_id="c",
        text=text or f"Evidence {chunk_id} with distinct content.",
        score=score,
        source_tier="tier_a",
        metadata={"source_book": title} if title else {},
    )


def test_feature_defaults_off_and_ratio_defaults_sixty_percent(monkeypatch):
    monkeypatch.delenv("TWO_LANE_ANCHORING_ENABLED", raising=False)
    monkeypatch.delenv("ANCHOR_LANE_RATIO", raising=False)
    resolved = Settings(_env_file=None)
    assert resolved.TWO_LANE_ANCHORING_ENABLED is False
    assert resolved.ANCHOR_LANE_RATIO == 0.60


def test_feature_and_thresholds_are_settings_driven(monkeypatch):
    monkeypatch.setenv("TWO_LANE_ANCHORING_ENABLED", "true")
    monkeypatch.setenv("ANCHOR_LANE_RATIO", "0.75")
    monkeypatch.setenv("ANCHOR_LANE_ADMISSION_THRESHOLD", "0.2")
    monkeypatch.setenv("EXPANSION_LANE_ADMISSION_THRESHOLD", "0.3")
    resolved = Settings(_env_file=None)
    assert resolved.TWO_LANE_ANCHORING_ENABLED is True
    assert resolved.ANCHOR_LANE_RATIO == 0.75
    assert resolved.ANCHOR_LANE_ADMISSION_THRESHOLD == 0.2
    assert resolved.EXPANSION_LANE_ADMISSION_THRESHOLD == 0.3


def test_ranking_off_fingerprint_is_unchanged():
    ranked = [
        _source("e1", score=1.00),
        _source("e2", score=0.95),
        _source("e3", score=0.90),
        _source("a1", score=0.70, title="The Art of Seduction"),
    ]
    kwargs = {
        "final_top_k": 3,
        "intent": infer_retrieval_intent("What does The Art of Seduction say?"),
        "tier": RetrievalTier.qdrant_mongo,
        "query": "What does The Art of Seduction say?",
    }
    baseline = select_with_diversity(ranked, **kwargs)
    explicit_off = select_with_diversity(
        ranked,
        **kwargs,
        anchor_query=kwargs["query"],
        two_lane_anchoring_enabled=False,
    )
    assert [item.model_dump() for item in explicit_off.candidates] == [
        item.model_dump() for item in baseline.candidates
    ]
    assert explicit_off.diagnostics == baseline.diagnostics


def test_ranking_on_is_deterministic_and_records_trace_coverage():
    query = "What does The Art of Seduction say about charm?"
    ranked = [
        _source("e1", score=1.00),
        _source("e2", score=0.95),
        _source("e3", score=0.90),
        _source("a1", score=0.70, title="The Art of Seduction"),
        _source("a2", score=0.65, title="The Art of Seduction"),
        _source("a3", score=0.60, title="The Art of Seduction"),
    ]
    kwargs = {
        "final_top_k": 3,
        "intent": infer_retrieval_intent(query),
        "tier": RetrievalTier.qdrant_mongo,
        "query": query,
        "anchor_query": query,
        "two_lane_anchoring_enabled": True,
    }
    first = select_with_diversity(ranked, **kwargs)
    second = select_with_diversity(ranked, **kwargs)
    first_ids = [item.chunk_id for item in first.candidates]
    assert first_ids == [item.chunk_id for item in second.candidates]
    assert (
        first.diagnostics["two_lane_anchoring"]
        == second.diagnostics["two_lane_anchoring"]
    )
    trace = first.diagnostics["two_lane_anchoring"]
    assert trace["anchor_seats"] >= 1
    assert len(trace["selected"]) == len(first.candidates)
    assert any(
        (item.metadata.get("two_lane_anchoring") or {}).get("lane") == "anchor"
        for item in first.candidates
    )
