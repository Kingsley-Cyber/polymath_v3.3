"""
Graph discovery query — Phase 17 Wave 1 (Knowledge Mode).

Powers the "Agent Query" tab in GraphView. Unlike chat retrieval (which reads
chunk *text*), graph query reads topology:

  1. Extract query entities — match query words against Entity nodes in the corpus
  2. Expand subgraph — N-hop RELATES_TO expansion from those seeds
  3. Find bridges — entities that sit on paths between ≥2 seeds
  4. Find hubs — nodes with highest degree in the returned subgraph
  5. Find gaps — query-entity pairs with NO direct RELATES_TO edge

All Cypher is written for Neo4j **Community Edition** (no `shortestPath()`,
no GDS procedures). Corpus-scoped via the `Chunk.corpus_id` existence checks.

Reuse notes:
  - Driver comes from `ingestion_service.neo4j_driver` via the `_require_neo4j()`
    guard in `routers/graph.py` — same pattern as existing graph endpoints.
  - Expansion Cypher mirrors the weighted pattern from `mode_a.py` (Phase 16.1)
    but traverses `RELATES_TO` (entity-entity) instead of `MENTIONS`
    (chunk-entity).
"""

from __future__ import annotations

import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

# Very small stop-word list for the entity-name matcher. We don't want to match
# on "the", "and", etc. — not exhaustive, just noise reduction.
_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "has", "have", "in", "is", "it", "its", "of", "on", "or", "that", "the",
        "this", "to", "was", "were", "what", "when", "where", "which", "who",
        "why", "will", "with", "how", "do", "does", "did", "about", "between",
        "vs", "versus", "compared",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]{1,}")


def _tokenize(query: str) -> list[str]:
    """Extract meaningful tokens from the query, lowercased, stop-filtered."""
    return [
        t.lower()
        for t in _TOKEN_RE.findall(query or "")
        if t.lower() not in _STOP_WORDS and len(t) > 1
    ]


async def extract_query_entities(
    query: str,
    corpus_id: str,
    driver,
    limit_per_token: int = 3,
    qdrant=None,
) -> list[dict]:
    """
    Match query tokens against Entity nodes mentioned in this corpus.

    Phase 1 hybrid (additive, low-risk):
      • Path A — literal `CONTAINS` match on entity name fields. Tokens
        from the query are checked against normalized_name / canonical_name /
        display_name. Fast, deterministic, works without any cache or
        embedder being warm. Original behavior, unchanged.
      • Path B — vector scope via `analytics.query_scope_entities`. The
        query is embedded, the per-corpus `naive` Qdrant collection is
        searched for top-K chunks, and those chunks' MENTIONS are
        reciprocal-rank-fused into a set of relevant entity_ids. Only
        runs when a `qdrant` client is supplied AND the query is
        non-trivial.

    The two seed sets are merged at Cypher-WHERE-clause time so a single
    pass scores both. Literal matches drive `token_score` (count × token
    overlap). Vector matches drive `vector_match` (boolean). Final
    `score` is a weighted blend that lets either path surface an entity
    even when the other misses it — fixes the synonym/paraphrase blind
    spot of pure CONTAINS without sacrificing fast literal lookup.

    Returns: list of {entity_id, display_name, entity_type, mention_count,
                      score, vector_match, sources}
    Sources is the list of which paths claimed each seed
    (`["literal"]`, `["vector"]`, or `["literal", "vector"]`) — useful
    for downstream tracing.
    """
    tokens = _tokenize(query)

    # Path B — vector scope (best-effort, additive). Failures return an
    # empty set and we silently fall through to the literal path.
    vector_seed_ids: set[str] = set()
    if qdrant is not None and query and len(query.strip()) >= 3:
        try:
            from services.graph.analytics import query_scope_entities
            vector_seed_ids = await query_scope_entities(
                qdrant=qdrant,
                neo4j_driver=driver,
                corpus_id=corpus_id,
                query=query,
            )
        except Exception as exc:
            # Never fail the whole extraction because of a Qdrant /
            # embedder hiccup. The literal path keeps the endpoint
            # functional, just with the pre-Phase-1 synonym blind spot.
            logger.warning(
                "extract_query_entities: vector path failed (%s) — falling back to CONTAINS",
                exc,
            )
            vector_seed_ids = set()

    if not tokens and not vector_seed_ids:
        return []

    # Single Cypher pass — matches via EITHER literal token CONTAINS OR
    # the vector_seed_ids set. Mention_count comes from the same MENTIONS
    # subquery. `vector_match` flag rides through so the post-hydrate
    # scoring can boost vector hits even when their name has no token
    # overlap (the whole point of the vector path).
    cypher = """
    MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity)
    WHERE (
        size($tokens) > 0 AND ANY(tok IN $tokens WHERE
            toLower(coalesce(e.normalized_name, '')) CONTAINS tok OR
            toLower(coalesce(e.canonical_name, '')) CONTAINS tok OR
            toLower(coalesce(e.display_name, '')) CONTAINS tok
        )
    ) OR e.entity_id IN $vector_seed_ids
    WITH e, count(DISTINCT c) AS mention_count
    RETURN
        e.entity_id     AS entity_id,
        coalesce(e.display_name, e.normalized_name, '') AS display_name,
        coalesce(e.primary_entity_type, e.entity_type, 'other') AS entity_type,
        mention_count
    ORDER BY mention_count DESC
    LIMIT $hard_limit
    """
    # Hard limit budget — give vector seeds room to land on top even
    # when the literal path would have already filled the result set.
    hard_limit = max(
        max(1, limit_per_token * max(1, len(tokens))) * 3,
        len(vector_seed_ids) + 10,
    )

    async with driver.session() as session:
        result = await session.run(
            cypher,
            corpus_id=corpus_id,
            tokens=tokens,
            vector_seed_ids=list(vector_seed_ids),
            hard_limit=hard_limit,
        )
        rows = [dict(r) async for r in result]

    if not rows:
        logger.info(
            "graph_query.extract_query_entities: no entities matched "
            "tokens=%s vector_seeds=%d",
            tokens,
            len(vector_seed_ids),
        )
        return []

    # Score blend: literal-overlap × mentions, plus a vector bonus when
    # the entity_id is in the vector seed set. Vector bonus dominates
    # when literal overlap is 0 (synonym/paraphrase case), tokens
    # dominate when both paths matched (high-confidence convergence).
    for r in rows:
        name_low = (r.get("display_name") or "").lower()
        overlap = sum(1 for t in tokens if t in name_low)
        mentions = r.get("mention_count", 1)
        is_vector = r["entity_id"] in vector_seed_ids
        r["vector_match"] = is_vector
        # Sources: which path identified this seed. Both paths can claim
        # the same entity — that's the strongest signal.
        sources: list[str] = []
        if overlap > 0:
            sources.append("literal")
        if is_vector:
            sources.append("vector")
        r["sources"] = sources
        # Score: literal contribution + vector bonus. The +0.5 baseline
        # on the vector side means a synonym-only hit (overlap=0) still
        # ranks ABOVE a pure-mention-count literal hit with zero name
        # overlap — that's the synonym fix.
        literal_score = overlap * mentions
        vector_bonus = (0.5 + 0.5 * min(1.0, mentions / 10)) * mentions if is_vector else 0.0
        r["score"] = literal_score + vector_bonus

    rows.sort(key=lambda r: r["score"], reverse=True)
    # Result cap: the original per-token budget plus headroom for vector
    # hits that the token budget alone would have dropped.
    result_cap = max(
        len(tokens) * limit_per_token,
        min(len(vector_seed_ids), 8),
    )
    return rows[:result_cap]


async def expand_subgraph(
    entity_ids: list[str],
    corpus_id: str,
    driver,
    max_hops: int = 2,
    limit: int = 50,
) -> dict:
    """
    N-hop RELATES_TO expansion from seed entities, corpus-scoped.

    Returns: {nodes: [...], links: [...]} where:
      - nodes: [{id, display_name, entity_type, is_seed, mention_count}]
      - links: [{source, target, predicate, confidence}]
    """
    if not entity_ids:
        return {"nodes": [], "links": []}

    # Cap hops at 3 to keep traversal bounded. Path patterns with variable
    # depth (`*1..3`) are Community-safe as long as we LIMIT the result set.
    hops = max(1, min(int(max_hops), 3))

    cypher = f"""
    MATCH (seed:Entity)
    WHERE seed.entity_id IN $entity_ids

    // Find entities reachable within N hops, scoped to the corpus
    OPTIONAL MATCH path = (seed)-[rels:RELATES_TO*1..{hops}]-(other:Entity)
    WHERE EXISTS {{
        MATCH (c:Chunk {{corpus_id: $corpus_id}})-[:MENTIONS]->(other)
    }}
      AND all(rel IN rels WHERE $corpus_id IN coalesce(rel.corpus_ids, []))

    WITH collect(DISTINCT seed) AS seeds, collect(DISTINCT other) AS others
    WITH [n IN seeds + others WHERE n IS NOT NULL] AS all_entities

    UNWIND all_entities AS e
    WITH DISTINCT e, all_entities
    // Optional mention count scoped to corpus for hub sizing
    OPTIONAL MATCH (mc:Chunk {{corpus_id: $corpus_id}})-[:MENTIONS]->(e)
    WITH e, count(DISTINCT mc) AS mention_count, all_entities

    RETURN
        e.entity_id                                       AS id,
        coalesce(e.display_name, e.normalized_name, '')   AS display_name,
        coalesce(e.primary_entity_type, e.entity_type, 'other') AS entity_type,
        mention_count,
        e.entity_id IN $entity_ids                        AS is_seed
    LIMIT $limit
    """

    async with driver.session() as session:
        result = await session.run(
            cypher,
            entity_ids=entity_ids,
            corpus_id=corpus_id,
            limit=int(limit),
        )
        node_rows = [dict(r) async for r in result]

    if not node_rows:
        return {"nodes": [], "links": []}

    node_ids = [n["id"] for n in node_rows]

    # Pull RELATES_TO edges whose endpoints are both in the returned node set.
    edge_cypher = """
    MATCH (a:Entity)-[r:RELATES_TO]-(b:Entity)
    WHERE a.entity_id IN $node_ids AND b.entity_id IN $node_ids
      AND a.entity_id < b.entity_id  // de-dupe undirected edges
      AND $corpus_id IN coalesce(r.corpus_ids, [])
    RETURN
        a.entity_id                               AS source,
        b.entity_id                               AS target,
        coalesce(r.predicate, 'related_to')       AS predicate,
        coalesce(r.relation_family, 'WeakAssociation') AS relation_family,
        coalesce(r.confidence, 0.5)               AS confidence
    """

    async with driver.session() as session:
        result = await session.run(edge_cypher, node_ids=node_ids, corpus_id=corpus_id)
        edge_rows = [dict(r) async for r in result]

    return {"nodes": node_rows, "links": edge_rows}


async def find_bridges(
    driver,
    entity_ids: list[str],
    corpus_id: str,
    max_hops: int = 2,
    limit: int = 10,
) -> list[dict]:
    """
    Bridge detection — Community-safe version (no shortestPath, no GDS).

    A "bridge" is an Entity reachable from ≥2 seed entities within `max_hops`
    via RELATES_TO, that is NOT itself a seed. Ranked by the number of seeds
    it connects (higher = more central).
    """
    if len(entity_ids) < 2:
        # Bridges are only meaningful between ≥2 seed entities.
        return []

    hops = max(1, min(int(max_hops), 3))

    cypher = f"""
    MATCH (seed:Entity)
    WHERE seed.entity_id IN $entity_ids

    MATCH (seed)-[:RELATES_TO*1..{hops}]-(bridge:Entity)
    WHERE NOT bridge.entity_id IN $entity_ids
      AND EXISTS {{
          MATCH (c:Chunk {{corpus_id: $corpus_id}})-[:MENTIONS]->(bridge)
      }}

    WITH bridge, collect(DISTINCT seed.entity_id) AS connected_seeds
    WHERE size(connected_seeds) >= 2

    RETURN
        bridge.entity_id                                        AS entity_id,
        coalesce(bridge.display_name, bridge.normalized_name, '') AS display_name,
        coalesce(bridge.primary_entity_type, bridge.entity_type, 'other') AS entity_type,
        size(connected_seeds)                                   AS connected_seed_count,
        connected_seeds                                         AS connected_seeds
    ORDER BY connected_seed_count DESC
    LIMIT $limit
    """

    async with driver.session() as session:
        result = await session.run(
            cypher,
            entity_ids=entity_ids,
            corpus_id=corpus_id,
            limit=int(limit),
        )
        return [dict(r) async for r in result]


def find_hubs(
    nodes: list[dict],
    links: list[dict],
    top_n: int = 8,
) -> list[dict]:
    """
    Pure-Python degree count on the returned subgraph.

    Hub = node with highest unique neighbor count. Cheaper than a Cypher
    round-trip since we already have the subgraph in memory.
    """
    if not nodes or not links:
        return []

    degree: Counter[str] = Counter()
    for link in links:
        degree[link["source"]] += 1
        degree[link["target"]] += 1

    index = {n["id"]: n for n in nodes}
    scored = [
        {
            "entity_id": nid,
            "display_name": index.get(nid, {}).get("display_name", ""),
            "entity_type": index.get(nid, {}).get("entity_type", "other"),
            "degree": deg,
            "is_seed": index.get(nid, {}).get("is_seed", False),
        }
        for nid, deg in degree.most_common(top_n * 2)
        if nid in index
    ]
    return scored[:top_n]


async def find_gaps(
    driver,
    entity_ids: list[str],
) -> list[dict]:
    """
    Gap detection — for each unordered pair of seed entities, check whether a
    direct RELATES_TO edge exists. If not, that's a "gap" — the corpus mentions
    both entities, but never places them in direct relation. That's often
    interesting: either the relationship exists in the world but wasn't
    extracted, or the corpus genuinely doesn't connect them.

    Uses `r IS NULL` with OPTIONAL MATCH (the corrected Cypher from the
    Phase 17 vetted-analysis).
    """
    if len(entity_ids) < 2:
        return []

    cypher = """
    UNWIND $entity_ids AS eid_a
    UNWIND $entity_ids AS eid_b
    WITH eid_a, eid_b WHERE eid_a < eid_b

    MATCH (a:Entity {entity_id: eid_a})
    MATCH (b:Entity {entity_id: eid_b})

    OPTIONAL MATCH (a)-[r:RELATES_TO]-(b)

    WITH a, b, r WHERE r IS NULL

    RETURN
        a.entity_id                                          AS entity_a_id,
        coalesce(a.display_name, a.normalized_name, '')      AS entity_a_name,
        b.entity_id                                          AS entity_b_id,
        coalesce(b.display_name, b.normalized_name, '')      AS entity_b_name
    """

    async with driver.session() as session:
        result = await session.run(cypher, entity_ids=entity_ids)
        return [dict(r) async for r in result]
