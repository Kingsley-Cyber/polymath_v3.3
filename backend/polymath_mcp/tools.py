"""Polymath MCP tool surface — Phase 8.2+.

Thin adapters over backend.services.* — zero duplicate retrieval/graph/chat
business logic. Per CLAUDE.md "one retrieval implementation, multiple
transports."

Each tool:
  1. Validates args (Pydantic-style via type hints).
  2. Resolves user_id → allowed_corpus_ids (services.storage.mongo_reader).
  3. Filters corpus_ids against allowed set (silent drop on disallowed).
  4. Calls the underlying service.
  5. Returns an MCP-shaped JSON response.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from config import get_settings
from models.schemas import ChatRequest, GraphQueryRequest, ModelOverrides, RetrievalTier
from services.graph import neo4j_reader
from services.ingestion.admission import (
    release_ingest_slot as _release_ingest_slot,
    try_acquire_ingest_slot as _try_acquire_ingest_slot,
)
from services.ingestion_service import ingestion_service
from services.retriever import retriever_orchestrator

from .auth import (
    AuthError,
    assert_corpus_allowed,
    allowed_corpus_ids,
    get_current_user_id,
    resolve_request_scope,
)
from .app_guide import get_app_guide

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────


def _json_ready(value: Any) -> Any:
    """Convert Pydantic/dataclass/datetime values into plain JSON-ish data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return value.model_dump()
    if dataclasses.is_dataclass(value):
        return _json_ready(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(v) for v in value]
    return str(value)


def _trim_items(items: Any, limit: int) -> list[Any]:
    if not isinstance(items, list):
        return []
    safe_limit = max(1, min(int(limit or 8), 50))
    return [_json_ready(item) for item in items[:safe_limit]]


def _field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


async def _scope_corpus_ids(
    corpus_ids: list[str] | None,
    *,
    default_to_all: bool = False,
) -> list[str]:
    """Resolve requested corpus ids through MCP auth, optionally defaulting to all."""
    if corpus_ids is None and default_to_all:
        user_id = get_current_user_id()
        if user_id is None and get_settings().MCP_REQUIRE_AUTH:
            raise AuthError("MCP request missing valid authentication")
        allowed = await allowed_corpus_ids(user_id)
        return sorted(allowed)
    return await resolve_request_scope(corpus_ids)


def _neo4j_or_error() -> Any:
    """Return the Neo4j AsyncDriver or raise — graph tools require it."""
    settings = get_settings()
    if not settings.NEO4J_ENABLED:
        raise RuntimeError(
            "Neo4j is not enabled on this Polymath instance. "
            "Graph tools (polymath_get_chunk_extraction, polymath_search_entities, "
            "polymath_get_entity_relations) require NEO4J_ENABLED=true."
        )
    driver = ingestion_service.neo4j_driver
    if driver is None:
        raise RuntimeError(
            "Neo4j driver is not initialized. Backend lifespan may not have "
            "completed. Retry shortly."
        )
    return driver


# ── Search tools ───────────────────────────────────────────────────────────


async def _run_search(
    *,
    query: str,
    corpus_ids: list[str] | None,
    retrieval_tier: Literal[
        "qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"
    ],
    top_k: int | None,
    default_to_all: bool,
    retrieval_k: int | None = None,
    rerank_enabled: bool = True,
    top_k_summary: int | None = None,
    rerank_top_n: int | None = None,
    similarity_threshold: float | None = None,
    neo4j_expansion_cap: int | None = None,
    max_corpora_per_query: int | None = None,
    final_top_k: int | None = None,
    fact_seed_limit: int | None = None,
    search_mode: Literal["local", "global", "auto"] = "local",
) -> dict[str, Any]:
    settings = get_settings()
    scoped = await _scope_corpus_ids(corpus_ids, default_to_all=default_to_all)
    if not scoped:
        return {
            "chunks": [],
            "corpus_ids": [],
            "requested_tier": retrieval_tier,
            "effective_tier": retrieval_tier,
            "downgrade_reason": "no accessible corpora in request",
        }

    tier = RetrievalTier(retrieval_tier)
    # `top_k` is the legacy MCP-facing result cap. The retriever's
    # `retrieval_k` is a pre-rerank pool size, so keep them separate for the
    # newer coverage/facet-aware retrieval stack.
    result_cap = final_top_k if final_top_k is not None else (
        top_k if top_k is not None else settings.MCP_DEFAULT_TOP_K
    )
    effective_search_mode = search_mode
    if search_mode == "auto":
        try:
            from services.retriever.search_mode import resolve_search_mode

            effective_search_mode = str(resolve_search_mode("auto", query))
        except Exception as exc:
            logger.debug("MCP search_mode auto fallback to local: %s", exc)
            effective_search_mode = "local"

    result = await retriever_orchestrator.retrieve(
        query=query,
        corpus_ids=scoped,
        retrieval_tier=tier,
        collections=None,
        retrieval_k=retrieval_k,
        rerank_enabled=rerank_enabled,
        top_k_summary=top_k_summary,
        rerank_top_n=rerank_top_n,
        similarity_threshold=similarity_threshold,
        neo4j_expansion_cap=neo4j_expansion_cap,
        max_corpora_per_query=max_corpora_per_query,
        final_top_k=result_cap,
        fact_seed_limit=fact_seed_limit,
        search_mode=effective_search_mode,
    )
    return {
        "chunks": [_json_ready(c) for c in result.chunks],
        "corpus_ids": scoped,
        "requested_tier": _json_ready(result.requested_tier),
        "effective_tier": _json_ready(result.effective_tier),
        "downgrade_reason": result.downgrade_reason,
        "retrieval": {
            "search_mode": effective_search_mode,
            "requested_search_mode": search_mode,
            "result_cap": result_cap,
            "retrieval_k": retrieval_k,
            "rerank_enabled": rerank_enabled,
            "top_k_summary": top_k_summary,
            "rerank_top_n": rerank_top_n,
            "similarity_threshold": similarity_threshold,
            "neo4j_expansion_cap": neo4j_expansion_cap,
            "max_corpora_per_query": max_corpora_per_query,
            "fact_seed_limit": fact_seed_limit,
        },
    }


async def polymath_search(
    query: str,
    corpus_ids: list[str] | None = None,
    retrieval_tier: Literal[
        "qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"
    ] = "qdrant_mongo",
    top_k: int | None = None,
    retrieval_k: int | None = None,
    rerank_enabled: bool = True,
    top_k_summary: int | None = None,
    rerank_top_n: int | None = None,
    similarity_threshold: float | None = None,
    neo4j_expansion_cap: int | None = None,
    max_corpora_per_query: int | None = None,
    final_top_k: int | None = None,
    fact_seed_limit: int | None = None,
    search_mode: Literal["local", "global", "auto"] = "local",
) -> dict[str, Any]:
    """Search Polymath corpora and return ranked chunks.

    Delegates to services.retriever.retriever_orchestrator.retrieve — the same
    code path used by POST /api/chat. Same query + same corpus + same tier
    yields identical results across MCP and HTTP.

    Args:
        query: Natural-language search query.
        corpus_ids: Corpora to search. Each is silently dropped if the
            authenticated user lacks access. If omitted, searches every corpus
            visible to the MCP caller.
        retrieval_tier: 'qdrant_only' (raw vectors), 'qdrant_mongo' (default;
            vectors + parent hydration), or 'qdrant_mongo_graph' (adds Mode A
            graph expansion; requires use_neo4j=True on every selected corpus).
        top_k: Legacy result cap alias. Prefer final_top_k for new clients.
        retrieval_k: Pre-rerank candidate pool size.
        rerank_enabled: Disable the reranker when False.
        top_k_summary: Summary-vector gather budget.
        rerank_top_n: Candidate pool cap before rerank.
        similarity_threshold: Optional score floor.
        neo4j_expansion_cap: Graph Augmentation expansion cap.
        max_corpora_per_query: Corpus breadth cap for retrieval.
        final_top_k: Final result cap after rerank/diversity selection.
        fact_seed_limit: Graph fact seed budget.
        search_mode: local | global | auto.

    Returns:
        {
          "chunks": [{chunk_id, doc_id, corpus_id, parent_id, text, score, ...}],
          "corpus_ids": ["..."],
          "requested_tier": "...",
          "effective_tier": "...",
          "downgrade_reason": "..." | null
        }
    """
    return await _run_search(
        query=query,
        corpus_ids=corpus_ids,
        retrieval_tier=retrieval_tier,
        top_k=top_k,
        default_to_all=True,
        retrieval_k=retrieval_k,
        rerank_enabled=rerank_enabled,
        top_k_summary=top_k_summary,
        rerank_top_n=rerank_top_n,
        similarity_threshold=similarity_threshold,
        neo4j_expansion_cap=neo4j_expansion_cap,
        max_corpora_per_query=max_corpora_per_query,
        final_top_k=final_top_k,
        fact_seed_limit=fact_seed_limit,
        search_mode=search_mode,
    )


async def polymath_cross_corpus_search(
    query: str,
    corpus_ids: list[str] | None = None,
    retrieval_tier: Literal[
        "qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"
    ] = "qdrant_mongo",
    top_k: int | None = None,
    retrieval_k: int | None = None,
    rerank_enabled: bool = True,
    top_k_summary: int | None = None,
    rerank_top_n: int | None = None,
    similarity_threshold: float | None = None,
    neo4j_expansion_cap: int | None = None,
    max_corpora_per_query: int | None = None,
    final_top_k: int | None = None,
    fact_seed_limit: int | None = None,
    search_mode: Literal["local", "global", "auto"] = "local",
) -> dict[str, Any]:
    """Explicit cross-corpus retrieval tool for agents.

    Same backend path as polymath_search and /api/chat retrieval. If
    `corpus_ids` is omitted, searches every corpus visible to the MCP caller.
    Use this when an agent wants evidence chunks before deciding whether to
    ask chat or graph synthesis.
    """
    return await _run_search(
        query=query,
        corpus_ids=corpus_ids,
        retrieval_tier=retrieval_tier,
        top_k=top_k,
        default_to_all=True,
        retrieval_k=retrieval_k,
        rerank_enabled=rerank_enabled,
        top_k_summary=top_k_summary,
        rerank_top_n=rerank_top_n,
        similarity_threshold=similarity_threshold,
        neo4j_expansion_cap=neo4j_expansion_cap,
        max_corpora_per_query=max_corpora_per_query,
        final_top_k=final_top_k,
        fact_seed_limit=fact_seed_limit,
        search_mode=search_mode,
    )


async def polymath_chat_query(
    message: str,
    corpus_ids: list[str] | None = None,
    retrieval_tier: Literal[
        "qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"
    ] = "qdrant_mongo",
    conversation_id: str | None = None,
    model: str | None = None,
    query_profile: Literal["fast", "balanced", "thorough", "custom"] | None = None,
    reasoning_mode: str | None = None,
    reasoning_blend: list[str] | None = None,
    reasoning_cascade: bool | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    hyde_enabled: bool | None = None,
    hyde_model: str | None = None,
    rerank_enabled: bool | None = None,
    retrieval_k: int | None = None,
    top_k_summary: int | None = None,
    rerank_top_n: int | None = None,
    similarity_threshold: float | None = None,
    neo4j_expansion_cap: int | None = None,
    max_corpora_per_query: int | None = None,
    final_top_k: int | None = None,
    fact_seed_limit: int | None = None,
    search_mode: Literal["auto", "local", "global"] | None = None,
    thinking_effort: Literal["none", "low", "medium", "high", "auto"] | None = None,
    web_search_enabled: bool | None = None,
    web_fetch_depth: Literal["snippets", "normal", "deep"] | None = None,
    web_research_mode: bool | None = None,
    web_youtube_transcripts: bool | None = None,
    web_max_sources: int | None = None,
    selected_tools: list[str] | None = None,
    active_skill_ids: list[str] | None = None,
    max_sources: int = 8,
) -> dict[str, Any]:
    """Ask Polymath chat from MCP and return the final non-streamed answer.

    This uses services.chat_orchestrator, the same path as POST /api/chat. It
    streams internally, captures tokens/sources/trust signals, and returns one
    compact JSON object to the MCP caller.

    Args:
        message: Natural-language question or instruction.
        corpus_ids: Optional corpora to ground the answer. MCP now follows the
            live chat API's retrieval settings instead of imposing an old
            three-corpus sidecar cap.
        retrieval_tier: Retrieval route used by chat.
        conversation_id: Optional existing chat conversation id.
        model: Optional concrete model/pool/profile override.
        query_profile: fast | balanced | thorough | custom.
        reasoning_mode: Optional Polymath reasoning mode.
        reasoning_blend: Optional list of raw reasoning modes to blend.
        reasoning_cascade: Run the optional evidence pre-digest when true.
        temperature: Optional model temperature.
        max_tokens: Optional output cap.
        hyde_enabled: Enable/disable HyDE.
        hyde_model: Optional HyDE model override.
        rerank_enabled: Enable/disable reranker.
        retrieval_k: Pre-rerank pool size.
        top_k_summary: Summary-vector gather budget.
        rerank_top_n: Candidate pool cap before rerank.
        similarity_threshold: Optional retrieval score floor.
        neo4j_expansion_cap: Graph Augmentation expansion cap.
        max_corpora_per_query: Retrieval breadth cap.
        final_top_k: Final chunks sent to the LLM.
        fact_seed_limit: Graph fact seed budget.
        search_mode: auto | local | global.
        thinking_effort: Provider-native thinking/reasoning effort dial.
        web_search_enabled: Enable live web search tool path for the turn.
        web_fetch_depth: snippets | normal | deep.
        web_research_mode: Expand bounded web budgets when true.
        web_youtube_transcripts: Allow YouTube transcript evidence.
        web_max_sources: Maximum requested web sources.
        selected_tools: Tool IDs to enable for agentic chat.
        active_skill_ids: Skill IDs to inject into the prompt.
        max_sources: Number of source previews to return.
    """
    from services.chat_orchestrator import chat_orchestrator

    scoped = await _scope_corpus_ids(corpus_ids, default_to_all=False)
    if corpus_ids and not scoped:
        return {
            "status": "error",
            "error": "no accessible corpora in request",
            "answer": "",
            "sources": [],
        }

    override_data = {
        "model": model,
        "query_profile": query_profile,
        "reasoning_mode": reasoning_mode,
        "reasoning_blend": reasoning_blend,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "hyde_enabled": hyde_enabled,
        "hyde_model": hyde_model,
        "rerank_enabled": rerank_enabled,
        "retrieval_k": retrieval_k,
        "top_k_summary": top_k_summary,
        "rerank_top_n": rerank_top_n,
        "similarity_threshold": similarity_threshold,
        "neo4j_expansion_cap": neo4j_expansion_cap,
        "max_corpora_per_query": max_corpora_per_query,
        "final_top_k": final_top_k,
        "fact_seed_limit": fact_seed_limit,
        "search_mode": search_mode,
        "thinking_effort": thinking_effort,
        "web_search_enabled": web_search_enabled,
        "web_fetch_depth": web_fetch_depth,
        "web_research_mode": web_research_mode,
        "web_youtube_transcripts": web_youtube_transcripts,
        "web_max_sources": web_max_sources,
    }
    overrides = (
        ModelOverrides(**{k: v for k, v in override_data.items() if v is not None})
        if any(v is not None for v in override_data.values())
        else None
    )
    request = ChatRequest(
        conversation_id=conversation_id,
        message=message,
        corpus_ids=scoped or None,
        retrieval_tier=RetrievalTier(retrieval_tier),
        overrides=overrides,
        selected_tools=selected_tools,
        active_skill_ids=active_skill_ids,
        reasoning_cascade=reasoning_cascade,
    )

    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    trace_events: list[dict[str, Any]] = []
    done: dict[str, Any] = {}
    user_id = get_current_user_id()

    async for raw_event in chat_orchestrator.process_chat_request(
        request,
        user_id=user_id,
    ):
        for line in raw_event.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                signals.append({"type": "unparsed", "content": payload[:500]})
                continue
            event_type = event.get("type")
            if event_type == "token":
                answer_parts.append(event.get("content") or "")
            elif event_type == "thinking":
                thinking_parts.append(event.get("thinking") or "")
            elif event_type == "sources":
                sources = _trim_items(event.get("sources") or [], max_sources)
            elif event_type == "trace_event":
                trace_events.append(
                    _json_ready(event.get("trace_event") or event)
                )
            elif event_type == "done":
                done = _json_ready(event)
            elif event_type == "error":
                return {
                    "status": "error",
                    "error": event.get("content") or "chat query failed",
                    "answer": "".join(answer_parts).strip(),
                    "sources": sources,
                    "signals": signals,
                    "trace_events": trace_events,
                }
            else:
                signals.append(_json_ready(event))

    return {
        "status": "ok",
        "answer": "".join(answer_parts).strip(),
        "thinking": "".join(thinking_parts).strip() or None,
        "sources": sources,
        "signals": signals,
        "trace_events": trace_events,
        "conversation_id": done.get("conversation_id"),
        "model_used": done.get("model_used"),
        "chunks_returned": done.get("chunks_returned"),
        "strategy_used": done.get("strategy_used"),
        "query_profile_used": done.get("query_profile_used"),
        "reasoning_mode_used": done.get("reasoning_mode_used"),
        "hyde_applied": done.get("hyde_applied"),
        "agentic_mode_used": done.get("agentic_mode_used"),
        "tools_used": done.get("tools_used"),
        "skills_used": done.get("skills_used"),
        "reasoning_cascade_applied": done.get("reasoning_cascade_applied"),
        "downgrade_reason": done.get("downgrade_reason"),
        "corpus_ids": scoped,
    }


def _compact_graph_result(
    result: Any,
    *,
    include_graph: bool,
    include_trace: bool,
    max_items: int,
) -> dict[str, Any]:
    trace = _json_ready(_field(result, "trace", {}) or {})
    auto_synthesis = _json_ready(_field(result, "auto_synthesis", {}) or {})
    context_graph = _json_ready(_field(result, "context_graph", {}) or {})
    if not isinstance(trace, dict):
        trace = {}
    if not isinstance(auto_synthesis, dict):
        auto_synthesis = {}
    if not isinstance(context_graph, dict):
        context_graph = {}
    source_docs = (
        trace.get("source_docs")
        or trace.get("source_docs_raw")
        or trace.get("graph_scope_source_docs")
        or []
    )
    payload: dict[str, Any] = {
        "status": "ok",
        "session_id": _field(result, "session_id", ""),
        "corpus_id": _field(result, "corpus_id", ""),
        "corpus_ids": _json_ready(
            _field(result, "corpus_ids", None)
            or ([_field(result, "corpus_id", "")] if _field(result, "corpus_id", "") else [])
        ),
        "query": _field(result, "query", ""),
        "mode": _field(result, "mode", "auto"),
        "headline": _json_ready(_field(result, "headline", None)),
        "summary_markdown": auto_synthesis.get("markdown")
        or _field(result, "interpretation", ""),
        "themes": _trim_items(_field(result, "themes", []) or [], max_items),
        "bridges": _trim_items(
            _field(result, "bridges_v2", None) or _field(result, "bridges", []) or [],
            max_items,
        ),
        "gaps": _trim_items(
            _field(result, "gaps_v2", None) or _field(result, "questions", []) or [],
            max_items,
        ),
        "latent_topics": _trim_items(
            _field(result, "latent_topics", []) or [],
            max_items,
        ),
        "tensions": _trim_items(_field(result, "tensions", []) or [], max_items),
        "metrics": _json_ready(_field(result, "metrics", {}) or {}),
        "source_docs": _trim_items(source_docs, max_items),
        "insight_packet_summary": _json_ready(
            _field(result, "insight_packet_summary", {}) or {}
        ),
        "context_graph_preview": {
            "nodes": _trim_items(context_graph.get("nodes") or [], max_items),
            "links": _trim_items(context_graph.get("links") or [], max_items),
            "meta": _json_ready(context_graph.get("meta") or {}),
        },
        "trace_summary": {
            "anchor_terms": trace.get("anchor_terms") or [],
            "latent_terms": trace.get("latent_terms") or [],
            "stages": _trim_items(trace.get("stages") or [], max_items),
            "llm_context": _json_ready(trace.get("llm_context") or {}),
        },
    }
    if include_graph:
        payload["graph"] = _json_ready(_field(result, "graph", {}) or {})
        payload["context_graph"] = context_graph
    if include_trace:
        payload["trace"] = trace
    return payload


async def polymath_graph_query(
    query: str,
    corpus_id: str | None = None,
    corpus_ids: list[str] | None = None,
    mode: Literal["auto", "connect", "gaps", "themes"] = "auto",
    synthesis_mode: Literal["research", "nuance", "ideation", "gap"] = "research",
    validate_synthesis: bool = False,
    web_search_enabled: bool = False,
    web_fetch_depth: Literal["snippets", "normal", "deep"] = "normal",
    web_max_results: int = 5,
    session_id: str | None = None,
    model: str | None = None,
    agentic: bool = False,
    include_graph: bool = False,
    include_trace: bool = False,
    max_items: int = 8,
) -> dict[str, Any]:
    """Run Polymath Mission Control graph synthesis from MCP.

    This is the same service path as POST /api/graph/discover. It is
    corpus-scoped and evidence-aware: the output is a compact synthesis packet
    by default, with optional full graph/trace payloads when an agent needs
    deeper traversal context.

    Args:
        corpus_id: Legacy single corpus to analyze.
        corpus_ids: Preferred multi-corpus scope.
        query: Research question or concept neighborhood to explore.
        mode: Compatibility field; graph discovery currently behaves as auto.
        synthesis_mode: research | nuance | ideation. Mirrors the Graph Query
            #1 tabs in the app.
        validate_synthesis: Run the optional critique/revise loop.
        web_search_enabled: Add bounded live-web grounding before synthesis.
        web_fetch_depth: snippets | normal | deep for live-web page fetching.
        web_max_results: Maximum reranked live-web sources to add.
        session_id: Optional existing Mission Control session id.
        model: Optional synthesis model override.
        agentic: Enable agentic graph retrieval scouting.
        include_graph: Include full graph payload.
        include_trace: Include full trace/evidence diagnostics.
        max_items: Compact-list cap for previews.
    """
    requested_ids = list(corpus_ids or [])
    if not requested_ids and corpus_id:
        requested_ids = [corpus_id]
    scoped = await _scope_corpus_ids(requested_ids, default_to_all=False)
    if not scoped:
        return {"status": "error", "error": "no accessible corpora in request"}
    qdrant = ingestion_service.qdrant_client
    if qdrant is None:
        return {"status": "error", "error": "Qdrant is not connected"}
    db = ingestion_service.db
    if db is None:
        return {"status": "error", "error": "MongoDB is not connected"}

    from services.graph.orchestrator import discover

    try:
        result = await discover(
            qdrant=qdrant,
            neo4j_driver=ingestion_service.neo4j_driver,
            db=db,
            corpus_ids=scoped,
            query=query,
            mode=mode,
            synthesis_mode=synthesis_mode,
            validate_synthesis=validate_synthesis,
            web_search_enabled=web_search_enabled,
            web_fetch_depth=web_fetch_depth,
            web_max_results=web_max_results,
            session_id=session_id,
            user_id=get_current_user_id(),
            model_override=model,
            agentic=agentic,
        )
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        logger.exception("MCP graph query failed: %s", exc)
        return {
            "status": "error",
            "error": "Mission Control synthesis failed",
            "detail": str(exc),
        }
    return _compact_graph_result(
        result,
        include_graph=include_graph,
        include_trace=include_trace,
        max_items=max_items,
    ) | {"synthesis_mode": synthesis_mode}


async def polymath_graph_map_query(
    query: str,
    corpus_id: str | None = None,
    corpus_ids: list[str] | None = None,
    max_hops: int = 2,
    limit: int = 80,
    seed_limit_per_token: int = 3,
    max_items: int = 200,
) -> dict[str, Any]:
    """Run the lightweight graph-map query used by the canvas.

    This exposes the `/api/graph/query` behavior to MCP clients: seed entity
    extraction, hop traversal, edges, bridges, hubs, and gaps. It does not run
    synthesis. Use polymath_graph_query when you want the research / nuance /
    ideation packet and LLM answer.

    Args:
        query: Natural-language graph query.
        corpus_id: Legacy single corpus scope.
        corpus_ids: Preferred multi-corpus scope.
        max_hops: Entity traversal depth from seeds (1-3).
        limit: Per-corpus node limit.
        seed_limit_per_token: Entity seed budget per query token.
        max_items: Response trim cap for MCP payload size.
    """
    requested_ids = list(corpus_ids or [])
    if not requested_ids and corpus_id:
        requested_ids = [corpus_id]
    scoped = await _scope_corpus_ids(requested_ids, default_to_all=False)
    if not scoped:
        return {"status": "error", "error": "no accessible corpora in request"}

    body = GraphQueryRequest(
        corpus_ids=scoped,
        query=query,
        max_hops=max_hops,
        limit=limit,
        seed_limit_per_token=seed_limit_per_token,
    )
    driver = _neo4j_or_error()
    qdrant = ingestion_service.qdrant_client

    from services.graph.graph_query import (
        expand_subgraph,
        extract_query_entities,
        find_bridges,
        find_gaps,
        find_hubs,
    )

    db = ingestion_service.db
    corpus_metrics_map: dict[str, Any] = {}
    if db is not None:
        try:
            from services.graph.analytics import (
                compute_corpus_change_signature,
                get_cached_metrics,
            )

            for cid in scoped:
                try:
                    sig = await compute_corpus_change_signature(db, cid)
                    metrics = await get_cached_metrics(db, cid, sig)
                    if metrics is not None:
                        corpus_metrics_map[cid] = metrics
                except Exception as exc:
                    logger.debug("MCP graph map metrics lookup skipped for %s: %s", cid, exc)
        except Exception as exc:
            logger.debug("MCP graph map analytics cache unavailable: %s", exc)

    async def _run_one(cid: str) -> tuple[str, dict[str, Any]]:
        seeds = await extract_query_entities(
            body.query,
            cid,
            driver,
            limit_per_token=body.seed_limit_per_token,
            qdrant=qdrant,
        )
        if not seeds:
            return cid, {"nodes": [], "links": [], "bridges": [], "gaps": [], "seeds": []}

        seed_ids = [s["entity_id"] for s in seeds]
        seed_scores = {
            s["entity_id"]: float(s.get("score") or 0.0)
            for s in seeds
            if s.get("entity_id")
        }
        metrics = corpus_metrics_map.get(cid)
        subgraph = await expand_subgraph(
            entity_ids=seed_ids,
            corpus_id=cid,
            driver=driver,
            max_hops=body.max_hops,
            limit=body.limit,
            metrics=metrics,
            entity_scores=seed_scores,
        )
        bridges = await find_bridges(
            driver=driver,
            entity_ids=seed_ids,
            corpus_id=cid,
            max_hops=body.max_hops,
            metrics=metrics,
        )
        gaps = await find_gaps(driver=driver, entity_ids=seed_ids, metrics=metrics)
        return cid, {
            "nodes": subgraph["nodes"],
            "links": subgraph["links"],
            "bridges": bridges,
            "gaps": gaps,
            "seeds": seeds,
        }

    import asyncio as _asyncio

    sem = _asyncio.Semaphore(4)

    async def _gated(cid: str) -> tuple[str, dict[str, Any]]:
        async with sem:
            try:
                return await _run_one(cid)
            except Exception as exc:
                logger.warning(
                    "MCP graph map query: corpus=%s failed (%s); returning empty",
                    cid,
                    exc,
                )
                return cid, {"nodes": [], "links": [], "bridges": [], "gaps": [], "seeds": []}

    per_corpus = await _asyncio.gather(*[_gated(cid) for cid in scoped])

    merged_nodes: dict[str, dict[str, Any]] = {}
    merged_links: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    merged_bridges: dict[str, dict[str, Any]] = {}
    merged_gaps: list[dict[str, Any]] = []
    merged_seeds: dict[str, dict[str, Any]] = {}

    def _stamp(item: dict[str, Any], cid: str) -> dict[str, Any]:
        sc = list(item.get("source_corpora") or [])
        if cid and cid not in sc:
            sc.append(cid)
        item["source_corpora"] = sc
        item.setdefault("source_corpus", cid)
        return item

    for cid, payload in per_corpus:
        for node in payload["nodes"]:
            node_id = node.get("id")
            if not node_id:
                continue
            if node_id in merged_nodes:
                _stamp(merged_nodes[node_id], cid)
            else:
                merged_nodes[node_id] = _stamp(dict(node), cid)
        for link in payload["links"]:
            key = (link.get("source"), link.get("target"), link.get("predicate"))
            if key in merged_links:
                _stamp(merged_links[key], cid)
            else:
                merged_links[key] = _stamp(dict(link), cid)
        for bridge in payload["bridges"]:
            bridge_id = bridge.get("entity_id")
            if not bridge_id:
                continue
            if bridge_id in merged_bridges:
                _stamp(merged_bridges[bridge_id], cid)
                try:
                    merged_bridges[bridge_id]["connected_seed_count"] = (
                        int(merged_bridges[bridge_id].get("connected_seed_count") or 0)
                        + int(bridge.get("connected_seed_count") or 0)
                    )
                except Exception:
                    pass
            else:
                merged_bridges[bridge_id] = _stamp(dict(bridge), cid)
        for gap in payload["gaps"]:
            merged_gaps.append(_stamp(dict(gap), cid))
        for seed in payload["seeds"]:
            seed_id = seed.get("entity_id")
            if not seed_id:
                continue
            if seed_id in merged_seeds:
                _stamp(merged_seeds[seed_id], cid)
            else:
                merged_seeds[seed_id] = _stamp(dict(seed), cid)

    nodes = list(merged_nodes.values())
    links = list(merged_links.values())
    bridges = list(merged_bridges.values())

    merged_metrics = None
    if corpus_metrics_map:
        from types import SimpleNamespace

        merged_top_pr: dict[str, dict[str, Any]] = {}
        for metrics in corpus_metrics_map.values():
            for entry in getattr(metrics, "top_pagerank", None) or []:
                entity_id = entry.get("entity_id")
                if not entity_id:
                    continue
                current = merged_top_pr.get(entity_id)
                if current is None or float(entry.get("score", 0)) > float(
                    current.get("score", 0)
                ):
                    merged_top_pr[entity_id] = entry
        merged_metrics = SimpleNamespace(
            top_pagerank=sorted(
                merged_top_pr.values(),
                key=lambda e: float(e.get("score", 0)),
                reverse=True,
            )
        )

    hubs = find_hubs(nodes, links, metrics=merged_metrics)
    seed_entities = [
        {
            "id": seed["entity_id"],
            "display_name": seed.get("display_name", ""),
            "entity_type": seed.get("entity_type", "other"),
            "mention_count": seed.get("mention_count", 0),
            "is_seed": True,
            "source_corpora": seed.get("source_corpora") or [],
            "source_corpus": seed.get("source_corpus"),
        }
        for seed in merged_seeds.values()
    ]

    cap = max(1, min(int(max_items or 200), 500))
    return {
        "status": "ok",
        "query": body.query,
        "corpus_ids": scoped,
        "nodes": _trim_items(nodes, cap),
        "links": _trim_items(links, cap),
        "seed_entities": _trim_items(seed_entities, cap),
        "bridges": _trim_items(bridges, cap),
        "hubs": _trim_items(hubs, cap),
        "gaps": _trim_items(merged_gaps, cap),
        "metrics": {
            "node_count": len(nodes),
            "link_count": len(links),
            "seed_count": len(seed_entities),
            "bridge_count": len(bridges),
            "hub_count": len(hubs),
            "gap_count": len(merged_gaps),
        },
    }


async def polymath_graph_question_suggestions(
    question: str,
    corpus_id: str | None = None,
    corpus_ids: list[str] | None = None,
    model: str | None = None,
    force_refresh: bool = False,
    include_contextual: bool = True,
) -> dict[str, Any]:
    """Refine a graph/RAG question and optionally build contextual suggestions.

    This is the MCP surface for the lighter question-builder path behind
    `/api/graph/refine`. When include_contextual is true, the result can include
    question buckets for rag, research, nuance, and ideation.
    """
    requested_ids = list(corpus_ids or [])
    if not requested_ids and corpus_id:
        requested_ids = [corpus_id]
    scoped = await _scope_corpus_ids(requested_ids, default_to_all=False)
    if not scoped:
        return {"status": "error", "error": "no accessible corpora in request"}

    db = ingestion_service.db
    if db is None:
        return {"status": "error", "error": "MongoDB is not connected"}
    if not question or not question.strip():
        return {"status": "error", "error": "question is required"}

    user_id = get_current_user_id()
    resolved_model = model.strip() if isinstance(model, str) and model.strip() else None
    api_base = None
    api_key = None
    extra_params = None
    try:
        from services.query_model_resolver import (
            resolve as resolve_query_model,
            resolve_by_entry_id,
        )

        if resolved_model and (
            resolved_model.startswith("pool:") or resolved_model.startswith("profile:")
        ):
            _prefix, _, entry_id = resolved_model.partition(":")
            resolved = await resolve_by_entry_id(user_id, entry_id)
            if resolved:
                resolved_model = resolved.get("model")
                api_base = resolved.get("api_base")
                api_key = resolved.get("api_key")
                extra_params = resolved.get("extra_params") or None
            else:
                resolved_model = None

        if not resolved_model:
            resolved = await resolve_query_model(user_id, "graph_query")
            if not resolved:
                resolved = await resolve_query_model(user_id, "query")
            if resolved:
                resolved_model = resolved.get("model")
                api_base = resolved.get("api_base")
                api_key = resolved.get("api_key")
                extra_params = resolved.get("extra_params") or None
    except Exception as exc:
        logger.warning("MCP graph question model resolution failed: %s", exc)
        resolved_model = None

    from services.query_refinement import ensure_cache_index, refine_query

    await ensure_cache_index(db)
    try:
        result = await refine_query(
            db=db,
            question=question.strip(),
            corpus_ids=scoped,
            model=resolved_model,
            api_base=api_base,
            api_key=api_key,
            extra_params=extra_params,
            force_refresh=force_refresh,
            neo4j_driver=ingestion_service.neo4j_driver,
            include_contextual=include_contextual,
        )
    except Exception as exc:
        logger.exception("MCP graph question suggestions failed: %s", exc)
        return {
            "status": "error",
            "error": "question refinement failed",
            "detail": str(exc),
        }
    return {"status": "ok", "corpus_ids": scoped, **_json_ready(result)}


# ── Tool 2: polymath_get_chunk_extraction ──────────────────────────────────


async def polymath_get_chunk_extraction(
    corpus_id: str,
    chunk_id: str,
) -> dict[str, Any]:
    """Return all entities and relations extracted from a single chunk.

    Delegates to services.graph.neo4j_reader.get_chunk_extraction.

    Args:
        corpus_id: Owning corpus. Must be in the user's allowed set.
        chunk_id: Chunk to fetch (chunks are scoped per-corpus).

    Returns:
        {"chunk_id": "...", "corpus_id": "...", "entities": [...], "relations": [...]}
    """
    await assert_corpus_allowed(corpus_id)
    driver = _neo4j_or_error()
    return await neo4j_reader.get_chunk_extraction(driver, corpus_id, chunk_id)


# ── Tool 3: polymath_search_entities ───────────────────────────────────────


async def polymath_search_entities(
    corpus_id: str,
    query: str = "",
    limit: int = 20,
    doc_id: str | None = None,
) -> dict[str, Any]:
    """Search canonical entities mentioned within a corpus.

    Delegates to services.graph.neo4j_reader.get_entities (substring match on
    normalized_name + display_name). When `doc_id` is set, scope narrows to
    entities mentioned by that document only.

    Args:
        corpus_id: Owning corpus. Must be in the user's allowed set.
        query: Optional substring filter (case-insensitive). Empty = list all.
        limit: Maximum number of entities to return.
        doc_id: Optional document scope.

    Returns:
        {"entities": [{entity_id, normalized_name, display_name, entity_type,
                       confidence, mention_count}]}
    """
    await assert_corpus_allowed(corpus_id)
    driver = _neo4j_or_error()
    entities = await neo4j_reader.get_entities(
        driver, corpus_id=corpus_id, q=query, limit=limit, doc_id=doc_id
    )
    return {"entities": entities}


# ── Tool 4: polymath_get_entity_relations ──────────────────────────────────


async def polymath_get_entity_relations(
    corpus_id: str,
    entity_id: str | None = None,
    canonical_name: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return outgoing + incoming RELATES_TO edges for an entity.

    Delegates to services.graph.neo4j_reader.get_entity_relations. Provide
    either `entity_id` (preferred — exact match) or `canonical_name`
    (case-insensitive normalized lookup). Edges are corpus-scoped: the entity
    must be mentioned by at least one chunk in the given corpus.

    Args:
        corpus_id: Owning corpus. Must be in the user's allowed set.
        entity_id: Phase 14.3 type-discriminated ID (e.g. 'organization:apple-inc').
        canonical_name: Fallback lookup when entity_id unknown.
        limit: Max edges per direction (incoming + outgoing each capped at limit).

    Returns:
        {"edges": [{subject_id, subject_name, predicate, object_id, object_name,
                    confidence}]}
    """
    if not entity_id and not canonical_name:
        raise ValueError("Provide either entity_id or canonical_name")
    await assert_corpus_allowed(corpus_id)
    driver = _neo4j_or_error()
    edges = await neo4j_reader.get_entity_relations(
        driver,
        corpus_id=corpus_id,
        entity_id=entity_id,
        canonical_name=canonical_name,
        limit=limit,
    )
    return {"edges": edges}


# ── Phase 24 — Discovery + listing tools ───────────────────────────────────


async def polymath_app_guide(
    detail: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Return the agent-facing Polymath app map and workflow guide.

    Use this as the first MCP call when an agent needs to understand how to
    operate the app end to end: route names, retrieval tiers, graph modes,
    ingestion/update workflow, tool playbook, and write-safety rules.

    Args:
        detail: "summary" returns compact guidance. "full" also includes the
            full MCP server instruction text.
    """
    if detail not in ("summary", "full"):
        raise ValueError("detail must be 'summary' or 'full'")
    return _json_ready(get_app_guide(detail=detail))


async def polymath_list_corpora() -> dict[str, Any]:
    """List corpora the authenticated user can access.

    Each corpus carries doc/chunk counts and the embedding model so the
    caller can pick a suitable target for polymath_search.

    Returns:
        {"corpora": [{corpus_id, name, description, doc_count, chunk_count,
                      embedding_model_id, created_at, updated_at}, ...]}
    """
    from .auth import SYSTEM_USER_ID, allowed_corpus_ids, get_current_user_id

    uid = get_current_user_id()
    allowed = await allowed_corpus_ids(uid)
    # System auth (static MCP_API_KEY → SYSTEM_USER_ID) owns no corpora directly,
    # so an owner-filtered query returns nothing. List across all owners and let
    # `allowed` (which already returns ALL corpora for the system user) scope.
    # Real users still list only their own.
    list_uid = None if uid in (None, SYSTEM_USER_ID) else uid
    docs = await ingestion_service.list_corpora(user_id=list_uid)
    return {
        "corpora": [
            {
                "corpus_id": d["corpus_id"],
                "name": d.get("name"),
                "description": d.get("description"),
                "doc_count": d.get("doc_count", 0),
                "chunk_count": d.get("chunk_count", 0),
                "embedding_model_id": d.get("embedding_model_id"),
                "created_at": (d.get("created_at").isoformat()
                               if d.get("created_at") else None),
                "updated_at": (d.get("updated_at").isoformat()
                               if d.get("updated_at") else None),
            }
            for d in docs
            if d["corpus_id"] in allowed
        ]
    }


async def polymath_list_documents(
    corpus_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List documents inside a corpus the user can access.

    Args:
        corpus_id: corpus to list. Rejected if outside the user's allowed set.
        limit: max results (1–500).
        offset: pagination offset.

    Returns:
        {"documents": [{doc_id, filename, source_tier, chunk_count,
                        parent_count, write_state, ingested_at}, ...]}
    """
    from .auth import SYSTEM_USER_ID, get_current_user_id

    uid = get_current_user_id()
    await assert_corpus_allowed(corpus_id)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    # assert_corpus_allowed already enforced the corpus boundary; the system key
    # owns no corpora directly, so don't owner-filter the doc listing (would be
    # empty). Real users keep their owner filter as defence-in-depth.
    list_uid = None if uid in (None, SYSTEM_USER_ID) else uid
    docs = await ingestion_service.list_documents(
        corpus_id, user_id=list_uid, limit=limit, offset=offset
    )
    return {
        "documents": [
            {
                "doc_id": d.get("doc_id"),
                "filename": d.get("filename"),
                "source_tier": d.get("source_tier"),
                "chunk_count": d.get("chunk_count", 0),
                "parent_count": int(
                    d.get("parent_count") or len(d.get("parent_chunks") or [])
                ),
                "write_state": d.get("write_state", {}),
                "ingested_at": (d.get("ingested_at").isoformat()
                                if d.get("ingested_at") else None),
            }
            for d in docs
        ]
    }


async def polymath_list_skills() -> dict[str, Any]:
    """List the user's authored skills (Phase 24 instruction-mode CRUD).

    Each skill is a behavior-modifier — markdown instructions appended as
    `<skill>` context on chat turns. Use polymath_invoke_skill to apply
    one's instructions when answering yourself, since skills are
    prompt-level, not callable.

    Returns:
        {"skills": [{id, name, slash_command, description, enabled}, ...]}
    """
    from services.skills_registry import skills_registry

    skills = await skills_registry.list_skills()
    return {
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "slash_command": s.slash_command,
                "description": s.description,
                "enabled": s.enabled,
            }
            for s in skills
        ]
    }


async def polymath_get_skill(skill_id: str) -> dict[str, Any]:
    """Fetch a single skill's full instructions text.

    Args:
        skill_id: Mongo ObjectId of the skill.

    Returns:
        {id, name, slash_command, description, instructions, enabled}
        or {"error": "..."} on miss.
    """
    from services.skills_registry import skills_registry

    s = await skills_registry.get_skill(skill_id)
    if not s:
        return {"error": f"skill {skill_id} not found"}
    return {
        "id": s.id,
        "name": s.name,
        "slash_command": s.slash_command,
        "description": s.description,
        "instructions": s.instructions,
        "enabled": s.enabled,
    }


async def polymath_list_tools() -> dict[str, Any]:
    """List the user's authored Polymath tools (Python functions exposed to the
    agentic chat loop). Distinct from MCP tools registered here.

    Returns:
        {"tools": [{id, name, slash_command, description, parameters, enabled}, ...]}
    """
    from services.tool_registry import tool_registry

    tools = await tool_registry.list_tools()
    return {
        "tools": [
            {
                "id": t.id,
                "name": t.name,
                "slash_command": t.slash_command,
                "description": t.description,
                "parameters": t.parameters,
                "enabled": t.enabled,
            }
            for t in tools
        ]
    }


# ── Phase 30 — Corpus / document lifecycle (write surface) ────────────────
# These tools let an autonomous agent (OpenClaw, etc.) close the research
# loop: discover → search → synthesize → CREATE corpus → INGEST → poll →
# verify. Read-only tools above stay unchanged; the write surface is gated
# on the same JWT/API-key auth path but adds size + URL-safety checks
# because writes have higher blast radius than reads.


def _require_user_id_for_write() -> str:
    """Return the authenticated user_id, raising AuthError when the request
    cannot be attributed.

    Writes (create_corpus, ingest, delete) must have an owner. The read path
    is permissive (defaults to "list all corpora the system can see"); the
    write path is not — a corpus with `user_id=None` would be orphaned and
    invisible to the corpus listing's per-user filter.

    Acceptable identities:
      - real user_id from JWT
      - SYSTEM_USER_ID sentinel when authenticated via the static MCP_API_KEY
        (system-level agent; the corpus is owned by the sentinel and visible
        to anyone holding the same key)

    Raises AuthError when MCP_REQUIRE_AUTH=True and no user_id is present.
    """
    uid = get_current_user_id()
    if uid is None:
        if get_settings().MCP_REQUIRE_AUTH:
            raise AuthError(
                "Authentication required to perform write operations "
                "(create_corpus, ingest, delete). Provide a JWT or "
                "MCP_API_KEY."
            )
        # MCP_REQUIRE_AUTH=False — single-user dev mode. Fall back to the
        # system sentinel so the corpus still has a non-null owner field.
        from .auth import SYSTEM_USER_ID  # local import — avoid cycle
        return SYSTEM_USER_ID
    return uid


def _safe_ingest_url(url: str) -> tuple[bool, str]:
    """Return (allowed, reason). Refuses non-http(s) schemes and — unless
    MCP_INGEST_URL_ALLOW_PRIVATE is enabled — IPs in loopback / link-local /
    RFC1918 / unique-local ranges.

    Hostname-based blocks (e.g., "localhost", "metadata.google.internal")
    are caught here too. DNS-rebind isn't fully defended against — the
    httpx client below also enforces the size cap, so a malicious responder
    can't tarpit forever.
    """
    from urllib.parse import urlparse
    import ipaddress

    try:
        parsed = urlparse(url)
    except Exception:  # pragma: no cover — urlparse is permissive
        return False, "URL is not parseable"
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported URL scheme {parsed.scheme!r}; use http or https"
    if not parsed.hostname:
        return False, "URL has no hostname"

    if get_settings().MCP_INGEST_URL_ALLOW_PRIVATE:
        return True, ""

    host = parsed.hostname.lower()
    # Hostname blocklist — catches the most common SSRF targets before DNS.
    blocked_hostnames = {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
    if host in blocked_hostnames:
        return False, f"hostname {host!r} is blocked (SSRF safety)"

    # IP literal — reject all private / loopback / link-local / reserved ranges.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not a bare IP — caller may still resolve it to a private range via
        # DNS. We document the limit rather than implement DNS-rebind
        # protection (which requires a custom resolver hook).
        return True, ""
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
        return False, f"IP {host} is in a private/reserved range"
    return True, ""


def _summarize_write_state(doc: dict[str, Any]) -> str:
    """Translate the document's WriteState booleans into a single status
    string the agent can poll against.

    Mapping:
      - error field non-empty → "failed"
      - write_state.verified == False → "failed_verify"
      - all three (mongo, qdrant, neo4j) written → "complete"
      - any of them written → "processing"
      - none written → "queued"

    Neo4j is honored only when the document's frozen config opted in
    (`use_neo4j=True`); a doc with use_neo4j=False completes at the
    mongo + qdrant boundary.
    """
    if doc.get("error"):
        return "failed"
    ws = doc.get("write_state") or {}
    if ws.get("verified") is False:
        return "failed_verify"
    mongo_done = bool(ws.get("mongo_written"))
    qdrant_done = bool(ws.get("qdrant_written"))
    cfg = doc.get("ingestion_config") or doc.get("default_ingestion_config") or {}
    summary_required = bool(
        cfg.get("chunk_summarization")
        and any(
            k in ("naive", "hrag")
            for k in (cfg.get("target_qdrant_collections") or [])
        )
    )
    summaries_indexed_raw = ws.get("summaries_indexed")
    summaries_done = (
        not summary_required
        or bool(summaries_indexed_raw)
        or (summaries_indexed_raw is None and qdrant_done)
    )
    # use_neo4j is stored on the frozen doc config — honor it so a corpus
    # without graph extraction doesn't get stuck in "processing" forever.
    needs_neo4j = bool(cfg.get("use_neo4j"))
    neo4j_done = (not needs_neo4j) or bool(ws.get("neo4j_written"))

    if mongo_done and qdrant_done and summaries_done and neo4j_done:
        return "complete"
    if mongo_done or qdrant_done or ws.get("neo4j_written"):
        return "processing"
    return "queued"


async def polymath_create_corpus(
    name: str,
    description: str | None = None,
    preset: Literal["custom", "fast", "balanced", "deep"] = "balanced",
    use_neo4j: bool | None = None,
    chunk_summarization: bool | None = None,
    embedding_model_id: str | None = None,
    use_summary_settings: bool = True,
) -> dict[str, Any]:
    """Create a new Polymath corpus owned by the authenticated user.

    The corpus is immediately searchable once documents are ingested. The
    preset selects sensible defaults; explicit overrides win where provided.

    Args:
        name: Human-readable corpus name. Must be non-empty.
        description: Optional longer description.
        preset: 'fast' (vectors only, no graph, no summaries),
            'balanced' (graph + vectors, no parent summaries — default),
            'deep' (graph + vectors + Ghost A summaries),
            'custom' (leave toggles alone — caller knows what they want).
        use_neo4j: Override the preset's graph toggle. None = honor preset.
        chunk_summarization: Override the preset's Ghost A summary toggle.
            None = honor preset.
        embedding_model_id: Override the embedder model. Defaults to server
            default. Once any document is ingested this is LOCKED.
        use_summary_settings: When True and chunk_summarization is omitted,
            Settings → Ingestion → Summary can promote the default balanced
            create path to deep/summary-enabled for agents.

    Returns:
        {"corpus_id", "name", "description", "preset", "use_neo4j",
         "chunk_summarization", "embedding_model_id", "created_at",
         "status": "created"}
    """
    from models.schemas import IngestionConfig

    uid = _require_user_id_for_write()
    if not name or not name.strip():
        raise ValueError("Corpus name must be non-empty")

    # Build the config. We let IngestionConfig fill the defaults, then apply
    # the explicit overrides. apply_preset() runs inside create_corpus and
    # rewrites toggles to match the preset for 'fast'/'balanced'/'deep'; the
    # explicit overrides below survive when preset='custom'.
    config_kwargs: dict[str, Any] = {"preset": preset}
    if chunk_summarization is None and use_summary_settings and preset in {"balanced", "custom"}:
        try:
            from services.settings import settings_service

            summary_defaults = (await settings_service.get_runtime_ingestion_settings(uid)).summary
            if summary_defaults.enabled:
                config_kwargs["chunk_summarization"] = True
                if preset == "balanced":
                    config_kwargs["preset"] = "deep"
        except Exception:
            pass
    if use_neo4j is not None:
        config_kwargs["use_neo4j"] = use_neo4j
    if chunk_summarization is not None:
        config_kwargs["chunk_summarization"] = chunk_summarization
    if embedding_model_id:
        config_kwargs["embedding_model_id"] = embedding_model_id

    config = IngestionConfig(**config_kwargs)

    doc = await ingestion_service.create_corpus(
        name=name.strip(),
        description=description,
        user_id=uid,
        ingestion_config=config,
    )
    # `doc` is the masked corpus row from the service. Compact it for MCP —
    # we don't want to leak the full default_ingestion_config (which
    # includes pool entries and per-corpus knobs the agent doesn't need to
    # see).
    cfg = doc.get("default_ingestion_config") or {}
    return {
        "status": "created",
        "corpus_id": doc.get("corpus_id"),
        "name": doc.get("name"),
        "description": doc.get("description"),
        "preset": cfg.get("preset"),
        "use_neo4j": cfg.get("use_neo4j"),
        "chunk_summarization": cfg.get("chunk_summarization"),
        "embedding_model_id": doc.get("embedding_model_id"),
        "created_at": (
            doc.get("created_at").isoformat()
            if hasattr(doc.get("created_at"), "isoformat")
            else doc.get("created_at")
        ),
    }


async def _ingest_bytes(
    *,
    data: bytes,
    filename: str,
    corpus_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Shared ingestion path used by both polymath_ingest_from_url and
    polymath_upload_document. Builds the effective IngestionConfig from the
    corpus's stored default and queues the worker.

    Returns the compact JSON the MCP tools surface — kept here so the URL
    and base64 paths can't drift apart.
    """
    from models.schemas import IngestionConfig

    if not data:
        raise ValueError("Document is empty")
    max_bytes = get_settings().MCP_INGEST_MAX_BYTES
    if len(data) > max_bytes:
        raise ValueError(
            f"Document is {len(data)} bytes; MCP cap is {max_bytes}. "
            "Use the multipart HTTP endpoint for larger files."
        )

    # IMPORTANT: use the raw corpus row so the api_key fields in the stored
    # ingestion_config keep their Fernet ciphertext. The masked variant from
    # get_corpus() would defeat the worker's decrypt path and silently
    # kill the summary/extraction ghosts.
    corpus = await ingestion_service._get_corpus_raw(corpus_id)
    if not corpus:
        raise ValueError(f"corpus_id {corpus_id!r} not found")
    base_cfg_dict = corpus.get("default_ingestion_config") or {}
    cfg = IngestionConfig(**base_cfg_dict)

    # Admission gate — share the per-process slot pool with the HTTP router
    # so an agent looping over 500 docs through MCP can't bypass the
    # INGEST_MAX_ACTIVE_JOBS cap that the 0a47b8f fix established for the
    # multipart path. Acquire BEFORE handing the bytes to ingestion_service
    # so the work that materializes parser state / spawns extractors doesn't
    # start until we know we have a slot.
    if not await _try_acquire_ingest_slot():
        cap = get_settings().INGEST_MAX_ACTIVE_JOBS
        raise ValueError(
            f"Ingest queue is full (cap={cap}); retry shortly. "
            "MCP shares the gate with the HTTP /ingest path."
        )
    try:
        # model="" matches the HTTP ingest router: it means "use the pools
        # configured on the corpus's IngestionConfig" rather than the
        # back-compat single-model override.
        result = await ingestion_service.ingest(
            data=data,
            filename=filename,
            corpus_id=corpus_id,
            user_id=user_id,
            ingestion_config=cfg,
            model="",
        )
        return {
            "status": "queued",
            "doc_id": result.doc_id,
            "job_id": result.job_id,
            "corpus_id": corpus_id,
            "filename": result.filename,
            "source_tier": result.source_tier,
            "size_bytes": len(data),
        }
    finally:
        # Slot is held for the full duration of ingest() — unlike the
        # HTTP router, the MCP path awaits the entire pipeline rather
        # than scheduling _run() as a background task, so the slot
        # release naturally lines up with "ingest finished." The HTTP
        # router's _run()'s finally block does the equivalent release
        # for the background-task case (see routers/ingestion.py:1014).
        await _release_ingest_slot()


async def polymath_ingest_from_url(
    corpus_id: str,
    url: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """Fetch a document from a public URL and queue it for ingestion.

    Use this when the agent has discovered a file (arXiv PDF, GitHub raw
    blob, transcript text, exported HTML, etc.) and wants Polymath to learn
    from it. Before calling on ambiguous content, ask the user or caller for
    the ingestion profile described by polymath_app_guide and whether parent
    summaries are required. The server downloads the file synchronously, then
    kicks off the async ingestion worker. Use polymath_get_ingest_status to
    poll completion.

    Safety:
      - Only http(s) URLs are accepted.
      - Loopback / RFC1918 / link-local IPs are blocked by default (SSRF).
        Flip MCP_INGEST_URL_ALLOW_PRIVATE if you really need them.
      - File size is capped at MCP_INGEST_MAX_BYTES (default 50 MB).
      - Fetch timeout is MCP_INGEST_URL_TIMEOUT_SECONDS (default 60 s).

    Args:
        corpus_id: Target corpus. Must be user-accessible.
        url: Public http(s) URL to the file.
        filename: Optional override; inferred from URL tail if omitted.

    Returns:
        {"status": "queued", "doc_id", "job_id", "corpus_id", "filename",
         "source_tier", "size_bytes"}
    """
    import httpx

    await assert_corpus_allowed(corpus_id)
    uid = _require_user_id_for_write()

    allowed, reason = _safe_ingest_url(url)
    if not allowed:
        raise ValueError(f"URL refused: {reason}")

    settings = get_settings()
    timeout = settings.MCP_INGEST_URL_TIMEOUT_SECONDS
    max_bytes = settings.MCP_INGEST_MAX_BYTES

    # Stream the download so an oversize file aborts as soon as we cross the
    # cap, rather than after fully buffering it in memory.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError(
                    f"URL advertises {content_length} bytes; MCP cap is "
                    f"{max_bytes}. Use the multipart HTTP endpoint."
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"Download exceeded MCP cap of {max_bytes} bytes "
                        "while streaming"
                    )
                chunks.append(chunk)
            data = b"".join(chunks)

    if not filename:
        # Derive a filename from the URL path. Empty/trailing-slash URLs
        # land on a default so the docling adapter can still infer a type.
        from urllib.parse import urlparse, unquote
        tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        filename = unquote(tail) or "download"

    return await _ingest_bytes(
        data=data,
        filename=filename,
        corpus_id=corpus_id,
        user_id=uid,
    )


async def polymath_upload_document(
    corpus_id: str,
    filename: str,
    content_base64: str,
) -> dict[str, Any]:
    """Upload a document body (base64-encoded) and queue it for ingestion.

    Use this when the agent has the file bytes locally and there's no
    fetchable URL. Before calling on ambiguous content, ask the user or
    caller for the ingestion profile described by polymath_app_guide and
    whether parent summaries are required. The base64 wire format is the
    cost of routing through MCP's JSON-RPC transport — files larger than
    ~30 MB raw should go through the multipart HTTP endpoint to avoid the
    33% expansion.

    Args:
        corpus_id: Target corpus.
        filename: Original filename (drives type detection in the parser).
        content_base64: Base64-encoded file bytes. Strip any
            `data:...;base64,` prefix before sending — this tool decodes
            raw base64 only.

    Returns:
        {"status": "queued", "doc_id", "job_id", "corpus_id", "filename",
         "source_tier", "size_bytes"}
    """
    import base64
    import binascii

    await assert_corpus_allowed(corpus_id)
    uid = _require_user_id_for_write()

    if not filename or not filename.strip():
        raise ValueError("filename must be non-empty")
    if not content_base64:
        raise ValueError("content_base64 must be non-empty")

    # Strip a data-URL prefix if the caller forgot to. Cheaper than failing
    # the request and asking the agent to retry.
    payload = content_base64
    if payload.startswith("data:") and ";base64," in payload:
        payload = payload.split(";base64,", 1)[1]

    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"content_base64 is not valid base64: {exc}") from exc

    return await _ingest_bytes(
        data=data,
        filename=filename.strip(),
        corpus_id=corpus_id,
        user_id=uid,
    )


async def polymath_get_ingest_status(
    doc_id: str,
    corpus_id: str | None = None,
) -> dict[str, Any]:
    """Poll the ingestion status of a queued document.

    Status values:
      - "queued"          — worker has not started writing any store yet
      - "processing"      — at least one of Mongo/Qdrant/Neo4j is written
      - "complete"        — all expected stores are written (Neo4j is only
                            expected when the corpus uses graph extraction)
      - "failed"          — error field on the document record
      - "failed_verify"   — write_state.verified is False (post-write
                            consistency check tripped)
      - "not_found"       — no document row with this doc_id

    Args:
        doc_id: Document id returned by polymath_ingest_from_url /
            polymath_upload_document.
        corpus_id: Optional narrowing scope. When provided, must be
            user-accessible.

    Returns:
        {"doc_id", "corpus_id", "filename", "status", "chunk_count",
         "parent_count", "write_state", "error", "ingested_at",
         "source_tier", "warnings"}
    """
    if corpus_id:
        await assert_corpus_allowed(corpus_id)

    uid = get_current_user_id()
    if uid is None and get_settings().MCP_REQUIRE_AUTH:
        raise AuthError("Authentication required to poll ingest status")

    # Pass user_id ONLY when we have a real one. SYSTEM_USER_ID has access
    # to every document; the service-layer filter would otherwise fail to
    # match because document rows are stored under the per-user JWT subject.
    from .auth import SYSTEM_USER_ID
    scoped_uid = uid if uid not in (None, SYSTEM_USER_ID) else None

    doc = await ingestion_service.get_job_status(
        doc_id, corpus_id=corpus_id, user_id=scoped_uid
    )
    if not doc:
        return {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "status": "not_found",
            "error": "no document with that doc_id",
        }

    ws = doc.get("write_state") or {}
    return {
        "doc_id": doc.get("doc_id", doc_id),
        "corpus_id": doc.get("corpus_id"),
        "filename": doc.get("filename"),
        "status": _summarize_write_state(doc),
        "chunk_count": int(doc.get("chunk_count") or 0),
        "parent_count": int(
            doc.get("parent_count") or len(doc.get("parent_chunks") or [])
        ),
        "source_tier": doc.get("source_tier"),
        "write_state": {
            "mongo_written": bool(ws.get("mongo_written")),
            "qdrant_written": bool(ws.get("qdrant_written")),
            "summaries_indexed": bool(ws.get("summaries_indexed")),
            "neo4j_written": bool(ws.get("neo4j_written")),
            "verified": ws.get("verified"),
            "warnings": ws.get("warnings") or [],
            "verify_errors": ws.get("verify_errors") or [],
        },
        "error": doc.get("error"),
        "ingested_at": (
            doc.get("ingested_at").isoformat()
            if hasattr(doc.get("ingested_at"), "isoformat")
            else doc.get("ingested_at")
        ),
    }


async def polymath_delete_document(
    corpus_id: str,
    doc_id: str,
) -> dict[str, Any]:
    """Delete a document and its chunks from a corpus.

    Cascades the removal across Qdrant points, Neo4j Entity/Mention
    nodes, Mongo chunks, and the Mongo document record. Corpus-level
    aggregate counters self-repair on the next list_corpora call.

    Use sparingly — there is no undo. A typical agent workflow only
    calls this to clean up an upload that demonstrably failed (poll
    returned 'failed' or 'failed_verify' and a retry is queued).

    Args:
        corpus_id: Owning corpus. Must be user-accessible.
        doc_id: Document to remove.

    Returns:
        {"doc_id", "corpus_id", "status": "deleted" | "not_found"}
    """
    await assert_corpus_allowed(corpus_id)
    _require_user_id_for_write()  # gate writes behind a real identity

    removed = await ingestion_service.delete_document(corpus_id, doc_id)
    return {
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "status": "deleted" if removed else "not_found",
    }


async def polymath_backfill_summaries(
    corpus_id: str,
    generate: bool = True,
    index: bool = True,
    limit: int | None = 200,
    batch: int = 32,
) -> dict[str, Any]:
    """Repair parent summaries for an existing corpus.

    Use this when a corpus was created with the balanced preset or when
    documents show ``summaries_indexed=false`` after upload. The tool can
    generate missing parent summaries using Settings → Ingestion → Summary
    Defaults, index existing summaries into Qdrant, or both. It never exposes
    provider API keys and does not require deleting/reingesting documents.

    Args:
        corpus_id: Corpus to repair. Must be accessible to the caller.
        generate: When true, summarize missing body-parent chunks.
        index: When true, upsert all available parent-summary vectors.
        limit: Max missing parent summaries to generate in this call. Use None
            only for deliberate full-corpus repair.
        batch: Parent batch size for generation/indexing.

    Returns:
        Summary health before/after plus generated/indexed counts.
    """
    await assert_corpus_allowed(corpus_id)
    scoped_uid = _require_user_id_for_write()
    return await ingestion_service.backfill_parent_summaries(
        corpus_id,
        user_id=scoped_uid,
        generate=generate,
        index=index,
        limit=limit,
        batch=batch,
    )


# ── Registry — single source of truth for the MCP server to register ───────

ALL_TOOLS = (
    polymath_app_guide,
    polymath_search,
    polymath_cross_corpus_search,
    polymath_chat_query,
    polymath_graph_query,
    polymath_graph_map_query,
    polymath_graph_question_suggestions,
    polymath_get_chunk_extraction,
    polymath_search_entities,
    polymath_get_entity_relations,
    # Phase 24 — discovery + skills/tools listing
    polymath_list_corpora,
    polymath_list_documents,
    polymath_list_skills,
    polymath_get_skill,
    polymath_list_tools,
    # Phase 30 — corpus / document lifecycle (write surface)
    polymath_create_corpus,
    polymath_ingest_from_url,
    polymath_upload_document,
    polymath_get_ingest_status,
    polymath_delete_document,
    polymath_backfill_summaries,
)

__all__ = [
    "ALL_TOOLS",
    "polymath_app_guide",
    "polymath_search",
    "polymath_cross_corpus_search",
    "polymath_chat_query",
    "polymath_graph_query",
    "polymath_graph_map_query",
    "polymath_graph_question_suggestions",
    "polymath_get_chunk_extraction",
    "polymath_search_entities",
    "polymath_get_entity_relations",
    "polymath_list_corpora",
    "polymath_list_documents",
    "polymath_list_skills",
    "polymath_get_skill",
    "polymath_list_tools",
    "polymath_create_corpus",
    "polymath_ingest_from_url",
    "polymath_upload_document",
    "polymath_get_ingest_status",
    "polymath_delete_document",
    "polymath_backfill_summaries",
    "AuthError",
]
