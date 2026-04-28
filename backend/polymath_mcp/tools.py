"""Polymath MCP tool surface — Phase 8.2.

Four tools, all thin adapters over backend.services.* — zero new business logic.
Per CLAUDE.md "one retrieval implementation, multiple transports."

Each tool:
  1. Validates args (Pydantic-style via type hints).
  2. Resolves user_id → allowed_corpus_ids (services.storage.mongo_reader).
  3. Filters corpus_ids against allowed set (silent drop on disallowed).
  4. Calls the underlying service.
  5. Returns an MCP-shaped JSON response.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from config import get_settings
from models.schemas import RetrievalTier
from services.graph import neo4j_reader
from services.ingestion_service import ingestion_service
from services.retriever import retriever_orchestrator

from .auth import (
    AuthError,
    assert_corpus_allowed,
    resolve_request_scope,
)

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────


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


# ── Tool 1: polymath_search ────────────────────────────────────────────────


async def polymath_search(
    query: str,
    corpus_ids: list[str],
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
            authenticated user lacks access.
        retrieval_tier: 'qdrant_only' (raw vectors), 'qdrant_mongo' (default;
            vectors + parent hydration), or 'qdrant_mongo_graph' (adds Mode A
            graph expansion; requires use_neo4j=True on every selected corpus).
        top_k: Override server default (settings.MCP_DEFAULT_TOP_K, default 5).

    Returns:
        {
          "chunks": [{chunk_id, doc_id, corpus_id, parent_id, text, score, ...}],
          "requested_tier": "...",
          "effective_tier": "...",
          "downgrade_reason": "..." | null
        }
    """
    settings = get_settings()
    scoped = await resolve_request_scope(corpus_ids)
    if not scoped:
        return {
            "chunks": [],
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
        "chunks": [
            c.model_dump() if hasattr(c, "model_dump") else dict(c)
            for c in result.chunks
        ],
        "requested_tier": str(result.requested_tier),
        "effective_tier": str(result.effective_tier),
        "downgrade_reason": result.downgrade_reason,
    }


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
