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
from services.chat_orchestrator import (  # noqa: E402
    ChatOrchestrator,
    _is_web_search_enabled_for_request,
)


def _parse_sse(frame: str) -> dict:
    assert frame.startswith("data: ")
    return json.loads(frame.removeprefix("data: ").strip())


@pytest.mark.asyncio
async def test_web_toggle_routes_to_agentic_model_and_runs_deterministic_web_tool_prelude(
    monkeypatch,
):
    monkeypatch.setattr(co.settings, "LIVE_WEB_SEARCH_ENABLED", True, raising=False)
    monkeypatch.setattr(co.settings, "AGENTIC_MODE_ENABLED", False, raising=False)
    monkeypatch.setattr(co.settings, "DEFAULT_COMPLETION_MODEL", "openai/base-query", raising=False)
    monkeypatch.setattr(co.settings, "AGENTIC_MODEL", "openai/env-agentic", raising=False)
    monkeypatch.setattr(co.settings, "NEO4J_ENABLED", False, raising=False)

    orchestrator = ChatOrchestrator()
    conversation_id = ObjectId()
    resolver_kinds: list[str] = []
    stream_calls: list[dict] = []
    saved_assistant: dict = {}
    web_args: list[dict] = []

    request = ChatRequest(
        message="What changed in OpenAI model routing today?",
        corpus_ids=[],
        overrides=ModelOverrides(
            model="openai/base-query",
            web_search_enabled=True,
        ),
    )

    async def fake_load_or_create(_request):
        return conversation_id, ModelConfig(model="openai/base-query"), []

    async def fake_resolve_query_model_kind(user_id, kind):
        resolver_kinds.append(kind)
        if kind == "agentic":
            return {
                "model": "openai/agentic-tools",
                "api_base": None,
                "api_key": None,
                "extra_params": {},
            }
        return None

    async def fake_query_profile(_request, user_id=None):
        return {
            "retrieval_k": 0,
            "rerank_enabled": False,
            "query_profile": "fast",
            "hyde_explicit": False,
            "top_k_summary": 0,
            "rerank_top_n": 0,
            "similarity_threshold": 0.0,
            "neo4j_expansion_cap": 0,
            "max_corpora_per_query": 1,
            "final_top_k": 1,
            "fact_seed_limit": 0,
        }

    async def fake_apply_hyde(_request, user_id=None, hyde_explicit=False):
        return _request.message, False

    async def fake_retrieve(**_kwargs):
        return RetrievalResult(
            chunks=[],
            facts=[],
            requested_tier=RetrievalTier.qdrant_mongo,
            effective_tier=RetrievalTier.qdrant_mongo,
        )

    async def fake_trim(messages, model):
        return messages, False, "", 42, 4096

    async def fake_get_tools_by_ids(_tool_ids):
        return []

    async def fake_append_message(_conversation_id, _message):
        return True

    async def fake_execute_web_search_tool(args, request=None):
        web_args.append(args)
        object.__setattr__(
            request,
            "_pending_tool_sources",
            [
                SourceChunk(
                    chunk_id="web-1",
                    parent_id="web-1",
                    doc_id="https://example.test",
                    corpus_id="live-web",
                    text="OpenAI update source",
                    score=0.9,
                    source_tier="web_search",
                    metadata={"url": "https://example.test"},
                )
            ],
        )
        return json.dumps(
            {
                "query": args["query"],
                "results": [{"title": "OpenAI update", "url": "https://example.test"}],
            }
        )

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

    async def fake_stream_chat(*, messages, model, overrides, tools=None, **kwargs):
        stream_calls.append(
            {
                "messages": messages,
                "model": model,
                "tools": tools,
            }
        )
        yield {"content": "Final answer after checking the live web."}

    monkeypatch.setattr(orchestrator, "_load_or_create_conversation", fake_load_or_create)
    monkeypatch.setattr(orchestrator, "_resolve_reasoning", lambda _request: (None, None))
    monkeypatch.setattr(orchestrator, "_resolve_query_profile", fake_query_profile)
    monkeypatch.setattr(orchestrator, "_apply_hyde", fake_apply_hyde)
    monkeypatch.setattr(orchestrator, "_trim_history", fake_trim)
    monkeypatch.setattr(orchestrator, "_execute_web_search_tool", fake_execute_web_search_tool)
    monkeypatch.setattr(orchestrator, "_save_assistant_message", fake_save_assistant_message)
    monkeypatch.setattr(co, "resolve_query_model_kind", fake_resolve_query_model_kind)
    monkeypatch.setattr(co.retriever_orchestrator, "retrieve", fake_retrieve)
    monkeypatch.setattr(co.tool_registry, "get_tools_by_ids", fake_get_tools_by_ids)
    monkeypatch.setattr(co.conversation_service, "append_message", fake_append_message)
    monkeypatch.setattr(
        co.context_manager,
        "build_augmented_prompt",
        lambda **kwargs: kwargs["query"],
    )
    monkeypatch.setattr(co.llm_service, "stream_chat", fake_stream_chat)

    events = [
        _parse_sse(frame)
        async for frame in orchestrator.process_chat_request(request, user_id="user-1")
    ]

    assert _is_web_search_enabled_for_request(request) is True
    assert resolver_kinds == ["agentic"]
    assert request.overrides.model == "openai/agentic-tools"
    assert stream_calls[0]["model"] == "openai/agentic-tools"
    assert stream_calls[0]["tools"] is None

    continuation_messages = stream_calls[0]["messages"]
    assistant_tool_message = next(
        msg for msg in continuation_messages if msg.get("role") == "assistant"
    )
    tool_result_message = next(
        msg for msg in continuation_messages if msg.get("role") == "tool"
    )
    assert assistant_tool_message["tool_calls"][0]["function"]["name"] == "web_search"
    assert tool_result_message["tool_call_id"] == "server_web_search_1"
    assert web_args == [
        {"query": "What changed in OpenAI model routing today?", "max_results": 7}
    ]

    event_types = [event["type"] for event in events]
    assert "tool_call_start" in event_types
    assert "tool_result" in event_types
    assert "sources" in event_types
    assert "token" in event_types
    done = events[-1]
    assert done["type"] == "done"
    assert done["model_used"] == "openai/agentic-tools"
    assert done["agentic_mode_used"] is True
    assert done["tools_used"] == ["web_search"]
    assert saved_assistant["content"] == "Final answer after checking the live web."
