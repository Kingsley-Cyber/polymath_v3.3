from models.schemas import RetrievalTier, SourceChunk
from services.chat_orchestrator import _is_graph_augmented_tier
from services.retriever import (
    _document_anchor_limit_for,
    _filter_fast_grounded_candidates,
    _rerank_enabled_for_tier,
)


def test_fast_grounding_filter_drops_fillers_but_fails_open() -> None:
    grounded = SourceChunk(
        chunk_id="grounded",
        parent_id="p-grounded",
        doc_id="d-grounded",
        corpus_id="c1",
        text="Made to Stick describes sticky ideas.",
        score=0.8,
        source_tier="vector",
        metadata={"query_grounding": {"matched_count": 2}},
    )
    filler = SourceChunk(
        chunk_id="filler",
        parent_id="p-filler",
        doc_id="d-filler",
        corpus_id="c1",
        text="A wooden stick rotates.",
        score=0.7,
        source_tier="vector",
        metadata={"query_grounding": {"matched_count": 0}},
    )
    weak = SourceChunk(
        chunk_id="weak",
        parent_id="p-weak",
        doc_id="d-weak",
        corpus_id="c1",
        text="A generic message example.",
        score=0.75,
        source_tier="vector",
        metadata={"query_grounding": {"matched_count": 1}},
    )

    filtered, dropped = _filter_fast_grounded_candidates([grounded, weak, filler])
    assert filtered == [grounded]
    assert dropped == 2

    ungrounded, dropped = _filter_fast_grounded_candidates([filler])
    assert ungrounded == [filler]
    assert dropped == 0


def test_fast_grounding_prefers_contiguous_named_phrase() -> None:
    exact = SourceChunk(
        chunk_id="exact",
        parent_id="p-exact",
        doc_id="d-exact",
        corpus_id="c1",
        doc_name="Purple Ocean notes",
        text="Purple Ocean enters a proven market with a new angle.",
        score=0.8,
        source_tier="vector",
        metadata={
            "query_grounding": {
                "matched": ["purple", "ocean"],
                "matched_count": 2,
            }
        },
    )
    separated = SourceChunk(
        chunk_id="separated",
        parent_id="p-separated",
        doc_id="d-separated",
        corpus_id="c1",
        text="Ocean conditions matter. The system requires planning.",
        score=0.9,
        source_tier="vector",
        metadata={
            "query_grounding": {
                "matched": ["ocean", "require"],
                "matched_count": 2,
            }
        },
    )

    filtered, dropped = _filter_fast_grounded_candidates(
        [separated, exact],
        query="What exact law does Purple Ocean require?",
    )

    assert filtered == [exact]
    assert dropped == 1


def test_graph_context_gate_only_allows_graph_augmented_tier():
    assert not _is_graph_augmented_tier(RetrievalTier.qdrant_only)
    assert not _is_graph_augmented_tier(RetrievalTier.qdrant_mongo)
    assert _is_graph_augmented_tier(RetrievalTier.qdrant_mongo_graph)
    assert _is_graph_augmented_tier("qdrant_mongo_graph")


def test_document_anchor_recall_is_only_for_hydrated_tiers():
    assert _document_anchor_limit_for(RetrievalTier.qdrant_only, retrieval_k=40) == 0
    assert _document_anchor_limit_for(RetrievalTier.qdrant_mongo, retrieval_k=40) > 0
    assert (
        _document_anchor_limit_for(RetrievalTier.qdrant_mongo_graph, retrieval_k=40) > 0
    )


def test_fast_search_never_invokes_cross_encoder_reranking():
    assert not _rerank_enabled_for_tier(True, RetrievalTier.qdrant_only)
    assert not _rerank_enabled_for_tier(False, RetrievalTier.qdrant_only)
    assert _rerank_enabled_for_tier(True, RetrievalTier.qdrant_mongo)
    assert _rerank_enabled_for_tier(True, RetrievalTier.qdrant_mongo_graph)
