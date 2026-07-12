import pytest

from models.schemas import RetrievalResult, RetrievalTier, SourceChunk
from services.retriever import (
    RetrieverOrchestrator,
    _missing_concept_support_query,
    reranker_service,
)


def _chunk(
    chunk_id: str,
    *,
    corpus_id: str,
    text: str,
    score: float,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        doc_id=f"doc-{chunk_id}",
        corpus_id=corpus_id,
        text=text,
        score=score,
        source_tier="tier_a",
    )


def _result(
    chunks: list[SourceChunk],
    *,
    answerable: bool,
    coverage: float,
    missing: list[str],
) -> RetrievalResult:
    return RetrievalResult(
        chunks=chunks,
        requested_tier=RetrievalTier.qdrant_mongo_graph,
        effective_tier=RetrievalTier.qdrant_mongo_graph,
        diagnostics={
            "selection": {
                "sufficiency": {
                    "answerable": answerable,
                    "required_coverage": coverage,
                    "missing_atoms": missing,
                }
            },
            "counts": {"candidates": len(chunks)},
            "total_s": 1.0,
        },
    )


def test_missing_concept_support_query_is_bounded_and_concept_only():
    result = _result(
        [],
        answerable=False,
        coverage=0.4,
        missing=[
            "relationship",
            "concept:sticky",
            "concept:message_design",
            "concept:emotional",
            "concept:ignored_fourth",
        ],
    )

    assert _missing_concept_support_query(result) == "sticky message design emotional"


@pytest.mark.asyncio
async def test_cross_corpus_missing_concept_repair_fetches_and_adopts_better_context(
    monkeypatch,
):
    primary = _result(
        [
            _chunk(
                "primary",
                corpus_id="marketing",
                text=(
                    "Emotional contrast improves branded dropshipping video ad "
                    "design and connects visual intensity to response."
                ),
                score=0.90,
            )
        ],
        answerable=False,
        coverage=0.75,
        missing=["concept:sticky_message"],
    )
    support = RetrievalResult(
        chunks=[
            _chunk(
                "support",
                corpus_id="books",
                text=(
                    "Sticky message design uses concrete emotional stories so "
                    "an idea is memorable."
                ),
                score=0.88,
            )
        ],
        requested_tier=RetrievalTier.qdrant_mongo,
        effective_tier=RetrievalTier.qdrant_mongo,
    )
    orchestrator = RetrieverOrchestrator()
    support_calls = []

    async def fake_retrieve_uncached(**kwargs):
        support_calls.append(kwargs)
        return support

    async def fake_rerank(query, chunks):
        ranked = []
        for chunk in chunks:
            copied = chunk.model_copy()
            copied.score = 0.95 if "Sticky message" in copied.text else 0.90
            ranked.append(copied)
        return sorted(ranked, key=lambda chunk: chunk.score, reverse=True)

    monkeypatch.setattr(orchestrator, "_retrieve_uncached", fake_retrieve_uncached)
    monkeypatch.setattr(reranker_service, "rerank", fake_rerank)

    repaired = await orchestrator._repair_cross_corpus_missing_concepts(
        primary,
        {
            "query": (
                "How can emotional contrast and sticky message design improve "
                "branded dropshipping video ads?"
            ),
            "ranking_query": (
                "How can emotional contrast and sticky message design improve "
                "branded dropshipping video ads?"
            ),
            "corpus_ids": ["marketing", "books"],
            "retrieval_tier": RetrievalTier.qdrant_mongo_graph,
            "collections": None,
            "rerank_enabled": True,
            "search_mode": "local",
            "final_top_k": 4,
        },
    )

    meta = repaired.diagnostics["external_sufficiency_repair"]
    assert support_calls[0]["query"] == "sticky message"
    assert support_calls[0]["retrieval_tier"] == RetrievalTier.qdrant_mongo
    assert support_calls[0]["rerank_enabled"] is False
    assert meta["adopted"] is True
    assert meta["coverage_after"] > meta["coverage_before"]
    assert "support" in [chunk.chunk_id for chunk in repaired.chunks]
