from models.schemas import ChatMessage, SourceChunk
from services.web_query_builder import build_web_search_query, build_web_search_tool_call


def _source(doc_name: str, score: float) -> SourceChunk:
    return SourceChunk(
        chunk_id=doc_name,
        parent_id=doc_name,
        doc_id=doc_name,
        corpus_id="corpus-a",
        text="Local source text",
        score=score,
        source_tier="qdrant_mongo",
        doc_name=doc_name,
        metadata={},
    )


def test_builder_uses_prior_user_context_only_for_disambiguation():
    query, rag_terms, context_terms, history_count = build_web_search_query(
        current_query="Explain the RemoteEvent validation patterns.",
        recent_messages=[
            ChatMessage(role="user", content="We are working in Roblox Luau security."),
            ChatMessage(role="assistant", content="Assistant text is ignored."),
            ChatMessage(role="user", content="Focus on server-side checks for RemoteEvent abuse."),
        ],
        rag_sources=[],
    )

    assert query == "Roblox RemoteEvent OnServerEvent validation security"
    assert rag_terms == ()
    assert context_terms == ("Roblox", "Luau", "security", "server-side")
    assert history_count == 2


def test_builder_does_not_pollute_query_with_irrelevant_high_scoring_rag():
    query, rag_terms, context_terms, history_count = build_web_search_query(
        current_query="RemoteEvent validation patterns",
        recent_messages=[],
        rag_sources=[
            _source("Scientific Advertising dealer psychology.md", 0.98),
            _source("PsychoGAT game scenes.pdf", 0.94),
        ],
    )

    assert query == "Roblox RemoteEvent OnServerEvent validation security"
    assert rag_terms == ()
    assert context_terms == ()
    assert history_count == 0


def test_builder_strips_local_corpus_language_from_remoteevent_web_query():
    query, rag_terms, context_terms, history_count = build_web_search_query(
        current_query=(
            "For Roblox Luau, what are the safest RemoteEvent validation "
            "patterns? Use current official guidance and my local Phase5 corpus."
        ),
        recent_messages=[],
        rag_sources=[],
    )

    assert query == "Roblox RemoteEvent OnServerEvent validation security official docs"
    assert "local" not in query.lower()
    assert "corpus" not in query.lower()
    assert "Phase5" not in query
    assert rag_terms == ()
    assert context_terms == ()
    assert history_count == 0


def test_builder_preserves_research_and_dataset_anchors():
    query, rag_terms, context_terms, _history_count = build_web_search_query(
        current_query=(
            "RAG wise, using these: CIFAR-10 dataset patterns 2x requires "
            "modification 90% enterprise applications PsychoGAT"
        ),
        recent_messages=[],
        rag_sources=[],
    )

    assert "CIFAR-10" in query
    assert "PsychoGAT" in query
    assert "enterprise" in query
    assert "applications" in query
    assert "RAG" not in query
    assert rag_terms == ()
    assert context_terms == ()


def test_tool_call_shape_matches_native_web_search_message_contract():
    plan = build_web_search_tool_call(
        current_query="With Web enabled, search the SearXNG Obscura setup.",
        recent_messages=[],
        rag_sources=[],
        max_results=7,
    )

    assert plan.tool_call["id"] == "server_web_search_1"
    assert plan.tool_call["function"]["name"] == "web_search"
    assert plan.args == {"query": "SearXNG Obscura setup", "max_results": 7}
    assert plan.attempted is True
    assert plan.native_tool_call is False
    assert plan.strategy == "deterministic"
