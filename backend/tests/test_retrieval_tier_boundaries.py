from models.schemas import RetrievalTier
from services.chat_orchestrator import _is_graph_augmented_tier
from services.retriever import _document_anchor_limit_for, _rerank_enabled_for_tier


def test_graph_context_gate_only_allows_graph_augmented_tier():
    assert not _is_graph_augmented_tier(RetrievalTier.qdrant_only)
    assert not _is_graph_augmented_tier(RetrievalTier.qdrant_mongo)
    assert _is_graph_augmented_tier(RetrievalTier.qdrant_mongo_graph)
    assert _is_graph_augmented_tier("qdrant_mongo_graph")


def test_document_anchor_recall_is_only_for_hydrated_tiers():
    assert _document_anchor_limit_for(RetrievalTier.qdrant_only, retrieval_k=40) == 0
    assert _document_anchor_limit_for(RetrievalTier.qdrant_mongo, retrieval_k=40) > 0
    assert _document_anchor_limit_for(RetrievalTier.qdrant_mongo_graph, retrieval_k=40) > 0


def test_fast_search_never_invokes_cross_encoder_reranking():
    assert not _rerank_enabled_for_tier(True, RetrievalTier.qdrant_only)
    assert not _rerank_enabled_for_tier(False, RetrievalTier.qdrant_only)
    assert _rerank_enabled_for_tier(True, RetrievalTier.qdrant_mongo)
    assert _rerank_enabled_for_tier(True, RetrievalTier.qdrant_mongo_graph)
