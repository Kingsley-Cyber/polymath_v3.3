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

# Phase 24 perf — track fire-and-forget background tasks (Mongo writes that
# don't need to block the SSE stream). Strong refs prevent asyncio GC from
# killing them mid-flight; entries clear themselves via add_done_callback.
_BG_TASKS: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine off the SSE critical path. Logs failures."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BG_TASKS.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.warning("background task failed: %s", exc)

    task.add_done_callback(_on_done)

logger = logging.getLogger(__name__)
settings = get_settings()

HYDE_FAILURE_TTL_SECONDS = 600.0
_HYDE_FAILURE_CACHE: dict[str, float] = {}
_MAX_PERSISTED_SOURCE_PREVIEWS = 10
_MAX_PERSISTED_SOURCE_TEXT_CHARS = 900
_MAX_PERSISTED_SOURCE_SUMMARY_CHARS = 500


def _hyde_failure_key(model: str | None, api_base: str | None) -> str:
    """Group HyDE failures by endpoint so one bad helper model doesn't tax every query."""
    return f"{api_base or '(litellm)'}::{model or '(default)'}"


def _clip_source_text(value: Any, max_chars: int) -> str | None:
    """Return a bounded text preview for persisted source snippets."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _compact_source_previews(sources: list[Any] | None) -> list[dict[str, Any]] | None:
    """Persist small source previews so reloaded chat messages keep citations.

    Full hydrated chunks can be large, especially with parent-document RAG. The
    frontend only needs enough text to make a reloaded RetrievalBadge useful,
    so we cap count and text length before saving to MongoDB.
    """
    if not sources:
        return None

    previews: list[dict[str, Any]] = []
    for source in sources[:_MAX_PERSISTED_SOURCE_PREVIEWS]:
        if hasattr(source, "model_dump"):
            data = source.model_dump(mode="json")
        elif isinstance(source, dict):
            data = dict(source)
        else:
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
        previews.append(data)

    return previews or None


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

        # Step 1: Load or create conversation
        (
            conversation_id,
            model_config,
            existing_messages,
        ) = await self._load_or_create_conversation(request)

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

        # Phase F — fallback resolution: when no explicit prefix or override
        # is in play, look up the user's query prefs and substitute the chosen
        # pool entry's full credentials. Skipped when the user already gave
        # an explicit pool:/profile: choice or set request.overrides.model.
        if (
            user_id
            and not profile_creds
            and not (model_used.startswith("pool:") or model_used.startswith("profile:"))
            and not (request.overrides and request.overrides.model)
            and model_used in (settings.DEFAULT_COMPLETION_MODEL, settings.AGENTIC_MODEL)
        ):
            # Phase 24 — tool selection drives the auto-fallback to the
            # agentic pool entry. The legacy agentic toggle is gone; whether
            # the user has tools active is now the trigger. Phase 24 perf —
            # resolver imported at module-level.
            kind = "agentic" if (request.selected_tools or agentic_on_request) else "query"
            qres = await resolve_query_model_kind(user_id, kind)
            if qres:
                model_used = qres["model"]
                profile_creds = {
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                }
                logger.info(
                    "Phase F query prefs resolution: user=%s kind=%s → %s",
                    user_id, kind, model_used,
                )

        # Resolve reasoning mode (Phase 15) — per-request overrides win,
        # else falls back to server-side default (wired at settings layer).
        reasoning_mode, reasoning_blend = self._resolve_reasoning(request)

        # Resolve Query Profile (Phase 18 / 23) — preset bundles retrieval_k +
        # rerank + HyDE. Custom profile loads extra knobs from user settings.
        # Individual overrides on ModelOverrides still win.
        profile_cfg = await self._resolve_query_profile(request, user_id=user_id)
        profile_k = profile_cfg["retrieval_k"]
        profile_rerank = profile_cfg["rerank_enabled"]
        profile_hyde = profile_cfg["hyde_enabled"]
        query_profile_used = profile_cfg["query_profile"]
        reasoning_mode_used = reasoning_mode or "none"

        # Phase 17 — HyDE: when enabled, generate a hypothetical answer and
        # use IT as the retrieval query. Answers tend to embed closer to
        # answer-shaped chunks than questions do. Graceful fallback on failure.
        retrieval_query, hyde_applied = await self._apply_hyde(
            request, user_id=user_id
        )

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
            )
        sources = retrieval.chunks

        # Pt 10a (Cluster 1) — Fact-centric retrieval, runs in parallel with
        # the chunk path. Bypasses the reranker. Seed entities come from the
        # provenance of the just-retrieved chunks (Mode A annotations). v1
        # filters to fact_type=property for definitional queries; broader
        # fact_type sets land in week 2.
        facts: list = []
        try:
            from services.retriever.fact_retrieval import fact_retrieval as _fact_retrieval

            seed_entities: list[str] = []
            for s in sources:
                for p in (s.provenance or []):
                    ent = p.get("entity")
                    if ent and ent not in seed_entities:
                        seed_entities.append(ent)
            if seed_entities:
                facts = await _fact_retrieval.retrieve_facts_for_entities(
                    entity_names=seed_entities[:12],
                    corpus_ids=request.corpus_ids,
                    fact_types=["property"],  # v1: definitional only
                    limit=8,
                )
        except Exception as exc:
            logger.warning("Fact retrieval skipped: %s", exc)
            facts = []

        # Pt 10d (Cluster 2 — Graph Decoration) — post-retrieval enrichment.
        # ALWAYS computed (cheap, read-only, try/except → empty fallback).
        # Whether it reaches the chat prompt depends on the reasoning mode.
        # When the LLM is already instructed to build the graph itself
        # (graph_reason / kg_augmented / graphrag_integrated, either as the
        # main mode or anywhere in reasoning_blend), the decoration is
        # withheld from the prompt to avoid the "contradict or ignore"
        # conflict — but it's still computed so the reasoning cascade can
        # consume it as structured pre-digest input.
        decoration: list = []
        try:
            from services.retriever.graph_decoration import (
                graph_decorator as _graph_decorator,
                should_skip_inline_decoration as _should_skip_inline_decoration,
            )

            decoration = await _graph_decorator.decorate_winners(
                winning_chunks=sources,
                corpus_ids=request.corpus_ids,
                wanted_families=None,  # v1: no QueryFacets yet — accept all families
                neighbor_limit=8,
                chunks_per_neighbor=3,
            )
        except Exception as exc:
            logger.warning("Graph decoration skipped: %s", exc)
            decoration = []
            _should_skip_inline_decoration = None  # type: ignore[assignment]

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
        agentic_mode_used = bool(request.selected_tools)
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
        MAX_TOOL_CALLS = 5
        tool_call_count = 0

        # Persist the RAW user message, not the RAG-augmented one. The object
        # `user_message.content` was overwritten above with the full augmented
        # prompt (context block + skills + analysis + question). Saving that
        # back poisoned history: every subsequent turn reloaded the prior
        # turn's retrieved chunks as "user input", compounding bloat. Rebuild
        # a clean ChatMessage from request.message so Mongo stores only what
        # the user typed.
        _fire_and_forget(
            conversation_service.append_message(
                conversation_id,
                self._create_user_message(request.message, model_used),
            )
        )

        while tool_call_count < MAX_TOOL_CALLS:
            # Convert messages to dict format for LLM. Baseline system prompt
            # (Phase 23) is prepended every turn so style/length/anti-list
            # guidance survives regardless of whether reasoning mode is set.
            message_dicts = [
                {"role": "system", "content": POLYMATH_SYSTEM_PROMPT},
                *(
                    {"role": msg.role, "content": msg.content}
                    for msg in trimmed_messages
                ),
            ]

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
            try:
                async for chunk in llm_service.stream_chat(
                    messages=message_dicts,
                    model=model_used,
                    overrides=request.overrides,
                    tools=tool_schemas,
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
                yield build_sse_chunk(
                    ChatChunk(type="error", content=f"LLM streaming error: {e}")
                )
                return

            stream_end = perf_counter()

            # If no tool calls, this is the final response
            if not tool_calls:
                break

            # Announce tool execution before running — lets the UI show "⚙ Running: <tool>"
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_call_start",
                    content=json.dumps(
                        [
                            {
                                "name": c.get("function", {}).get("name", ""),
                                "args": c.get("function", {}).get("arguments", "{}"),
                            }
                            for c in tool_calls
                        ]
                    ),
                    conversation_id=str(conversation_id),
                )
            )

            # If we have tool calls, execute them
            tool_call_count += len(tool_calls)
            tool_results = await self._execute_tools(tool_calls, tools)

            # Emit tool results — paired 1:1 with the start event
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_result",
                    content=json.dumps(
                        [
                            {
                                "name": c.get("function", {}).get("name", ""),
                                "result": r,
                            }
                            for c, r in zip(tool_calls, tool_results)
                        ]
                    ),
                    conversation_id=str(conversation_id),
                )
            )

            # Append tool results to message history and continue loop
            for result in tool_results:
                trimmed_messages.append(ChatMessage(role="tool", content=result))

        # === END ReAct LOOP ===

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

        # Phase 24 perf — fire-and-forget the assistant-message save. The
        # save includes count_tokens (cold-start: 1-4s), Mongo insert + update
        # (~50ms). Awaiting all that before the done frame meant the user
        # sat 2-4s after the last token before seeing "complete". Now: yield
        # done immediately, persist in background. Tradeoff: if the worker
        # process dies in <500ms after yield, the assistant message is lost.
        # Acceptable — happens essentially never.
        _fire_and_forget(
            self._save_assistant_message(
                conversation_id,
                assistant_content,
                assistant_thinking if assistant_thinking else None,
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
            )
        )

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
        "balanced": {"retrieval_k": 40, "rerank_enabled": True, "hyde_enabled": False},
        "thorough": {"retrieval_k": 60, "rerank_enabled": True, "hyde_enabled": True},
    }

    async def _resolve_query_profile(
        self, request: ChatRequest, user_id: str | None = None
    ) -> dict:
        """
        Resolve Query Profile into eight concrete knobs. Returns a dict:
          retrieval_k, rerank_enabled, hyde_enabled,
          top_k_summary, rerank_top_n, similarity_threshold,
          neo4j_expansion_cap, max_corpora_per_query

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
            "query_profile": profile_key,
            **extras,
        }

    async def _apply_hyde(
        self, request: ChatRequest, user_id: str | None = None
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
    ) -> ChatMessage:
        """Saves the assistant's final message to the database."""
        assistant_message = ChatMessage(
            role="assistant",
            content=content,
            thinking=thinking,
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

        await conversation_service.append_message(
            str(conversation_id), assistant_message
        )
        return assistant_message

    async def _load_tools(self, request: ChatRequest) -> tuple[list, list[dict]]:
        """Load tools and their schemas if any are selected.

        Phase 24 perf — when process_chat_request already fetched the tools
        in parallel with skills (for the trust-signal name list), it stashes
        the result on `request._tools_preloaded`. We reuse it here instead
        of issuing a duplicate Mongo round-trip.
        """
        if not request.selected_tools:
            return [], []

        preloaded = getattr(request, "_tools_preloaded", None)
        tools = preloaded if preloaded is not None else (
            await tool_registry.get_tools_by_ids(request.selected_tools)
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
        return tools, tool_schemas

    async def _execute_tools(self, tool_calls: list, tools: list) -> list[str]:
        """Execute tool calls and return results."""
        results = []
        for call in tool_calls:
            tool_name = call.get("function", {}).get("name")
            tool = next((t for t in tools if t.name == tool_name), None)
            if not tool:
                results.append(f"Error: Tool '{tool_name}' not found.")
                continue

            try:
                args = json.loads(call.get("function", {}).get("arguments", "{}"))
                result = tool_registry.execute_tool(tool.code, tool.name, args)
                results.append(str(result))
            except Exception as e:
                results.append(f"Error executing tool {tool_name}: {e}")
        return results


# Global instance
chat_orchestrator = ChatOrchestrator()
