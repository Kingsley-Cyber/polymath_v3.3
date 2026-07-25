from __future__ import annotations

import copy
from types import SimpleNamespace

import httpx
import pytest

from services.ingestion import summary_tree_llm


@pytest.mark.asyncio
async def test_summary_tree_llm_wraps_prompt_only_provider(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 9},
                "choices": [{"message": {"content": '{"summary":"ok"}'}}],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            calls.append(copy.deepcopy(json))
            return FakeResponse()

    monkeypatch.setattr(summary_tree_llm, "get_settings", lambda: SimpleNamespace(
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
    ))
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    llm = summary_tree_llm.summary_tree_llm_from_pool(
        [
            {
                "provider_preset": "longcat",
                "model": "LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": "test-key",
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        max_tokens=300,
    )

    assert llm is not None
    assert await llm("Return semantic summary JSON.") == '{"summary":"ok"}'
    assert len(calls) == 1
    prompt = calls[0]["messages"][0]["content"]
    assert '<schema_control contract="summary_tree_semantic_heal.v1"' in prompt
    assert "<json_payload>" in prompt
    assert "response_format" not in calls[0]
    assert calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_summary_tree_llm_uses_native_json_object_provider(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 9},
                "choices": [{"message": {"content": '{"summary":"ok"}'}}],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            calls.append(copy.deepcopy(json))
            return FakeResponse()

    monkeypatch.setattr(summary_tree_llm, "get_settings", lambda: SimpleNamespace(
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
    ))
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    llm = summary_tree_llm.summary_tree_llm_from_pool(
        [
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "test-key",
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        max_tokens=300,
    )

    assert llm is not None
    assert await llm("Return semantic summary JSON.") == '{"summary":"ok"}'
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "<schema_control" not in calls[0]["messages"][0]["content"]
    assert calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_summary_tree_llm_forces_siliconflow_thinking_off(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 9},
                "choices": [{"message": {"content": '{"summary":"ok"}'}}],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            calls.append(copy.deepcopy(json))
            return FakeResponse()

    monkeypatch.setattr(summary_tree_llm, "get_settings", lambda: SimpleNamespace(
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
    ))
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    llm = summary_tree_llm.summary_tree_llm_from_pool(
        [
            {
                "provider_preset": "siliconflow",
                "model": "openai/tencent/Hy3-preview",
                "base_url": "https://api.siliconflow.com/v1",
                "api_key": "test-key",
                "max_concurrent": 1,
                "extra_params": {
                    "enable_thinking": True,
                    "reasoning_effort": "high",
                },
            }
        ],
        max_tokens=300,
    )

    assert llm is not None
    assert await llm("Return semantic summary JSON.") == '{"summary":"ok"}'
    assert len(calls) == 1
    assert calls[0]["enable_thinking"] is False
    assert "thinking" not in calls[0]
    assert "reasoning_effort" not in calls[0]
