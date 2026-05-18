# backend/services/chat_orchestrator.py
# Chat orchestrator service - moves business logic from router to service layer
# Orchestrates: conversation loading, message creation, trimming, LLM streaming, saving
# All functions are async. Import: from services.chat_orchestrator import chat_orchestrator

import asyncio
import json
import logging
from datetime import datetime
from time import perf_counter
from typing import Any, AsyncGenerator

from bson import ObjectId
from config import get_settings
from models.schemas import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ModelConfig,
    ModelOverrides,
    RetrievalTier,
)
from services.context_manager import context_manager
from services.conversation import conversation_service
from services.llm import llm_service
from services.retriever import retriever_orchestrator
from services.tool_registry import tool_registry
# Phase 24 perf — hoist hot-path imports to module level so each chat turn
# doesn't pay the import-resolution cost (was previously inside `try:` blocks
# in process_chat_request).
from services.skills_registry import skills_registry
from services.reasoning_cascade import analyze as reasoning_cascade_analyze
from services.query_model_resolver import (
    resolve as resolve_query_model_kind,
    resolve_by_entry_id,
)
from services.settings import settings_service
from utils.streaming import build_sse_chunk
from utils.tokens import count_tokens

logger = logging.getLogger(__name__)
settings = get_settings()

HYDE_FAILURE_TTL_SECONDS = 600.0
_HYDE_FAILURE_CACHE: dict[str, float] = {}
_MAX_PERSISTED_SOURCE_PREVIEWS = 10
_MAX_PERSISTED_WEB_SOURCE_PREVIEWS = 7
_MAX_PERSISTED_SOURCE_TEXT_CHARS = 900
_MAX_PERSISTED_SOURCE_SUMMARY_CHARS = 500
_MAX_TOOL_CALLS_PER_TURN = 3
_MAX_WEB_SEARCH_CALLS_PER_TURN = 1
_MAX_WEB_SEARCH_RESULTS_PER_CALL = 7
_RAW_TOOL_REQUEST_MARKERS = (
    "<｜｜dsml｜｜tool_calls",
    "<tool_calls",
    "tool_calls>",
    "invoke name=",
    "\"tool_calls\"",
    "'tool_calls'",
)


def _hyde_failure_key(model: str | None, api_base: str | None) -> str:
    """Group HyDE failures by endpoint so one bad helper model doesn't tax every query."""
    return f"{api_base or '(litellm)'}::{model or '(default)'}"


_HYDE_SOURCE_CONSTRAINT_MARKERS = (
    "retrieved excerpts",
    "provided excerpts",
    "provided context",
    "direct textual support",
    "direct support",
    "distinguish direct",
    "distinguish textual",
    "verbatim",
    "quote",
    "quoted",
    "cite",
    "citation",
)


def _should_skip_hyde_for_query(query: str) -> bool:
    """Avoid query rewriting when the user is auditing source support.

    HyDE is valuable for broad cross-domain discovery, but source-constrained
    questions need the original wording preserved. A hypothetical answer can
    accidentally smuggle in the very bridge the user is asking us to verify.
    """
    text = (query or "").lower()
    if not text:
        return False
    if any(marker in text for marker in _HYDE_SOURCE_CONSTRAINT_MARKERS):
        return True
    return ("based on" in text or "according to" in text) and (
        "inferred" in text
        or "textual" in text
        or "evidence" in text
        or "support" in text
    )


def _clip_source_text(value: Any, max_chars: int) -> str | None:
    """Return a bounded text preview for persisted source snippets."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _source_to_dict(source: Any) -> dict[str, Any] | None:
    if hasattr(source, "model_dump"):
        return source.model_dump(mode="json")
    if isinstance(source, dict):
        return dict(source)
    return None


def _source_metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _is_web_source_data(data: dict[str, Any]) -> bool:
    return (
        data.get("source_tier") == "web_search"
        or data.get("corpus_id") == "live-web"
        or str(data.get("chunk_id") or "").startswith("web:")
    )


def _web_source_key(source: Any) -> str | None:
    data = _source_to_dict(source)
    if not data or not _is_web_source_data(data):
        return None
    metadata = _source_metadata(data)
    key = metadata.get("url") or data.get("doc_id") or data.get("chunk_id")
    return str(key).strip() if key else None


def _append_deduped_web_sources(existing: list[Any], pending: list[Any]) -> list[Any]:
    """Append web/tool sources while keeping one entry per URL."""
    if not pending:
        return existing

    seen = {
        key
        for source in existing
        if (key := _web_source_key(source))
    }
    merged = list(existing)
    for source in pending:
        key = _web_source_key(source)
        if key:
            if key in seen:
                continue
            seen.add(key)
        merged.append(source)
    return merged


def _source_identity_key(source: Any) -> str | None:
    """Stable key for exact source-card dedupe.

    This intentionally does not collapse every chunk from the same document:
    two different sections can both be useful evidence. It does remove the
    same chunk/source card when it enters through multiple retrieval lanes.
    """
    data = _source_to_dict(source)
    if not data:
        return None
    if _is_web_source_data(data):
        web_key = _web_source_key(data)
        return f"web:{web_key}" if web_key else None
    chunk_id = str(data.get("chunk_id") or "").strip()
    if chunk_id:
        return f"chunk:{chunk_id}"
    parent_id = str(data.get("parent_id") or "").strip()
    doc_id = str(data.get("doc_id") or "").strip()
    if parent_id or doc_id:
        return f"parent:{doc_id}:{parent_id}"
    text = " ".join(str(data.get("text") or "").split())[:240]
    return f"text:{text}" if text else None


def _dedupe_sources_for_context(sources: list[Any] | None) -> list[Any]:
    """Preserve order while removing exact duplicate source cards."""
    if not sources:
        return []
    deduped: list[Any] = []
    seen: set[str] = set()
    duplicates = 0
    for source in sources:
        key = _source_identity_key(source)
        if key and key in seen:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        deduped.append(source)
    if duplicates:
        logger.info("source dedupe removed %d duplicate source card(s)", duplicates)
    return deduped


def _clip_trace_value(value: Any, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _format_utility_web_trace(web_result: str) -> str | None:
    """Build a human-readable trace for the Utility web-query helper.

    This is intentionally structured telemetry, not a raw hidden chain of
    thought. It lets the UI show which helper model ran, what query it chose,
    how much history it used, and how the web lane followed through.
    """
    try:
        payload = json.loads(web_result)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, dict):
        return None
    utility = pipeline.get("utility_query_enrichment")
    if not isinstance(utility, dict):
        return None

    model = utility.get("model") or "not configured"
    attempted = bool(utility.get("attempted"))
    applied = bool(utility.get("applied"))
    fallback = utility.get("fallback_reason") or "none"
    history_count = utility.get("history_user_messages_used") or 0
    duration_ms = utility.get("duration_ms") or 0
    base_query = _clip_trace_value(utility.get("base_query"))
    final_query = _clip_trace_value(payload.get("query"))
    js_render = pipeline.get("js_render") if isinstance(pipeline.get("js_render"), dict) else {}
    fetch_attempts = pipeline.get("full_page_fetch_attempts") or 0
    fetch_successes = pipeline.get("full_page_fetch_successes") or 0
    final_results = pipeline.get("final_reranked_results") or 0
    final_limit = pipeline.get("final_result_limit") or _MAX_WEB_SEARCH_RESULTS_PER_CALL

    return (
        "[Utility web query trace]\n"
        f"model: {model}\n"
        f"prompt_version: {utility.get('prompt_version') or 'unknown'}\n"
        f"attempted: {str(attempted).lower()} | applied: {str(applied).lower()} | "
        f"fallback: {fallback}\n"
        f"context: {history_count} prior user message(s)\n"
        f"base_query: {base_query}\n"
        f"final_query: {final_query}\n"
        f"duration_ms: {duration_ms}\n"
        f"web_followthrough: candidates={pipeline.get('candidate_results') or 0}, "
        f"fetches={fetch_attempts}/{fetch_successes}, "
        f"reranked={final_results}/{final_limit}, "
        f"ranked_by={pipeline.get('ranked_by') or 'unknown'}\n"
        f"obscura: configured={str(bool(js_render.get('configured'))).lower()}, "
        f"attempted={str(bool(js_render.get('attempted'))).lower()}, "
        f"rendered={str(bool(js_render.get('rendered'))).lower()}"
    )


def _format_web_query_builder_trace(plan: Any) -> str:
    """Human-readable trace for deterministic Web-toggle query construction."""
    args = getattr(plan, "args", {}) or {}
    context_terms = ", ".join(getattr(plan, "context_terms_used", ()) or ())
    rag_terms = ", ".join(getattr(plan, "rag_terms_used", ()) or ())
    return (
        "[Deterministic web query trace]\n"
        f"strategy: {getattr(plan, 'strategy', 'deterministic')}\n"
        f"prompt_version: {getattr(plan, 'prompt_version', 'unknown')}\n"
        "model: none\n"
        f"context: {getattr(plan, 'history_user_messages_used', 0)} prior user message(s) scanned\n"
        f"context_terms_used: {context_terms or 'none'}\n"
        f"rag_terms_used: {rag_terms or 'none'}\n"
        f"query: {_clip_trace_value(args.get('query'), 260)}\n"
        f"max_results: {args.get('max_results') or _MAX_WEB_SEARCH_RESULTS_PER_CALL}\n"
        f"duration_ms: {getattr(plan, 'duration_ms', 0)}"
    )


def _format_model_api_trace(
    *,
    name: str,
    model: str | None,
    status: str,
    purpose: str,
    duration_s: float | None = None,
    detail: str | None = None,
) -> str:
    lines = [
        "[Model API call]",
        f"name: {name}",
        f"model: {model or 'resolved at runtime'}",
        f"status: {status}",
        f"purpose: {purpose}",
    ]
    if duration_s is not None:
        lines.append(f"duration_s: {duration_s:.2f}")
    if detail:
        lines.append(f"detail: {_clip_trace_value(detail, 320)}")
    return "\n".join(lines)


def _web_chunk_content_preview(chunk: Any, *, max_chars: int = 1600) -> str:
    text = str(getattr(chunk, "text", "") or "")
    marker = "\nContent: "
    if marker in text:
        text = text.split(marker, 1)[1]
    return text[:max_chars].strip()


def _tool_schema_name(schema: dict[str, Any]) -> str:
    fn = schema.get("function") if isinstance(schema, dict) else None
    return str((fn or {}).get("name") or "")


def _tool_call_name(call: dict[str, Any]) -> str:
    fn = call.get("function") if isinstance(call, dict) else None
    return str((fn or {}).get("name") or "")


def _web_search_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live web when the user explicitly enables the Web "
                "toggle or when a tool-capable turn needs current/external "
                "information. Use a concise query that preserves the user's "
                "technical anchors and acronyms. Do not include local corpus "
                "names, file names, or internal project labels. Prefer official "
                "docs, vendor/developer blogs, framework docs, and production "
                "guides unless the user asks for papers. Do not search isolated "
                "generic words. The server runs controlled SearXNG searches, "
                "fetches pages when useful, reranks locally, and returns up to "
                "seven results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The web search query to run.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_WEB_SEARCH_RESULTS_PER_CALL,
                        "description": "Maximum final reranked web results to return.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _looks_like_raw_tool_request_content(content: str) -> bool:
    """Detect tool-call syntax leaked as text without parsing/executing it."""
    text = (content or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in _RAW_TOOL_REQUEST_MARKERS):
        return True
    return "web_search" in text and (
        "<" in text
        or "{" in text
        or "invoke" in text
        or "parameter" in text
    )


def _is_web_search_enabled_for_request(request: ChatRequest) -> bool:
    """True when this turn should expose the native web_search tool."""
    return bool(
        settings.LIVE_WEB_SEARCH_ENABLED
        and request.overrides
        and getattr(request.overrides, "web_search_enabled", None)
    )


def _available_tool_schemas(
    tool_schemas: list[dict[str, Any]],
    *,
    web_search_call_count: int,
) -> list[dict[str, Any]]:
    if web_search_call_count < _MAX_WEB_SEARCH_CALLS_PER_TURN:
        return tool_schemas
    return [
        schema
        for schema in tool_schemas
        if _tool_schema_name(schema) != "web_search"
    ]


def _limit_tool_calls_for_turn(
    tool_calls: list[dict[str, Any]],
    *,
    remaining_tool_calls: int,
    web_search_call_count: int,
) -> tuple[list[dict[str, Any]], int, bool, bool]:
    """Keep one web_search call per turn while preserving other tools."""
    allowed: list[dict[str, Any]] = []
    selected_web_search_calls = 0
    dropped_for_tool_limit = False
    dropped_for_web_limit = False
    remaining_web_search_calls = max(
        0,
        _MAX_WEB_SEARCH_CALLS_PER_TURN - web_search_call_count,
    )

    for call in tool_calls:
        if len(allowed) >= remaining_tool_calls:
            dropped_for_tool_limit = True
            continue

        if _tool_call_name(call) == "web_search":
            if selected_web_search_calls >= remaining_web_search_calls:
                dropped_for_web_limit = True
                continue
            selected_web_search_calls += 1

        allowed.append(call)

    return (
        allowed,
        selected_web_search_calls,
        dropped_for_tool_limit,
        dropped_for_web_limit,
    )


def _compact_source_previews(sources: list[Any] | None) -> list[dict[str, Any]] | None:
    """Persist small source previews so reloaded chat messages keep citations.

    Full hydrated chunks can be large, especially with parent-document RAG. The
    frontend only needs enough text to make a reloaded RetrievalBadge useful.
    Web sources are retained deliberately instead of being clipped out by a
    full corpus chunk list.
    """
    if not sources:
        return None

    corpus_previews: list[dict[str, Any]] = []
    web_previews: list[dict[str, Any]] = []
    seen_web_keys: set[str] = set()

    for source in sources:
        data = _source_to_dict(source)
        if data is None:
            continue

        data["text"] = _clip_source_text(
            data.get("text"), _MAX_PERSISTED_SOURCE_TEXT_CHARS
        ) or ""
        if data.get("summary"):
            data["summary"] = _clip_source_text(
                data.get("summary"), _MAX_PERSISTED_SOURCE_SUMMARY_CHARS
            )
        if isinstance(data.get("provenance"), list):
            data["provenance"] = data["provenance"][:5]

        if _is_web_source_data(data):
            key = _web_source_key(data)
            if key and key in seen_web_keys:
                continue
            if key:
                seen_web_keys.add(key)
            if len(web_previews) < _MAX_PERSISTED_WEB_SOURCE_PREVIEWS:
                web_previews.append(data)
            continue

        if len(corpus_previews) < _MAX_PERSISTED_SOURCE_PREVIEWS:
            corpus_previews.append(data)

    web_slots = min(len(web_previews), _MAX_PERSISTED_SOURCE_PREVIEWS)
    corpus_slots = max(0, _MAX_PERSISTED_SOURCE_PREVIEWS - web_slots)
    previews = corpus_previews[:corpus_slots] + web_previews[:web_slots]

    return previews or None


def _is_graph_augmented_tier(tier: Any) -> bool:
    """True only for the Neo4j-backed Graph Augmented retrieval tier."""
    value = getattr(tier, "value", tier)
    return value == RetrievalTier.qdrant_mongo_graph.value


# Baseline system prompt, applied to every chat turn regardless of reasoning
# mode. Exists to fix the pre-Phase-23 pattern where the only style guidance
# was the optional reasoning template — leaving reasoning=none produced raw
# RLHF-default listy output. Layer this prompt first, layer reasoning on top
# if requested. Tuned for Mistral 7B+ / Claude / GPT-4-class models; tiny
# local models (<3B) will partially ignore it.
POLYMATH_SYSTEM_PROMPT = (
    "You are a knowledgeable collaborator answering from retrieved context.\n"
    "\n"
    "Follow these rules:\n"
    "- Match response length to question complexity. A one-line question gets "
    "a one-line answer. Do not pad.\n"
    "- Write in prose. Use bullets or numbered lists ONLY when the user asks, "
    "or when the answer is genuinely a list (e.g. 'what are the five…').\n"
    "- Synthesize across the context. Do NOT narrate chunk-by-chunk "
    "('Source 1 says X, Source 2 says Y'). Integrate.\n"
    "- Cite only when quoting directly or when a claim is genuinely contested "
    "across sources. Do not cite in every sentence.\n"
    "- Skip preambles ('Based on the provided context…', 'Great question…'). "
    "Start with the answer.\n"
    "- If the context doesn't contain the answer, say so in one sentence. "
    "Don't invent, don't pad.\n"
    "\n"
    "Sound like a smart friend explaining, not a research assistant producing "
    "a report."
)


class ChatOrchestrator:
    """Orchestrates the complete chat pipeline."""

    async def process_chat_request(
        self, request: ChatRequest, user_id: str | None = None
    ) -> AsyncGenerator[str, None]:
        """
        Main orchestrator for chat requests.

        Orchestrates the complete pipeline:
        1. Load or create conversation
        2. Create and save user message
        3. Trim history to fit context window
        4. Stream LLM response
        5. Save assistant message

        Args:
            request: ChatRequest with message and optional conversation_id
            user_id: Authenticated user id (Phase 19.3 — required to resolve
                     `profile:<id>` model strings into custom model profiles).

        Yields:
            SSE-formatted chunks
        """
        # Track timing and metadata
        start_time = datetime.utcnow()
        trimming_applied = False
        trimming_details = ""
        trace_events: list[dict[str, Any]] = []

        def _record_trace_event(
            *,
            lane: str,
            title: str,
            status: str,
            content: str = "",
            metadata: dict[str, Any] | None = None,
        ) -> str:
            event = {
                "id": f"trace-{len(trace_events) + 1}",
                "lane": lane,
                "title": title,
                "status": status,
                "content": content,
                "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "metadata": metadata or {},
            }
            trace_events.append(event)
            return build_sse_chunk(
                ChatChunk(
                    type="trace_event",
                    trace_event=event,
                    conversation_id=str(conversation_id),
                )
            )

        # Step 1: Load or create conversation
        (
            conversation_id,
            model_config,
            existing_messages,
        ) = await self._load_or_create_conversation(request)
        object.__setattr__(request, "_user_id", user_id)
        object.__setattr__(
            request,
            "_recent_chat_messages",
            list(existing_messages[-6:] if existing_messages else []),
        )

        # Step 2: Get model to use
        model_used = self._get_model_to_use(request, model_config)

        # Step 3: Create user message
        user_message = self._create_user_message(request.message, model_used)

        # Phase 19.3 / Phase E — resolve `profile:<id>` (legacy Custom Models)
        # and `pool:<id>` (unified Model Pool) prefixes into concrete
        # base_url + api_key + model. Both fall through to the same LiteLLM
        # `openai/*` passthrough path.
        profile_creds: dict = {}
        agentic_on_request = (
            request.overrides.agentic_mode
            if (request.overrides and request.overrides.agentic_mode is not None)
            else settings.AGENTIC_MODE_ENABLED
        )
        web_search_enabled = _is_web_search_enabled_for_request(request)
        web_only_tool_route = bool(
            web_search_enabled
            and not request.selected_tools
            and not agentic_on_request
        )
        tool_route_active = bool(
            request.selected_tools or agentic_on_request
        )
        if user_id and (
            model_used.startswith("profile:") or model_used.startswith("pool:")
        ):
            prefix, _, _id = model_used.partition(":")

            # Use the unified resolver which already walks:
            #   1. settings.models.query_model_pool  (Sprint 3 unified)
            #   2. legacy model_pool collection
            #   3. legacy model_profiles collection
            # and returns a normalized dict with `model` already provider-
            # prefixed. Phase 24 perf — imported at module-level.
            _resolved = await resolve_by_entry_id(user_id, _id)

            if _resolved:
                profile_creds = {
                    "api_base": _resolved.get("api_base"),
                    "api_key": _resolved.get("api_key"),
                    "extra_params": _resolved.get("extra_params") or None,
                }
                model_used = _resolved["model"]
                logger.info(
                    "%s resolved: user=%s id=%s → %s",
                    prefix, user_id, _id, model_used,
                )
            else:
                logger.warning(
                    "%s not found: user=%s id=%s; "
                    "falling back to DEFAULT_COMPLETION_MODEL.",
                    prefix, user_id, _id,
                )
                model_used = settings.DEFAULT_COMPLETION_MODEL

            # Critical: sync request.overrides.model with the resolved/fallback
            # value so _build_request_body (llm.py:102) doesn't clobber the
            # body back to the unresolved `pool:...` / `profile:...` string.
            if request.overrides is not None:
                request.overrides.model = model_used

        # Phase F — role resolution. User-selected tools and explicit agentic
        # mode still route the answer stream through the tool-capable role.
        # Web-only turns use a separate dedicated planner model for the native
        # web_search call, then keep the final answer on the selected chat model.
        if tool_route_active:
            qres = (
                await resolve_query_model_kind(user_id, "agentic")
                if user_id
                else None
            )
            if qres:
                model_used = qres["model"]
                profile_creds = {
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                }
                logger.info(
                    "Phase F query prefs resolution: user=%s kind=%s → %s",
                    user_id, "agentic", model_used,
                )
            else:
                model_used = settings.AGENTIC_MODEL
                profile_creds = {}
                logger.info(
                    "Agentic env fallback resolution: user=%s kind=agentic → %s",
                    user_id or "-", model_used,
                )

        elif (
            user_id
            and not profile_creds
            and not (model_used.startswith("pool:") or model_used.startswith("profile:"))
            and not (request.overrides and request.overrides.model)
            and model_used in (settings.DEFAULT_COMPLETION_MODEL, settings.AGENTIC_MODEL)
        ):
            qres = await resolve_query_model_kind(user_id, "query")
            if qres:
                model_used = qres["model"]
                profile_creds = {
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                }
                logger.info(
                    "Phase F query prefs resolution: user=%s kind=%s → %s",
                    user_id, "query", model_used,
                )

        if request.overrides is not None:
            request.overrides.model = model_used

        yield _record_trace_event(
            lane="model_call",
            title="Chat model route",
            status="done",
            content=(
                "Resolved the final chat model before retrieval and tool "
                "execution."
            ),
            metadata={"model": model_used, "web_planner_split": web_only_tool_route},
        )

        # Phase 29 — vision-capability pre-flight. If the user attached
        # images but picked a non-vision model, the LLM call will 4xx
        # mid-stream and surface as a generic transport error. Catch
        # the mismatch HERE with a clear SSE error so the user knows
        # exactly what to fix. Runs after model resolution (so we
        # know the final model_used) but before retrieval (so we
        # don't waste pipeline work on a doomed request).
        if request.attachments:
            from services.vision_capabilities import (
                attachments_include_image,
                supports_vision,
                vision_capable_models_hint,
            )

            if attachments_include_image(request.attachments) and not supports_vision(model_used):
                logger.warning(
                    "vision mismatch: model=%r has no vision support but "
                    "request has image attachments — emitting SSE error",
                    model_used,
                )
                yield build_sse_chunk(
                    ChatChunk(
                        type="error",
                        content=(
                            f"The selected model ({model_used}) doesn't "
                            f"support images. {vision_capable_models_hint()}"
                        ),
                    )
                )
                return

        # Resolve reasoning mode (Phase 15) — per-request overrides win,
        # else falls back to server-side default (wired at settings layer).
        reasoning_mode, reasoning_blend = self._resolve_reasoning(request)

        # Resolve Query Profile (Phase 18 / 23) — preset bundles retrieval_k +
        # rerank + HyDE. Custom profile loads extra knobs from user settings.
        # Individual overrides on ModelOverrides still win.
        profile_cfg = await self._resolve_query_profile(request, user_id=user_id)
        profile_k = profile_cfg["retrieval_k"]
        profile_rerank = profile_cfg["rerank_enabled"]
        query_profile_used = profile_cfg["query_profile"]
        reasoning_mode_used = reasoning_mode or "none"

        # Phase 17 — HyDE: when enabled, generate a hypothetical answer and
        # use IT as the retrieval query. Answers tend to embed closer to
        # answer-shaped chunks than questions do. Graceful fallback on failure.
        hyde_trace_enabled = bool(request.overrides and request.overrides.hyde_enabled)
        if hyde_trace_enabled:
            hyde_model_trace = (
                request.overrides.hyde_model
                if request.overrides
                else None
            ) or settings.HYDE_MODEL
            yield _record_trace_event(
                lane="model_call",
                title="HyDE query helper",
                status="running",
                content=_format_model_api_trace(
                    name="HyDE query helper",
                    model=hyde_model_trace,
                    status="starting",
                    purpose=(
                        "Generate a hypothetical answer used only as the "
                        "local RAG retrieval query."
                    ),
                ),
                metadata={"model": hyde_model_trace},
            )
        hyde_start = perf_counter()
        retrieval_query, hyde_applied = await self._apply_hyde(
            request,
            user_id=user_id,
            hyde_explicit=bool(profile_cfg.get("hyde_explicit", False)),
        )
        if hyde_trace_enabled:
            yield _record_trace_event(
                lane="model_call",
                title="HyDE query helper",
                status="done" if hyde_applied else "skipped",
                content=_format_model_api_trace(
                    name="HyDE query helper",
                    model=hyde_model_trace,
                    status="finished" if hyde_applied else "skipped_or_fallback",
                    purpose=(
                        "Generate a hypothetical answer used only as the "
                        "local RAG retrieval query."
                    ),
                    duration_s=perf_counter() - hyde_start,
                    detail=(
                        "HyDE applied"
                        if hyde_applied
                        else "Raw user query was used for retrieval."
                    ),
                ),
                metadata={
                    "model": hyde_model_trace,
                    "duration_s": perf_counter() - hyde_start,
                    "applied": hyde_applied,
                },
            )

        yield _record_trace_event(
            lane="retrieval",
            title="Local RAG retrieval",
            status="running",
            content=(
                "Starting corpus retrieval before any web-search merge. "
                f"query={_clip_trace_value(retrieval_query, 220)}"
            ),
            metadata={
                "retrieval_k": profile_k,
                "rerank_enabled": profile_rerank,
                "query_profile": query_profile_used,
                "hyde_applied": hyde_applied,
            },
        )
        rag_start = perf_counter()

        # Step 3.5: Retrieval Pipeline
        #   atomic mode: decompose query → fan-out retrieval → merge
        #   all other modes: standard single-query retrieval
        if reasoning_mode == "atomic":
            from services.reasoning import atomic_retrieve

            retrieval = await atomic_retrieve(
                query=retrieval_query,
                corpus_ids=request.corpus_ids,
                retrieval_tier=request.retrieval_tier,
                collections=request.collections,
                model=model_used,
            )
        else:
            # Phase 27 — resolve search-mode dispatch. "auto" infers from
            # the user's actual message (NOT the HyDE-expanded retrieval
            # query, which would have lost the original phrasing signal).
            from services.retriever.search_mode import resolve_search_mode

            requested_mode = (
                getattr(request.overrides, "search_mode", None)
                if request.overrides
                else None
            )
            resolved_mode = resolve_search_mode(requested_mode, request.message)
            logger.info(
                "search_mode: requested=%s resolved=%s",
                requested_mode or "auto",
                resolved_mode,
            )
            retrieval = await retriever_orchestrator.retrieve(
                query=retrieval_query,
                corpus_ids=request.corpus_ids,
                retrieval_tier=request.retrieval_tier,
                collections=request.collections,
                retrieval_k=profile_k,
                rerank_enabled=profile_rerank,
                ranking_query=request.message,
                top_k_summary=profile_cfg["top_k_summary"],
                rerank_top_n=profile_cfg["rerank_top_n"],
                similarity_threshold=profile_cfg["similarity_threshold"],
                neo4j_expansion_cap=profile_cfg["neo4j_expansion_cap"],
                max_corpora_per_query=profile_cfg["max_corpora_per_query"],
                final_top_k=profile_cfg["final_top_k"],
                fact_seed_limit=profile_cfg["fact_seed_limit"],
                search_mode=resolved_mode,
            )
        sources = _dedupe_sources_for_context(retrieval.chunks)
        effective_tier_for_trace = getattr(
            retrieval.effective_tier,
            "value",
            retrieval.effective_tier,
        )
        yield _record_trace_event(
            lane="retrieval",
            title="Local RAG retrieval",
            status="done",
            content=(
                "Corpus retrieval finished. "
                f"raw_chunks={len(retrieval.chunks or [])} "
                f"deduped_context_chunks={len(sources or [])}"
            ),
            metadata={
                "duration_s": perf_counter() - rag_start,
                "effective_tier": str(effective_tier_for_trace),
                "chunks": len(sources or []),
            },
        )

        graph_context_enabled = (
            _is_graph_augmented_tier(retrieval.effective_tier)
            and settings.NEO4J_ENABLED
        )
        facts: list = list(getattr(retrieval, "facts", []) or [])
        if facts and not graph_context_enabled:
            logger.warning(
                "Dropping %d graph facts because effective tier is %s",
                len(facts),
                retrieval.effective_tier,
            )
            facts = []

        # Pt 10d (Cluster 2 — Graph Decoration) — graph-tier-only
        # post-retrieval enrichment. Vector Base and Hybrid never call Neo4j
        # here. When facts already answer the query, decoration is redundant,
        # so skip the extra traversal.
        decoration: list = []
        if not graph_context_enabled:
            decoration = []
        elif len(facts) >= 3:
            logger.info(
                "Graph decoration skipped — %d facts answered the query (Pt 10d.1 gate)",
                len(facts),
            )
        else:
            try:
                from services.retriever.graph_decoration import (
                    graph_decorator as _graph_decorator,
                )

                # Phase 5b — pass db through so decorate_winners can
                # annotate each row with cached structural metrics
                # (betweenness, pagerank, fragile_bridge membership).
                # db=None falls back to base decoration unchanged.
                _db_for_decoration = getattr(
                    __import__(
                        "services.ingestion_service",
                        fromlist=["ingestion_service"],
                    ).ingestion_service,
                    "db",
                    None,
                )
                decoration = await _graph_decorator.decorate_winners(
                    winning_chunks=sources,
                    corpus_ids=request.corpus_ids,
                    wanted_families=None,  # v1: no QueryFacets yet — accept all families
                    neighbor_limit=8,
                    chunks_per_neighbor=3,
                    db=_db_for_decoration,
                )
            except Exception as exc:
                logger.warning("Graph decoration skipped: %s", exc)
                decoration = []

        # Opt-in live web lane. When enabled, execute one deterministic
        # web_search tool step before the final answer. Earlier versions only
        # exposed the native tool schema and let the model decide whether to
        # call it, which made the UI toggle mean "may search" instead of
        # "will search". The prelude below keeps the native tool-call shape
        # (assistant tool_call + tool result) while guaranteeing the backend
        # runs deterministic query construction, SearXNG, page fetch, rerank,
        # and source merge once per Web-enabled turn.
        web_sources: list = []
        prefetched_react_messages: list[dict] = []
        prefetched_tool_call_count = 0
        prefetched_web_search_call_count = 0

        # Trust-signal snapshot — captured here so it carries through to
        # both the `done` SSE frame and the persisted assistant message.
        # `agentic_on_request` was resolved earlier (line ~80) and reflects
        # the per-request override else server default.
        chunks_returned = len(sources)
        strategy_used = retrieval.effective_tier
        if hasattr(strategy_used, "value"):
            strategy_used = strategy_used.value  # enum → str
        downgrade_reason = retrieval.downgrade_reason
        # Phase 24 — trust signal renamed in spirit. The agentic toggle is
        # gone; "agentic-mode-used" now means tool-calling was active this
        # turn, not merely that a tool-capable model exists in settings.
        agentic_mode_used = bool(request.selected_tools or web_search_enabled)
        # Corpus IDs scoped for this turn — None on the request becomes []
        # on the message so the FE state-derivation can treat empty as
        # "NO_RAG" without an extra falsy check.
        collections_queried_for_msg: list[str] = list(request.corpus_ids or [])

        # Notify client when the requested retrieval tier was downgraded
        # (e.g. graph requested but not all corpora have use_neo4j=True).
        if retrieval.downgrade_reason:
            yield build_sse_chunk(
                ChatChunk(
                    type="tier_downgraded",
                    content=retrieval.downgrade_reason,
                    conversation_id=str(conversation_id),
                )
            )

        if web_search_enabled:
            from services.web_query_builder import build_web_search_tool_call

            plan = build_web_search_tool_call(
                current_query=request.message,
                recent_messages=list(existing_messages[-6:] if existing_messages else []),
                rag_sources=sources,
                max_results=_MAX_WEB_SEARCH_RESULTS_PER_CALL,
            )
            yield _record_trace_event(
                lane="planning",
                title="Deterministic web query builder",
                status="done",
                content=_format_web_query_builder_trace(plan),
                metadata={
                    "model": None,
                    "duration_ms": plan.duration_ms,
                    "strategy": plan.strategy,
                    "history_user_messages_used": plan.history_user_messages_used,
                    "context_terms_used": list(plan.context_terms_used),
                    "rag_terms_used": list(plan.rag_terms_used),
                },
            )
            web_args = dict(plan.args)
            web_args["max_results"] = max(
                1,
                min(
                    int(web_args.get("max_results") or _MAX_WEB_SEARCH_RESULTS_PER_CALL),
                    _MAX_WEB_SEARCH_RESULTS_PER_CALL,
                ),
            )
            web_call = {
                "id": "server_web_search_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps(web_args),
                },
            }
            logger.info(
                (
                    "mandatory web_search prelude starting deterministic_query=%r "
                    "max_results=%d context_terms=%s rag_terms=%s"
                ),
                str(web_args.get("query") or "")[:160],
                web_args["max_results"],
                list(plan.context_terms_used),
                list(plan.rag_terms_used),
            )
            object.__setattr__(request, "_skip_web_query_enrichment", True)
            object.__setattr__(
                request,
                "_web_query_builder",
                {
                    "attempted": plan.attempted,
                    "model": None,
                    "prompt_version": plan.prompt_version,
                    "duration_ms": plan.duration_ms,
                    "history_user_messages_used": plan.history_user_messages_used,
                    "fallback_reason": plan.fallback_reason,
                    "strategy": plan.strategy,
                    "context_terms_used": list(plan.context_terms_used),
                    "rag_terms_used": list(plan.rag_terms_used),
                },
            )
            object.__setattr__(request, "_web_query_planner", None)
            yield _record_trace_event(
                lane="tool_call",
                title="Native web_search tool call",
                status="running",
                content=(
                    "Server stored a deterministic web_search tool call before "
                    "executing it."
                ),
                metadata={
                    "tool_name": "web_search",
                    "args": web_args,
                    "stored_before_execution": True,
                    "planned_by_model": None,
                    "planned_by": "deterministic_web_query_builder",
                },
            )
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_call_start",
                    content=json.dumps(
                        [
                            {
                                "name": "web_search",
                                "args": web_call["function"]["arguments"],
                            }
                        ]
                    ),
                    conversation_id=str(conversation_id),
                )
            )
            web_result = await self._execute_web_search_tool(web_args, request)
            logger.info("mandatory web_search prelude completed")
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_result",
                    content=json.dumps(
                        [{"name": "web_search", "result": web_result}]
                    ),
                    conversation_id=str(conversation_id),
                )
            )
            yield _record_trace_event(
                lane="tool_result",
                title="web_search tool result",
                status="done",
                content=(
                    "Native web_search returned results and pipeline telemetry."
                ),
                metadata={"tool_name": "web_search"},
            )
            prefetched_tool_call_count = 1
            prefetched_web_search_call_count = 1
            prefetched_react_messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [web_call],
                        "reasoning_content": (
                            "Server executed the mandatory web_search step "
                            "before the final answer."
                        ),
                    },
                    {
                        "role": "tool",
                        "tool_call_id": web_call["id"],
                        "name": "web_search",
                        "content": web_result,
                    },
                ]
            )
            pending_tool_sources = getattr(request, "_pending_tool_sources", [])
            if pending_tool_sources:
                web_sources = list(pending_tool_sources)
                sources = _dedupe_sources_for_context(
                    _append_deduped_web_sources(
                        list(sources or []),
                        web_sources,
                    )
                )
                chunks_returned = len(sources)
                object.__setattr__(request, "_pending_tool_sources", [])
                logger.info(
                    "mandatory web_search merged sources local_plus_web=%d web_added=%d",
                    chunks_returned,
                    len(web_sources),
                )

        if sources:
            yield build_sse_chunk(
                ChatChunk(
                    type="sources",
                    sources=sources,
                    conversation_id=str(conversation_id),
                )
            )

        # Phase 24 — Skills (multi-select) + Tools, fetched in PARALLEL.
        # Both are independent Mongo reads; running serially wasted ~50-100ms
        # per turn. asyncio.gather collapses them to one round-trip's worth
        # of latency. Result of the tools fetch is cached in
        # `_tools_loaded_for_signal` so _load_tools below skips the duplicate
        # query (it was the same call run twice in the legacy code).
        skills_task = (
            skills_registry.get_skills_by_ids(request.active_skill_ids)
            if request.active_skill_ids
            else None
        )
        tools_task = (
            tool_registry.get_tools_by_ids(request.selected_tools)
            if request.selected_tools
            else None
        )
        skills_loaded: list = []
        tools_loaded: list = []
        if skills_task and tools_task:
            try:
                skills_loaded, tools_loaded = await asyncio.gather(
                    skills_task, tools_task
                )
            except Exception as exc:
                logger.warning("Failed parallel skills+tools fetch: %s", exc)
        elif skills_task:
            try:
                skills_loaded = await skills_task
            except Exception as exc:
                logger.warning("Failed to load active skills: %s", exc)
        elif tools_task:
            try:
                tools_loaded = await tools_task
            except Exception as exc:
                logger.warning("Failed to load tools: %s", exc)

        active_skills_dicts: list[dict] = [
            {
                "name": s.name,
                "slash_command": s.slash_command,
                "instructions": s.instructions,
            }
            for s in skills_loaded
        ]
        if web_search_enabled:
            active_skills_dicts.append(
                {
                    "name": "Live Web Search",
                    "slash_command": "/web",
                    "instructions": (
                        "The user enabled live web search for this turn. You "
                        "are using a search that was already executed as a controlled native "
                        "web_search tool step before this answer. Use the "
                        "supplied web sources only when relevant, cite URLs for "
                        "web-backed facts, and do not call web_search again."
                    ),
                    "auto_selected": True,
                }
            )
        if active_skills_dicts:
            logger.info(
                "Skills active: %s",
                [s["name"] for s in active_skills_dicts],
            )
            for s in active_skills_dicts:
                inst = s["instructions"] or ""
                preview = inst[:400].replace("\n", " ⏎ ")
                logger.info(
                    "  ↳ skill='%s' slash=%s injected_chars=%d preview=%s%s",
                    s["name"],
                    s.get("slash_command") or "(none)",
                    len(inst),
                    preview,
                    "…" if len(inst) > 400 else "",
                )
        active_tool_names: list[str] = [t.name for t in tools_loaded]
        if web_search_enabled:
            active_tool_names.append("web_search")
        # Cache the loaded tools so _load_tools below doesn't repeat the
        # Mongo round-trip. Stash on `request` (mutates the Pydantic model
        # via __dict__ since it's the simplest hand-off; the field isn't
        # serialized back to the client).
        if tools_loaded:
            object.__setattr__(request, "_tools_preloaded", tools_loaded)

        # Phase 24 — Reasoning cascade (opt-in). Run BEFORE building the
        # augmented prompt so analysis can be embedded as a context block.
        analysis_text: str | None = None
        if request.reasoning_cascade and sources:
            try:
                # Phase 24 perf — analyze imported at module-level as
                # reasoning_cascade_analyze.
                # Pass the chat model + creds as the final fallback. If user
                # hasn't picked a reasoning model AND no REASONING_MODEL env,
                # the cascade reuses whatever model is already running the
                # chat — never silently degrades to a hardcoded Ollama default.
                analysis_text = await reasoning_cascade_analyze(
                    user_message.content,
                    sources,
                    user_id=user_id,
                    chat_model=model_used,
                    chat_api_base=profile_creds.get("api_base") if profile_creds else None,
                    chat_api_key=profile_creds.get("api_key") if profile_creds else None,
                    chat_extra_params=profile_creds.get("extra_params") if profile_creds else None,
                )
            except Exception as exc:
                logger.warning("Reasoning cascade failed: %s", exc)

        # Build augmented prompt — works whether or not we have sources, as
        # long as skills or analysis or sources is present.
        # Pt 10d — decide whether decoration reaches the chat prompt. The
        # decoration was already computed above; the gate here is whether
        # the active reasoning mode tells the LLM to infer the graph
        # itself. If yes, withhold inline decoration (and rely on the
        # reasoning cascade or the LLM's own graph-reasoning prompt). If
        # no, pass it through to build_augmented_prompt for inline
        # rendering inside the existing citation `(via ...)` parens.
        inline_decoration: list = []
        if decoration:
            try:
                from services.retriever.graph_decoration import (
                    should_skip_inline_decoration as _should_skip_inline_decoration_fn,
                )

                if not _should_skip_inline_decoration_fn(reasoning_mode, reasoning_blend):
                    inline_decoration = decoration
            except Exception:
                # If the helper somehow fails, prefer "render" over "drop"
                # since the underlying check is just a string-set lookup.
                inline_decoration = decoration

        # Code lane (Phase 2) — if retrieval surfaced code chunks, auto-detect
        # the dominant language and append a virtual skill carrying generic
        # code-synthesis rules plus the language-specific override (when one
        # is defined). Skipped silently when the user has already activated
        # a /code-* skill manually. The skill flows through the standard
        # active_skills_dicts envelope — no special rendering path.
        from services.code_lane_skills import maybe_inject_code_skill
        skill_count_before_code_lane = len(active_skills_dicts)
        active_skills_dicts = maybe_inject_code_skill(sources, active_skills_dicts)
        if active_skills_dicts and len(active_skills_dicts) > skill_count_before_code_lane:
            auto = active_skills_dicts[-1]
            if auto.get("auto_selected"):
                logger.info("Code lane: auto-injected skill %s", auto["name"])

        # Phase 29 — inline text-file attachments into the user message
        # BEFORE the RAG augmentation runs. Text files (.md/.txt/code
        # files) are part of the user's request context; the model
        # should see them alongside RAG sources. Image attachments are
        # handled separately at the multimodal-dict conversion step
        # below — they can't be flattened to text.
        attachments = list(request.attachments or [])
        text_attachments = [a for a in attachments if a.kind == "text"]
        image_attachments = [a for a in attachments if a.kind == "image"]

        # Phase 29 follow-up — budget attachment tokens against the
        # context window BEFORE history trimming. Without this, the
        # trimmer only sees `user_message.token_count` reflecting the
        # raw text, and the multimodal image_url blocks (which can run
        # ~1000-1600 tokens each per provider) silently overflow the
        # context. Text-attachment bodies are also counted here so
        # the trimmer accounts for them even though they get inlined
        # AFTER this point — count_tokens reads the raw `att.content`.
        if attachments:
            from utils.tokens import estimate_attachment_tokens

            attachment_tokens = estimate_attachment_tokens(
                attachments, model_used,
            )
            if attachment_tokens > 0:
                user_message.token_count = (
                    (user_message.token_count or 0) + attachment_tokens
                )
                logger.info(
                    "attachment token budget: +%d (images=%d, text=%d) → "
                    "user_message.token_count=%d",
                    attachment_tokens,
                    len(image_attachments),
                    len(text_attachments),
                    user_message.token_count,
                )
        if text_attachments:
            inlined_parts: list[str] = []
            for att in text_attachments:
                # Cap per-file text at ~32K chars (~8K tokens). Truncation
                # is honest — show the prefix and stamp a marker so the
                # model knows the file was cut off.
                body_text = att.content
                truncated = False
                if len(body_text) > 32_000:
                    body_text = body_text[:32_000]
                    truncated = True
                marker = (
                    f"\n[...content truncated — file was {att.size_bytes} bytes]"
                    if truncated
                    else ""
                )
                inlined_parts.append(
                    f"<attached_file name=\"{att.filename}\" "
                    f"mime=\"{att.mime_type}\">\n{body_text}{marker}\n</attached_file>"
                )
            attachments_block = "\n\n".join(inlined_parts)
            # Prepend the attachments block so the user's question reads
            # last (most-recent / highest-attention position). If the
            # user's text is empty (attachment-only turn), the joint
            # validator ensured at least one attachment exists.
            existing_text = (user_message.content or "").strip()
            user_message.content = (
                f"{attachments_block}\n\n{existing_text}"
                if existing_text
                else attachments_block
            )

        if sources or facts or active_skills_dicts or analysis_text:
            user_message.content = context_manager.build_augmented_prompt(
                query=user_message.content,
                sources=sources,
                facts=facts,
                corpus_ids=request.corpus_ids,
                reasoning_mode=reasoning_mode,
                reasoning_blend=reasoning_blend,
                active_skills=active_skills_dicts or None,
                analysis=analysis_text,
                decoration=inline_decoration,
            )

        # Step 4: Prepare messages for context
        messages_for_context = existing_messages + [user_message]

        # Step 5: Trim history to fit context window
        (
            trimmed_messages,
            trimming_applied,
            trimming_details,
            tokens_used_post_trim,
            tokens_max,
        ) = await self._trim_history(messages_for_context, model_used)

        # Always emit a budget frame so the UI can render "X / Y tokens"
        yield build_sse_chunk(
            ChatChunk(
                type="budget",
                conversation_id=str(conversation_id),
                tokens_used=tokens_used_post_trim,
                tokens_max=tokens_max,
                trimming_applied=trimming_applied,
            )
        )

        # Send trimming notification if history was trimmed
        if trimming_applied:
            yield build_sse_chunk(
                ChatChunk(
                    type="trimming",
                    content=trimming_details,
                    conversation_id=str(conversation_id),
                    trimming_applied=True,
                    trimming_details=trimming_details,
                )
            )

        # Step 6: Load tools if agentic mode is enabled
        tools, tool_schemas = await self._load_tools(request)

        # === START ReAct LOOP ===
        tool_call_count = prefetched_tool_call_count
        web_search_call_count = prefetched_web_search_call_count
        tool_limit_reached = False
        react_messages: list[dict] = list(prefetched_react_messages)

        # Persist the RAW user message, not the RAG-augmented one. The object
        # `user_message.content` was overwritten above with the full augmented
        # prompt (context block + skills + analysis + question). Saving that
        # back poisoned history: every subsequent turn reloaded the prior
        # turn's retrieved chunks as "user input", compounding bloat. Rebuild
        # a clean ChatMessage from request.message so Mongo stores only what
        # the user typed.
        user_saved = await conversation_service.append_message(
            str(conversation_id),
            self._create_user_message(request.message, model_used),
        )
        if not user_saved:
            logger.error("Failed to persist user message for %s", conversation_id)
            yield build_sse_chunk(
                ChatChunk(
                    type="error",
                    content="Failed to save the user message. Please retry.",
                    conversation_id=str(conversation_id),
                )
            )
            return

        while tool_call_count < _MAX_TOOL_CALLS_PER_TURN:
            # Convert messages to dict format for LLM. Baseline system prompt
            # (Phase 23) is prepended every turn so style/length/anti-list
            # guidance survives regardless of whether reasoning mode is set.
            message_dicts: list[dict] = [
                {"role": "system", "content": POLYMATH_SYSTEM_PROMPT},
                *(
                    {"role": msg.role, "content": msg.content}
                    for msg in trimmed_messages
                ),
            ]

            # Phase 29 — multimodal injection for image attachments. The
            # text content (RAG sources + inlined text-file attachments
            # + user query) is already on the last user message. Convert
            # that message's `content` from a plain string to an OpenAI/
            # LiteLLM multimodal content array: one text block followed
            # by image_url blocks (one per image attachment). LiteLLM
            # passes the array through to the upstream provider, which
            # handles the multimodal completion natively.
            #
            # Only the FINAL user message gets multimodal content —
            # previous turns are history and stay text-only. Attachments
            # are per-turn (Phase 29 design choice — they don't persist).
            if image_attachments:
                for i in range(len(message_dicts) - 1, -1, -1):
                    if message_dicts[i].get("role") == "user":
                        text_content = message_dicts[i].get("content") or ""
                        content_blocks: list[dict] = [
                            {"type": "text", "text": text_content},
                        ]
                        for att in image_attachments:
                            # `data:image/png;base64,xxx` URI format —
                            # universally accepted by OpenAI/Anthropic/
                            # Gemini multimodal endpoints, and LiteLLM
                            # forwards the URL field unchanged.
                            data_url = (
                                f"data:{att.mime_type};base64,{att.content}"
                            )
                            content_blocks.append({
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            })
                        message_dicts[i] = {
                            "role": "user",
                            "content": content_blocks,
                        }
                        break
            if react_messages:
                message_dicts.extend(react_messages)
            active_tool_schemas = _available_tool_schemas(
                tool_schemas,
                web_search_call_count=web_search_call_count,
            )

            assistant_content = ""
            assistant_thinking = ""
            tool_calls = []

            # Perf instrumentation — measure TTFT (time to first token),
            # stream duration, and post-stream tail so we can tell an LLM
            # that's slow to respond apart from a blocking post-stream hook.
            stream_start = perf_counter()
            first_token_at: float | None = None
            stream_end: float | None = None

            # Step 7: Stream LLM response
            yield _record_trace_event(
                lane="model_call",
                title="Chat model stream",
                status="running",
                content=_format_model_api_trace(
                    name="Chat model stream",
                    model=model_used,
                    status="starting",
                    purpose=(
                        "Generate the assistant response using the "
                        "retrieved RAG context and any completed tool results."
                    ),
                    detail=(
                        f"messages={len(message_dicts)} "
                        f"tools={'yes' if active_tool_schemas else 'no'}"
                    ),
                ),
                metadata={
                    "model": model_used,
                    "messages": len(message_dicts),
                    "tools_enabled": bool(active_tool_schemas),
                },
            )
            try:
                async for chunk in llm_service.stream_chat(
                    messages=message_dicts,
                    model=model_used,
                    overrides=request.overrides,
                    tools=active_tool_schemas or None,
                    **profile_creds,
                ):
                    if first_token_at is None and (
                        chunk.get("content") or chunk.get("thinking") or chunk.get("tool_calls")
                    ):
                        first_token_at = perf_counter()
                    if chunk.get("tool_calls"):
                        tool_calls.extend(chunk["tool_calls"])
                    elif chunk.get("thinking"):
                        assistant_thinking += chunk["thinking"]
                        yield build_sse_chunk(
                            ChatChunk(
                                type="thinking",
                                thinking=chunk["thinking"],
                                conversation_id=str(conversation_id),
                            )
                        )
                    elif chunk.get("content"):
                        assistant_content += chunk["content"]
                        yield build_sse_chunk(
                            ChatChunk(
                                type="token",
                                content=chunk["content"],
                                conversation_id=str(conversation_id),
                            )
                        )

            except Exception as e:
                logger.error(f"Error during LLM streaming: {e}")
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model stream",
                    status="error",
                    content=f"LLM streaming error: {e}",
                    metadata={"model": model_used},
                )
                yield build_sse_chunk(
                    ChatChunk(type="error", content=f"LLM streaming error: {e}")
                )
                return

            stream_end = perf_counter()
            yield _record_trace_event(
                lane="model_call",
                title="Chat model stream",
                status="done",
                content=_format_model_api_trace(
                    name="Chat model stream",
                    model=model_used,
                    status="finished",
                    purpose=(
                        "Generate the assistant response using the "
                        "retrieved RAG context and any completed tool results."
                    ),
                    duration_s=stream_end - stream_start,
                    detail=(
                        f"content_chars={len(assistant_content)} "
                        f"thinking_chars={len(assistant_thinking)} "
                        f"tool_calls={len(tool_calls)}"
                    ),
                ),
                metadata={
                    "model": model_used,
                    "duration_s": stream_end - stream_start,
                    "content_chars": len(assistant_content),
                    "thinking_chars": len(assistant_thinking),
                    "tool_calls": len(tool_calls),
                },
            )

            # If no tool calls, this is the final response
            if not tool_calls:
                if _looks_like_raw_tool_request_content(assistant_content):
                    logger.info(
                        "Suppressed raw tool-call syntax in assistant content; "
                        "forcing final no-tool answer for %s",
                        conversation_id,
                    )
                    assistant_content = ""
                    tool_limit_reached = True
                break

            remaining_tool_calls = _MAX_TOOL_CALLS_PER_TURN - tool_call_count
            (
                tool_calls,
                selected_web_search_calls,
                dropped_for_tool_limit,
                dropped_for_web_limit,
            ) = _limit_tool_calls_for_turn(
                tool_calls,
                remaining_tool_calls=remaining_tool_calls,
                web_search_call_count=web_search_call_count,
            )
            if dropped_for_tool_limit:
                tool_limit_reached = True
            if dropped_for_web_limit:
                logger.info(
                    "Dropped extra web_search tool call(s); limit is %d per turn",
                    _MAX_WEB_SEARCH_CALLS_PER_TURN,
                )
            if not tool_calls:
                break

            # Announce tool execution before running — lets the UI show "⚙ Running: <tool>"
            tool_call_summaries = [
                {
                    "name": c.get("function", {}).get("name", ""),
                    "args": c.get("function", {}).get("arguments", "{}"),
                }
                for c in tool_calls
            ]
            yield _record_trace_event(
                lane="tool_call",
                title="Native tool call",
                status="running",
                content=json.dumps(tool_call_summaries),
                metadata={
                    "tool_count": len(tool_call_summaries),
                    "stored_before_execution": True,
                },
            )
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_call_start",
                    content=json.dumps(tool_call_summaries),
                    conversation_id=str(conversation_id),
                )
            )

            # If we have tool calls, execute them
            tool_call_count += len(tool_calls)
            web_search_call_count += selected_web_search_calls
            tool_limit_reached = tool_limit_reached or (
                tool_call_count >= _MAX_TOOL_CALLS_PER_TURN
            )
            tool_results = await self._execute_tools(tool_calls, tools, request)

            # Emit tool results — paired 1:1 with the start event
            tool_result_summaries = [
                {
                    "name": c.get("function", {}).get("name", ""),
                    "result": r,
                }
                for c, r in zip(tool_calls, tool_results)
            ]
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_result",
                    content=json.dumps(tool_result_summaries),
                    conversation_id=str(conversation_id),
                )
            )
            yield _record_trace_event(
                lane="tool_result",
                title="Native tool result",
                status="done",
                content=json.dumps(
                    [
                        {
                            "name": item["name"],
                            "result_preview": _clip_trace_value(item["result"], 500),
                        }
                        for item in tool_result_summaries
                    ]
                ),
                metadata={"tool_count": len(tool_result_summaries)},
            )

            # Append tool results to message history and continue loop
            assistant_tool_calls: list[dict] = []
            for i, call in enumerate(tool_calls):
                call_id = call.get("id") or f"call_{tool_call_count}_{i}"
                fn = call.get("function") or {}
                assistant_tool_calls.append(
                    {
                        "id": call_id,
                        "type": call.get("type") or "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "{}",
                        },
                    }
                )
            assistant_tool_message = {
                "role": "assistant",
                "content": assistant_content or None,
                "tool_calls": assistant_tool_calls,
            }
            if assistant_thinking.strip():
                # DeepSeek-style thinking models require prior assistant
                # reasoning_content to be echoed when continuing after a
                # tool call. Without this, LiteLLM/provider rejects the
                # follow-up and the streamed answer never gets persisted.
                assistant_tool_message["reasoning_content"] = assistant_thinking
            react_messages.append(assistant_tool_message)
            for i, (call, result) in enumerate(zip(tool_calls, tool_results)):
                fn = call.get("function") or {}
                react_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": (
                            call.get("id")
                            or assistant_tool_calls[min(i, len(assistant_tool_calls) - 1)][
                                "id"
                            ]
                        ),
                        "name": fn.get("name") or "",
                        "content": result,
                    }
                )

            pending_tool_sources = getattr(request, "_pending_tool_sources", [])
            if pending_tool_sources:
                sources = _dedupe_sources_for_context(
                    _append_deduped_web_sources(
                        list(sources or []),
                        list(pending_tool_sources),
                    )
                )
                chunks_returned = len(sources)
                object.__setattr__(request, "_pending_tool_sources", [])
                yield build_sse_chunk(
                    ChatChunk(
                        type="sources",
                        sources=sources,
                        conversation_id=str(conversation_id),
                    )
                )

        # === END ReAct LOOP ===

        if react_messages and (tool_limit_reached or not assistant_content.strip()):
            if tool_limit_reached:
                assistant_content = ""
            final_messages: list[dict] = [
                {"role": "system", "content": POLYMATH_SYSTEM_PROMPT},
                *(
                    {"role": msg.role, "content": msg.content}
                    for msg in trimmed_messages
                ),
                *react_messages,
                {
                    "role": "user",
                    "content": (
                        "Use the gathered corpus and tool results above to answer "
                        "the original question now. Do not call any more tools, "
                        "and do not write tool-call syntax, XML, JSON, or DSML. "
                        "Write only the user-facing answer."
                    ),
                },
            ]
            try:
                final_stream_start = perf_counter()
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model final stream",
                    status="running",
                    content=_format_model_api_trace(
                        name="Chat model final no-tool stream",
                        model=model_used,
                        status="starting",
                        purpose=(
                            "Force a user-facing answer after tool "
                            "activity without allowing more tool calls."
                        ),
                        detail=f"messages={len(final_messages)} tools=no",
                    ),
                    metadata={
                        "model": model_used,
                        "messages": len(final_messages),
                        "tools_enabled": False,
                    },
                )
                async for chunk in llm_service.stream_chat(
                    messages=final_messages,
                    model=model_used,
                    overrides=request.overrides,
                    tools=None,
                    **profile_creds,
                ):
                    if chunk.get("thinking"):
                        assistant_thinking += chunk["thinking"]
                        yield build_sse_chunk(
                            ChatChunk(
                                type="thinking",
                                thinking=chunk["thinking"],
                                conversation_id=str(conversation_id),
                            )
                        )
                    elif chunk.get("content"):
                        assistant_content += chunk["content"]
                        yield build_sse_chunk(
                            ChatChunk(
                                type="token",
                                content=chunk["content"],
                                conversation_id=str(conversation_id),
                            )
                        )
                final_duration_s = perf_counter() - final_stream_start
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model final stream",
                    status="done",
                    content=_format_model_api_trace(
                        name="Chat model final no-tool stream",
                        model=model_used,
                        status="finished",
                        purpose=(
                            "Force a user-facing answer after tool "
                            "activity without allowing more tool calls."
                        ),
                        duration_s=final_duration_s,
                        detail=(
                            f"content_chars={len(assistant_content)} "
                            f"thinking_chars={len(assistant_thinking)}"
                        ),
                    ),
                    metadata={
                        "model": model_used,
                        "duration_s": final_duration_s,
                        "content_chars": len(assistant_content),
                        "thinking_chars": len(assistant_thinking),
                    },
                )
            except Exception as e:
                logger.error(f"Error during final no-tool LLM streaming: {e}")
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model final stream",
                    status="error",
                    content=f"LLM streaming error: {e}",
                    metadata={"model": model_used},
                )
                yield build_sse_chunk(
                    ChatChunk(type="error", content=f"LLM streaming error: {e}")
                )
                return

        if not assistant_content.strip():
            logger.error("LLM returned an empty assistant response for %s", conversation_id)
            yield _record_trace_event(
                lane="final",
                title="Assistant final answer",
                status="error",
                content="The model returned no user-facing answer after retrieval.",
                metadata={"model": model_used},
            )
            yield build_sse_chunk(
                ChatChunk(
                    type="error",
                    content=(
                        "The model did not return an answer after retrieval. "
                        "Please retry the question."
                    ),
                    conversation_id=str(conversation_id),
                )
            )
            return

        # Phase 15 — self_correct review pass:
        # draft has streamed; now ask the LLM to review. If errors found,
        # emit the critique as a `thinking` chunk, then stream the revision
        # as additional tokens. Transparent — user sees the correction.
        if reasoning_mode == "self_correct" and assistant_content.strip() and sources:
            try:
                from services.reasoning import self_correct_review

                revised, was_revised, issues = await self_correct_review(
                    query=request.message,
                    chunks=sources,
                    initial_answer=assistant_content,
                    model=model_used,
                )
                if was_revised:
                    critique = "; ".join(issues[:3])  # cap at first 3 issues for display
                    yield build_sse_chunk(
                        ChatChunk(
                            type="thinking",
                            thinking=f"⟳ Revising: {critique}",
                            conversation_id=str(conversation_id),
                        )
                    )
                    # Stream the revised answer as a second block of tokens.
                    # Simple chunk-by-chunk emission (no re-call to LLM — just
                    # send the revised text as tokens so the UI appends it).
                    yield build_sse_chunk(
                        ChatChunk(
                            type="token",
                            content=f"\n\n---\n**Revised answer:**\n\n{revised}",
                            conversation_id=str(conversation_id),
                        )
                    )
                    assistant_content = f"{assistant_content}\n\n---\n**Revised answer:**\n\n{revised}"
            except Exception as exc:
                logger.warning("self_correct post-pass failed (%s) — keeping draft", exc)

        # Phase 24 — collect skill/tool/reasoning trust signals for this turn
        skills_used_names = [s["name"] for s in active_skills_dicts]
        reasoning_cascade_applied = bool(analysis_text)
        yield _record_trace_event(
            lane="final",
            title="Assistant final answer",
            status="done",
            content=(
                "Final answer assembled and ready to persist. "
                f"content_chars={len(assistant_content)}"
            ),
            metadata={
                "model": model_used,
                "content_chars": len(assistant_content),
                "trace_events": len(trace_events) + 1,
            },
        )

        try:
            thinking_to_save = assistant_thinking.strip() or None
            await self._save_assistant_message(
                conversation_id,
                assistant_content,
                thinking_to_save,
                model_used,
                trimming_applied,
                chunks_returned=chunks_returned,
                strategy_used=strategy_used,
                query_profile_used=query_profile_used,
                reasoning_mode_used=reasoning_mode_used,
                hyde_applied=hyde_applied,
                agentic_mode_used=agentic_mode_used,
                downgrade_reason=downgrade_reason,
                collections_queried=collections_queried_for_msg,
                skills_used=skills_used_names,
                tools_used=active_tool_names,
                reasoning_cascade_applied=reasoning_cascade_applied,
                sources=sources,
                trace_events=trace_events,
            )
        except Exception as exc:
            logger.error("Failed to persist assistant message for %s: %s", conversation_id, exc)
            yield build_sse_chunk(
                ChatChunk(
                    type="error",
                    content=(
                        "The answer was generated, but the backend could not "
                        "save it. Please retry."
                    ),
                    conversation_id=str(conversation_id),
                )
            )
            return

        # Step 9: Send completion chunk — carries trust-signal fields so the
        # live UI renders the RetrievalBadge without waiting for a reload.
        yield build_sse_chunk(
            ChatChunk(
                type="done",
                conversation_id=str(conversation_id),
                model_used=model_used,
                chunks_returned=chunks_returned,
                strategy_used=strategy_used,
                query_profile_used=query_profile_used,
                reasoning_mode_used=reasoning_mode_used,
                hyde_applied=hyde_applied,
                agentic_mode_used=agentic_mode_used,
                downgrade_reason=downgrade_reason,
                collections_queried=collections_queried_for_msg,
                skills_used=skills_used_names,
                tools_used=active_tool_names,
                reasoning_cascade_applied=reasoning_cascade_applied,
            )
        )

        # Log completion — break the total into ttft / stream / tail so we can
        # tell a slow LLM apart from a blocking post-stream hook.
        done_emitted = perf_counter()
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        ttft_s = (first_token_at - stream_start) if first_token_at else None
        stream_s = (stream_end - first_token_at) if (first_token_at and stream_end) else None
        tail_s = (done_emitted - stream_end) if stream_end else None
        ttft_str = f"{ttft_s:.2f}s" if ttft_s is not None else "n/a"
        stream_str = f"{stream_s:.2f}s" if stream_s is not None else "n/a"
        tail_str = f"{tail_s:.3f}s" if tail_s is not None else "n/a"
        logger.info(
            f"Chat completed conv={conversation_id} model={model_used} "
            f"total={elapsed:.2f}s ttft={ttft_str} stream={stream_str} tail={tail_str}"
        )

    # ── Phase 18 Query Profile ──────────────────────────────────────────────

    # Preset defaults. Profile is a speed preset that bundles retrieval_k +
    # rerank + HyDE. Individual overrides on ModelOverrides take precedence.
    _QUERY_PROFILE_PRESETS: dict[str, dict] = {
        "fast": {"retrieval_k": 10, "rerank_enabled": False, "hyde_enabled": False},
        # Pt10c — balanced now enables HyDE by default. Cross-domain
        # queries on heterogeneous libraries (e.g. "how does generative
        # AI apply to urban planning?") were producing wrong-domain
        # retrieval because the raw query embedding cosine-matched on
        # surface tokens like "design" instead of conceptual content.
        # HyDE generates a hypothetical answer first, embeds THAT, then
        # retrieves — which routes the search to the actually-relevant
        # documents. The ~1-2s latency cost is acceptable for a
        # knowledge-graph application where quality > speed.
        "balanced": {"retrieval_k": 40, "rerank_enabled": True, "hyde_enabled": True},
        "thorough": {"retrieval_k": 60, "rerank_enabled": True, "hyde_enabled": True},
    }

    async def _resolve_query_profile(
        self, request: ChatRequest, user_id: str | None = None
    ) -> dict:
        """
        Resolve Query Profile into concrete knobs. Returns a dict:
          retrieval_k, rerank_enabled, hyde_enabled,
          top_k_summary, rerank_top_n, similarity_threshold,
          neo4j_expansion_cap, max_corpora_per_query, fact_seed_limit

        Priority per knob:
          1. explicit per-request override on ModelOverrides
          2. profile preset (fast / balanced / thorough / custom)
          3. None where the preset doesn't specify (retriever falls back to
             its own defaults / hardcoded constants)

        "custom" profile loads the full RetrievalSettings object from the
        user's saved settings. `final_top_k` is intentionally global: Speed
        controls how wide the search/rerank pool is, while Final K controls
        how many chunks reach the LLM after that pool is ranked.
        """
        overrides = request.overrides
        hyde_explicit = bool(overrides and overrides.hyde_enabled is not None)
        profile_key = (
            overrides.query_profile if overrides and overrides.query_profile else "balanced"
        )

        # Defaults for the extra knobs — None means "let retriever decide"
        extras = {
            "top_k_summary": None,
            "rerank_top_n": None,
            "similarity_threshold": None,
            "neo4j_expansion_cap": None,
            "max_corpora_per_query": None,
            "final_top_k": None,
            "fact_seed_limit": None,
        }

        saved_retrieval_settings = None
        if user_id:
            try:
                gs = await settings_service.get_settings(user_id)
                saved_retrieval_settings = gs.retrieval
                extras["final_top_k"] = saved_retrieval_settings.final_top_k
            except Exception as exc:
                logger.warning(
                    "Retrieval settings load failed for %s (%s) — "
                    "using profile defaults",
                    user_id,
                    exc,
                )

        if profile_key == "custom":
            preset = dict(self._QUERY_PROFILE_PRESETS["balanced"])  # safe fallback
            if saved_retrieval_settings is not None:
                rs = saved_retrieval_settings
                preset = {
                    "retrieval_k": rs.top_k_child,
                    "rerank_enabled": rs.rerank_enabled,
                    # HyDE stays a user-toggled concern regardless of custom
                    "hyde_enabled": False,
                }
                extras.update(
                    {
                        "top_k_summary": rs.top_k_summary,
                        "rerank_top_n": rs.rerank_top_n,
                        "similarity_threshold": rs.similarity_threshold,
                        "neo4j_expansion_cap": rs.neo4j_expansion_cap,
                        "max_corpora_per_query": rs.max_corpora_per_query,
                        "final_top_k": rs.final_top_k,
                        "fact_seed_limit": getattr(rs, "fact_seed_limit", 12),
                    }
                )
                logger.info(
                    "Custom profile resolved for user %s: k=%s rerank=%s thresh=%s final_k=%s",
                    user_id,
                    preset["retrieval_k"],
                    preset["rerank_enabled"],
                    extras["similarity_threshold"],
                    extras["final_top_k"],
                )
        else:
            preset = self._QUERY_PROFILE_PRESETS.get(
                profile_key, self._QUERY_PROFILE_PRESETS["balanced"]
            )

        # Per-request overrides win on the three classic knobs
        retrieval_k = (
            overrides.retrieval_k
            if (overrides and overrides.retrieval_k is not None)
            else preset["retrieval_k"]
        )
        rerank_enabled = (
            overrides.rerank_enabled
            if (overrides and overrides.rerank_enabled is not None)
            else preset["rerank_enabled"]
        )
        hyde_enabled = (
            overrides.hyde_enabled
            if (overrides and overrides.hyde_enabled is not None)
            else preset["hyde_enabled"]
        )

        if overrides is not None:
            for key in (
                "top_k_summary",
                "rerank_top_n",
                "similarity_threshold",
                "neo4j_expansion_cap",
                "max_corpora_per_query",
                "final_top_k",
                "fact_seed_limit",
            ):
                value = getattr(overrides, key, None)
                if value is not None:
                    extras[key] = value

        # Mirror the HyDE decision onto request.overrides so _apply_hyde
        # sees the resolved value (preserves existing call contract).
        if request.overrides is None:
            request.overrides = ModelOverrides()
        if request.overrides.hyde_enabled is None:
            request.overrides.hyde_enabled = hyde_enabled

        return {
            "retrieval_k": retrieval_k,
            "rerank_enabled": bool(rerank_enabled),
            "hyde_enabled": bool(hyde_enabled),
            "hyde_explicit": hyde_explicit,
            "query_profile": profile_key,
            **extras,
        }

    async def _apply_hyde(
        self,
        request: ChatRequest,
        user_id: str | None = None,
        *,
        hyde_explicit: bool = False,
    ) -> tuple[str, bool]:
        """
        Phase 17 — Hypothetical Document Embeddings.

        When `overrides.hyde_enabled` is True, call a small/fast LLM to write
        a 2-3 sentence hypothetical answer to the user's question. Return
        that text (which will be embedded for Qdrant search) instead of the
        raw query.

        Returns:
            (retrieval_query, applied) — `applied` is True ONLY when the
            HyDE call succeeded and produced a non-empty hypothesis. Mere
            `hyde_enabled=True` is not sufficient — used for the trust-signal
            badge that distinguishes "asked for HyDE" from "actually ran HyDE".

        Model resolution order:
          1. request.overrides.hyde_model (per-request)
          2. Phase F — user query prefs `hyde_pool_id` → pool entry creds
          3. settings.HYDE_MODEL (server default)

        On any failure (LLM down, malformed response), log a warning and
        fall back to the original query (applied=False).
        """
        overrides = request.overrides
        if not (overrides and overrides.hyde_enabled):
            return request.message, False
        source_constrained = _should_skip_hyde_for_query(request.message)
        if source_constrained and not hyde_explicit:
            logger.info(
                "HyDE skipped for source-constrained query: '%s'",
                request.message[:80],
            )
            return request.message, False
        if source_constrained:
            logger.info(
                "HyDE source-constrained guard bypassed by explicit toggle: '%s'",
                request.message[:80],
            )

        hyde_model = (overrides.hyde_model if overrides else None) or settings.HYDE_MODEL
        hyde_api_base: str | None = None
        hyde_api_key: str | None = None
        hyde_extra: dict | None = None

        # Phase F — only consult prefs when no per-request override given.
        # Phase 24 perf — resolver imported at module-level.
        if user_id and not (overrides and overrides.hyde_model):
            qres = await resolve_query_model_kind(user_id, "hyde")
            if qres:
                hyde_model = qres["model"]
                hyde_api_base = qres["api_base"]
                hyde_api_key = qres["api_key"]
                hyde_extra = qres["extra_params"] or None
                logger.info(
                    "HyDE — Phase F prefs resolution: user=%s → %s",
                    user_id, hyde_model,
                )

        failure_key = _hyde_failure_key(hyde_model, hyde_api_base)
        failed_at = _HYDE_FAILURE_CACHE.get(failure_key)
        if failed_at is not None:
            age = perf_counter() - failed_at
            if age < HYDE_FAILURE_TTL_SECONDS:
                logger.warning(
                    "HyDE skipped for %.0fs after endpoint failure "
                    "(model=%s api_base=%s). Falling back to raw query.",
                    HYDE_FAILURE_TTL_SECONDS - age,
                    hyde_model,
                    hyde_api_base or "(litellm default)",
                )
                return request.message, False
            _HYDE_FAILURE_CACHE.pop(failure_key, None)

        prompt = (
            "Write a concise, plausible 2-3 sentence answer to this question "
            "as if you already knew the answer. Focus on style and structure "
            "over accuracy — we'll search for the real sources after. Do not "
            "preface with 'The answer is' or similar; just write the answer.\n\n"
            f"Question: {request.message}"
        )
        start = perf_counter()
        try:
            # HyDE is a pre-retrieval helper, not the answer. Keep it on a
            # short leash so a slow/broken helper endpoint cannot dominate
            # the whole chat turn.
            hypothetical = await llm_service.complete_sync(
                messages=[{"role": "user", "content": prompt}],
                model=hyde_model,
                temperature=0.3,
                max_tokens=settings.HYDE_MAX_TOKENS,
                api_base=hyde_api_base,
                api_key=hyde_api_key,
                extra_params=hyde_extra,
                timeout=settings.HYDE_TIMEOUT_SECONDS,
            )
            hypothetical = (hypothetical or "").strip()
            if not hypothetical:
                logger.warning("HyDE returned empty output — using raw query")
                return request.message, False

            _HYDE_FAILURE_CACHE.pop(failure_key, None)
            logger.info(
                "HyDE active [model=%s duration=%.2fs]: query='%s' → hypothesis='%s'",
                hyde_model,
                perf_counter() - start,
                request.message[:80],
                hypothetical[:120],
            )
            return hypothetical, True
        except Exception as exc:
            _HYDE_FAILURE_CACHE[failure_key] = perf_counter()
            logger.warning(
                "HyDE call failed after %.2fs/%ss (model=%s api_base=%s) — "
                "%s: %s. "
                "Fix: set Settings → Models → HyDE to a working entry, or "
                "override HYDE_MODEL env to a pulled Ollama model / cloud "
                "model. Falling back to raw query.",
                perf_counter() - start,
                settings.HYDE_TIMEOUT_SECONDS,
                hyde_model,
                hyde_api_base or "(litellm default)",
                type(exc).__name__,
                exc,
            )
            return request.message, False

    def _resolve_reasoning(
        self, request: ChatRequest
    ) -> tuple[str | None, list[str] | None]:
        """
        Phase 15 resolution: per-request overrides > server default.
        Returns (mode, blend). Either can be None, which callers treat as 'none'.
        """
        mode: str | None = None
        blend: list[str] | None = None
        if request.overrides:
            mode = request.overrides.reasoning_mode or None
            blend = request.overrides.reasoning_blend or None
        # Server-side default (if no per-request value). Settings service seeds
        # AGENTIC_MODE_ENABLED etc. the same way; reasoning has no env var —
        # it's purely persisted per-user in ChatLLMSettings.default_reasoning_mode,
        # which is read on the frontend via settingsStore.loadFromAPI and sent
        # with every request. So if `mode` is None here, treat as "none".
        return mode, blend

    async def _load_or_create_conversation(
        self, request: ChatRequest
    ) -> tuple[ObjectId, ModelConfig, list[ChatMessage]]:
        """
        Load existing conversation or create new one.

        Args:
            request: ChatRequest with optional conversation_id

        Returns:
            Tuple of (conversation_id, model_config, existing_messages)
        """
        if request.conversation_id and ObjectId.is_valid(request.conversation_id):
            conv_id = ObjectId(request.conversation_id)
            conversation = await conversation_service.get_conversation(str(conv_id))
            if conversation and conversation.id:
                return (
                    ObjectId(conversation.id),
                    conversation.model_config_conversation,
                    conversation.messages,
                )
        # Create a new conversation if no valid ID provided
        model_config = ModelConfig()
        if request.overrides and request.overrides.model:
            model_config.model = request.overrides.model

        new_conv_id_str = await conversation_service.create_conversation(
            title=request.message[:50], model_config=model_config
        )
        return ObjectId(new_conv_id_str), model_config, []

    def _get_model_to_use(self, request: ChatRequest, model_config: ModelConfig) -> str:
        """
        Determine which model to use based on request overrides, agentic mode,
        or conversation config. Priority:
          1. explicit overrides.model (user-specified for this turn)
          2. agentic mode (per-request override or server-side default) → agentic_model
          3. conversation's configured model
        """
        if request.overrides and request.overrides.model:
            return request.overrides.model

        per_request_agentic = (
            request.overrides.agentic_mode if request.overrides else None
        )
        agentic_on = (
            per_request_agentic
            if per_request_agentic is not None
            else settings.AGENTIC_MODE_ENABLED
        )
        if agentic_on:
            if request.overrides and request.overrides.agentic_model:
                return request.overrides.agentic_model
            return settings.AGENTIC_MODEL

        # Phase 24 — defaults are empty everywhere now. Resolution chain:
        #   1. conversation's stored model (real value the user picked)
        #   2. settings.DEFAULT_COMPLETION_MODEL env (deployer-set)
        #   3. raise — user must configure a model
        # The legacy ollama/llama3.2:3b literal is treated as "unset" so
        # pre-Phase-24 conversations don't keep firing dead requests.
        LEGACY = {"ollama/llama3.2:3b", "ollama/qwen3:1.7b"}
        stored = (model_config.model or "").strip()
        if stored and stored not in LEGACY:
            return stored
        env_default = (settings.DEFAULT_COMPLETION_MODEL or "").strip()
        if env_default and env_default not in LEGACY:
            return env_default
        # Nothing configured — surface a clean error rather than silently
        # binding to a dead Ollama model.
        raise ValueError(
            "No chat model configured. Pick one in the chat header's model "
            "selector, or set DEFAULT_COMPLETION_MODEL in your .env."
        )

    def _create_user_message(self, message: str, model: str) -> ChatMessage:
        """Create a user message object without saving it."""
        return ChatMessage(
            role="user",
            content=message,
            token_count=count_tokens(message, model),
            created_at=datetime.utcnow(),
        )

    async def _trim_history(
        self, messages: list[ChatMessage], model: str
    ) -> tuple[list[ChatMessage], bool, str, int, int]:
        """
        Trim conversation history to fit context window.

        Returns:
            Tuple of (trimmed_messages, was_trimmed, details, tokens_used, tokens_max)
        """
        from utils.tokens import get_model_context_limit

        trim_result = context_manager.trim_history(
            messages=messages,
            model=model,
        )
        tokens_max = get_model_context_limit(model)

        return (
            trim_result.messages,
            trim_result.was_trimmed,
            trim_result.details,
            trim_result.tokens_after,
            tokens_max,
        )

    async def _save_assistant_message(
        self,
        conversation_id: ObjectId,
        content: str,
        thinking: str | None,
        model: str,
        trimming_applied: bool,
        *,
        chunks_returned: int | None = None,
        strategy_used: str | None = None,
        query_profile_used: str | None = None,
        reasoning_mode_used: str | None = None,
        hyde_applied: bool = False,
        agentic_mode_used: bool = False,
        downgrade_reason: str | None = None,
        collections_queried: list[str] | None = None,
        skills_used: list[str] | None = None,
        tools_used: list[str] | None = None,
        reasoning_cascade_applied: bool = False,
        sources: list[Any] | None = None,
        trace_events: list[dict[str, Any]] | None = None,
    ) -> ChatMessage:
        """Saves the assistant's final message to the database."""
        assistant_message = ChatMessage(
            role="assistant",
            content=content,
            thinking=thinking,
            trace_events=trace_events or [],
            model_used=model,
            token_count=count_tokens(content, model),
            created_at=datetime.utcnow(),
            trimming_applied=trimming_applied,
            collections_queried=collections_queried or [],
            chunks_returned=chunks_returned,
            sources=_compact_source_previews(sources),
            strategy_used=strategy_used,
            query_profile_used=query_profile_used,
            reasoning_mode_used=reasoning_mode_used,
            hyde_applied=hyde_applied,
            agentic_mode_used=agentic_mode_used,
            downgrade_reason=downgrade_reason,
            skills_used=skills_used or [],
            tools_used=tools_used or [],
            reasoning_cascade_applied=reasoning_cascade_applied,
        )

        saved = await conversation_service.append_message(
            str(conversation_id), assistant_message
        )
        if not saved:
            raise RuntimeError("conversation_service.append_message returned False")
        return assistant_message

    async def _load_tools(self, request: ChatRequest) -> tuple[list, list[dict]]:
        """Load tools and their schemas if any are selected.

        Phase 24 perf — when process_chat_request already fetched the tools
        in parallel with skills (for the trust-signal name list), it stashes
        the result on `request._tools_preloaded`. We reuse it here instead
        of issuing a duplicate Mongo round-trip.
        """
        web_search_enabled = _is_web_search_enabled_for_request(request)
        if not request.selected_tools and not web_search_enabled:
            return [], []

        preloaded = getattr(request, "_tools_preloaded", None)
        tools = (
            preloaded
            if preloaded is not None
            else await tool_registry.get_tools_by_ids(request.selected_tools or [])
        )
        tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        if web_search_enabled:
            tool_schemas.append(_web_search_tool_schema())
        return tools, tool_schemas

    async def _execute_tools(
        self,
        tool_calls: list,
        tools: list,
        request: ChatRequest | None = None,
    ) -> list[str]:
        """Execute tool calls and return results."""
        results = []
        for call in tool_calls:
            tool_name = call.get("function", {}).get("name")
            try:
                args = json.loads(call.get("function", {}).get("arguments", "{}"))
            except Exception as e:
                results.append(f"Error parsing tool arguments for {tool_name}: {e}")
                continue

            if tool_name == "web_search":
                results.append(await self._execute_web_search_tool(args, request))
                continue

            tool = next((t for t in tools if t.name == tool_name), None)
            if not tool:
                results.append(f"Error: Tool '{tool_name}' not found.")
                continue

            try:
                result = tool_registry.execute_tool(tool.code, tool.name, args)
                results.append(str(result))
            except Exception as e:
                results.append(f"Error executing tool {tool_name}: {e}")
        return results

    async def _execute_web_search_tool(
        self,
        args: dict,
        request: ChatRequest | None = None,
    ) -> str:
        if not settings.LIVE_WEB_SEARCH_ENABLED:
            return json.dumps({"error": "web_search is disabled by the server"})

        raw_query_arg = args.get("query")
        query = " ".join(str(raw_query_arg or "").split()).strip()
        if (
            request is not None
            and request.message
            and (not query or query.lower() in {"true", "false", "null", "none"})
        ):
            query = " ".join(request.message.split()).strip()
        if not query:
            return json.dumps({"error": "query is required"})
        try:
            max_results = int(
                args.get("max_results") or _MAX_WEB_SEARCH_RESULTS_PER_CALL
            )
        except (TypeError, ValueError):
            max_results = _MAX_WEB_SEARCH_RESULTS_PER_CALL
        max_results = max(1, min(max_results, _MAX_WEB_SEARCH_RESULTS_PER_CALL))

        try:
            from services.web_freshness import (
                live_web_search,
                infer_web_search_time_range,
                refine_tool_search_query,
                rerank_web_source_chunks,
                web_hits_to_source_chunks,
            )
            from services.web_query_enrichment import (
                WebQueryEnrichmentResult,
                enrich_web_search_query,
            )

            skip_enrichment = bool(
                request is not None
                and getattr(request, "_skip_web_query_enrichment", False)
            )
            if skip_enrichment:
                base_query = refine_tool_search_query(
                    query,
                    request.message if request is not None else None,
                )
                builder_meta = (
                    getattr(request, "_web_query_builder", None)
                    if request is not None
                    else None
                )
                planner_meta = (
                    getattr(request, "_web_query_planner", None)
                    if request is not None
                    else None
                )
                enrichment = WebQueryEnrichmentResult(
                    query=base_query,
                    base_query=base_query,
                    applied=False,
                    attempted=False,
                    model=(
                        (builder_meta or {}).get("model")
                        if builder_meta is not None
                        else (planner_meta or {}).get("model")
                    ),
                    fallback_reason=(
                        "deterministic_web_query_builder_used"
                        if builder_meta is not None
                        else "native_web_planner_query_used"
                    ),
                )
            else:
                enrichment = await enrich_web_search_query(
                    tool_query=query,
                    original_query=request.message if request is not None else None,
                    user_id=getattr(request, "_user_id", None),
                    recent_messages=getattr(request, "_recent_chat_messages", None),
                )
            query = enrichment.query
            search_query = query[:300]
            candidate_limit = max(
                max_results,
                int(settings.LIVE_WEB_SEARCH_CANDIDATE_RESULTS or max_results),
            )
            time_range = infer_web_search_time_range(search_query)
            hits = await live_web_search._search_searxng_pool(
                search_query,
                max_results=candidate_limit,
                time_range=time_range,
            )
            prior_web_urls: set[str] = set()
            if request is not None and request.conversation_id:
                prior_web_urls = await conversation_service.get_recent_web_source_urls(
                    request.conversation_id
                )
            fetched, fetch_stats, hits_to_fetch, web_pipeline = (
                await live_web_search._fetch_pages_for_search(
                    search_query=search_query,
                    hits=hits,
                    max_results=max_results,
                    prior_web_urls=prior_web_urls,
                )
            )
            fetch_limit = len(hits_to_fetch)
            fetch_stats_by_url = {str(item.get("url")): item for item in fetch_stats}
            candidate_chunks = web_hits_to_source_chunks(
                hits,
                fetched_markdown=fetched,
                fetch_stats_by_url=fetch_stats_by_url,
                search_query=search_query,
                max_chars=int(settings.OBSCURA_MAX_CHARS or 4000),
            )
            chunks = await rerank_web_source_chunks(
                search_query,
                candidate_chunks,
                limit=max_results,
            )
            hits_by_url = {hit.url: hit for hit in hits}
            result_items = []
            for chunk in chunks:
                url = str((chunk.metadata or {}).get("url") or chunk.doc_id)
                hit = hits_by_url.get(url)
                result_items.append(
                    {
                        "title": (hit.title if hit else chunk.doc_name),
                        "url": url,
                        "content": _web_chunk_content_preview(chunk),
                        "snippet": (hit.snippet[:700] if hit else ""),
                        "published_date": (
                            hit.published_date if hit else (chunk.metadata or {}).get("published_date")
                        ),
                        "search_query": (chunk.metadata or {}).get("search_query"),
                        "time_range": (chunk.metadata or {}).get("time_range"),
                        "full_page_fetched": bool(
                            (chunk.metadata or {}).get("full_page_fetched")
                        ),
                        "evidence_mode": (chunk.metadata or {}).get("evidence_mode"),
                        "fetch_status": (chunk.metadata or {}).get("fetch_status"),
                        "fetch_method": (chunk.metadata or {}).get("fetch_method"),
                        "cache_hit": bool((chunk.metadata or {}).get("cache_hit")),
                        "web_content_untrusted": True,
                    }
                )
            if request is not None and chunks:
                pending = list(getattr(request, "_pending_tool_sources", []) or [])
                pending = _append_deduped_web_sources(pending, chunks)
                object.__setattr__(request, "_pending_tool_sources", pending)

            search_queries = sorted(
                {
                    str(hit.search_query or search_query)
                    for hit in hits
                    if str(hit.search_query or search_query).strip()
                }
            )
            obscura_domains = [
                domain.strip()
                for domain in str(
                    getattr(settings, "LIVE_WEB_OBSCURA_DOMAINS", "") or ""
                ).split(",")
                if domain.strip()
            ]
            pipeline = {
                "web_search_calls_this_turn": 1,
                "web_search_call_limit": _MAX_WEB_SEARCH_CALLS_PER_TURN,
                "candidate_limit_requested": candidate_limit,
                "candidate_results": len(hits),
                "search_queries": search_queries[:12],
                "freshness_time_range": time_range,
                "snippet_rerank_applied": bool(
                    settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES
                    and len(hits) > len(hits_to_fetch)
                    and bool(hits_to_fetch)
                ),
                "snippet_rerank_fetch_limit": fetch_limit,
                "full_page_fetch_enabled": bool(
                    settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES
                ),
                "full_page_fetch_attempts": len(hits_to_fetch),
                "full_page_fetch_successes": len(fetched),
                "fetcher": getattr(settings, "LIVE_WEB_PAGE_FETCHER", "auto"),
                "fetches": fetch_stats[:10],
                "js_render": {
                    "configured": bool(settings.OBSCURA_COMMAND),
                    "policy": "allowlisted fallback after static extraction fails",
                    "allowlisted_domains": obscura_domains,
                    "attempted": any(
                        bool(item.get("obscura_attempted")) for item in fetch_stats
                    ),
                    "rendered": any(
                        bool(item.get("js_rendered")) for item in fetch_stats
                    ),
                },
                "final_chunk_candidates": len(candidate_chunks),
                "final_reranked_results": len(chunks),
                "final_result_limit": max_results,
                "ranked_by": settings.RERANKER_MODEL,
                "utility_query_enrichment": {
                    "attempted": enrichment.attempted,
                    "applied": enrichment.applied,
                    "model": enrichment.model,
                    "base_query": enrichment.base_query,
                    "prompt_version": enrichment.prompt_version,
                    "duration_ms": enrichment.duration_ms,
                    "history_user_messages_used": enrichment.history_user_messages_used,
                    "fallback_reason": enrichment.fallback_reason,
                },
                "web_query_planner": (
                    getattr(request, "_web_query_planner", None)
                    if request is not None
                    else None
                ),
                "web_query_builder": (
                    getattr(request, "_web_query_builder", None)
                    if request is not None
                    else None
                ),
            }
            pipeline.update(web_pipeline)
            logger.info(
                (
                    "web_search pipeline query=%r candidates=%d "
                    "fetch_attempts=%d fetch_successes=%d final=%d "
                    "time_range=%r js_rendered=%s snippet_only=%s "
                    "redis_search_cache_hit=%s redis_page_cache_hit=%s "
                    "utility_attempted=%s utility_applied=%s "
                    "utility_history_user_messages=%s planner_native_tool=%s "
                    "deterministic_web_builder=%s"
                ),
                search_query,
                len(hits),
                len(hits_to_fetch),
                len(fetched),
                len(chunks),
                time_range,
                pipeline["js_render"]["rendered"],
                pipeline.get("snippet_only"),
                pipeline.get("redis_search_cache_hit"),
                pipeline.get("redis_page_cache_hit"),
                enrichment.attempted,
                enrichment.applied,
                enrichment.history_user_messages_used,
                bool((pipeline.get("web_query_planner") or {}).get("native_tool_call")),
                bool((pipeline.get("web_query_builder") or {}).get("attempted")),
            )

            return json.dumps(
                {
                    "query": search_query,
                    "candidate_results": len(hits),
                    "reranked_results": len(chunks),
                    "ranked_by": settings.RERANKER_MODEL,
                    "freshness_time_range": time_range,
                    "full_page_fetch_enabled": bool(
                        settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES
                    ),
                    "pipeline": pipeline,
                    "results": result_items,
                    "note": (
                        "Use these only when relevant. Cite the URL for any "
                        "claim that depends on web results."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.warning("web_search tool failed: %s", e)
            return json.dumps({"error": f"web_search failed: {e}"})


# Global instance
chat_orchestrator = ChatOrchestrator()
