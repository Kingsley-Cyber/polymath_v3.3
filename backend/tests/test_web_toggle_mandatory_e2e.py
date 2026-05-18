import json
import os

import pytest
from bson import ObjectId

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from models.schemas import (  # noqa: E402
    ChatMessage,
    ChatRequest,
    ModelConfig,
    ModelOverrides,
    RetrievalResult,
    RetrievalTier,
    SourceChunk,
)
from services import chat_orchestrator as co  # noqa: E402
from services import web_freshness as wf  # noqa: E402
from services import web_query_enrichment as wqe  # noqa: E402
from services.chat_orchestrator import ChatOrchestrator  # noqa: E402
from services.web_freshness import WebSearchHit  # noqa: E402


def _parse_sse(frame: str) -> dict:
    assert frame.startswith("data: ")
    return json.loads(frame.removeprefix("data: ").strip())


def _local_source(chunk_id: str) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"{chunk_id}-parent",
        doc_id=f"{chunk_id}-doc",
        corpus_id="corpus-a",
        text=f"Local RAG evidence for {chunk_id}",
        score=0.88,
        source_tier="qdrant_mongo",
        corpus_name="Local Corpus",
        doc_name=f"{chunk_id}.md",
        metadata={},
    )


def _hit(i: int) -> WebSearchHit:
    domain = "example-js.test" if i == 3 else f"source{i}.example.test"
    return WebSearchHit(
        title=f"Utility web source {i}",
        url=f"https://{domain}/article-{i}",
        snippet=(
            "Polymath web retrieval utility model GLM SearXNG Obscura "
            f"final sources evidence {i}"
        ),
        score=1.0 - (i * 0.01),
        engines=("searxng",),
        search_query="Polymath web retrieval utility model GLM Obscura SearXNG final sources",
    )


async def _fast_query_profile(_request, user_id=None):
    return {
        "retrieval_k": 3,
        "rerank_enabled": True,
        "query_profile": "fast",
        "hyde_explicit": False,
        "top_k_summary": 0,
        "rerank_top_n": 8,
        "similarity_threshold": 0.0,
        "neo4j_expansion_cap": 0,
        "max_corpora_per_query": 1,
        "final_top_k": 3,
        "fact_seed_limit": 0,
    }


@pytest.mark.asyncio
async def test_web_toggle_on_runs_mandatory_utility_to_web_to_rerank_pipeline(
    monkeypatch,
):
    monkeypatch.setattr(co.settings, "LIVE_WEB_SEARCH_ENABLED", True, raising=False)
    monkeypatch.setattr(
        co.settings,
        "LIVE_WEB_SEARCH_CANDIDATE_RESULTS",
        12,
        raising=False,
    )
    monkeypatch.setattr(co.settings, "LIVE_WEB_SEARCH_FETCH_FULL_PAGES", True, raising=False)
    monkeypatch.setattr(co.settings, "LIVE_WEB_FETCH_MAX_PAGES", 4, raising=False)
    monkeypatch.setattr(co.settings, "LIVE_WEB_PAGE_FETCHER", "auto", raising=False)
    monkeypatch.setattr(co.settings, "OBSCURA_COMMAND", "obscura", raising=False)
    monkeypatch.setattr(
        co.settings,
        "LIVE_WEB_OBSCURA_DOMAINS",
        "example-js.test,producthunt.com",
        raising=False,
    )
    monkeypatch.setattr(co.settings, "OBSCURA_MAX_CHARS", 4000, raising=False)
    monkeypatch.setattr(
        co.settings,
        "RERANKER_MODEL",
        "llama.cpp/qwen3-reranker",
        raising=False,
    )
    monkeypatch.setattr(co.settings, "AGENTIC_MODE_ENABLED", False, raising=False)
    monkeypatch.setattr(
        co.settings,
        "DEFAULT_COMPLETION_MODEL",
        "openai/base-query",
        raising=False,
    )
    monkeypatch.setattr(co.settings, "AGENTIC_MODEL", "openai/env-agentic", raising=False)
    monkeypatch.setattr(co.settings, "NEO4J_ENABLED", False, raising=False)
    monkeypatch.setattr(
        wqe.get_settings(),
        "LIVE_WEB_QUERY_EXPANSION_TIMEOUT_SECONDS",
        4.0,
        raising=False,
    )

    orchestrator = ChatOrchestrator()
    conversation_id = ObjectId()
    steps: list[str] = []
    route_resolutions: list[tuple[str, str]] = []
    utility_resolutions: list[tuple[str, str]] = []
    captured: dict = {}
    saved_assistant: dict = {}

    existing_messages = [
        ChatMessage(role="user", content="Oldest unrelated corpus question."),
        ChatMessage(role="assistant", content="Assistant text must not enter query rewrite."),
        ChatMessage(role="user", content="Prior context one: inspect SearXNG YAML and Obscura."),
        ChatMessage(role="assistant", content="Assistant context is ignored for utility rewrite."),
        ChatMessage(role="user", content="Prior context two: GLM should enrich the web query."),
    ]
    request = ChatRequest(
        message=(
            "With Web enabled, verify Polymath web retrieval uses the utility "
            "model and final seven sources."
        ),
        corpus_ids=["corpus-a"],
        overrides=ModelOverrides(
            model="openai/base-query",
            web_search_enabled=True,
        ),
    )

    async def fake_load_or_create(_request):
        return conversation_id, ModelConfig(model="openai/base-query"), existing_messages

    async def fake_orchestrator_resolve(user_id, kind):
        route_resolutions.append((user_id, kind))
        if kind == "agentic":
            return {
                "model": "deepseek/deepseek-v4-flash",
                "api_base": None,
                "api_key": None,
                "extra_params": {},
            }
        return None

    async def fake_utility_resolve(user_id, kind):
        utility_resolutions.append((user_id, kind))
        assert kind == "utility"
        return {
            "model": "openai/glm-5-turbo",
            "api_base": "https://api.z.ai/api/coding/paas/v4",
            "api_key": "test-key",
            "extra_params": {},
        }

    async def fake_apply_hyde(_request, user_id=None, hyde_explicit=False):
        return _request.message, False

    async def fake_retrieve(**kwargs):
        steps.append("local_rag")
        captured["rag_kwargs"] = kwargs
        return RetrievalResult(
            chunks=[_local_source("local-1"), _local_source("local-1")],
            facts=[],
            requested_tier=RetrievalTier.qdrant_mongo,
            effective_tier=RetrievalTier.qdrant_mongo,
        )

    async def fake_utility_complete(messages, **kwargs):
        steps.append("utility_glm")
        captured["utility_messages"] = messages
        captured["utility_kwargs"] = kwargs
        return "Polymath web retrieval utility model final seven sources Obscura SearXNG"

    async def fake_search_searxng_pool(query, *, max_results=None, time_range=None):
        steps.append("searxng")
        captured["searxng"] = {
            "query": query,
            "max_results": max_results,
            "time_range": time_range,
        }
        return [_hit(i) for i in range(1, 11)]

    async def fake_get_recent_web_source_urls(_conversation_id):
        captured["prior_url_lookup"] = _conversation_id
        return {"https://previous.example.test/already-seen"}

    async def fake_fetch_pages_for_search(
        *,
        search_query,
        hits,
        max_results,
        prior_web_urls=None,
    ):
        steps.append("fetch_obscura")
        captured["fetch"] = {
            "search_query": search_query,
            "hit_count": len(hits),
            "max_results": max_results,
            "prior_web_urls": set(prior_web_urls or set()),
        }
        selected = hits[:4]
        fetched = {
            hit.url: f"Fetched page text for {hit.title} about GLM web retrieval."
            for hit in selected
        }
        fetch_stats = [
            {
                "url": selected[0].url,
                "status": "ok",
                "method": "httpx",
                "chars": 240,
            },
            {
                "url": selected[1].url,
                "status": "ok",
                "method": "httpx",
                "chars": 260,
            },
            {
                "url": selected[2].url,
                "status": "ok",
                "method": "obscura",
                "chars": 280,
                "obscura_attempted": True,
                "js_rendered": True,
            },
            {
                "url": selected[3].url,
                "status": "ok",
                "method": "httpx",
                "chars": 220,
            },
        ]
        telemetry = {
            "selected_full_page_urls": [hit.url for hit in selected],
            "snippet_only": False,
            "redis_search_cache_hit": False,
            "redis_page_cache_hit": False,
            "obscura_attempt_rate": 0.25,
            "obscura_success_rate": 1.0,
        }
        return fetched, fetch_stats, selected, telemetry

    async def fake_rerank_web_source_chunks(query, chunks, *, limit):
        steps.append("rerank_web")
        captured["rerank"] = {
            "query": query,
            "candidate_count": len(chunks),
            "limit": limit,
        }
        ranked = sorted(chunks, key=lambda chunk: chunk.score, reverse=True)
        return ranked[:limit]

    async def fake_trim(messages, model):
        return messages, False, "", 100, 4096

    async def fake_get_tools_by_ids(_tool_ids):
        return []

    async def fake_append_message(_conversation_id, _message):
        return True

    def fake_build_augmented_prompt(**kwargs):
        steps.append("augment_prompt_with_rag_and_web")
        captured["prompt_sources"] = kwargs["sources"]
        captured["prompt_query"] = kwargs["query"]
        return f"AUGMENTED PROMPT\n{kwargs['query']}"

    async def fake_stream_chat(*, messages, model, overrides, tools=None, **kwargs):
        steps.append("final_deepseek")
        captured["final_stream"] = {
            "messages": messages,
            "model": model,
            "tools": tools,
            "overrides_model": overrides.model if overrides else None,
        }
        yield {"content": "Final answer using local RAG plus seven web sources."}

    async def fake_save_assistant_message(
        _conversation_id,
        content,
        thinking,
        model,
        trimming_applied,
        **kwargs,
    ):
        saved_assistant.update(
            {
                "content": content,
                "thinking": thinking,
                "model": model,
                **kwargs,
            }
        )
        return ChatMessage(role="assistant", content=content, model_used=model)

    monkeypatch.setattr(orchestrator, "_load_or_create_conversation", fake_load_or_create)
    monkeypatch.setattr(orchestrator, "_resolve_reasoning", lambda _request: (None, None))
    monkeypatch.setattr(orchestrator, "_resolve_query_profile", _fast_query_profile)
    monkeypatch.setattr(orchestrator, "_apply_hyde", fake_apply_hyde)
    monkeypatch.setattr(orchestrator, "_trim_history", fake_trim)
    monkeypatch.setattr(orchestrator, "_save_assistant_message", fake_save_assistant_message)
    monkeypatch.setattr(co, "resolve_query_model_kind", fake_orchestrator_resolve)
    monkeypatch.setattr(wqe, "resolve_query_model_kind", fake_utility_resolve)
    monkeypatch.setattr(wqe.llm_service, "complete_sync", fake_utility_complete)
    monkeypatch.setattr(wf.live_web_search, "_search_searxng_pool", fake_search_searxng_pool)
    monkeypatch.setattr(wf.live_web_search, "_fetch_pages_for_search", fake_fetch_pages_for_search)
    monkeypatch.setattr(wf, "rerank_web_source_chunks", fake_rerank_web_source_chunks)
    monkeypatch.setattr(co.retriever_orchestrator, "retrieve", fake_retrieve)
    monkeypatch.setattr(co.conversation_service, "append_message", fake_append_message)
    monkeypatch.setattr(
        co.conversation_service,
        "get_recent_web_source_urls",
        fake_get_recent_web_source_urls,
    )
    monkeypatch.setattr(co.tool_registry, "get_tools_by_ids", fake_get_tools_by_ids)
    monkeypatch.setattr(co.context_manager, "build_augmented_prompt", fake_build_augmented_prompt)
    monkeypatch.setattr(co.llm_service, "stream_chat", fake_stream_chat)

    events = [
        _parse_sse(frame)
        async for frame in orchestrator.process_chat_request(request, user_id="user-1")
    ]

    assert steps == [
        "local_rag",
        "utility_glm",
        "searxng",
        "fetch_obscura",
        "rerank_web",
        "augment_prompt_with_rag_and_web",
        "final_deepseek",
    ]
    assert route_resolutions == [("user-1", "agentic")]
    assert utility_resolutions == [("user-1", "utility")]
    assert request.overrides.model == "deepseek/deepseek-v4-flash"
    assert captured["rag_kwargs"]["ranking_query"] == request.message

    utility_prompt = captured["utility_messages"][1]["content"]
    assert "Current user request:" in utility_prompt
    assert request.message in utility_prompt
    assert "previous_user: Prior context one: inspect SearXNG YAML and Obscura." in utility_prompt
    assert "previous_user: Prior context two: GLM should enrich the web query." in utility_prompt
    assert "Oldest unrelated corpus question" not in utility_prompt
    assert "Assistant text must not enter query rewrite" not in utility_prompt
    assert captured["utility_kwargs"]["model"] == "openai/glm-5-turbo"
    assert captured["utility_kwargs"]["api_base"] == "https://api.z.ai/api/coding/paas/v4"
    assert captured["utility_kwargs"]["temperature"] == 0
    assert captured["utility_kwargs"]["max_tokens"] == 48

    assert captured["searxng"]["query"] == (
        "Polymath web retrieval utility model final seven sources Obscura SearXNG"
    )
    assert captured["searxng"]["max_results"] == 12
    assert captured["fetch"]["max_results"] == 7
    assert captured["fetch"]["hit_count"] == 10
    assert captured["rerank"] == {
        "query": captured["searxng"]["query"],
        "candidate_count": 10,
        "limit": 7,
    }

    event_types = [event["type"] for event in events]
    assert event_types.index("tool_call_start") < event_types.index("tool_result")
    assert event_types.index("tool_result") < event_types.index("sources")
    assert event_types.index("sources") < event_types.index("budget")
    assert event_types.index("budget") < event_types.index("token")
    assert event_types[-1] == "done"

    tool_event = events[event_types.index("tool_result")]
    tool_payload = json.loads(tool_event["content"])[0]
    web_result = json.loads(tool_payload["result"])
    pipeline = web_result["pipeline"]
    assert web_result["query"] == captured["searxng"]["query"]
    assert len(web_result["results"]) == 7
    assert pipeline["final_result_limit"] == 7
    assert pipeline["final_reranked_results"] == 7
    assert pipeline["candidate_limit_requested"] == 12
    assert pipeline["candidate_results"] == 10
    assert pipeline["js_render"]["configured"] is True
    assert pipeline["js_render"]["attempted"] is True
    assert pipeline["js_render"]["rendered"] is True
    assert pipeline["utility_query_enrichment"]["attempted"] is True
    assert pipeline["utility_query_enrichment"]["applied"] is True
    assert pipeline["utility_query_enrichment"]["model"] == "openai/glm-5-turbo"
    assert pipeline["utility_query_enrichment"]["history_user_messages_used"] == 2

    sources_event = events[event_types.index("sources")]
    assert len(sources_event["sources"]) == 8
    assert [source["chunk_id"] for source in sources_event["sources"][:1]] == ["local-1"]
    assert sum(source["corpus_id"] == "live-web" for source in sources_event["sources"]) == 7
    assert len(captured["prompt_sources"]) == 8

    final_stream = captured["final_stream"]
    assert final_stream["model"] == "deepseek/deepseek-v4-flash"
    assert final_stream["overrides_model"] == "deepseek/deepseek-v4-flash"
    assert final_stream["tools"] is None
    assistant_tool_message = next(
        msg for msg in final_stream["messages"] if msg.get("tool_calls")
    )
    tool_result_message = next(
        msg for msg in final_stream["messages"] if msg.get("role") == "tool"
    )
    assert assistant_tool_message["tool_calls"][0]["function"]["name"] == "web_search"
    assert tool_result_message["tool_call_id"] == "server_web_search_1"

    done = events[-1]
    assert done["model_used"] == "deepseek/deepseek-v4-flash"
    assert done["agentic_mode_used"] is True
    assert done["tools_used"] == ["web_search"]
    assert saved_assistant["chunks_returned"] == 8
    assert saved_assistant["sources"] == captured["prompt_sources"]


@pytest.mark.asyncio
async def test_web_toggle_off_is_pure_rag_and_does_not_call_web_or_utility(
    monkeypatch,
):
    monkeypatch.setattr(co.settings, "LIVE_WEB_SEARCH_ENABLED", True, raising=False)
    monkeypatch.setattr(co.settings, "AGENTIC_MODE_ENABLED", False, raising=False)
    monkeypatch.setattr(co.settings, "NEO4J_ENABLED", False, raising=False)

    orchestrator = ChatOrchestrator()
    conversation_id = ObjectId()
    steps: list[str] = []
    captured: dict = {}

    request = ChatRequest(
        message="With Web off, answer from local RAG only.",
        corpus_ids=["corpus-a"],
        overrides=ModelOverrides(
            model="openai/base-query",
            web_search_enabled=False,
        ),
    )

    async def fake_load_or_create(_request):
        return conversation_id, ModelConfig(model="openai/base-query"), []

    async def fake_resolve_query_model_kind(*_args, **_kwargs):
        raise AssertionError("Web-off explicit model route should not resolve agentic/utility")

    async def fake_apply_hyde(_request, user_id=None, hyde_explicit=False):
        return _request.message, False

    async def fake_retrieve(**kwargs):
        steps.append("local_rag")
        captured["rag_kwargs"] = kwargs
        return RetrievalResult(
            chunks=[_local_source("local-1")],
            facts=[],
            requested_tier=RetrievalTier.qdrant_mongo,
            effective_tier=RetrievalTier.qdrant_mongo,
        )

    async def fail_web_search(*_args, **_kwargs):
        raise AssertionError("web_search must not run when the toggle is off")

    async def fake_trim(messages, model):
        return messages, False, "", 40, 4096

    async def fake_append_message(_conversation_id, _message):
        return True

    async def fake_get_tools_by_ids(_tool_ids):
        return []

    def fake_build_augmented_prompt(**kwargs):
        steps.append("augment_prompt_with_rag_only")
        captured["prompt_sources"] = kwargs["sources"]
        return kwargs["query"]

    async def fake_stream_chat(*, messages, model, overrides, tools=None, **kwargs):
        steps.append("final_query_model")
        captured["final_stream"] = {
            "model": model,
            "tools": tools,
            "messages": messages,
        }
        yield {"content": "Final answer from local RAG only."}

    async def fake_save_assistant_message(
        _conversation_id,
        content,
        thinking,
        model,
        trimming_applied,
        **kwargs,
    ):
        captured["saved"] = {
            "content": content,
            "model": model,
            **kwargs,
        }
        return ChatMessage(role="assistant", content=content, model_used=model)

    monkeypatch.setattr(orchestrator, "_load_or_create_conversation", fake_load_or_create)
    monkeypatch.setattr(orchestrator, "_resolve_reasoning", lambda _request: (None, None))
    monkeypatch.setattr(orchestrator, "_resolve_query_profile", _fast_query_profile)
    monkeypatch.setattr(orchestrator, "_apply_hyde", fake_apply_hyde)
    monkeypatch.setattr(orchestrator, "_execute_web_search_tool", fail_web_search)
    monkeypatch.setattr(orchestrator, "_trim_history", fake_trim)
    monkeypatch.setattr(orchestrator, "_save_assistant_message", fake_save_assistant_message)
    monkeypatch.setattr(co, "resolve_query_model_kind", fake_resolve_query_model_kind)
    monkeypatch.setattr(wqe, "resolve_query_model_kind", fake_resolve_query_model_kind)
    monkeypatch.setattr(co.retriever_orchestrator, "retrieve", fake_retrieve)
    monkeypatch.setattr(co.conversation_service, "append_message", fake_append_message)
    monkeypatch.setattr(co.tool_registry, "get_tools_by_ids", fake_get_tools_by_ids)
    monkeypatch.setattr(co.context_manager, "build_augmented_prompt", fake_build_augmented_prompt)
    monkeypatch.setattr(co.llm_service, "stream_chat", fake_stream_chat)

    events = [
        _parse_sse(frame)
        async for frame in orchestrator.process_chat_request(request, user_id="user-1")
    ]

    assert steps == [
        "local_rag",
        "augment_prompt_with_rag_only",
        "final_query_model",
    ]
    event_types = [event["type"] for event in events]
    assert "tool_call_start" not in event_types
    assert "tool_result" not in event_types
    assert "sources" in event_types
    assert captured["final_stream"]["model"] == "openai/base-query"
    assert captured["final_stream"]["tools"] is None
    assert len(captured["prompt_sources"]) == 1
    done = events[-1]
    assert done["agentic_mode_used"] is False
    assert done["tools_used"] == []
    assert captured["saved"]["chunks_returned"] == 1
