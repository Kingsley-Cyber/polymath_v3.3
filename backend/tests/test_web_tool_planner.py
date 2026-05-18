import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from services import web_tool_planner as planner  # noqa: E402


def _web_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    }


@pytest.mark.asyncio
async def test_planner_uses_agentic_model_native_tool_call_and_recent_users(
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

    async def fake_complete_tool_calls(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps(
                            {
                                "query": "Roblox RemoteEvent validation security Luau",
                                "max_results": 99,
                            }
                        ),
                    },
                }
            ],
            "finish_reason": "tool_calls",
        }

    monkeypatch.setattr(planner, "resolve_query_model_kind", fake_resolve)
    monkeypatch.setattr(planner.llm_service, "complete_tool_calls", fake_complete_tool_calls)

    result = await planner.plan_web_search_tool_call(
        current_query=(
            "Use the selected Luau corpus and Web toggle to check current "
            "Roblox RemoteEvent validation security guidance."
        ),
        user_id="user-1",
        recent_messages=[
            SimpleNamespace(role="user", content="Old unrelated topic"),
            SimpleNamespace(role="assistant", content="Assistant content skipped"),
            SimpleNamespace(role="user", content="Prior RemoteEvent exploit concern"),
            SimpleNamespace(role="user", content="Latest Luau server validation concern"),
        ],
        tool_schema=_web_schema(),
        max_results=7,
    )

    assert captured["resolved"] == ("user-1", "agentic")
    assert captured["kwargs"]["model"] == "openai/glm-5-turbo"
    assert captured["kwargs"]["api_base"] == "https://api.z.ai/api/coding/paas/v4"
    assert captured["kwargs"]["tool_choice"]["function"]["name"] == "web_search"
    assert captured["kwargs"]["tools"][0]["function"]["name"] == "web_search"
    assert captured["kwargs"]["overrides"].temperature == 0
    assert captured["kwargs"]["overrides"].thinking_effort == "none"
    prompt = captured["messages"][1]["content"]
    assert "Prior RemoteEvent exploit concern" in prompt
    assert "Latest Luau server validation concern" in prompt
    assert "Assistant content skipped" not in prompt
    assert result.native_tool_call is True
    assert result.args == {
        "query": "Roblox RemoteEvent validation security Luau",
        "max_results": 7,
    }
    assert result.history_user_messages_used == 2


@pytest.mark.asyncio
async def test_planner_does_not_parse_text_when_native_tool_call_missing(
    monkeypatch,
):
    async def fake_resolve(_user_id, _kind):
        return {
            "model": "openai/glm-5-turbo",
            "api_base": None,
            "api_key": None,
            "extra_params": {},
        }

    async def fake_complete_tool_calls(*_args, **_kwargs):
        return {
            "tool_calls": [],
            "content": '{"tool_name":"web_search","tool_args":{"query":"bad raw json"}}',
            "finish_reason": "stop",
        }

    monkeypatch.setattr(planner, "resolve_query_model_kind", fake_resolve)
    monkeypatch.setattr(planner.llm_service, "complete_tool_calls", fake_complete_tool_calls)

    result = await planner.plan_web_search_tool_call(
        current_query="PsychoGAT CIFAR-10 dataset patterns enterprise applications",
        user_id="user-1",
        recent_messages=[],
        tool_schema=_web_schema(),
        max_results=7,
    )

    assert result.native_tool_call is False
    assert result.fallback_reason == "native_tool_call_missing"
    assert result.args["query"] == (
        "PsychoGAT CIFAR-10 dataset patterns enterprise applications"
    )


@pytest.mark.asyncio
async def test_planner_falls_back_when_no_model_configured(monkeypatch):
    async def fake_resolve(_user_id, _kind):
        return None

    async def fail_complete_tool_calls(*_args, **_kwargs):
        raise AssertionError("Planner model should not be called")

    monkeypatch.setattr(planner, "resolve_query_model_kind", fake_resolve)
    monkeypatch.setattr(planner.get_settings(), "AGENTIC_MODEL", "", raising=False)
    monkeypatch.setattr(planner.llm_service, "complete_tool_calls", fail_complete_tool_calls)

    result = await planner.plan_web_search_tool_call(
        current_query="Search selected corpus with web toggle for Roblox RemoteEvent.",
        user_id="user-1",
        recent_messages=[],
        tool_schema=_web_schema(),
        max_results=7,
    )

    assert result.attempted is False
    assert result.native_tool_call is False
    assert result.fallback_reason == "planner_not_configured"
    assert result.args["query"] == "Roblox RemoteEvent"
