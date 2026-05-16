from models.schemas import RetrievalTier
from services.chat_orchestrator import _is_graph_augmented_tier


def test_graph_context_gate_only_allows_graph_augmented_tier():
    assert not _is_graph_augmented_tier(RetrievalTier.qdrant_only)
    assert not _is_graph_augmented_tier(RetrievalTier.qdrant_mongo)
    assert _is_graph_augmented_tier(RetrievalTier.qdrant_mongo_graph)
    assert _is_graph_augmented_tier("qdrant_mongo_graph")
