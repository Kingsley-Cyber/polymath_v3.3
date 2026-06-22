"""Ollama thinking-mode wire mapping.

Ollama's native API uses `think` and streams reasoning as
`message.thinking`. Do not send DeepSeek's direct-API `thinking` object on
Ollama/Ollama Cloud routes; older LiteLLM builds parse that path as generate
chunks and fail once a streamed `thinking` field appears.
"""

from __future__ import annotations

from services.llm import LLMService
from services.thinking_mapper import apply_thinking_effort


def test_ollama_cloud_deepseek_auto_enables_native_think():
    body = {
        "model": "ollama_chat/deepseek-v4-flash:cloud",
        "messages": [],
        "stream": True,
    }

    apply_thinking_effort(body, body["model"], "auto")

    assert body["think"] is True
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_ollama_cloud_deepseek_none_disables_native_think():
    body = {
        "model": "ollama_chat/deepseek-v4-pro:cloud",
        "messages": [],
        "stream": True,
    }

    apply_thinking_effort(body, body["model"], "none")

    assert body["think"] is False
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_ollama_gpt_oss_uses_string_think_levels():
    body = {
        "model": "ollama_chat/gpt-oss:120b-cloud",
        "messages": [],
        "stream": True,
    }

    apply_thinking_effort(body, body["model"], "high")

    assert body["think"] == "high"


def test_llm_service_builds_native_ollama_chat_body():
    service = LLMService()
    body = {
        "model": "ollama_chat/deepseek-v4-flash:cloud",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "think": True,
        "max_tokens": 128,
    }

    native = service._build_ollama_chat_body(
        body=body,
        model=body["model"],
        messages=body["messages"],
        tools=None,
    )

    keep_alive = native.pop("keep_alive", None)
    assert keep_alive
    assert native == {
        "model": "deepseek-v4-flash:cloud",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "think": True,
        "options": {"num_predict": 128},
    }
