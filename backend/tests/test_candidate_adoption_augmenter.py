"""Baseline-augment candidate adoption policy (portable, no live stack)."""

from __future__ import annotations

from services.retriever.complex_query_candidate_adoption import (
    build_candidate_finalists,
    obligation_coverage_rate,
)


def _ch(cid: str, score: float = 1.0) -> dict:
    return {"chunk_id": cid, "text": f"text for {cid}", "score": score, "doc_id": "d"}


def test_preserves_baseline_and_blocks_novelty_without_need():
    baseline = [_ch("base_a"), _ch("base_b")]
    pool = baseline + [_ch("novel_graph"), _ch("noise")]
    obligations = [
        {"obligation_id": "o1", "ranked_child_ids": ["base_a"]},
        {"obligation_id": "o2", "ranked_child_ids": ["base_b"]},
    ]
    out, diag = build_candidate_finalists(
        baseline_finalists=baseline,
        ranked_pool=pool,
        cq_winner_ids=["novel_graph", "noise", "base_a"],
        protected_ids=["base_a"],
        graph_child_ids=["novel_graph"],
        final_top_k=6,
        obligation_results=obligations,
        graph_paths=[],
        query_class="direct_one_hop",
    )
    ids = [c["chunk_id"] for c in out]
    assert ids[:2] == ["base_a", "base_b"]
    assert "noise" not in ids
    # No uncovered obligation → relationship admission not required for direct_one_hop
    assert "novel_graph" not in ids
    assert diag["policy"] == "baseline_augment"
    assert diag["candidate_obligation_coverage"] >= diag["baseline_obligation_coverage"]


def test_admits_graph_chunk_for_uncovered_obligation():
    baseline = [_ch("base_a")]
    pool = baseline + [_ch("fill_o2"), _ch("noise")]
    obligations = [
        {"obligation_id": "o1", "ranked_child_ids": ["base_a"]},
        {"obligation_id": "o2", "ranked_child_ids": ["fill_o2"]},
    ]
    out, diag = build_candidate_finalists(
        baseline_finalists=baseline,
        ranked_pool=pool,
        cq_winner_ids=["fill_o2", "noise"],
        protected_ids=[],
        graph_child_ids=["fill_o2"],
        final_top_k=6,
        obligation_results=obligations,
        query_class="two_hop_dependency",
    )
    ids = [c["chunk_id"] for c in out]
    assert "base_a" in ids
    assert "fill_o2" in ids
    assert "noise" not in ids
    assert diag["candidate_obligation_coverage"] >= 1.0


def test_admits_relationship_graph_evidence_on_relationship_query():
    baseline = [_ch("base_a"), _ch("base_b")]
    pool = baseline + [_ch("rel_kid")]
    obligations = [
        {"obligation_id": "o1", "ranked_child_ids": ["base_a", "rel_kid"]},
        {"obligation_id": "o2", "ranked_child_ids": ["base_b"]},
    ]
    out, diag = build_candidate_finalists(
        baseline_finalists=baseline,
        ranked_pool=pool,
        cq_winner_ids=["rel_kid"],
        protected_ids=[],
        graph_child_ids=["rel_kid"],
        final_top_k=8,
        obligation_results=obligations,
        graph_paths=[{"supporting_child_ids": ["rel_kid"]}],
        query_class="two_hop_dependency",
    )
    ids = [c["chunk_id"] for c in out]
    assert ids[:2] == ["base_a", "base_b"]
    assert "rel_kid" in ids
    assert any(
        a.get("reason") == "provides_explicit_relationship_evidence"
        for a in diag["admitted_augmentations"]
    )


def test_coverage_rate_is_comparable():
    obligations = [
        {"obligation_id": "o1", "ranked_child_ids": ["a"]},
        {"obligation_id": "o2", "ranked_child_ids": ["b"]},
        {"obligation_id": "o3", "ranked_child_ids": ["c"]},
    ]
    assert obligation_coverage_rate(["a", "b"], obligations) == round(2 / 3, 4)
    assert obligation_coverage_rate(["a", "b", "c"], obligations) == 1.0
    assert obligation_coverage_rate([], obligations) == 0.0
