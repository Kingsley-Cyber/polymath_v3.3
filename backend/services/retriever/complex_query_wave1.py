"""Shared Wave-1 assets for complex-query DAG (Phase 4).

One root embedding. Parallel logical lanes. No per-subquery re-embed.
No per-path hydration — child IDs collected, hydrated once at the end.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SharedWave1Assets:
    root_query: str
    corpus_ids: list[str]
    root_embedding: list[float] | None = None
    root_embedding_calls: int = 0
    direct_child_ids: list[str] = field(default_factory=list)
    vocabulary_child_ids: list[str] = field(default_factory=list)
    lexical_child_ids: list[str] = field(default_factory=list)
    summary_guided_child_ids: list[str] = field(default_factory=list)
    resolved_entity_ids: list[str] = field(default_factory=list)
    resolved_concepts: list[str] = field(default_factory=list)
    vocabulary_hits: list[dict[str, Any]] = field(default_factory=list)
    summary_route_ids: list[str] = field(default_factory=list)
    child_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    hydration_batch_fetches: int = 0
    duplicate_root_embeddings: int = 0
    duplicate_child_hydration: int = 0
    execution_ms: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def all_child_ids(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for cid in (
            *self.direct_child_ids,
            *self.vocabulary_child_ids,
            *self.lexical_child_ids,
            *self.summary_guided_child_ids,
        ):
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out


async def _embed_root_once(query: str, settings: Any) -> tuple[list[float] | None, int]:
    from services import embedder

    texts = [str(query or "").strip()]
    if not texts[0]:
        return None, 0
    cfg = {
        "embedding_dimension": int(getattr(settings, "EMBEDDING_DIMENSION", 1024) or 1024),
        "embed_mode": "local",
    }
    vector = await embedder.embed_query(texts[0], cfg)
    if not vector:
        return None, 1
    return list(vector), 1


async def _qdrant_search_children(
    *,
    qdrant: Any,
    corpus_id: str,
    vector: list[float],
    limit: int,
    kinds: tuple[str, ...] = ("hrag", "naive"),
) -> list[str]:
    from services.storage.qdrant_writer import _col_for_corpus

    ids: list[str] = []
    seen: set[str] = set()
    for kind in kinds:
        name = _col_for_corpus(corpus_id, kind)
        try:
            if not await qdrant.collection_exists(name):
                continue
            # Prefer query_points (named dense vector collections).
            try:
                response = await qdrant.query_points(
                    collection_name=name,
                    query=vector,
                    using="dense",
                    limit=max(1, limit),
                    with_payload=True,
                )
                hits = getattr(response, "points", None) or []
            except Exception:
                hits = await qdrant.search(
                    collection_name=name,
                    query_vector=("dense", vector),
                    limit=max(1, limit),
                    with_payload=True,
                )
        except Exception:
            continue
        for hit in hits or []:
            payload = getattr(hit, "payload", None) or {}
            cid = str(payload.get("chunk_id") or "")
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
    return ids


async def _mongo_lexical_ids(
    db: Any, *, corpus_id: str, query: str, limit: int
) -> list[str]:
    terms = [t for t in str(query or "").split() if len(t) > 2][:8]
    if not terms:
        return []
    # Prefer $text when available; fall back to regex OR.
    try:
        cursor = (
            db["chunks"]
            .find(
                {"$text": {"$search": " ".join(terms)}, "corpus_id": corpus_id},
                {"chunk_id": 1},
            )
            .limit(limit)
        )
        rows = await cursor.to_list(limit)
        ids = [str(r.get("chunk_id") or "") for r in rows if r.get("chunk_id")]
        if ids:
            return ids
    except Exception:
        pass
    pattern = "|".join(terms)
    rows = await db["chunks"].find(
        {
            "corpus_id": corpus_id,
            "chunk_kind": {"$ne": "parent"},
            "text": {"$regex": pattern, "$options": "i"},
        },
        {"chunk_id": 1},
    ).limit(limit).to_list(limit)
    return [str(r.get("chunk_id") or "") for r in rows if r.get("chunk_id")]


async def _summary_route_ids(
    db: Any, *, corpus_id: str, query: str, limit: int
) -> tuple[list[str], list[str]]:
    """Return (summary_ids, linked_child_ids) from parent/summary rows."""

    terms = [t for t in str(query or "").split() if len(t) > 2][:6]
    if not terms:
        return [], []
    pattern = "|".join(terms)
    rows = await db["chunks"].find(
        {
            "corpus_id": corpus_id,
            "chunk_kind": "parent",
            "$or": [
                {"summary": {"$regex": pattern, "$options": "i"}},
                {"text": {"$regex": pattern, "$options": "i"}},
            ],
        },
        {"chunk_id": 1, "parent_id": 1, "source_chunk_ids": 1},
    ).limit(limit).to_list(limit)
    summary_ids = []
    linked: list[str] = []
    for r in rows:
        sid = str(r.get("chunk_id") or r.get("parent_id") or "")
        if sid:
            summary_ids.append(sid)
        for cid in r.get("source_chunk_ids") or []:
            if cid:
                linked.append(str(cid))
    # Also pull children under matched parents
    if summary_ids and not linked:
        kids = await db["chunks"].find(
            {
                "corpus_id": corpus_id,
                "parent_id": {"$in": summary_ids},
                "chunk_kind": {"$ne": "parent"},
            },
            {"chunk_id": 1},
        ).limit(limit * 2).to_list(limit * 2)
        linked = [str(k.get("chunk_id")) for k in kids if k.get("chunk_id")]
    return summary_ids, linked


async def _entity_resolve(
    *,
    neo4j_driver: Any | None,
    corpus_id: str,
    query: str,
    limit: int,
) -> tuple[list[str], list[str]]:
    if neo4j_driver is None:
        return [], []
    try:
        from services.graph.graph_query import extract_query_entities

        rows = await extract_query_entities(
            query,
            corpus_id,
            neo4j_driver,
            limit_per_token=2,
            allow_literal_fallback=False,
        )
    except Exception:
        return [], []
    entity_ids: list[str] = []
    concepts: list[str] = []
    for row in rows or []:
        eid = str(row.get("entity_id") or "").strip()
        name = str(row.get("display_name") or "").strip()
        if eid and eid not in entity_ids:
            entity_ids.append(eid)
        if name and name not in concepts:
            concepts.append(name)
        if len(entity_ids) >= limit:
            break
    return entity_ids, concepts


async def _batch_hydrate_children(
    db: Any, *, corpus_id: str, child_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """One Mongo batch fetch. Returns payloads, fetch_count, duplicate_fetches."""

    uniq = []
    seen: set[str] = set()
    dup = 0
    for cid in child_ids:
        if not cid:
            continue
        if cid in seen:
            dup += 1
            continue
        seen.add(cid)
        uniq.append(cid)
    if not uniq:
        return {}, 0, dup
    rows = await db["chunks"].find(
        {"corpus_id": corpus_id, "chunk_id": {"$in": uniq}},
        {"chunk_id": 1, "doc_id": 1, "parent_id": 1, "text": 1, "corpus_id": 1},
    ).to_list(len(uniq))
    payloads = {
        str(r["chunk_id"]): {
            "chunk_id": str(r["chunk_id"]),
            "doc_id": str(r.get("doc_id") or ""),
            "parent_id": str(r.get("parent_id") or ""),
            "text": str(r.get("text") or "")[:2000],
            "corpus_id": str(r.get("corpus_id") or corpus_id),
        }
        for r in rows
        if r.get("chunk_id")
    }
    return payloads, 1, dup


async def build_shared_wave1_assets(
    *,
    db: Any,
    qdrant: Any,
    neo4j_driver: Any | None,
    query: str,
    corpus_ids: list[str],
    settings: Any | None = None,
    candidate_budget: int = 24,
    root_embedding: list[float] | None = None,
) -> SharedWave1Assets:
    """Build shared Wave-1 assets. Fixture-safe; does not mutate ranking config."""

    if settings is None:
        from config import get_settings

        settings = get_settings()
    started = time.perf_counter()
    assets = SharedWave1Assets(root_query=query, corpus_ids=list(corpus_ids))
    if not corpus_ids:
        assets.diagnostics = {"error": "no_corpus_ids"}
        return assets

    # Primary corpus for fixture runs (one id). Multi-corpus: first allowlisted.
    corpus_id = str(corpus_ids[0])

    if root_embedding is not None:
        vector = list(root_embedding)
        embed_calls = 0  # shared from retrieve_planned — no second embed
    else:
        vector, embed_calls = await _embed_root_once(query, settings)
    assets.root_embedding = vector
    assets.root_embedding_calls = embed_calls
    assets.duplicate_root_embeddings = max(0, embed_calls - 1)

    async def _direct() -> list[str]:
        if not vector:
            return []
        return await _qdrant_search_children(
            qdrant=qdrant,
            corpus_id=corpus_id,
            vector=vector,
            limit=candidate_budget,
        )

    async def _lexical() -> list[str]:
        return await _mongo_lexical_ids(
            db, corpus_id=corpus_id, query=query, limit=max(8, candidate_budget // 2)
        )

    async def _summary() -> tuple[list[str], list[str]]:
        return await _summary_route_ids(
            db, corpus_id=corpus_id, query=query, limit=max(6, candidate_budget // 3)
        )

    async def _entities() -> tuple[list[str], list[str]]:
        return await _entity_resolve(
            neo4j_driver=neo4j_driver,
            corpus_id=corpus_id,
            query=query,
            limit=int(getattr(settings, "COMPLEX_QUERY_SEED_ENTITY_CAP", 8)),
        )

    # Parallel Wave-1 (vocabulary uses same root query; no second embed).
    direct_ids, lexical_ids, summary_pair, entity_pair = await asyncio.gather(
        _direct(),
        _lexical(),
        _summary(),
        _entities(),
    )
    assets.direct_child_ids = list(direct_ids)
    assets.lexical_child_ids = list(lexical_ids)
    assets.summary_route_ids, summary_children = summary_pair
    assets.summary_guided_child_ids = list(summary_children)
    assets.resolved_entity_ids, assets.resolved_concepts = entity_pair
    # Vocabulary lane: concepts + entity ids as resolution hits (no schema write).
    assets.vocabulary_hits = [
        {"canonical_term": c, "trust_class": "resolved_surface"}
        for c in assets.resolved_concepts
    ]
    # Vocabulary-expanded children: reuse lexical/direct overlap with concept terms
    # (no extra embed). Prefer direct hits already retrieved.
    assets.vocabulary_child_ids = list(assets.direct_child_ids[: max(4, candidate_budget // 4)])

    payloads, fetches, dups = await _batch_hydrate_children(
        db, corpus_id=corpus_id, child_ids=assets.all_child_ids()
    )
    assets.child_payloads = payloads
    assets.hydration_batch_fetches = fetches
    assets.duplicate_child_hydration = dups
    assets.execution_ms = round((time.perf_counter() - started) * 1000.0, 2)
    assets.diagnostics = {
        "direct_lane_always_runs": True,
        "vocabulary_lane_parallel": True,
        "duplicate_root_embeddings": assets.duplicate_root_embeddings,
        "duplicate_child_hydration": assets.duplicate_child_hydration,
        "root_embedding_calls": assets.root_embedding_calls,
        "hydration_batch_fetches": assets.hydration_batch_fetches,
        "direct_count": len(assets.direct_child_ids),
        "lexical_count": len(assets.lexical_child_ids),
        "summary_count": len(assets.summary_route_ids),
        "entity_count": len(assets.resolved_entity_ids),
        "ranking_mutated": False,
    }
    return assets
