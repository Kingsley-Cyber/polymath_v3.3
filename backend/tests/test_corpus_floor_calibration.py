"""P0.3 — corpus-floor calibration and consolidation.

Acceptance under test:
  - No sub-threshold corpus receives a forced final seat, on either decision
    path (ranking_policy corpus floor, planned_fusion finalist reservations,
    grounded-filter corpus preservation).
  - corpus_floor.skipped reports why a selected corpus was omitted.
  - Reserved seats keep their true relevance (the former unconditional +0.10
    reserve bonus is gone); protection is the selection reason, not a score.
  - Both paths share one calibrated reservation bound, so one path cannot
    seat a corpus the other rejected.
"""

from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import infer_retrieval_intent
from services.retriever.planned_fusion import (
    filter_grounded_planned_candidates,
    reserve_planned_finalists,
)
from services.retriever.ranking_policy import select_with_diversity
from services.retriever.reservation_policy import (
    corpus_reservation_bound,
    passes_corpus_reservation,
)


def _chunk(
    chunk_id: str,
    *,
    score: float,
    corpus_id: str = "alpha",
    doc_id: str | None = None,
    metadata: dict | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"p-{chunk_id}",
        doc_id=doc_id or f"doc-{chunk_id}",
        corpus_id=corpus_id,
        text=f"evidence text for {chunk_id} about architecture patterns",
        score=score,
        source_tier="tier_a",
        metadata=metadata or {},
    )


# ── shared gate ──────────────────────────────────────────────────────────────


def test_reservation_bound_semantics():
    assert corpus_reservation_bound(1.0) == 0.30
    assert corpus_reservation_bound(0.5) == 0.25
    assert corpus_reservation_bound(0.0) is None  # empty packet
    assert corpus_reservation_bound(7.3) is None  # uncalibrated logits
    assert passes_corpus_reservation(0.31, 1.0)
    assert not passes_corpus_reservation(0.29, 1.0)
    assert passes_corpus_reservation(0.29, 7.3)  # uncalibrated family passes


# ── ranking_policy corpus floor ──────────────────────────────────────────────


def test_corpus_floor_skip_reports_reason_and_trace():
    intent = infer_retrieval_intent("define one specific architecture pattern")
    ranked = [
        _chunk("a1", score=1.00),
        _chunk("a2", score=0.99),
        _chunk("b-weak", score=0.20, corpus_id="beta"),
    ]
    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
        multi_corpus=True,
        selected_corpus_ids=["alpha", "beta"],
    )
    floor = result.diagnostics["corpus_floor"]
    assert [c.corpus_id for c in result.candidates] == ["alpha", "alpha"]
    assert [entry["corpus_id"] for entry in floor["skipped"]] == ["beta"]
    assert floor["skipped"][0]["reason"] in {
        "below_reservation_bound",
        "below_relevance_floor",
    }
    assert floor["reservation_bound"] == 0.30
    trace = floor["eligibility"]["beta"]
    assert trace["best_score"] == 0.20
    assert trace["passed_reservation_bound"] is False


def test_corpus_floor_seat_keeps_true_relevance_no_bonus():
    intent = infer_retrieval_intent("compare architecture patterns across corpora")
    ranked = [
        _chunk("a1", score=1.00),
        _chunk("a2", score=0.99),
        _chunk("b1", score=0.50, corpus_id="beta"),
    ]
    result = select_with_diversity(
        ranked,
        final_top_k=3,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
        multi_corpus=True,
        selected_corpus_ids=["alpha", "beta"],
    )
    beta = next(c for c in result.candidates if c.corpus_id == "beta")
    rerank_meta = beta.metadata["diversity_rerank"]
    if rerank_meta["selected_by"] == "corpus_floor":
        # The seat keeps the candidate's own relevance. The former +0.10
        # bonus would push this above 0.55 for a 0.50-score candidate whose
        # normalized relevance is at most 0.50.
        assert rerank_meta["mmr_score"] <= 0.55
    floor = result.diagnostics["corpus_floor"]
    assert "beta" in floor["covered_corpora"]
    assert floor["skipped"] == []


def test_corpus_floor_strong_corpus_still_covered():
    intent = infer_retrieval_intent("compare architecture patterns across corpora")
    ranked = [
        _chunk("a1", score=1.00),
        _chunk("a2", score=0.99),
        _chunk("a3", score=0.98),
        _chunk("b1", score=0.90, corpus_id="beta"),
    ]
    result = select_with_diversity(
        ranked,
        final_top_k=3,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
        multi_corpus=True,
        selected_corpus_ids=["alpha", "beta"],
    )
    floor = result.diagnostics["corpus_floor"]
    assert {c.corpus_id for c in result.candidates} == {"alpha", "beta"}
    assert floor["covered_corpora"] == ["alpha", "beta"]


# ── planned_fusion finalist reservations ─────────────────────────────────────


def test_finalists_do_not_protect_subbound_existing_corpus_candidate():
    ranked = [
        _chunk("a1", score=0.90),
        _chunk("a2", score=0.85),
        _chunk("b-weak", score=0.05, corpus_id="beta"),
    ]
    preferred = [ranked[0], ranked[2]]  # diversity already picked weak beta
    output, diagnostics = reserve_planned_finalists(
        ranked,
        preferred,
        required_lane_ids=[],
        corpus_ids=["alpha", "beta"],
        max_candidates=2,
    )
    assert "beta" in diagnostics["skipped_corpus_reservations"]
    assert "beta" not in diagnostics["corpus_reservations"]
    detail = diagnostics["corpus_reservation_details"]["beta"]
    assert detail["outcome"] == "below_reservation_bound"
    assert detail["best_score"] == 0.05
    # The weak candidate keeps its diversity seat but never gains the
    # protected corpus-reservation marker.
    beta_chunk = next(c for c in output if c.corpus_id == "beta")
    assert "planned_corpus_reservations" not in (beta_chunk.metadata or {})


def test_finalists_reserve_strong_corpus_candidate():
    ranked = [
        _chunk("a1", score=0.90),
        _chunk("a2", score=0.85),
        _chunk("b1", score=0.60, corpus_id="beta"),
    ]
    preferred = [ranked[0], ranked[1]]
    output, diagnostics = reserve_planned_finalists(
        ranked,
        preferred,
        required_lane_ids=[],
        corpus_ids=["alpha", "beta"],
        max_candidates=3,
    )
    assert diagnostics["corpus_reservation_details"]["beta"]["outcome"] == (
        "reserved_ranked"
    )
    assert "beta" in diagnostics["corpus_reservations"]
    beta_chunk = next(c for c in output if c.corpus_id == "beta")
    assert (beta_chunk.metadata or {})["planned_corpus_reservations"] == ["beta"]


# ── grounded-filter corpus preservation ──────────────────────────────────────


def _grounded(chunk_id: str, *, score: float, lanes: dict[str, float], corpus_id: str = "alpha") -> SourceChunk:
    return _chunk(
        chunk_id,
        score=score,
        corpus_id=corpus_id,
        metadata={"planned_lane_grounding": lanes, "planned_lanes": list(lanes)},
    )


def test_grounded_filter_preservation_respects_reservation_bound():
    chunks = [
        _grounded("a1", score=0.95, lanes={"side_a": 0.9}),
        _grounded("a2", score=0.90, lanes={"side_b": 0.9}),
        _grounded("b-weak", score=0.05, lanes={}, corpus_id="beta"),
    ]
    filtered, diagnostics = filter_grounded_planned_candidates(
        chunks,
        ["side_a", "side_b"],
        selected_corpus_ids=["alpha", "beta"],
    )
    assert diagnostics["applied"] is True
    assert diagnostics["corpus_floor_candidates_preserved"] == []
    skipped = diagnostics["corpus_floor_candidates_skipped"]
    assert skipped and skipped[0]["corpus_id"] == "beta"
    assert skipped[0]["reason"] == "below_reservation_bound"
    assert all(c.corpus_id == "alpha" for c in filtered)


def test_grounded_filter_preserves_best_strong_corpus_candidate():
    chunks = [
        _grounded("a1", score=0.95, lanes={"side_a": 0.9}),
        _grounded("a2", score=0.90, lanes={"side_b": 0.9}),
        _grounded("b-mid", score=0.40, lanes={}, corpus_id="beta"),
        _grounded("b-best", score=0.55, lanes={}, corpus_id="beta"),
    ]
    filtered, diagnostics = filter_grounded_planned_candidates(
        chunks,
        ["side_a", "side_b"],
        selected_corpus_ids=["alpha", "beta"],
    )
    assert diagnostics["corpus_floor_candidates_preserved"] == ["beta"]
    beta_kept = [c for c in filtered if c.corpus_id == "beta"]
    assert [c.chunk_id for c in beta_kept] == ["b-best"]
