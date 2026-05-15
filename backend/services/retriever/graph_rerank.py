"""Graph-based reranking boost (Sprint #1).

Multiplies each candidate chunk's pre-rerank score by

    multiplier = 1 + alpha * log1p(min(max_entity_degree, MAX_DEGREE_CAP))

where `max_entity_degree` is the highest Neo4j degree of any :Entity that
chunk's :MENTIONS edges point to. The intuition: a chunk that mentions a
hub concept (high-degree entity) is structurally more central and should
be reranked higher than a chunk that only touches obscure entities.

This is a PageRank-shaped heuristic, not actual PageRank. It runs:
  - Single Cypher query per retrieval call (one round-trip)
  - O(N) over the candidate pool
  - In-place score mutation, no schema changes

Gate: ``RETRIEVAL_GRAPH_RERANK_ENABLED`` setting (default True). Disable
via env when you want to A/B test boost-vs-no-boost on the same query.

Sits between Mode A expansion and the rerank_top_n pool cap, so the
boost can promote hub-mentioning chunks into the cap window.
"""

from __future__ import annotations

import logging
import math
from typing import Iterable

from models.schemas import SourceChunk

logger = logging.getLogger(__name__)

# Cap on the degree input to log1p so hub entities (e.g. `Document`,
# which can have thousands of mentions) don't dominate. 50 is high
# enough to differentiate genuine hubs from obscure entities (log1p(50)
# ≈ 3.93, vs log1p(1) ≈ 0.69) but low enough that runaway hubs don't
# crowd everything else into a tie at the top.
MAX_DEGREE_CAP = 50

# Alpha controls how aggressive the boost is. With alpha=0.15:
#   degree=0  → multiplier=1.00 (no boost)
#   degree=1  → multiplier=1.10
#   degree=10 → multiplier=1.36
#   degree=50 → multiplier=1.59 (the maximum, since we cap input at 50)
# This keeps even hub mentions from more than ~60% boosting a chunk —
# the cross-encoder still has plenty of room to overrule on prose pools.
DEFAULT_ALPHA = 0.15


async def apply_graph_degree_boost(
    chunks: list[SourceChunk],
    corpus_ids: list[str],
    neo4j_driver,
    *,
    alpha: float = DEFAULT_ALPHA,
) -> list[SourceChunk]:
    """Multiply each chunk's score by a degree-derived multiplier.

    Mutates `chunks` in place and returns the same list for chaining.
    No-ops when ``neo4j_driver`` is None or the pool is empty.

    The Cypher uses a single MATCH on (Chunk)-[:MENTIONS]->(Entity), so
    the runtime is bounded by the number of mention edges among the
    candidate chunks. For a typical 60-chunk pool with ~5 entities
    each, this is ~300 edge probes — sub-100ms on Neo4j with the
    chunk_id index in place.
    """
    if not chunks or neo4j_driver is None or not corpus_ids:
        return chunks

    chunk_ids: list[str] = [
        str(c.chunk_id) for c in chunks if getattr(c, "chunk_id", None)
    ]
    if not chunk_ids:
        return chunks

    cypher = """
    UNWIND $chunk_ids AS cid
    MATCH (c:Chunk {chunk_id: cid})-[:MENTIONS]->(e:Entity)
    WITH c.chunk_id AS chunk_id, e
    OPTIONAL MATCH (e)-[r:RELATES_TO]-()
    WITH chunk_id, e, count(r) AS degree
    RETURN chunk_id, max(degree) AS max_degree
    """
    degree_by_id: dict[str, int] = {}
    try:
        async with neo4j_driver.session() as session:
            result = await session.run(cypher, chunk_ids=chunk_ids)
            async for record in result:
                cid = str(record.get("chunk_id") or "")
                deg = record.get("max_degree")
                if cid and deg is not None:
                    try:
                        degree_by_id[cid] = int(deg)
                    except Exception:
                        continue
    except Exception as exc:
        logger.warning(
            "graph_rerank: degree lookup failed (%d chunks, %d corpora): %s",
            len(chunk_ids),
            len(corpus_ids),
            exc,
        )
        return chunks

    if not degree_by_id:
        # No chunks have :MENTIONS edges — common for pure-prose corpora
        # without Ghost B (use_neo4j=False). Skip silently.
        return chunks

    boosted_count = 0
    max_multiplier = 1.0
    for chunk in chunks:
        cid = getattr(chunk, "chunk_id", None)
        if not cid:
            continue
        degree = degree_by_id.get(str(cid), 0)
        if degree <= 0:
            continue
        capped = min(degree, MAX_DEGREE_CAP)
        multiplier = 1.0 + alpha * math.log1p(capped)
        chunk.score = float(chunk.score) * multiplier
        boosted_count += 1
        if multiplier > max_multiplier:
            max_multiplier = multiplier

    logger.info(
        "graph_rerank: boosted %d/%d chunks (alpha=%.2f, max_mult=%.2f)",
        boosted_count,
        len(chunks),
        alpha,
        max_multiplier,
    )
    return chunks


def compute_multiplier(degree: int, alpha: float = DEFAULT_ALPHA) -> float:
    """Pure function for unit testing. Returns the multiplier that
    would be applied to a chunk whose top-mentioned entity has the
    given degree. ``degree <= 0`` returns 1.0 (no boost)."""
    if degree <= 0:
        return 1.0
    capped = min(int(degree), MAX_DEGREE_CAP)
    return 1.0 + alpha * math.log1p(capped)


# Phase 5a — metrics-aware rerank
# ─────────────────────────────────
# Cached PageRank scores from analytics.CorpusMetrics.top_pagerank are
# normalized values in roughly the 0.001-0.10 range. To combine them
# with raw degree counts (1-50+) under the same log1p + cap math, we
# scale them up to the same numeric range. 500 maps a typical PR of
# 0.05 to a pseudo-degree of 25 — comparable to a moderately well-
# connected entity. The MAX_DEGREE_CAP ceiling clips outliers either
# way, so the chosen scale only matters for the relative ordering of
# mid-range entries.
_PR_TO_DEGREE_SCALE = 500.0


async def apply_graph_degree_boost_metrics_aware(
    chunks: list[SourceChunk],
    corpus_ids: list[str],
    neo4j_driver,
    db,
    *,
    alpha: float = DEFAULT_ALPHA,
) -> list[SourceChunk]:
    """Phase 5a — graph-rerank with cache-augmented PageRank signal.

    Layers on top of the existing degree-count path: ALSO computes a
    PageRank-derived multiplier from the cached
    `analytics.CorpusMetrics.top_pagerank` entries, then takes
    MAX(degree_pseudo, pagerank_pseudo) per chunk. An entity might be
    a high-degree local hub OR a structurally-important global
    PageRank node (or both); either justifies a score boost.

    Compared to `apply_graph_degree_boost`:
      • Same Cypher round-trip to map chunks → mentioned entities +
        their degrees. The Cypher returns the entity_id list so we
        can also look them up in the cache.
      • Adds a Mongo `find_one` per corpus to fetch the cache row.
        ~5-20ms per corpus on a warm cache; skipped silently when
        the cache row is missing.
      • Same math (`1 + alpha * log1p(min(signal, MAX_DEGREE_CAP))`)
        so the multiplier range stays 1.0-1.6 regardless of which
        signal dominates. No re-calibration of `alpha` needed.

    Cold-fallback contract:
      • db is None → skip cache lookup; use degree-only multiplier.
      • Cache lookup raises → skip cache; use degree-only multiplier.
      • Cache returns None (no row) → degree-only multiplier.
      • Cache present but entity not in top_pagerank → that entity
        contributes only its degree.
      • Cypher fails → return chunks unchanged (same as pre-Phase-5a
        existing function).

    This is the function invoked when
    `settings.RETRIEVAL_CACHE_GRAPH_METRICS=True`. The flag is OFF by
    default so the existing degree-only path stays the production
    behavior until soak-tested.
    """
    if not chunks or neo4j_driver is None or not corpus_ids:
        return chunks

    chunk_ids: list[str] = [
        str(c.chunk_id) for c in chunks if getattr(c, "chunk_id", None)
    ]
    if not chunk_ids:
        return chunks

    # Same Cypher shape as the existing function, but returns the
    # per-entity (id, degree) list instead of pre-aggregating to
    # max(degree). We need the entity_ids to look them up in the
    # PageRank cache; degree is still collected so cold-cache entries
    # contribute via the local signal.
    cypher = """
    UNWIND $chunk_ids AS cid
    MATCH (c:Chunk {chunk_id: cid})-[:MENTIONS]->(e:Entity)
    WITH c.chunk_id AS chunk_id, e
    OPTIONAL MATCH (e)-[r:RELATES_TO]-()
    WITH chunk_id, e.entity_id AS entity_id, count(r) AS degree
    RETURN chunk_id,
           collect({entity_id: entity_id, degree: degree}) AS entities
    """
    chunk_to_entities: dict[str, list[dict]] = {}
    try:
        async with neo4j_driver.session() as session:
            result = await session.run(cypher, chunk_ids=chunk_ids)
            async for record in result:
                cid = str(record.get("chunk_id") or "")
                entities = list(record.get("entities") or [])
                if cid:
                    chunk_to_entities[cid] = entities
    except Exception as exc:
        logger.warning(
            "graph_rerank metrics-aware: cypher failed (%d chunks): %s",
            len(chunk_ids), exc,
        )
        return chunks

    if not chunk_to_entities:
        return chunks

    # Fetch + merge per-corpus PageRank lookups. Best-effort; cold
    # corpora contribute nothing and their chunks fall back to the
    # degree-only multiplier (identical to pre-Phase-5a behavior).
    pagerank_lookup: dict[str, float] = {}
    if db is not None:
        try:
            from services.graph.analytics import (
                compute_corpus_change_signature,
                get_cached_metrics,
            )
            for corpus_id in corpus_ids:
                try:
                    sig = await compute_corpus_change_signature(db, corpus_id)
                    metrics = await get_cached_metrics(db, corpus_id, sig)
                    if metrics is None:
                        continue
                    for entry in getattr(metrics, "top_pagerank", None) or []:
                        eid = entry.get("entity_id")
                        try:
                            score = float(entry.get("score") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if eid and score > pagerank_lookup.get(eid, 0.0):
                            pagerank_lookup[eid] = score
                except Exception as exc:
                    logger.debug(
                        "graph_rerank metrics-aware: cache miss corpus=%s: %s",
                        corpus_id, exc,
                    )
        except ImportError as exc:
            # Analytics module unavailable — log once and continue
            # with degree-only behavior.
            logger.warning(
                "graph_rerank metrics-aware: analytics import failed (%s) — "
                "degree-only fallback",
                exc,
            )

    by_chunk_id: dict[str, SourceChunk] = {
        str(c.chunk_id): c for c in chunks if getattr(c, "chunk_id", None)
    }

    boosted_count = 0
    max_multiplier = 1.0
    pr_hit_count = 0
    for cid, entities in chunk_to_entities.items():
        chunk = by_chunk_id.get(cid)
        if chunk is None:
            continue

        max_degree = 0
        max_pr_pseudo = 0.0
        for ent in entities:
            eid = ent.get("entity_id")
            try:
                deg = int(ent.get("degree") or 0)
            except (TypeError, ValueError):
                deg = 0
            if deg > max_degree:
                max_degree = deg
            if eid is not None and eid in pagerank_lookup:
                pseudo = pagerank_lookup[eid] * _PR_TO_DEGREE_SCALE
                if pseudo > max_pr_pseudo:
                    max_pr_pseudo = pseudo
                pr_hit_count += 1

        # Combined signal: MAX of degree and pagerank-derived pseudo-degree.
        # Both pass through identical log1p + cap + alpha math, so the
        # multiplier remains in the 1.0-1.6 range regardless of which
        # signal wins.
        combined = max(
            min(max_degree, MAX_DEGREE_CAP),
            min(int(max_pr_pseudo), MAX_DEGREE_CAP),
        )
        if combined > 0:
            multiplier = 1.0 + alpha * math.log1p(combined)
            chunk.score = float(chunk.score) * multiplier
            boosted_count += 1
            if multiplier > max_multiplier:
                max_multiplier = multiplier

    logger.info(
        "graph_rerank metrics-aware: boosted %d/%d chunks "
        "(alpha=%.2f, max_mult=%.2f, pr_lookup=%d, pr_hits=%d)",
        boosted_count,
        len(chunks),
        alpha,
        max_multiplier,
        len(pagerank_lookup),
        pr_hit_count,
    )
    return chunks


def chunks_iter_with_score(
    chunks: Iterable[SourceChunk],
) -> Iterable[tuple[str, float]]:
    """Helper for tests — emit (chunk_id, score) pairs."""
    for c in chunks:
        yield (str(getattr(c, "chunk_id", "") or ""), float(getattr(c, "score", 0.0) or 0.0))
