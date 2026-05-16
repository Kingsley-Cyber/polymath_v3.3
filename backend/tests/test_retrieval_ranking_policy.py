from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import infer_retrieval_intent
from services.retriever.ranking_policy import (
    apply_candidate_weights,
    candidate_kind,
    select_with_diversity,
)


def _chunk(
    chunk_id: str,
    *,
    score: float,
    parent_id: str | None = None,
    doc_id: str | None = None,
    summary: str | None = None,
    source_tier: str = "tier_a",
    provenance: list[dict] | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=parent_id or chunk_id,
        doc_id=doc_id or f"doc-{chunk_id}",
        corpus_id="corpus-1",
        text=summary or f"text {chunk_id}",
        summary=summary,
        score=score,
        source_tier=source_tier,
        provenance=provenance,
    )


def test_candidate_kind_detects_summary_lexical_and_child():
    assert candidate_kind(_chunk("c1", score=0.9)) == "child"
    assert candidate_kind(
        _chunk("p1_summary", score=0.9, summary="overview")
    ) == "summary"
    assert candidate_kind(
        _chunk("c2", score=0.9, source_tier="tier_a+lexical")
    ) == "lexical"


def test_broad_weighting_lifts_summary_over_near_child():
    intent = infer_retrieval_intent("summarize the main themes")
    chunks = [
        _chunk("child", score=0.90),
        _chunk("parent_summary", score=0.86, summary="theme overview"),
    ]

    weighted = apply_candidate_weights(
        chunks,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    assert weighted[0].chunk_id == "parent_summary"


def test_diversity_adds_max_two_strong_distinct_sources_for_broad_hybrid():
    intent = infer_retrieval_intent("summarize themes across documents")
    ranked = [
        _chunk("c1", score=1.00, parent_id="p1", doc_id="d1"),
        _chunk("c2", score=0.96, parent_id="p2", doc_id="d2"),
        _chunk("c3", score=0.93, parent_id="p3", doc_id="d3"),
        _chunk("c4", score=0.91, parent_id="p4", doc_id="d4"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    assert result.added == 2
    assert [c.chunk_id for c in result.candidates] == ["c1", "c2", "c3", "c4"]


def test_diversity_skips_weak_or_duplicate_candidates():
    intent = infer_retrieval_intent("summarize themes across documents")
    ranked = [
        _chunk("c1", score=1.00, parent_id="p1", doc_id="d1"),
        _chunk("c2", score=0.96, parent_id="p2", doc_id="d2"),
        _chunk("dup", score=0.95, parent_id="p1", doc_id="d1"),
        _chunk("weak", score=0.20, parent_id="p3", doc_id="d3"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    assert result.added == 0
    assert [c.chunk_id for c in result.candidates] == ["c1", "c2"]


def test_vector_base_does_not_expand_final_sources_for_diversity():
    intent = infer_retrieval_intent("summarize themes across documents")
    ranked = [
        _chunk("c1", score=1.00, parent_id="p1", doc_id="d1"),
        _chunk("c2", score=0.96, parent_id="p2", doc_id="d2"),
        _chunk("c3", score=0.93, parent_id="p3", doc_id="d3"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_only,
    )

    assert result.added == 0
    assert [c.chunk_id for c in result.candidates] == ["c1", "c2"]
