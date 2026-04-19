# backend/services/chat_orchestrator.py
# Chat orchestrator service - moves business logic from router to service layer
# Orchestrates: conversation loading, message creation, trimming, LLM streaming, saving
# All functions are async. Import: from services.chat_orchestrator import chat_orchestrator

import json
import logging
from datetime import datetime
from typing import AsyncGenerator

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
from utils.streaming import build_sse_chunk
from utils.tokens import count_tokens

logger = logging.getLogger(__name__)
settings = get_settings()


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
            _resolved: dict | None = None
            if prefix == "profile":
                from services.model_profiles import model_profiles_service
                _resolved = await model_profiles_service.get_resolved(user_id, _id)
            else:  # pool
                from services.model_pool import model_pool_service
                _resolved = await model_pool_service.get_resolved(user_id, _id)

            if _resolved:
                profile_creds = {
                    "api_base": _resolved["base_url"],
                    "api_key": _resolved["api_key"],
                    "extra_params": _resolved["extra_params"] or None,
                }
                model_used = f"openai/{_resolved['model_name']}"
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
            from services.query_model_resolver import resolve as _resolve_query_model

            kind = "agentic" if agentic_on_request else "query"
            qres = await _resolve_query_model(user_id, kind)
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

        # Resolve Query Profile (Phase 18) — preset bundles retrieval_k + rerank + HyDE.
        # Individual overrides on ModelOverrides still win.
        profile_k, profile_rerank, profile_hyde = self._resolve_query_profile(request)

        # Phase 17 — HyDE: when enabled, generate a hypothetical answer and
        # use IT as the retrieval query. Answers tend to embed closer to
        # answer-shaped chunks than questions do. Graceful fallback on failure.
        retrieval_query = await self._apply_hyde(request, user_id=user_id)

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
            )
        sources = retrieval.chunks

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

            user_message.content = context_manager.build_augmented_prompt(
                query=user_message.content,
                sources=sources,
                corpus_ids=request.corpus_ids,
                reasoning_mode=reasoning_mode,
                reasoning_blend=reasoning_blend,
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

        # Save user message before starting loop
        await conversation_service.append_message(conversation_id, user_message)

        while tool_call_count < MAX_TOOL_CALLS:
            # Convert messages to dict format for LLM
            message_dicts = [
                {"role": msg.role, "content": msg.content} for msg in trimmed_messages
            ]

            assistant_content = ""
            assistant_thinking = ""
            tool_calls = []

            # Step 7: Stream LLM response
            try:
                async for chunk in llm_service.stream_chat(
                    messages=message_dicts,
                    model=model_used,
                    overrides=request.overrides,
                    tools=tool_schemas,
                    **profile_creds,
                ):
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

        # Step 8: Save assistant message
        await self._save_assistant_message(
            conversation_id,
            assistant_content,
            assistant_thinking if assistant_thinking else None,
            model_used,
            trimming_applied,
        )

        # Step 9: Send completion chunk
        yield build_sse_chunk(
            ChatChunk(
                type="done",
                conversation_id=str(conversation_id),
                model_used=model_used,
            )
        )

        # Log completion
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        logger.info(
            f"Chat completed for conversation {conversation_id}, "
            f"model={model_used}, elapsed={elapsed:.2f}s"
        )

    # ── Phase 18 Query Profile ──────────────────────────────────────────────

    # Preset defaults. Profile is a speed preset that bundles retrieval_k +
    # rerank + HyDE. Individual overrides on ModelOverrides take precedence.
    _QUERY_PROFILE_PRESETS: dict[str, dict] = {
        "fast": {"retrieval_k": 10, "rerank_enabled": False, "hyde_enabled": False},
        "balanced": {"retrieval_k": 40, "rerank_enabled": True, "hyde_enabled": False},
        "thorough": {"retrieval_k": 60, "rerank_enabled": True, "hyde_enabled": True},
    }

    def _resolve_query_profile(
        self, request: ChatRequest
    ) -> tuple[int | None, bool, bool]:
        """
        Resolve Query Profile into three concrete knobs:
          (retrieval_k, rerank_enabled, hyde_enabled)

        Priority per knob:
          1. explicit per-request override on ModelOverrides
          2. profile preset (from overrides.query_profile or server default)
          3. None/True/False from the preset dict
        """
        overrides = request.overrides
        # Step 1 — which profile? per-request > server default (resolved at frontend load)
        profile_key = (
            overrides.query_profile if overrides and overrides.query_profile else "balanced"
        )
        preset = self._QUERY_PROFILE_PRESETS.get(
            profile_key, self._QUERY_PROFILE_PRESETS["balanced"]
        )

        # Step 2 — individual knob resolution
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
        # HyDE: the existing ModelOverrides.hyde_enabled wins; profile provides default
        hyde_enabled = (
            overrides.hyde_enabled
            if (overrides and overrides.hyde_enabled is not None)
            else preset["hyde_enabled"]
        )

        # Profile-driven HyDE is picked up by _apply_hyde via overrides (below).
        # We mutate the request.overrides so _apply_hyde sees the preset decision
        # without needing its own resolver.
        if request.overrides is None:
            request.overrides = ModelOverrides()
        if request.overrides.hyde_enabled is None:
            request.overrides.hyde_enabled = hyde_enabled

        return retrieval_k, bool(rerank_enabled), bool(hyde_enabled)

    async def _apply_hyde(
        self, request: ChatRequest, user_id: str | None = None
    ) -> str:
        """
        Phase 17 — Hypothetical Document Embeddings.

        When `overrides.hyde_enabled` is True, call a small/fast LLM to write
        a 2-3 sentence hypothetical answer to the user's question. Return
        that text (which will be embedded for Qdrant search) instead of the
        raw query. Questions embed poorly; answers embed close to real
        answer-shaped chunks.

        Model resolution order:
          1. request.overrides.hyde_model (per-request)
          2. Phase F — user query prefs `hyde_pool_id` → pool entry creds
          3. settings.HYDE_MODEL (server default)

        On any failure (LLM down, malformed response), log a warning and
        fall back to the original query.
        """
        overrides = request.overrides
        if not (overrides and overrides.hyde_enabled):
            return request.message

        hyde_model = (overrides.hyde_model if overrides else None) or settings.HYDE_MODEL
        hyde_api_base: str | None = None
        hyde_api_key: str | None = None
        hyde_extra: dict | None = None

        # Phase F — only consult prefs when no per-request override given.
        if user_id and not (overrides and overrides.hyde_model):
            from services.query_model_resolver import resolve as _resolve_query_model

            qres = await _resolve_query_model(user_id, "hyde")
            if qres:
                hyde_model = qres["model"]
                hyde_api_base = qres["api_base"]
                hyde_api_key = qres["api_key"]
                hyde_extra = qres["extra_params"] or None
                logger.info(
                    "HyDE — Phase F prefs resolution: user=%s → %s",
                    user_id, hyde_model,
                )

        prompt = (
            "Write a concise, plausible 2-3 sentence answer to this question "
            "as if you already knew the answer. Focus on style and structure "
            "over accuracy — we'll search for the real sources after. Do not "
            "preface with 'The answer is' or similar; just write the answer.\n\n"
            f"Question: {request.message}"
        )
        try:
            hypothetical = await llm_service.complete_sync(
                messages=[{"role": "user", "content": prompt}],
                model=hyde_model,
                temperature=0.3,
                max_tokens=512,
                api_base=hyde_api_base,
                api_key=hyde_api_key,
                extra_params=hyde_extra,
            )
            hypothetical = (hypothetical or "").strip()
            if not hypothetical:
                logger.warning("HyDE returned empty output — using raw query")
                return request.message

            logger.info(
                "HyDE active [model=%s]: query='%s' → hypothesis='%s'",
                hyde_model,
                request.message[:80],
                hypothetical[:120],
            )
            return hypothetical
        except Exception as exc:
            logger.warning(
                "HyDE call failed (%s) — falling back to raw query", exc
            )
            return request.message

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

        # Conversations created under an older release may carry the legacy
        # hardcoded default (`ollama/llama3.2:3b`) even when that model isn't
        # pulled locally. Treat the legacy default as "unset" and fall through
        # to DEFAULT_COMPLETION_MODEL from env.
        stored = (model_config.model or "").strip()
        if not stored or stored == "ollama/llama3.2:3b":
            return settings.DEFAULT_COMPLETION_MODEL
        return stored

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
        )

        await conversation_service.append_message(
            str(conversation_id), assistant_message
        )
        return assistant_message

    async def _load_tools(self, request: ChatRequest) -> tuple[list, list[dict]]:
        """Load tools and their schemas if agentic mode is enabled."""
        if not request.selected_tools:
            return [], []

        tools = await tool_registry.get_tools_by_ids(request.selected_tools)
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
