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
    # use_neo4j is stored on the frozen doc config — honor it so a corpus
    # without graph extraction doesn't get stuck in "processing" forever.
    cfg = doc.get("ingestion_config") or doc.get("default_ingestion_config") or {}
    needs_neo4j = bool(cfg.get("use_neo4j"))
    neo4j_done = (not needs_neo4j) or bool(ws.get("neo4j_written"))

    if mongo_done and qdrant_done and neo4j_done:
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


async def polymath_ingest_from_url(
    corpus_id: str,
    url: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """Fetch a document from a public URL and queue it for ingestion.

    Use this when the agent has discovered a file (arXiv PDF, GitHub raw
    blob, etc.) and wants Polymath to learn from it. The server downloads
    the file synchronously, then kicks off the async ingestion worker.
    Use polymath_get_ingest_status to poll completion.

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
    fetchable URL. The base64 wire format is the cost of routing through
    MCP's JSON-RPC transport — files larger than ~30 MB raw should go
    through the multipart HTTP endpoint to avoid the 33% expansion.

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
        "parent_count": len(doc.get("parent_chunks") or []),
        "source_tier": doc.get("source_tier"),
        "write_state": {
            "mongo_written": bool(ws.get("mongo_written")),
            "qdrant_written": bool(ws.get("qdrant_written")),
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
    # Phase 30 — corpus / document lifecycle (write surface)
    polymath_create_corpus,
    polymath_ingest_from_url,
    polymath_upload_document,
    polymath_get_ingest_status,
    polymath_delete_document,
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
    "polymath_create_corpus",
    "polymath_ingest_from_url",
    "polymath_upload_document",
    "polymath_get_ingest_status",
    "polymath_delete_document",
    "AuthError",
]
