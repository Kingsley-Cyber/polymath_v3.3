import json
import os

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from services.llm import LLMService


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aread(self) -> bytes:
        return b""

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeClient:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.requests: list[dict] = []

    def stream(self, _method, _url, *, json, headers, timeout):  # noqa: A002
        self.requests.append(json)
        return _FakeStreamContext(_FakeStreamResponse(self.lines))


@pytest.mark.asyncio
async def test_stream_chat_keeps_reasoning_content_separate(monkeypatch):
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {"reasoning_content": "I should verify this first."},
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {"content": "Here is the answer."},
                        "finish_reason": None,
                    }
                ]
            }
        ),
        "data: [DONE]",
    ]
    client = _FakeClient(lines)
    svc = LLMService()

    async def fake_get_client():
        return client

    async def fake_resolve_api_key(_model):
        return None

    monkeypatch.setattr(svc, "_get_client", fake_get_client)
    monkeypatch.setattr(svc, "_resolve_api_key", fake_resolve_api_key)

    chunks = [
        chunk
        async for chunk in svc.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            model="deepseek/deepseek-v4-flash",
        )
    ]

    assert chunks == [
        {"thinking": "I should verify this first."},
        {"content": "Here is the answer."},
    ]
    assert "I should verify" not in "".join(
        chunk.get("content", "") for chunk in chunks
    )


@pytest.mark.asyncio
async def test_stream_chat_extracts_split_think_tags(monkeypatch):
    lines = [
        _sse({"choices": [{"delta": {"content": "Before <thi"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "nk>hidden reason</thi"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "nk> after"}, "finish_reason": None}]}),
        "data: [DONE]",
    ]
    client = _FakeClient(lines)
    svc = LLMService()

    async def fake_get_client():
        return client

    async def fake_resolve_api_key(_model):
        return None

    monkeypatch.setattr(svc, "_get_client", fake_get_client)
    monkeypatch.setattr(svc, "_resolve_api_key", fake_resolve_api_key)

    chunks = [
        chunk
        async for chunk in svc.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            model="ollama/local",
        )
    ]

    assert "".join(chunk.get("thinking", "") for chunk in chunks) == "hidden reason"
    assert "".join(chunk.get("content", "") for chunk in chunks) == "Before  after"


@pytest.mark.asyncio
async def test_stream_chat_extracts_reasoning_tag_with_attributes(monkeypatch):
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "content": (
                                'A <reasoning source="model">private note'
                                "</reasoning> B"
                            )
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        "data: [DONE]",
    ]
    client = _FakeClient(lines)
    svc = LLMService()

    async def fake_get_client():
        return client

    async def fake_resolve_api_key(_model):
        return None

    monkeypatch.setattr(svc, "_get_client", fake_get_client)
    monkeypatch.setattr(svc, "_resolve_api_key", fake_resolve_api_key)

    chunks = [
        chunk
        async for chunk in svc.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            model="ollama/local",
        )
    ]

    assert "".join(chunk.get("thinking", "") for chunk in chunks) == "private note"
    assert "".join(chunk.get("content", "") for chunk in chunks) == "A  B"


@pytest.mark.asyncio
async def test_stream_chat_assembles_native_tool_call_deltas(monkeypatch):
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_web_1",
                                    "type": "function",
                                    "function": {
                                        "name": "web_",
                                        "arguments": '{"qu',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "search",
                                        "arguments": 'ery":"OpenAI',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ' news"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]
    client = _FakeClient(lines)
    svc = LLMService()

    async def fake_get_client():
        return client

    async def fake_resolve_api_key(_model):
        return None

    monkeypatch.setattr(svc, "_get_client", fake_get_client)
    monkeypatch.setattr(svc, "_resolve_api_key", fake_resolve_api_key)

    chunks = [
        chunk
        async for chunk in svc.stream_chat(
            messages=[{"role": "user", "content": "search"}],
            model="openai/gpt-4o",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "web_search", "parameters": {}},
                }
            ],
        )
    ]

    assert chunks == [
        {
            "tool_calls": [
                {
                    "id": "call_web_1",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": '{"query":"OpenAI news"}',
                    },
                }
            ]
        }
    ]
    assert client.requests[0]["tools"][0]["function"]["name"] == "web_search"


@pytest.mark.asyncio
async def test_stream_chat_streams_reasoning_during_tool_call_deltas(monkeypatch):
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "I need current evidence before answering.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_web_1",
                                    "type": "function",
                                    "function": {
                                        "name": "web_",
                                        "arguments": '{"qu',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": " Searching the web now.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "search",
                                        "arguments": 'ery":"psychometrics"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]
    client = _FakeClient(lines)
    svc = LLMService()

    async def fake_get_client():
        return client

    async def fake_resolve_api_key(_model):
        return None

    monkeypatch.setattr(svc, "_get_client", fake_get_client)
    monkeypatch.setattr(svc, "_resolve_api_key", fake_resolve_api_key)

    chunks = [
        chunk
        async for chunk in svc.stream_chat(
            messages=[{"role": "user", "content": "search"}],
            model="openai/gpt-4o",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "web_search", "parameters": {}},
                }
            ],
        )
    ]

    assert chunks == [
        {"thinking": "I need current evidence before answering."},
        {"thinking": " Searching the web now."},
        {
            "tool_calls": [
                {
                    "id": "call_web_1",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": '{"query":"psychometrics"}',
                    },
                }
            ]
        },
    ]
