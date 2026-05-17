import pytest

from services.web_freshness import (
    build_search_query,
    parse_searxng_results,
    refine_tool_search_query,
    rerank_web_source_chunks,
    select_related_search_terms,
    web_hits_to_source_chunks,
)


def test_search_query_uses_user_query_without_expansion():
    query = build_search_query("Compare C++23 modules with old header patterns")
    assert query == "Compare C++23 modules with old header patterns"


def test_search_query_ignores_graph_related_terms_when_supplied():
    query = build_search_query(
        "the narrative constructions and chatroom data concepts",
        [
            "Narrative construction",
            "chatroom interaction",
            "data",
            "Narrative construction",
        ],
    )

    assert query == "the narrative constructions and chatroom data concepts"


def test_refine_tool_search_query_replaces_tiny_ambiguous_query():
    query = refine_tool_search_query(
        "small",
        "what is the current way ahead for AI small language models on mobile RAG",
    )

    assert query == "what is the current way ahead for AI small language models on mobile RAG"


def test_refine_tool_search_query_preserves_user_acronyms():
    query = refine_tool_search_query(
        "on-device language model deployment mobile",
        "what is the current way ahead for AI small language models on mobile RAG",
    )

    assert query == "on-device language model deployment mobile AI RAG"


def test_related_search_terms_are_bounded_and_deduped():
    terms = select_related_search_terms(
        "chatroom discourse concepts",
        [
            "Chatroom Interaction",
            "chatroom interaction",
            "entity",
            "Discourse analysis",
            "https://example.com",
        ],
    )

    assert terms == ["Chatroom Interaction", "Discourse analysis"]


def test_related_search_terms_ignore_unrelated_graph_terms():
    terms = select_related_search_terms(
        "what is the current way ahead for ai small language models on mobile rag",
        [
            "Table Data Gateway",
            "Gateway",
            "Row Data Gateway",
            "Domain Model",
            "small language model deployment",
        ],
    )

    assert terms == ["small language model deployment"]


def test_parse_searxng_results_dedupes_and_strips_html():
    payload = {
        "results": [
            {
                "title": "<b>Swift 6</b>",
                "url": "https://developer.apple.com/swift/",
                "content": "Latest <em>language</em> notes",
                "engines": ["duckduckgo", "bing"],
                "score": 2.0,
            },
            {
                "title": "duplicate",
                "url": "https://developer.apple.com/swift/",
                "content": "ignored",
            },
            {
                "title": "bad",
                "url": "javascript:alert(1)",
                "content": "ignored",
            },
        ]
    }

    hits = parse_searxng_results(payload, max_results=5)

    assert len(hits) == 1
    assert hits[0].title == "Swift 6"
    assert hits[0].snippet == "Latest language notes"
    assert hits[0].engines == ("duckduckgo", "bing")


def test_web_hits_become_source_chunks_with_url_context():
    hits = parse_searxng_results(
        {
            "results": [
                {
                    "title": "PyTorch docs",
                    "url": "https://pytorch.org/docs/stable/index.html",
                    "content": "Current PyTorch API reference.",
                    "engine": "google",
                }
            ]
        },
        max_results=1,
    )

    chunks = web_hits_to_source_chunks(
        hits,
        search_query="pytorch docs latest",
        expanded_terms=["PyTorch"],
    )

    assert chunks[0].source_tier == "web_search"
    assert chunks[0].corpus_name == "Live Web"
    assert "URL: https://pytorch.org/docs/stable/index.html" in chunks[0].text
    assert "Search query: pytorch docs latest" in chunks[0].text
    assert chunks[0].metadata["url"] == "https://pytorch.org/docs/stable/index.html"


@pytest.mark.asyncio
async def test_rerank_web_source_chunks_uses_local_reranker(monkeypatch):
    hits = parse_searxng_results(
        {
            "results": [
                {
                    "title": "Dictionary small",
                    "url": "https://example.com/small",
                    "content": "Small means little in size.",
                },
                {
                    "title": "Mobile RAG SLM deployment",
                    "url": "https://example.com/mobile-rag",
                    "content": "On-device RAG combines a small language model with local retrieval.",
                },
                {
                    "title": "Running shoes",
                    "url": "https://example.com/shoes",
                    "content": "Running shoe catalog.",
                },
            ]
        },
        max_results=14,
    )
    chunks = web_hits_to_source_chunks(hits, search_query="mobile RAG SLM")

    async def fake_rerank(query, pool):
        assert query == "mobile RAG SLM"
        by_url = {chunk.metadata["url"]: chunk.model_copy() for chunk in pool}
        ranked = [
            by_url["https://example.com/mobile-rag"],
            by_url["https://example.com/small"],
            by_url["https://example.com/shoes"],
        ]
        for score, chunk in zip((9.5, -7.0, -11.0), ranked):
            chunk.score = score
        return ranked

    import services.reranker as reranker_module

    monkeypatch.setattr(reranker_module.reranker_service, "rerank", fake_rerank)

    ranked = await rerank_web_source_chunks("mobile RAG SLM", chunks, limit=2)

    assert [chunk.metadata["url"] for chunk in ranked] == [
        "https://example.com/mobile-rag",
        "https://example.com/small",
    ]
    assert ranked[0].score == 9.5
