"""Utility-model search-query enrichment for opt-in live web.

The native web_search tool remains the source of truth. This helper only
polishes that tool query before it reaches SearXNG, and it falls back to the
deterministic query refiner whenever Utility is unavailable or unsafe.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from config import get_settings
from services.llm import llm_service
from services.query_model_resolver import resolve as resolve_query_model_kind
from services.web_freshness import refine_tool_search_query

logger = logging.getLogger(__name__)

PROMPT_VERSION = "web-query-enrichment-v1"
_MAX_CONTEXT_MESSAGES_SCAN = 8
_MAX_RECENT_USER_MESSAGES = 2
_MAX_HISTORY_CHARS = 1200
_MAX_QUERY_CHARS = 220
_MAX_QUERY_TERMS = 18
_MIN_QUERY_TERMS = 3
_MIN_BASE_OVERLAP = 0.45
_CONTROL_PREFIX_RE = re.compile(
    r"^(?:search\s+query|query|web\s+query|search)\s*:\s*",
    flags=re.IGNORECASE,
)
_UNSAFE_OUTPUT_MARKERS = (
    "{",
    "}",
    "[",
    "]",
    "<tool_calls",
    "tool_calls",
    "```",
    "http://",
    "https://",
)
_LOW_SIGNAL_TERMS = {
    "about",
    "after",
    "answer",
    "based",
    "enabled",
    "find",
    "for",
    "from",
    "live",
    "local",
    "look",
    "one",
    "query",
    "rag",
    "results",
    "run",
    "search",
    "sentence",
    "the",
    "then",
    "use",
    "web",
    "with",
}


@dataclass(frozen=True)
class WebQueryEnrichmentResult:
    query: str
    base_query: str
    applied: bool
    attempted: bool = False
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    duration_ms: int = 0
    history_user_messages_used: int = 0
    fallback_reason: str | None = None


def _query_terms(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+#-]{1,}", text.lower())
        if token not in _LOW_SIGNAL_TERMS
    ]


def _message_field(message: Any, field: str) -> str:
    if isinstance(message, dict):
        return str(message.get(field) or "")
    return str(getattr(message, field, "") or "")


def _compact_recent_user_history(messages: list[Any] | None) -> tuple[str, int]:
    if not messages:
        return "", 0
    lines: list[str] = []
    total = 0
    user_messages: list[str] = []
    for message in reversed(list(messages)[-_MAX_CONTEXT_MESSAGES_SCAN:]):
        role = _message_field(message, "role").strip().lower() or "message"
        if role != "user":
            continue
        content = re.sub(r"\s+", " ", _message_field(message, "content")).strip()
        if not content:
            continue
        user_messages.append(content[:320])
        if len(user_messages) >= _MAX_RECENT_USER_MESSAGES:
            break
    for content in reversed(user_messages):
        line = f"previous_user: {content}"
        total += len(line)
        if total > _MAX_HISTORY_CHARS:
            break
        lines.append(line)
    return "\n".join(lines), len(lines)


def _sanitize_query_candidate(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        text = lines[0]
    text = _CONTROL_PREFIX_RE.sub("", text)
    text = re.sub(r"^[\-\*\d\.\)\s]+", "", text)
    text = text.strip(" `\"'“”‘’")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(".?!;:")
    return text[:_MAX_QUERY_CHARS]


def _is_safe_enrichment(candidate: str, base_query: str) -> bool:
    if not candidate:
        return False
    lower = candidate.lower()
    if any(marker in lower for marker in _UNSAFE_OUTPUT_MARKERS):
        return False
    candidate_terms = set(_query_terms(candidate))
    base_terms = set(_query_terms(base_query))
    if len(candidate_terms) < _MIN_QUERY_TERMS or len(candidate_terms) > _MAX_QUERY_TERMS:
        return False
    if not base_terms:
        return True
    overlap = candidate_terms & base_terms
    return (len(overlap) / len(base_terms)) >= _MIN_BASE_OVERLAP


def _fallback(
    base_query: str,
    *,
    reason: str,
    model: str | None = None,
    attempted: bool = False,
    history_user_messages_used: int = 0,
    started_at: float | None = None,
) -> WebQueryEnrichmentResult:
    duration_ms = 0
    if started_at is not None:
        duration_ms = int((perf_counter() - started_at) * 1000)
    return WebQueryEnrichmentResult(
        query=base_query,
        base_query=base_query,
        applied=False,
        attempted=attempted,
        model=model,
        duration_ms=duration_ms,
        history_user_messages_used=history_user_messages_used,
        fallback_reason=reason,
    )


async def enrich_web_search_query(
    *,
    tool_query: str,
    original_query: str | None,
    user_id: str | None,
    recent_messages: list[Any] | None = None,
) -> WebQueryEnrichmentResult:
    """Improve a native web_search query with the configured Utility model.

    The helper is deliberately conservative: one short non-streaming call,
    temperature zero, tight timeout, compact chat history, and token-overlap
    validation before accepting the model's rewrite.
    """

    base_query = refine_tool_search_query(tool_query, original_query)
    if not user_id:
        return _fallback(base_query, reason="missing_user_id")

    started_at = perf_counter()
    try:
        resolved = await resolve_query_model_kind(user_id, "utility")
    except Exception as exc:
        logger.info("utility_web_query_enrichment resolver failed: %s", exc)
        return _fallback(base_query, reason="resolver_failed", started_at=started_at)

    if not resolved:
        return _fallback(base_query, reason="utility_not_configured", started_at=started_at)

    model = resolved.get("model")
    history, history_count = _compact_recent_user_history(recent_messages)
    prompt = (
        "Rewrite the web search query for SearXNG.\n"
        "Use the current user request, the native tool query, and at most the "
        "two most recent previous user messages only to preserve meaning.\n"
        "Return exactly one plain search query. Do not answer the question. "
        "Do not use JSON, quotes, bullets, URLs, or tool syntax. Keep important "
        "proper nouns, acronyms, product names, and technical terms.\n\n"
        f"Current user request:\n{(original_query or '').strip()[:900]}\n\n"
        f"Native tool query:\n{base_query}\n\n"
        f"Recent user context (latest two prior user turns):\n{history or '(none)'}"
    )
    timeout = max(
        0.25,
        min(
            float(
                getattr(get_settings(), "LIVE_WEB_QUERY_EXPANSION_TIMEOUT_SECONDS", 4.0)
                or 4.0
            ),
            10.0,
        ),
    )

    try:
        raw = await llm_service.complete_sync(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a fast query-rewrite helper. Return only a "
                        "search-engine query."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0,
            max_tokens=48,
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            extra_params=resolved.get("extra_params") or None,
            timeout=timeout,
        )
    except Exception as exc:
        logger.info(
            "utility_web_query_enrichment failed model=%s duration_ms=%d error=%s",
            model,
            int((perf_counter() - started_at) * 1000),
            exc,
        )
        return _fallback(
            base_query,
            reason="utility_call_failed",
            model=model,
            attempted=True,
            history_user_messages_used=history_count,
            started_at=started_at,
        )

    candidate = _sanitize_query_candidate(raw)
    if not _is_safe_enrichment(candidate, base_query):
        logger.info(
            "utility_web_query_enrichment rejected model=%s base=%r candidate=%r duration_ms=%d",
            model,
            base_query[:160],
            candidate[:160],
            int((perf_counter() - started_at) * 1000),
        )
        return _fallback(
            base_query,
            reason="unsafe_or_low_overlap_output",
            model=model,
            attempted=True,
            history_user_messages_used=history_count,
            started_at=started_at,
        )

    final_query = refine_tool_search_query(candidate, original_query)
    duration_ms = int((perf_counter() - started_at) * 1000)
    applied = final_query != base_query
    logger.info(
        (
            "utility_web_query_enrichment attempted=True applied=%s model=%s "
            "base=%r final=%r duration_ms=%d history_user_messages_used=%d "
            "prompt_version=%s"
        ),
        applied,
        model,
        base_query[:160],
        final_query[:160],
        duration_ms,
        history_count,
        PROMPT_VERSION,
    )
    return WebQueryEnrichmentResult(
        query=final_query,
        base_query=base_query,
        applied=applied,
        attempted=True,
        model=model,
        duration_ms=duration_ms,
        history_user_messages_used=history_count,
    )
