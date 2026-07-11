"""Wire-contract tests for model-pool extra parameters."""

from __future__ import annotations

import json

import pytest

from services.llm import LLMService


def test_provider_extra_params_strip_internal_flags_and_disable_thinking() -> None:
    body = {"model": "openai/tencent/Hy3", "messages": [], "stream": True}

    LLMService._merge_provider_extra_params(
        body,
        {
            "disable_thinking": True,
            "routing_policy": "balanced",
            "provider_canary_passed": True,
            "temperature": 0,
        },
    )

    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert "disable_thinking" not in body
    assert "routing_policy" not in body
    assert "provider_canary_passed" not in body


def test_provider_extra_params_preserve_explicit_thinking_and_reserved_fields() -> None:
    messages = [{"role": "user", "content": "original"}]
    body = {
        "model": "openai/tencent/Hy3",
        "messages": messages,
        "stream": True,
    }

    LLMService._merge_provider_extra_params(
        body,
        {
            "disable_thinking": True,
            "thinking": {"type": "enabled"},
            "model": "wrong/model",
            "messages": [],
            "stream": False,
        },
        reserved=frozenset({"model", "messages", "stream"}),
    )

    assert body["thinking"] == {"type": "enabled"}
    assert body["model"] == "openai/tencent/Hy3"
    assert body["messages"] is messages
    assert body["stream"] is True


class _Response:
    status_code = 200

    async def aread(self) -> bytes:
        return b""

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        yield f'data: {json.dumps({"choices": [{"delta": {"content": "ok"}}]})}'
        yield "data: [DONE]"


class _StreamContext:
    async def __aenter__(self) -> _Response:
        return _Response()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _StreamClient:
    def __init__(self) -> None:
        self.body: dict | None = None

    def stream(self, _method, _url, *, json, headers, timeout):  # noqa: A002
        self.body = json
        return _StreamContext()


class _PostResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {"content": "ok", "tool_calls": []},
                    "finish_reason": "stop",
                }
            ]
        }


class _PostClient:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def post(self, _url, *, json, headers, timeout):  # noqa: A002
        self.bodies.append(json)
        return _PostResponse()


@pytest.mark.asyncio
async def test_stream_chat_uses_sanitized_hy3_payload(monkeypatch) -> None:
    client = _StreamClient()
    service = LLMService()

    async def fake_get_client():
        return client

    async def fake_resolve_api_key(_model):
        return None

    monkeypatch.setattr(service, "_get_client", fake_get_client)
    monkeypatch.setattr(service, "_resolve_api_key", fake_resolve_api_key)

    chunks = [
        chunk
        async for chunk in service.stream_chat(
            [{"role": "user", "content": "answer briefly"}],
            model="openai/tencent/Hy3",
            extra_params={
                "disable_thinking": True,
                "routing_policy": "balanced",
                "temperature": 0,
            },
        )
    ]

    assert chunks == [{"content": "ok"}]
    assert client.body is not None
    assert client.body["thinking"] == {"type": "disabled"}
    assert client.body["temperature"] == 0
    assert "disable_thinking" not in client.body
    assert "routing_policy" not in client.body


@pytest.mark.asyncio
async def test_sync_and_tool_calls_use_sanitized_hy3_payload(monkeypatch) -> None:
    client = _PostClient()
    service = LLMService()

    async def fake_get_client():
        return client

    async def fake_resolve_api_key(_model):
        return None

    monkeypatch.setattr(service, "_get_client", fake_get_client)
    monkeypatch.setattr(service, "_resolve_api_key", fake_resolve_api_key)
    extras = {"disable_thinking": True, "routing_policy": "balanced"}

    result = await service.complete_sync(
        [{"role": "user", "content": "answer"}],
        model="openai/tencent/Hy3",
        extra_params=extras,
    )
    tools = await service.complete_tool_calls(
        [{"role": "user", "content": "choose"}],
        model="openai/tencent/Hy3",
        tools=[{"type": "function", "function": {"name": "pick"}}],
        extra_params=extras,
    )

    assert result == "ok"
    assert tools["content"] == "ok"
    assert len(client.bodies) == 2
    for body in client.bodies:
        assert body["thinking"] == {"type": "disabled"}
        assert "disable_thinking" not in body
        assert "routing_policy" not in body
