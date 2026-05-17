import pytest
from types import SimpleNamespace

from services.web_freshness import (
    _query_should_include_social_sources,
    _diversify_web_source_chunks,
    _extract_webpage_text,
    _raw_source_candidate_urls,
    build_web_search_queries,
    build_search_query,
    infer_web_search_time_range,
    parse_searxng_results,
    refine_tool_search_query,
    rerank_web_source_chunks,
    select_related_search_terms,
    web_hits_to_source_chunks,
    live_web_search,
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


def test_infer_web_search_time_range_for_current_fast_moving_query():
    assert (
        infer_web_search_time_range(
            "current way ahead as of 2026 for mobile small language model RAG"
        )
        == "year"
    )
    assert infer_web_search_time_range("latest Gemini release notes") == "month"
    assert infer_web_search_time_range("TSLA earnings market sentiment") == "month"
    assert infer_web_search_time_range("today OpenAI outage") == "day"
    assert infer_web_search_time_range("history of Smalltalk MVC") is None


def test_build_web_search_queries_adds_social_variants_for_practical_current_query():
    queries = build_web_search_queries(
        "current way ahead as of 2026 for mobile small language model RAG deployment"
    )

    assert queries[0] == (
        "current way ahead as of 2026 for mobile small language model RAG deployment"
    )
    assert any(query.startswith("!hfm ") for query in queries)
    assert any(query.startswith("!red ") for query in queries)
    assert _query_should_include_social_sources(queries[0]) is True


def test_build_web_search_queries_adds_huggingface_model_variant():
    queries = build_web_search_queries("local mobile LLM model options 4GB VRAM")

    assert queries[0] == "local mobile LLM model options 4GB VRAM"
    assert "!hfm mobile llm" in queries
    assert "!hfm GGUF small LLM" in queries
    assert not any(query.startswith("!reu ") for query in queries)


def test_build_web_search_queries_adds_finance_news_variants():
    queries = build_web_search_queries("TSLA earnings market sentiment")

    assert queries[0] == "TSLA earnings market sentiment"
    assert "!reu TSLA earnings market sentiment" in queries
    assert "!bin TSLA earnings market sentiment" in queries
    assert "!ddn TSLA earnings market sentiment" in queries
    assert "!red TSLA earnings market sentiment" in queries


def test_build_web_search_queries_adds_research_variants():
    queries = build_web_search_queries("mobile RAG research papers 2026")

    assert queries[0] == "mobile RAG research papers 2026"
    assert "!arx mobile RAG research papers 2026" in queries
    assert "!sem mobile RAG research papers 2026" in queries


def test_build_web_search_queries_adds_roblox_domain_variants():
    queries = build_web_search_queries("make roblox inventory system")

    assert "make roblox inventory system site:create.roblox.com/docs" in queries
    assert "make roblox inventory system site:devforum.roblox.com" in queries
    assert "!gh roblox luau make roblox inventory system" in queries
    assert "!yt roblox make roblox inventory system tutorial" in queries
    assert "!red roblox make roblox inventory system" in queries


def test_build_web_search_queries_adds_direct_roblox_api_doc_variant():
    queries = build_web_search_queries("Roblox RemoteEvent security server client")

    assert (
        "RemoteEvent site:create.roblox.com/docs/reference/engine/classes/RemoteEvent"
        in queries
    )


def test_build_web_search_queries_adds_ai_video_variants():
    queries = build_web_search_queries(
        "ComfyUI WAN 2.1 local AI video workflow RTX 4090"
    )

    assert "!hfm Wan video" in queries
    assert any(query.startswith("!gh ComfyUI ") for query in queries)
    assert any("site:civitai.com" in query for query in queries)
    assert any("site:replicate.com" in query for query in queries)
    assert any("site:fal.ai" in query for query in queries)


def test_build_web_search_queries_adds_creator_economy_variants():
    queries = build_web_search_queries("Roblox UGC market trends")

    assert "Roblox UGC market trends site:rolimons.com" in queries
    assert "Roblox UGC market trends site:devforum.roblox.com" in queries
    assert "!red Roblox UGC market trends" in queries
    assert "!yt Roblox UGC market trends trend analysis" in queries
    assert not any(query.startswith("!reu ") for query in queries)


def test_build_web_search_queries_preserves_explicit_marketplace_sources():
    queries = build_web_search_queries(
        "AI video tool market demand Product Hunt Gumroad Polymarket"
    )

    assert any("site:producthunt.com" in query for query in queries)
    assert any("site:gumroad.com" in query for query in queries)
    assert any("site:polymarket.com" in query for query in queries)


def test_build_web_search_queries_adds_cyber_variants():
    queries = build_web_search_queries("CVE docker container escape owasp")

    assert "!nvd docker container escape" in queries
    assert "CVE docker container escape owasp site:cisa.gov" in queries
    assert "CVE docker container escape owasp site:owasp.org" in queries
    assert "CVE docker container escape owasp site:docs.docker.com" in queries


def test_build_web_search_queries_skips_social_variants_for_research_query():
    queries = build_web_search_queries(
        "mobile small language model RAG research papers 2026"
    )

    assert not any(query.startswith("!red ") for query in queries)


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


def test_refine_tool_search_query_preserves_hardware_terms():
    query = refine_tool_search_query(
        "small language models 4GB RAM mobile deployment 2026 on-device RAG",
        "what are some model options i have for a 4gb vram mobile device",
    )

    assert "vram" in query.lower()
    assert "4gb" in query.lower()


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
        search_query="pytorch docs latest",
        time_range="month",
    )

    chunks = web_hits_to_source_chunks(
        hits,
        expanded_terms=["PyTorch"],
    )

    assert chunks[0].source_tier == "web_search"
    assert chunks[0].corpus_name == "Live Web"
    assert "URL: https://pytorch.org/docs/stable/index.html" in chunks[0].text
    assert "Search query: pytorch docs latest" in chunks[0].text
    assert "Freshness filter: month" in chunks[0].text
    assert chunks[0].metadata["url"] == "https://pytorch.org/docs/stable/index.html"
    assert chunks[0].metadata["time_range"] == "month"


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


def test_diversify_web_source_chunks_caps_research_for_practical_queries():
    hits = parse_searxng_results(
        {
            "results": [
                {
                    "title": "Mobile RAG paper",
                    "url": "https://arxiv.org/html/2602.13229v1",
                    "content": "Academic paper about on-device RAG.",
                },
                {
                    "title": "Another deployment paper",
                    "url": "https://doi.org/10.1234/mobile-rag",
                    "content": "Another academic paper.",
                },
                {
                    "title": "Google AI Edge guide",
                    "url": "https://developers.googleblog.com/en/google-ai-edge-small-language-models-multimodality-rag-function-calling/",
                    "content": "Developer guidance for on-device models.",
                },
                {
                    "title": "ExecuTorch docs",
                    "url": "https://pytorch.org/executorch/stable/index.html",
                    "content": "Production documentation for on-device AI.",
                },
            ]
        },
        max_results=4,
    )
    chunks = web_hits_to_source_chunks(hits, search_query="mobile RAG deployment")

    selected = _diversify_web_source_chunks(
        "mobile RAG deployment",
        chunks,
        limit=3,
    )

    assert [chunk.metadata["url"] for chunk in selected] == [
        "https://arxiv.org/html/2602.13229v1",
        "https://developers.googleblog.com/en/google-ai-edge-small-language-models-multimodality-rag-function-calling/",
        "https://pytorch.org/executorch/stable/index.html",
    ]


def test_diversify_web_source_chunks_allows_research_when_requested():
    hits = parse_searxng_results(
        {
            "results": [
                {
                    "title": "Mobile RAG paper",
                    "url": "https://arxiv.org/html/2602.13229v1",
                    "content": "Academic paper about on-device RAG.",
                },
                {
                    "title": "Another deployment paper",
                    "url": "https://doi.org/10.1234/mobile-rag",
                    "content": "Another academic paper.",
                },
            ]
        },
        max_results=2,
    )
    chunks = web_hits_to_source_chunks(hits, search_query="mobile RAG research papers")

    selected = _diversify_web_source_chunks(
        "mobile RAG research papers",
        chunks,
        limit=2,
    )

    assert [chunk.metadata["url"] for chunk in selected] == [
        "https://arxiv.org/html/2602.13229v1",
        "https://doi.org/10.1234/mobile-rag",
    ]


def test_diversify_web_source_chunks_keeps_one_social_source_when_available():
    hits = parse_searxng_results(
        {
            "results": [
                {
                    "title": "Vendor deployment guide",
                    "url": "https://example.com/mobile-rag-guide",
                    "content": "Practical mobile RAG deployment guidance.",
                },
                {
                    "title": "Framework docs",
                    "url": "https://pytorch.org/executorch/stable/index.html",
                    "content": "ExecuTorch on-device documentation.",
                },
                {
                    "title": "Developer blog",
                    "url": "https://developers.googleblog.com/en/ai-edge/",
                    "content": "AI Edge deployment notes.",
                },
                {
                    "title": "Community thread",
                    "url": "https://www.reddit.com/r/LocalLLaMA/comments/example/mobile_rag/",
                    "content": "Developers discuss practical on-device RAG issues.",
                },
            ]
        },
        max_results=4,
    )
    chunks = web_hits_to_source_chunks(hits, search_query="current mobile RAG deployment")

    selected = _diversify_web_source_chunks(
        "current mobile RAG deployment",
        chunks,
        limit=3,
    )

    assert any("reddit.com" in chunk.metadata["url"] for chunk in selected)


@pytest.mark.asyncio
async def test_search_pool_mixes_social_variants(monkeypatch):
    calls = []

    async def fake_search(query, *, max_results=None, time_range=None):
        calls.append((query, max_results, time_range))
        if query.startswith("!red"):
            return [
                *parse_searxng_results(
                    {
                        "results": [
                            {
                                "title": "Ignored Reddit front page",
                                "url": "https://www.reddit.com/r/LocalLLaMA/",
                                "content": "A listing page, not a thread.",
                            },
                            {
                                "title": "Reddit mobile RAG",
                                "url": "https://www.reddit.com/r/LocalLLaMA/comments/example/mobile_rag/",
                                "content": "A community deployment thread.",
                            }
                        ]
                    },
                    max_results=2,
                    search_query=query,
                    time_range=time_range,
                )
            ]
        if query.startswith("!hn"):
            return [
                *parse_searxng_results(
                    {
                        "results": [
                            {
                                "title": "Ignored non-HN result",
                                "url": "https://example.org/not-hn",
                                "content": "Search engine ignored the bang.",
                            },
                            {
                                "title": "HN mobile RAG",
                                "url": "https://news.ycombinator.com/item?id=1",
                                "content": "A technical discussion.",
                            }
                        ]
                    },
                    max_results=3,
                    search_query=query,
                    time_range=time_range,
                )
            ]
        return parse_searxng_results(
            {
                "results": [
                    {
                        "title": f"Base {i}",
                        "url": f"https://example.com/{i}",
                        "content": "Base result.",
                    }
                    for i in range(8)
                ]
            },
            max_results=8,
            search_query=query,
            time_range=time_range,
        )

    monkeypatch.setattr(live_web_search, "_search_searxng", fake_search)

    hits = await live_web_search._search_searxng_pool(
        "current mobile RAG deployment",
        max_results=6,
        time_range="year",
    )

    urls = [hit.url for hit in hits]
    assert "https://www.reddit.com/r/LocalLLaMA/comments/example/mobile_rag/" in urls
    assert "https://www.reddit.com/r/LocalLLaMA/" not in urls
    assert "https://news.ycombinator.com/item?id=1" in urls
    assert "https://example.org/not-hn" not in urls
    assert all(
        call[2] is None if call[0].startswith("!hfm") else call[2] == "year"
        for call in calls
    )
    assert all(call[1] == 5 for call in calls)


def test_extract_webpage_text_prefers_article_body():
    text = _extract_webpage_text(
        """
        <html>
          <head><title>Mobile deployment guide</title></head>
          <body>
            <nav>home docs blog</nav>
            <article>
              <h1>Deploying small models on device</h1>
              <p>Use quantized models with a mobile inference runtime.</p>
              <p>Keep retrieval local and compact for latency.</p>
              <p>This paragraph adds enough realistic page body text for the
              extractor to keep the result instead of treating it as chrome.</p>
            </article>
          </body>
        </html>
        """,
        max_chars=500,
    )

    assert text is not None
    assert "Mobile deployment guide" in text
    assert "Deploying small models on device" in text
    assert "home docs blog" not in text


def test_raw_source_candidate_urls_map_creator_docs_to_github_raw():
    urls = _raw_source_candidate_urls(
        "https://create.roblox.com/docs/reference/engine/classes/RemoteEvent"
    )

    assert urls == [
        "https://raw.githubusercontent.com/Roblox/creator-docs/main/"
        "content/en-us/reference/engine/classes/RemoteEvent.yaml"
    ]


def test_raw_source_candidate_urls_map_creator_docs_member_to_parent_yaml():
    urls = _raw_source_candidate_urls(
        "https://create.roblox.com/docs/reference/engine/classes/RemoteEvent/OnServerEvent"
    )

    assert urls == [
        "https://raw.githubusercontent.com/Roblox/creator-docs/main/"
        "content/en-us/reference/engine/classes/RemoteEvent.yaml"
    ]


@pytest.mark.asyncio
async def test_fetch_one_page_uses_static_before_allowlisted_obscura(monkeypatch):
    settings = SimpleNamespace(
        OBSCURA_COMMAND="obscura",
        LIVE_WEB_PAGE_FETCHER="auto",
        LIVE_WEB_OBSCURA_DOMAINS="producthunt.com",
        LIVE_WEB_FETCH_CACHE_TTL_SECONDS=0,
        OBSCURA_TIMEOUT_SECONDS=10.0,
        OBSCURA_MAX_CHARS=4000,
    )
    calls = []

    async def fake_raw(url):
        calls.append(("raw", url))
        return None

    async def fake_httpx(url):
        calls.append(("httpx", url))
        return "Static extraction result."

    async def fake_obscura(url):
        calls.append(("obscura", url))
        return "Obscura extraction result."

    monkeypatch.setattr("services.web_freshness.get_settings", lambda: settings)
    monkeypatch.setattr(live_web_search, "_fetch_one_with_raw_adapter", fake_raw)
    monkeypatch.setattr(live_web_search, "_fetch_one_with_httpx", fake_httpx)
    monkeypatch.setattr(live_web_search, "_fetch_one_with_obscura", fake_obscura)

    text = await live_web_search._fetch_one_page("https://example.com/docs")

    assert text == "Static extraction result."
    assert calls == [
        ("raw", "https://example.com/docs"),
        ("httpx", "https://example.com/docs"),
    ]


@pytest.mark.asyncio
async def test_fetch_one_page_uses_obscura_only_after_static_failure(monkeypatch):
    settings = SimpleNamespace(
        OBSCURA_COMMAND="obscura",
        LIVE_WEB_PAGE_FETCHER="auto",
        LIVE_WEB_OBSCURA_DOMAINS="producthunt.com",
        LIVE_WEB_FETCH_CACHE_TTL_SECONDS=0,
        OBSCURA_TIMEOUT_SECONDS=10.0,
        OBSCURA_MAX_CHARS=4000,
    )
    calls = []

    async def fake_empty(_url):
        calls.append("static")
        return None

    async def fake_obscura(url):
        calls.append("obscura")
        return f"Rendered {url}"

    monkeypatch.setattr("services.web_freshness.get_settings", lambda: settings)
    monkeypatch.setattr(live_web_search, "_fetch_one_with_raw_adapter", fake_empty)
    monkeypatch.setattr(live_web_search, "_fetch_one_with_httpx", fake_empty)
    monkeypatch.setattr(live_web_search, "_fetch_one_with_obscura", fake_obscura)

    text = await live_web_search._fetch_one_page(
        "https://www.producthunt.com/products/opencutai-video"
    )

    assert text == "Rendered https://www.producthunt.com/products/opencutai-video"
    assert calls == ["static", "static", "obscura"]
