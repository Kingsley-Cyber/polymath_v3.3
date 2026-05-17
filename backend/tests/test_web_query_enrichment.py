import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from services import web_query_enrichment as wqe  # noqa: E402


@pytest.mark.asyncio
async def test_utility_enrichment_uses_recent_chat_and_preserves_query_anchors(
    monkeypatch,
):
    captured: dict = {}

    async def fake_resolve(user_id, kind):
        captured["resolved"] = (user_id, kind)
        return {
            "model": "openai/glm-5-turbo",
            "api_base": "https://api.z.ai/api/coding/paas/v4",
            "api_key": "test-key",
            "extra_params": {},
        }

    async def fake_complete_sync(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "Roblox RemoteEvent server validation security best practices"

    monkeypatch.setattr(wqe, "resolve_query_model_kind", fake_resolve)
    monkeypatch.setattr(wqe.llm_service, "complete_sync", fake_complete_sync)

    result = await wqe.enrich_web_search_query(
        tool_query="Roblox RemoteEvent validation RAG",
        original_query=(
            "With Web enabled, run one live web search for Roblox RemoteEvent "
            "security server client validation. Use local RAG context plus the "
            "web results."
        ),
        user_id="user-1",
        recent_messages=[
            SimpleNamespace(role="user", content="We are reviewing Roblox security."),
            SimpleNamespace(role="assistant", content="Server authority matters."),
        ],
    )

    assert captured["resolved"] == ("user-1", "utility")
    assert captured["kwargs"]["model"] == "openai/glm-5-turbo"
    assert captured["kwargs"]["temperature"] == 0
    assert captured["kwargs"]["max_tokens"] == 48
    assert "We are reviewing Roblox security" in captured["messages"][1]["content"]
    assert result.attempted is True
    assert result.applied is True
    assert result.model == "openai/glm-5-turbo"
    assert "Roblox RemoteEvent" in result.query
    assert "RAG" not in result.query


@pytest.mark.asyncio
async def test_utility_enrichment_falls_back_when_utility_is_not_configured(
    monkeypatch,
):
    async def fake_resolve(_user_id, _kind):
        return None

    async def fail_complete_sync(*_args, **_kwargs):
        raise AssertionError("Utility LLM should not be called")

    monkeypatch.setattr(wqe, "resolve_query_model_kind", fake_resolve)
    monkeypatch.setattr(wqe.llm_service, "complete_sync", fail_complete_sync)

    result = await wqe.enrich_web_search_query(
        tool_query="RemoteEvent",
        original_query="Search for Roblox RemoteEvent security server validation.",
        user_id="user-1",
        recent_messages=[],
    )

    assert result.query == "Roblox RemoteEvent security server validation"
    assert result.attempted is False
    assert result.applied is False
    assert result.fallback_reason == "utility_not_configured"


@pytest.mark.asyncio
async def test_utility_enrichment_rejects_low_overlap_or_tool_syntax(monkeypatch):
    async def fake_resolve(_user_id, _kind):
        return {
            "model": "openai/glm-5-turbo",
            "api_base": None,
            "api_key": None,
            "extra_params": {},
        }

    async def fake_complete_sync(*_args, **_kwargs):
        return '<tool_calls>{"name":"web_search","arguments":{"query":"jobs"}}</tool_calls>'

    monkeypatch.setattr(wqe, "resolve_query_model_kind", fake_resolve)
    monkeypatch.setattr(wqe.llm_service, "complete_sync", fake_complete_sync)

    result = await wqe.enrich_web_search_query(
        tool_query="Roblox RemoteEvent security server client validation",
        original_query="Search for Roblox RemoteEvent security server client validation.",
        user_id="user-1",
        recent_messages=[],
    )

    assert result.query == "Roblox RemoteEvent security server client validation"
    assert result.attempted is True
    assert result.applied is False
    assert result.fallback_reason == "unsafe_or_low_overlap_output"
