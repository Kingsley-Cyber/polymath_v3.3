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
from services.web_freshness import _PageFetchResult, WebSearchHit  # noqa: E402


def _collapse_rag_steps(steps: list[str]) -> list[str]:
    """Collapse consecutive 'local_rag' repeats into one.

    The support-pass machinery (coverage + evidence-plan gap-fills, source-
    allocation branch) legitimately calls retrieve() several times per turn;
    these tests guard the PIPELINE ORDER and the absence of web/utility
    calls, not the retrieval count.
    """
    collapsed: list[str] = []
    for step in steps:
        if step == "local_rag" and collapsed and collapsed[-1] == "local_rag":
            continue
        collapsed.append(step)
    return collapsed



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
async def test_web_toggle_on_runs_agentic_rag_web_loop_to_rerank_pipeline(
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
            "With Web enabled, verify Polymath web retrieval uses Obscura, "
            "SearXNG, reranking, and final seven sources."
        ),
        corpus_ids=["corpus-a"],
        overrides=ModelOverrides(
            model="deepseek/deepseek-v4-flash",
            web_search_enabled=True,
        ),
    )

    async def fake_load_or_create(_request):
        return conversation_id, ModelConfig(model="deepseek/deepseek-v4-flash"), existing_messages

    async def fake_orchestrator_resolve(user_id, kind):
        raise AssertionError("Web toggle should not resolve a planner model")

    async def fake_utility_resolve(*_args, **_kwargs):
        raise AssertionError("Mandatory web prelude should not use Utility rewrite")

    async def fake_apply_hyde(_request, user_id=None, hyde_explicit=False, **_kwargs):
        return _request.message, False

    async def fake_retrieve(**kwargs):
        steps.append("local_rag")
        captured["rag_kwargs"] = kwargs
        # The final answerability chunk gate (348b7a6) drops chunks whose
        # text covers none of the query concepts; cover them so the local
        # chunk survives into the combined sources event.
        chunk = _local_source("local-1")
        chunk.text = (
            "Polymath web retrieval verification: Obscura fetch, SearXNG "
            "search, reranking, and the final seven sources pipeline."
        )
        return RetrievalResult(
            chunks=[chunk, chunk],
            facts=[],
            requested_tier=RetrievalTier.qdrant_mongo,
            effective_tier=RetrievalTier.qdrant_mongo,
        )

    async def fake_search_live_web_pool(query, *, max_results=None, time_range=None):
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
        fetch_depth="normal",
        youtube_transcripts_enabled=True,
        max_fetch_pages=None,
    ):
        steps.append("fetch_obscura")
        captured["fetch"] = {
            "search_query": search_query,
            "hit_count": len(hits),
            "max_results": max_results,
            "prior_web_urls": set(prior_web_urls or set()),
            "fetch_depth": fetch_depth,
            "youtube_transcripts_enabled": youtube_transcripts_enabled,
            "max_fetch_pages": max_fetch_pages,
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
            "snippet_sufficiency_score": 0.31,
            "snippet_sufficiency_reason": "insufficient_query_coverage",
            "snippet_sufficiency": {
                "useful_snippet_chars": 860,
                "top3_snippet_chars": 520,
                "useful_snippet_count": 4,
                "distinct_domains": 3,
                "query_coverage": 0.38,
                "stronger_evidence_required": True,
            },
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

    stream_invocations: list[dict] = []

    async def fake_stream_chat(*, messages, model, overrides, tools=None, **kwargs):
        invocation = {
            "messages": messages,
            "model": model,
            "tools": tools,
            "tool_choice": kwargs.get("tool_choice"),
            "overrides_model": overrides.model if overrides else None,
        }
        stream_invocations.append(invocation)
        steps.append("deepseek_tool_loop" if len(stream_invocations) == 1 else "final_deepseek")
        if len(stream_invocations) == 1:
            captured["first_stream"] = invocation
        else:
            captured["final_stream"] = invocation
        if len(stream_invocations) == 1:
            assert tools is not None
            yield {
                "thinking": (
                    "RAG context may not be enough for current web guidance; "
                    "I need official/live evidence before answering."
                )
            }
            yield {
                "tool_calls": [
                    {
                        "id": "call_web_1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps(
                                {
                                    "query": (
                                        "Polymath web retrieval Obscura SearXNG "
                                        "reranking final seven sources"
                                    ),
                                    "max_results": 7,
                                }
                            ),
                        },
                    }
                ]
            }
            return
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
    monkeypatch.setattr(wf.live_web_search, "_search_live_web_pool", fake_search_live_web_pool)
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

    assert _collapse_rag_steps(steps) == [
        "local_rag",
        "augment_prompt_with_rag_and_web",
        "deepseek_tool_loop",
        "searxng",
        "fetch_obscura",
        "rerank_web",
        "final_deepseek",
    ]
    assert utility_resolutions == []
    assert request.overrides.model == "deepseek/deepseek-v4-flash"
    # ranking_query is now prefixed with corpus-alias lexical anchors
    # ("polymath polymaths ..."); the guard is that the user's message still
    # drives ranking, not an exact-equality snapshot.
    assert request.message in captured["rag_kwargs"]["ranking_query"]

    assert captured["searxng"]["query"] == (
        "Polymath web retrieval Obscura SearXNG reranking final seven sources"
    )
    assert captured["searxng"]["max_results"] == 12
    assert captured["fetch"]["max_results"] == 7
    assert captured["fetch"]["hit_count"] == 10
    assert captured["fetch"]["fetch_depth"] == "normal"
    assert captured["fetch"]["youtube_transcripts_enabled"] is True
    assert captured["fetch"]["max_fetch_pages"] == 4
    assert captured["rerank"] == {
        "query": captured["searxng"]["query"],
        "candidate_count": 10,
        "limit": 7,
    }

    event_types = [event["type"] for event in events]
    trace_events = [
        event["trace_event"]
        for event in events
        if event["type"] == "trace_event"
    ]
    trace_titles = [event["title"] for event in trace_events]
    # Budget telemetry is now emitted earlier in the stream; the guard is
    # that sources reach the client before the turn completes.
    assert event_types.index("sources") < event_types.index("done")
    assert event_types.index("budget") < event_types.index("tool_call_start")
    assert event_types.index("tool_call_start") < event_types.index("tool_result")
    assert event_types.index("tool_result") < event_types.index("token")
    assert event_types[-1] == "done"
    assert "Local RAG retrieval" in trace_titles
    assert "Agentic web loop ready" in trace_titles
    assert "Deterministic web query builder" not in trace_titles
    assert "Web planner tool-call model" not in trace_titles
    assert "Native tool call" in trace_titles
    assert "Web retrieval decision trace" in trace_titles
    assert "Utility web query helper" not in trace_titles
    assert "Native tool result" in trace_titles
    assert "Chat model stream" in trace_titles

    ready_trace_event = next(
        event
        for event in trace_events
        if event["title"] == "Agentic web loop ready" and event["status"] == "done"
    )
    assert ready_trace_event["metadata"]["web_search_required_before_final"] is True
    assert ready_trace_event["metadata"]["max_web_search_calls"] == 3

    decision_trace_event = next(
        event
        for event in trace_events
        if event["title"] == "Web retrieval decision trace" and event["status"] == "done"
    )
    decision_trace = decision_trace_event["content"]
    assert "[Web retrieval decision trace]" in decision_trace
    assert "snippet_decision: fetch_pages_or_enrich_snippets" in decision_trace
    assert "score=0.31" in decision_trace
    assert "reason=insufficient_query_coverage" in decision_trace
    assert "page_fetch: attempts=4, successes=4" in decision_trace
    assert "obscura: configured=true, attempted=true, rendered=true" in decision_trace
    assert "reranker:" in decision_trace
    assert decision_trace_event["metadata"]["raw_chain_of_thought"] is False

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
    assert pipeline["fetch_depth"] == "normal"
    assert pipeline["research_mode"] is False
    assert pipeline["youtube_transcripts_enabled"] is True
    assert pipeline["search_health"] == "ok"
    assert pipeline["js_render"]["configured"] is True
    assert pipeline["js_render"]["attempted"] is True
    assert pipeline["js_render"]["rendered"] is True
    assert pipeline["utility_query_enrichment"]["attempted"] is False
    assert pipeline["utility_query_enrichment"]["applied"] is False
    assert pipeline["utility_query_enrichment"]["model"] is None
    assert pipeline["utility_query_enrichment"]["fallback_reason"] == (
        "native_web_planner_query_used"
    )
    assert pipeline["web_query_planner"] is None
    assert pipeline["web_query_builder"] is None

    sources_events = [event for event in events if event["type"] == "sources"]
    # Previously an interim local-RAG sources event preceded the final
    # combined one; the stream now emits sources once. The guard is the
    # FINAL event's composition (1 local + 7 web), asserted below.
    assert len(sources_events) >= 1
    sources_event = sources_events[-1]
    assert len(sources_event["sources"]) == 8
    assert [source["chunk_id"] for source in sources_event["sources"][:1]] == ["local-1"]
    assert sum(source["corpus_id"] == "live-web" for source in sources_event["sources"]) == 7
    assert len(captured["prompt_sources"]) == 1

    first_stream = captured["first_stream"]
    assert first_stream["model"] == "deepseek/deepseek-v4-flash"
    assert first_stream["tool_choice"] == {
        "type": "function",
        "function": {"name": "web_search"},
    }
    assert [tool["function"]["name"] for tool in first_stream["tools"]] == [
        "web_search",
    ]

    final_stream = captured["final_stream"]
    assert final_stream["model"] == "deepseek/deepseek-v4-flash"
    assert final_stream["overrides_model"] == "deepseek/deepseek-v4-flash"
    assert final_stream["tools"] is not None
    assert final_stream["tool_choice"] is None
    assistant_tool_message = next(
        msg for msg in final_stream["messages"] if msg.get("tool_calls")
    )
    tool_result_message = next(
        msg for msg in final_stream["messages"] if msg.get("role") == "tool"
    )
    assert assistant_tool_message["tool_calls"][0]["function"]["name"] == "web_search"
    assert "reasoning_content" in assistant_tool_message
    assert tool_result_message["tool_call_id"] == "call_web_1"
    evidence_message = next(
        msg
        for msg in final_stream["messages"]
        if msg.get("role") == "user"
        and "[EVIDENCE PACKET]" in str(msg.get("content") or "")
    )
    assert "Web health: ok" in evidence_message["content"]
    assert "Obscura: rendered" in evidence_message["content"]

    done = events[-1]
    assert done["model_used"] == "deepseek/deepseek-v4-flash"
    assert done["agentic_mode_used"] is True
    assert done["tools_used"] == ["web_search"]
    assert saved_assistant["chunks_returned"] == 8
    assert len(saved_assistant["sources"]) == 8
    assert saved_assistant["thinking"] is None
    assert saved_assistant["trace_events"] == trace_events


@pytest.mark.asyncio
async def test_fetch_page_tool_records_obscura_rendered_page(monkeypatch):
    orchestrator = ChatOrchestrator()
    request = ChatRequest(
        message="Fetch this JS-rendered page.",
        corpus_ids=["corpus-a"],
        overrides=ModelOverrides(web_search_enabled=True),
    )

    async def fake_fetch_one_page_with_stats(url, **_kwargs):
        return _PageFetchResult(
            url=url,
            text="Rendered JS page text with evidence for the answer.",
            method="obscura_js",
            status="ok",
            chars=49,
            obscura_attempted=True,
            js_rendered=True,
        )

    monkeypatch.setattr(
        wf.live_web_search,
        "_fetch_one_page_with_stats",
        fake_fetch_one_page_with_stats,
    )

    result = await orchestrator._execute_fetch_page_tool(
        {
            "url": "https://example-js.test/article",
            "reason": "snippet points to a JS-rendered source",
        },
        request,
    )

    payload = json.loads(result)
    assert payload["method"] == "obscura_js"
    assert payload["obscura_attempted"] is True
    assert payload["obscura_rendered"] is True
    assert payload["web_content_untrusted"] is True
    assert "Rendered JS page text" in payload["content"]

    pending_sources = getattr(request, "_pending_tool_sources")
    assert len(pending_sources) == 1
    assert pending_sources[0].corpus_id == "live-web"
    assert pending_sources[0].metadata["retriever"] == "fetch_page"
    assert pending_sources[0].metadata["js_rendered"] is True


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

    async def fake_apply_hyde(_request, user_id=None, hyde_explicit=False, **_kwargs):
        return _request.message, False

    async def fake_retrieve(**kwargs):
        steps.append("local_rag")
        captured["rag_kwargs"] = kwargs
        # The answerability gate (348a7a6+) text-covers query concepts from
        # chunk text; a generic filler chunk now short-circuits the turn as
        # unanswerable before prompt build. Cover the query's terms.
        chunk = _local_source("local-1")
        chunk.text = (
            "With the web toggle off, the answer comes from local RAG "
            "evidence only, retrieved from the local corpus."
        )
        return RetrievalResult(
            chunks=[chunk],
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

    assert _collapse_rag_steps(steps) == [
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
