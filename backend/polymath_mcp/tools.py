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
from models.schemas import ChatRequest, ModelOverrides, RetrievalTier
from services.graph import neo4j_reader
from services.ingestion_service import ingestion_service
from services.retriever import retriever_orchestrator

from .auth import (
    AuthError,
    assert_corpus_allowed,
    allowed_corpus_ids,
    get_current_user_id,
    resolve_request_scope,
)

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
    k = top_k if top_k is not None else settings.MCP_DEFAULT_TOP_K
    result = await retriever_orchestrator.retrieve(
        query=query,
        corpus_ids=scoped,
        retrieval_tier=tier,
        collections=None,
        retrieval_k=k,
    )
    return {
        "chunks": [_json_ready(c) for c in result.chunks],
        "corpus_ids": scoped,
        "requested_tier": _json_ready(result.requested_tier),
        "effective_tier": _json_ready(result.effective_tier),
        "downgrade_reason": result.downgrade_reason,
    }


async def polymath_search(
    query: str,
    corpus_ids: list[str] | None = None,
    retrieval_tier: Literal[
        "qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"
    ] = "qdrant_mongo",
    top_k: int | None = None,
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
        top_k: Override server default (settings.MCP_DEFAULT_TOP_K, default 5).

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
    )


async def polymath_cross_corpus_search(
    query: str,
    corpus_ids: list[str] | None = None,
    retrieval_tier: Literal[
        "qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"
    ] = "qdrant_mongo",
    top_k: int | None = None,
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
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_sources: int = 8,
) -> dict[str, Any]:
    """Ask Polymath chat from MCP and return the final non-streamed answer.

    This uses services.chat_orchestrator, the same path as POST /api/chat. It
    streams internally, captures tokens/sources/trust signals, and returns one
    compact JSON object to the MCP caller.

    Args:
        message: Natural-language question or instruction.
        corpus_ids: Optional corpora to ground the answer. Current chat API
            supports up to 3 corpora per turn; use polymath_cross_corpus_search
            for broader evidence gathering.
        retrieval_tier: Retrieval route used by chat.
        conversation_id: Optional existing chat conversation id.
        model: Optional concrete model/pool/profile override.
        query_profile: fast | balanced | thorough | custom.
        reasoning_mode: Optional Polymath reasoning mode.
        temperature: Optional model temperature.
        max_tokens: Optional output cap.
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
    if len(scoped) > 3:
        return {
            "status": "error",
            "error": (
                "polymath_chat_query currently accepts at most 3 corpora. "
                "Use polymath_cross_corpus_search for wider corpus discovery, "
                "then ask chat with the most relevant 1-3 corpus_ids."
            ),
            "answer": "",
            "sources": [],
            "corpus_ids": scoped,
        }

    override_data = {
        "model": model,
        "query_profile": query_profile,
        "reasoning_mode": reasoning_mode,
        "temperature": temperature,
        "max_tokens": max_tokens,
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
    )

    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
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
            elif event_type == "done":
                done = _json_ready(event)
            elif event_type == "error":
                return {
                    "status": "error",
                    "error": event.get("content") or "chat query failed",
                    "answer": "".join(answer_parts).strip(),
                    "sources": sources,
                    "signals": signals,
                }
            else:
                signals.append(_json_ready(event))

    return {
        "status": "ok",
        "answer": "".join(answer_parts).strip(),
        "thinking": "".join(thinking_parts).strip() or None,
        "sources": sources,
        "signals": signals,
        "conversation_id": done.get("conversation_id"),
        "model_used": done.get("model_used"),
        "chunks_returned": done.get("chunks_returned"),
        "strategy_used": done.get("strategy_used"),
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
    corpus_id: str,
    query: str,
    mode: Literal["auto", "connect", "gaps", "themes"] = "auto",
    session_id: str | None = None,
    model: str | None = None,
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
        corpus_id: Corpus to analyze.
        query: Research question or concept neighborhood to explore.
        mode: Compatibility field; graph discovery currently behaves as auto.
        session_id: Optional existing Mission Control session id.
        model: Optional synthesis model override.
        include_graph: Include full graph payload.
        include_trace: Include full trace/evidence diagnostics.
        max_items: Compact-list cap for previews.
    """
    await assert_corpus_allowed(corpus_id)
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
            corpus_id=corpus_id,
            query=query,
            mode=mode,
            session_id=session_id,
            user_id=get_current_user_id(),
            model_override=model,
            agentic=False,
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
    )


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


async def polymath_list_corpora() -> dict[str, Any]:
    """List corpora the authenticated user can access.

    Each corpus carries doc/chunk counts and the embedding model so the
    caller can pick a suitable target for polymath_search.

    Returns:
        {"corpora": [{corpus_id, name, description, doc_count, chunk_count,
                      embedding_model_id, created_at, updated_at}, ...]}
    """
    from .auth import allowed_corpus_ids, get_current_user_id

    uid = get_current_user_id()
    allowed = await allowed_corpus_ids(uid)
    docs = await ingestion_service.list_corpora(user_id=uid)
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
    from .auth import get_current_user_id

    uid = get_current_user_id()
    await assert_corpus_allowed(corpus_id)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    docs = await ingestion_service.list_documents(
        corpus_id, user_id=uid, limit=limit, offset=offset
    )
    return {
        "documents": [
            {
                "doc_id": d.get("doc_id"),
                "filename": d.get("filename"),
                "source_tier": d.get("source_tier"),
                "chunk_count": d.get("chunk_count", 0),
                "parent_count": len(d.get("parent_chunks") or []),
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


# ── Registry — single source of truth for the MCP server to register ───────

ALL_TOOLS = (
    polymath_search,
    polymath_cross_corpus_search,
    polymath_chat_query,
    polymath_graph_query,
    polymath_get_chunk_extraction,
    polymath_search_entities,
    polymath_get_entity_relations,
    # Phase 24 — discovery + skills/tools listing
    polymath_list_corpora,
    polymath_list_documents,
    polymath_list_skills,
    polymath_get_skill,
    polymath_list_tools,
)

__all__ = [
    "ALL_TOOLS",
    "polymath_search",
    "polymath_cross_corpus_search",
    "polymath_chat_query",
    "polymath_graph_query",
    "polymath_get_chunk_extraction",
    "polymath_search_entities",
    "polymath_get_entity_relations",
    "polymath_list_corpora",
    "polymath_list_documents",
    "polymath_list_skills",
    "polymath_get_skill",
    "polymath_list_tools",
    "AuthError",
]
