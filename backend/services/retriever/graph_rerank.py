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


def chunks_iter_with_score(
    chunks: Iterable[SourceChunk],
) -> Iterable[tuple[str, float]]:
    """Helper for tests — emit (chunk_id, score) pairs."""
    for c in chunks:
        yield (str(getattr(c, "chunk_id", "") or ""), float(getattr(c, "score", 0.0) or 0.0))
