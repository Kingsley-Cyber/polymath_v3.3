"""Deterministic web query builder for mandatory Web-toggle turns.

The Web toggle means "search the web", not "ask another model if/how to
search".  This module keeps that route deterministic: use the current user
request as the anchor, add only safe disambiguating context, and emit the same
tool-call shape the rest of the pipeline already understands.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from services.web_freshness import refine_tool_search_query

PROMPT_VERSION = "deterministic-web-query-builder-v1"
_MAX_CONTEXT_MESSAGES_SCAN = 8
_MAX_RECENT_USER_MESSAGES = 2
_MAX_HISTORY_CHARS = 640
_MAX_QUERY_CHARS = 300
_MAX_QUERY_TERMS = 12

_CONTROL_PHRASES = (
    r"\bwith\s+(?:the\s+)?web\s+(?:toggle\s+)?(?:on|enabled)\b",
    r"\bweb\s+toggle\b",
    r"\bweb\s+enabled\b",
    r"\bselected\s+(?:local\s+)?(?:rag\s+)?corpus\b",
    r"\bselected\s+sources\b",
    r"\blocal\s+rag\b",
    r"\brag\s+(?:wise|context|retrieval|chunks?)\b",
    r"\busing\s+these\b",
    r"\buse\s+these\b",
    r"\bbased\s+on\s+(?:the\s+)?(?:rag|sources|corpus)\b",
)

_LEADING_FILLER = (
    r"^(?:please\s+)?(?:search|find|look\s+up)\s+(?:for\s+)?",
    r"^(?:please\s+)?(?:explain|summarize|describe)\s+(?:the\s+)?",
    r"^(?:what\s+is|what\s+are|how\s+do\s+i|how\s+does)\s+",
)

_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "should",
        "show",
        "tell",
        "that",
        "the",
        "their",
        "then",
        "there",
        "this",
        "to",
        "use",
        "uses",
        "using",
        "verify",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "working",
        "you",
        "focus",
    }
)

_PROTECTED_LOWER_TERMS = frozenset(
    {
        "agent",
        "api",
        "cifar-10",
        "glm",
        "hyde",
        "llama.cpp",
        "llamacpp",
        "luau",
        "mlx",
        "obscura",
        "polymath",
        "psychogat",
        "qdrant",
        "rag",
        "remoteevent",
        "searxng",
    }
)

_ROBLOX_MARKERS = frozenset(
    {
        "remoteevent",
        "remotefunction",
        "roblox",
        "luau",
        "datastore",
        "replicatedstorage",
    }
)

_SECURITY_MARKERS = frozenset(
    {
        "auth",
        "authentication",
        "exploit",
        "injection",
        "permission",
        "permissions",
        "security",
        "server",
        "validate",
        "validation",
    }
)


@dataclass(frozen=True)
class WebQueryBuildPlan:
    tool_call: dict[str, Any]
    args: dict[str, Any]
    attempted: bool
    native_tool_call: bool
    prompt_version: str = PROMPT_VERSION
    duration_ms: int = 0
    history_user_messages_used: int = 0
    rag_terms_used: tuple[str, ...] = ()
    context_terms_used: tuple[str, ...] = ()
    strategy: str = "deterministic"
    fallback_reason: str | None = None


def _message_field(message: Any, field: str) -> str:
    if isinstance(message, dict):
        return str(message.get(field) or "")
    return str(getattr(message, field, "") or "")


def _compact_recent_user_messages(messages: list[Any] | None) -> tuple[list[str], int]:
    if not messages:
        return [], 0
    user_messages: list[str] = []
    total = 0
    for message in reversed(list(messages)[-_MAX_CONTEXT_MESSAGES_SCAN:]):
        role = _message_field(message, "role").strip().lower() or "message"
        if role != "user":
            continue
        content = re.sub(r"\s+", " ", _message_field(message, "content")).strip()
        if not content:
            continue
        clipped = content[:320]
        total += len(clipped)
        if total > _MAX_HISTORY_CHARS:
            break
        user_messages.append(clipped)
        if len(user_messages) >= _MAX_RECENT_USER_MESSAGES:
            break
    user_messages.reverse()
    return user_messages, len(user_messages)


def _strip_control_language(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for pattern in _CONTROL_PHRASES:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    for pattern in _LEADING_FILLER:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,:;-")
    return text


def _raw_tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#/_-]*%?", value)


def _token_key(token: str) -> str:
    return token.strip("._-/").lower()


def _is_useful_token(token: str) -> bool:
    key = _token_key(token)
    if not key:
        return False
    if key in _PROTECTED_LOWER_TERMS:
        return True
    if re.search(r"\d", token):
        return True
    if re.search(r"[A-Z].*[A-Z]", token):
        return True
    if key in _STOPWORDS:
        return False
    return len(key) >= 3


def _dedupe_terms(terms: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = term.strip(" .,:;!?()[]{}\"'")
        if not cleaned or not _is_useful_token(cleaned):
            continue
        key = _token_key(cleaned)
        if key in seen:
            continue
        selected.append(cleaned)
        seen.add(key)
        if len(selected) >= _MAX_QUERY_TERMS:
            break
    return selected


def _extract_terms(value: str) -> list[str]:
    return _dedupe_terms(_raw_tokens(_strip_control_language(value)))


def _has_domain_overlap(base_keys: set[str], candidate_terms: list[str]) -> bool:
    candidate_keys = {_token_key(term) for term in candidate_terms}
    if not candidate_keys:
        return False
    if base_keys & candidate_keys:
        return True
    if base_keys & _ROBLOX_MARKERS and candidate_keys & _ROBLOX_MARKERS:
        return True
    if base_keys & _SECURITY_MARKERS and candidate_keys & _SECURITY_MARKERS:
        return True
    return False


def _looks_ambiguous(terms: list[str]) -> bool:
    keys = {_token_key(term) for term in terms}
    if len(terms) <= 3:
        return True
    return bool(keys & _ROBLOX_MARKERS) and not {"roblox", "luau"} & keys


def _context_terms(current_terms: list[str], recent_messages: list[str]) -> tuple[str, ...]:
    if not recent_messages:
        return ()
    current_keys = {_token_key(term) for term in current_terms}
    if not _looks_ambiguous(current_terms):
        return ()

    terms: list[str] = []
    for message in recent_messages:
        candidate = _extract_terms(message)
        if not _has_domain_overlap(current_keys, candidate):
            continue
        for term in candidate:
            key = _token_key(term)
            if key not in current_keys and key not in {_token_key(t) for t in terms}:
                terms.append(term)
            if len(terms) >= 4:
                return tuple(terms)
    return tuple(terms)


def _source_text(source: Any) -> str:
    parts = []
    for field in ("doc_name", "title", "source", "doc_id"):
        value = getattr(source, field, None)
        if value:
            parts.append(str(value))
    metadata = getattr(source, "metadata", None)
    if isinstance(metadata, dict):
        for field in ("title", "source", "url"):
            value = metadata.get(field)
            if value:
                parts.append(str(value))
    return " ".join(parts)


def _source_score(source: Any) -> float:
    try:
        return float(getattr(source, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rag_terms(current_terms: list[str], sources: list[Any] | None) -> tuple[str, ...]:
    if not sources:
        return ()
    current_keys = {_token_key(term) for term in current_terms}
    if not current_keys:
        return ()

    terms: list[str] = []
    seen: set[str] = set()
    for source in sources[:5]:
        if _source_score(source) < 0.72:
            continue
        candidate = _extract_terms(_source_text(source))
        if not _has_domain_overlap(current_keys, candidate):
            continue
        for term in candidate:
            key = _token_key(term)
            if key in current_keys or key in seen:
                continue
            terms.append(term)
            seen.add(key)
            if len(terms) >= 3:
                return tuple(terms)
    return tuple(terms)


def _apply_domain_completion(terms: list[str]) -> list[str]:
    keys = {_token_key(term) for term in terms}
    additions: list[str] = []
    if keys & _ROBLOX_MARKERS:
        if "remoteevent" in keys and "roblox" not in keys:
            additions.append("Roblox")
        if {"remoteevent", "roblox"} & keys and "luau" not in keys:
            additions.append("Luau")
        if keys & _SECURITY_MARKERS and "security" not in keys:
            additions.append("security")
    return _dedupe_terms(terms + additions)


def build_web_search_query(
    *,
    current_query: str,
    recent_messages: list[Any] | None = None,
    rag_sources: list[Any] | None = None,
) -> tuple[str, tuple[str, ...], tuple[str, ...], int]:
    """Build a bounded SearXNG query without an LLM call."""
    clean = _strip_control_language(current_query)
    terms = _extract_terms(clean)
    history, history_count = _compact_recent_user_messages(recent_messages)
    ctx_terms = _context_terms(terms, history)
    rag_terms = _rag_terms(terms, rag_sources)
    terms = _apply_domain_completion(terms + list(ctx_terms) + list(rag_terms))

    if terms:
        query = " ".join(terms)
    else:
        query = clean or current_query
    query = refine_tool_search_query(query, clean or current_query)
    return query[:_MAX_QUERY_CHARS], rag_terms, ctx_terms, history_count


def build_web_search_tool_call(
    *,
    current_query: str,
    recent_messages: list[Any] | None,
    rag_sources: list[Any] | None,
    max_results: int,
) -> WebQueryBuildPlan:
    started_at = perf_counter()
    query, rag_terms, ctx_terms, history_count = build_web_search_query(
        current_query=current_query,
        recent_messages=recent_messages,
        rag_sources=rag_sources,
    )
    max_results = max(1, int(max_results))
    args = {"query": query, "max_results": max_results}
    return WebQueryBuildPlan(
        tool_call={
            "id": "server_web_search_1",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": json.dumps(args),
            },
        },
        args=args,
        attempted=True,
        native_tool_call=False,
        duration_ms=int((perf_counter() - started_at) * 1000),
        history_user_messages_used=history_count,
        rag_terms_used=rag_terms,
        context_terms_used=ctx_terms,
    )
