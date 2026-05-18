"""Dedicated native tool-call planner for mandatory live web turns.

Agent Zero lets the main chat model decide the web-search tool call. Polymath
keeps the same sequential planner -> tool -> answer shape, but uses a
dedicated agentic/planner model so the final answer model can stay focused on
synthesis.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from config import get_settings
from models.schemas import ModelOverrides
from services.llm import llm_service
from services.query_model_resolver import resolve as resolve_query_model_kind
from services.web_freshness import refine_tool_search_query

logger = logging.getLogger(__name__)

PROMPT_VERSION = "web-tool-planner-v1"
_MAX_CONTEXT_MESSAGES_SCAN = 8
_MAX_RECENT_USER_MESSAGES = 2
_MAX_HISTORY_CHARS = 640
_MAX_QUERY_CHARS = 260


@dataclass(frozen=True)
class WebToolPlan:
    tool_call: dict[str, Any]
    args: dict[str, Any]
    model: str | None
    attempted: bool
    native_tool_call: bool
    prompt_version: str = PROMPT_VERSION
    duration_ms: int = 0
    history_user_messages_used: int = 0
    fallback_reason: str | None = None
    content_preview: str | None = None


def _message_field(message: Any, field: str) -> str:
    if isinstance(message, dict):
        return str(message.get(field) or "")
    return str(getattr(message, field, "") or "")


def _compact_recent_user_history(messages: list[Any] | None) -> tuple[str, int]:
    if not messages:
        return "", 0
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

    lines: list[str] = []
    total = 0
    for content in reversed(user_messages):
        line = f"previous_user: {content}"
        total += len(line)
        if total > _MAX_HISTORY_CHARS:
            break
        lines.append(line)
    return "\n".join(lines), len(lines)


def _bounded_query(value: Any, fallback: str) -> str:
    query = " ".join(str(value or "").split()).strip()
    if not query:
        query = fallback
    return query[:_MAX_QUERY_CHARS]


def _clean_search_query(query: str, fallback: str) -> str:
    """Remove Polymath UI/control wording from fallback/planner queries."""
    text = refine_tool_search_query(query, fallback)
    cleaned = text
    replacements = (
        r"\bwith\s+(?:the\s+)?web\s+toggle\s+(?:on|enabled)?\b",
        r"\bweb\s+toggle\b",
        r"\bselected\s+(?:local\s+)?(?:rag\s+)?corpus\b",
        r"\bselected\s+sources\b",
        r"\blocal\s+rag\b",
        r"\brag\s+context\b",
    )
    for pattern in replacements:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*(?:search|find|look up)\s+(?:for\s+)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;-")
    return cleaned[:_MAX_QUERY_CHARS] if cleaned else text[:_MAX_QUERY_CHARS]


def _fallback_plan(
    *,
    query: str,
    max_results: int,
    reason: str,
    model: str | None = None,
    attempted: bool = False,
    started_at: float | None = None,
    history_user_messages_used: int = 0,
    content_preview: str | None = None,
) -> WebToolPlan:
    duration_ms = int((perf_counter() - started_at) * 1000) if started_at else 0
    query = _clean_search_query(query, query)
    args = {"query": query, "max_results": max_results}
    return WebToolPlan(
        tool_call={
            "id": "server_web_search_1",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": json.dumps(args),
            },
        },
        args=args,
        model=model,
        attempted=attempted,
        native_tool_call=False,
        duration_ms=duration_ms,
        history_user_messages_used=history_user_messages_used,
        fallback_reason=reason,
        content_preview=content_preview,
    )


def _normalize_native_web_call(
    call: dict[str, Any],
    *,
    fallback_query: str,
    max_results: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    fn = call.get("function") if isinstance(call, dict) else None
    if not isinstance(fn, dict) or fn.get("name") != "web_search":
        return None

    try:
        raw_args = json.loads(fn.get("arguments") or "{}")
    except Exception:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raw_args = {}

    query = _bounded_query(raw_args.get("query"), fallback_query)
    query = _clean_search_query(query, fallback_query)
    try:
        requested_limit = int(raw_args.get("max_results") or max_results)
    except (TypeError, ValueError):
        requested_limit = max_results
    requested_limit = max(1, min(requested_limit, max_results))
    args = {"query": query, "max_results": requested_limit}

    normalized = {
        "id": str(call.get("id") or "server_web_search_1"),
        "type": str(call.get("type") or "function"),
        "function": {
            "name": "web_search",
            "arguments": json.dumps(args),
        },
    }
    return normalized, args


async def plan_web_search_tool_call(
    *,
    current_query: str,
    user_id: str | None,
    recent_messages: list[Any] | None,
    tool_schema: dict[str, Any],
    max_results: int,
) -> WebToolPlan:
    """Ask the dedicated planner model for one native ``web_search`` call.

    If no planner model is configured or the provider returns text instead of
    native tool calls, the Web toggle still runs with a deterministic query.
    The fallback never parses raw JSON from model text.
    """
    fallback_query = _clean_search_query(current_query, current_query)
    history, history_count = _compact_recent_user_history(recent_messages)
    settings = get_settings()
    started_at = perf_counter()

    resolved: dict[str, Any] | None = None
    if user_id:
        try:
            resolved = await resolve_query_model_kind(user_id, "agentic")
        except Exception as exc:
            logger.info("web_tool_planner resolver failed: %s", exc)

    model = (
        (resolved or {}).get("model")
        or (settings.AGENTIC_MODEL or "").strip()
        or None
    )
    if not model:
        return _fallback_plan(
            query=fallback_query,
            max_results=max_results,
            reason="planner_not_configured",
            started_at=started_at,
            history_user_messages_used=history_count,
        )

    messages = [
        {
            "role": "system",
            "content": (
                "You are Polymath's dedicated web tool planner. Call exactly "
                "one native web_search tool. Do not answer the user. Do not "
                "write JSON or tool syntax in text."
            ),
        },
        {
            "role": "user",
            "content": (
                "Create one concise SearXNG search query.\n"
                "The current user request is primary. Prior user context is "
                "only for disambiguation.\n"
                "Keep proper nouns, acronyms, product names, versions, dates, "
                "and technical terms. Remove local corpus labels and UI words "
                "like RAG, Web toggle, selected corpus, and sources.\n\n"
                f"Current user request:\n{current_query[:900]}\n\n"
                f"Recent user context:\n{history or '(none)'}"
            ),
        },
    ]
    overrides = ModelOverrides(
        temperature=0,
        max_tokens=96,
        thinking_effort="none",
    )
    try:
        response = await llm_service.complete_tool_calls(
            messages,
            model=model,
            overrides=overrides,
            tools=[tool_schema],
            tool_choice={
                "type": "function",
                "function": {"name": "web_search"},
            },
            api_base=(resolved or {}).get("api_base"),
            api_key=(resolved or {}).get("api_key"),
            extra_params=(resolved or {}).get("extra_params") or None,
            timeout=12.0,
        )
    except Exception as exc:
        logger.info("web_tool_planner call failed model=%s error=%s", model, exc)
        return _fallback_plan(
            query=fallback_query,
            max_results=max_results,
            reason="planner_call_failed",
            model=model,
            attempted=True,
            started_at=started_at,
            history_user_messages_used=history_count,
        )

    for call in response.get("tool_calls") or []:
        normalized = _normalize_native_web_call(
            call,
            fallback_query=fallback_query,
            max_results=max_results,
        )
        if not normalized:
            continue
        tool_call, args = normalized
        duration_ms = int((perf_counter() - started_at) * 1000)
        logger.info(
            "web_tool_planner native_tool_call=True model=%s query=%r duration_ms=%d",
            model,
            args["query"][:180],
            duration_ms,
        )
        return WebToolPlan(
            tool_call=tool_call,
            args=args,
            model=model,
            attempted=True,
            native_tool_call=True,
            duration_ms=duration_ms,
            history_user_messages_used=history_count,
            content_preview=(response.get("content") or "")[:240] or None,
        )

    content_preview = (response.get("content") or "")[:240] or None
    logger.info(
        "web_tool_planner missing native tool call model=%s finish=%s content=%r",
        model,
        response.get("finish_reason"),
        content_preview,
    )
    return _fallback_plan(
        query=fallback_query,
        max_results=max_results,
        reason="native_tool_call_missing",
        model=model,
        attempted=True,
        started_at=started_at,
        history_user_messages_used=history_count,
        content_preview=content_preview,
    )
