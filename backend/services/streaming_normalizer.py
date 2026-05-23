"""Normalize streamed LLM deltas into response and reasoning lanes.

This module mirrors Agent Zero's useful bit: trust provider-native reasoning
fields when present, otherwise extract XML-like thinking blocks from ordinary
content while buffering partial tags across SSE chunk boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TypedDict


class StreamDelta(TypedDict):
    content: str
    thinking: str


@dataclass
class _ThinkingTag:
    name: str
    close: str


_OPEN_TAG_RE = re.compile(r"<(?P<name>think|thinking|reasoning)(?:\s[^>]*)?>", re.I)
_OPEN_PREFIXES = ("<think", "<thinking", "<reasoning")


class StreamingNormalizer:
    """Stateful normalizer for one streamed chat completion."""

    def __init__(self) -> None:
        self._native_reasoning = False
        self._inside_tag: _ThinkingTag | None = None
        self._pending = ""

    def add(self, *, content: str = "", thinking: str = "") -> StreamDelta:
        if thinking:
            self._native_reasoning = True

        if self._native_reasoning:
            response = self._pending + (content or "")
            self._pending = ""
            return {"content": response, "thinking": thinking or ""}

        return self._parse_content(content or "")

    def flush(self) -> StreamDelta:
        if not self._pending:
            return {"content": "", "thinking": ""}
        pending = self._pending
        self._pending = ""
        if self._inside_tag:
            return {"content": "", "thinking": pending}
        return {"content": pending, "thinking": ""}

    def _parse_content(self, content: str) -> StreamDelta:
        text = self._pending + content
        self._pending = ""
        response_parts: list[str] = []
        thinking_parts: list[str] = []

        while text:
            if self._inside_tag:
                close = self._inside_tag.close
                lower = text.lower()
                close_at = lower.find(close)
                if close_at >= 0:
                    thinking_parts.append(text[:close_at])
                    text = text[close_at + len(close) :]
                    self._inside_tag = None
                    continue

                keep = _partial_suffix_len(text, close)
                if keep:
                    thinking_parts.append(text[:-keep])
                    self._pending = text[-keep:]
                else:
                    thinking_parts.append(text)
                break

            match = _OPEN_TAG_RE.search(text)
            if match:
                response_parts.append(text[: match.start()])
                name = match.group("name").lower()
                self._inside_tag = _ThinkingTag(name=name, close=f"</{name}>")
                text = text[match.end() :]
                continue

            keep = _partial_open_suffix_len(text)
            if keep:
                response_parts.append(text[:-keep])
                self._pending = text[-keep:]
            else:
                response_parts.append(text)
            break

        return {
            "content": "".join(response_parts),
            "thinking": "".join(thinking_parts),
        }


def extract_stream_delta(chunk: dict[str, Any]) -> tuple[str, str]:
    """Return ``(content, thinking)`` from one OpenAI-compatible SSE chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return "", ""
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    message = (
        choice.get("message")
        or (choice.get("model_extra") or {}).get("message")
        or {}
    )

    content = _get_field(delta, "content") or _get_field(message, "content")
    thinking = (
        _get_field(delta, "thinking")
        or _get_field(delta, "reasoning_content")
        or _get_field(message, "thinking")
        or _get_field(message, "reasoning_content")
    )
    return content or "", thinking or ""


def _get_field(source: Any, field: str) -> str:
    if isinstance(source, dict):
        value = source.get(field)
    else:
        value = getattr(source, field, None)
    return value if isinstance(value, str) else ""


def _partial_suffix_len(text: str, token: str) -> int:
    lower = text.lower()
    max_len = min(len(lower), len(token) - 1)
    for size in range(max_len, 0, -1):
        if lower.endswith(token[:size]):
            return size
    return 0


def _partial_open_suffix_len(text: str) -> int:
    lower = text.lower()
    max_len = min(len(lower), max(len(prefix) for prefix in _OPEN_PREFIXES) + 32)
    for size in range(max_len, 0, -1):
        suffix = lower[-size:]
        if any(prefix.startswith(suffix) for prefix in _OPEN_PREFIXES):
            return size
        if any(suffix.startswith(prefix) and ">" not in suffix for prefix in _OPEN_PREFIXES):
            return size
    return 0
