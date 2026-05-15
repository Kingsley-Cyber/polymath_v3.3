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
    metrics=None,
) -> list[dict]:
    """
    Bridge detection — Phase 2 hybrid (analytics-aware, path-count fallback).

    Pre-Phase-2 behavior (active when `metrics` is None or the cache
    yields no seed-anchored bridges): Cypher path-count — entities
    reachable from ≥2 seeds within `max_hops` via RELATES_TO, ranked by
    seed-count. Community-safe (no shortestPath, no GDS).

    Phase 2 elite path (active when `metrics` is provided AND its
    `fragile_bridges` / `entity_betweenness` touch the seed set):
      • fragile_bridges supplies *interdisciplinary articulation edges*
        — removing one disconnects two domains. Each entry already has
        source/target/source_domain/target_domain. We filter to entries
        where either endpoint is a seed.
      • entity_betweenness supplies the mathematical definition of a
        bridge — a node lying on the shortest path between other
        nodes. We filter to entities NOT in the seed set, rank by
        betweenness score.
      • Both signals merge into the same result list with a `source`
        field ("fragile" | "betweenness") for traceability.

    When the elite path returns nothing meaningful (cold cache,
    metrics with no overlap, single-seed query, etc.) the original
    Cypher path-count keeps the endpoint functional.

    Result shape (additive — pre-Phase-2 fields preserved):
      {entity_id, display_name, entity_type, connected_seed_count,
       connected_seeds, betweenness?, fragile_partner?, source}
    """
    if not entity_ids:
        return []

    seed_set = set(entity_ids)

    # Phase 2 elite path — try the cached analytics first when present.
    if metrics is not None:
        elite: list[dict] = []

        # Path 1 — fragile_bridges: cross-domain articulation edges
        # anchored to at least one seed. Each entry IS a bridge edge,
        # not a single-node bridge, so we emit the non-seed endpoint
        # as the "bridge entity" and carry the partner endpoint as
        # `fragile_partner` for downstream rendering.
        fragiles = getattr(metrics, "fragile_bridges", None) or []
        for fb in fragiles:
            src = fb.get("source")
            tgt = fb.get("target")
            if src in seed_set and tgt not in seed_set:
                non_seed, partner = tgt, src
                non_seed_name = fb.get("target_name", "")
            elif tgt in seed_set and src not in seed_set:
                non_seed, partner = src, tgt
                non_seed_name = fb.get("source_name", "")
            else:
                continue
            elite.append({
                "entity_id": non_seed,
                "display_name": non_seed_name,
                "entity_type": "other",  # fragile_bridges doesn't carry type
                "connected_seed_count": 1,
                "connected_seeds": [partner],
                "fragile_partner": partner,
                "source": "fragile",
                "evidence": fb.get("evidence", ""),
            })

        # Path 2 — betweenness centrality: entities with high topological
        # bottleneck score that are NOT seeds. Filter to entities present
        # in the corpus metrics (entity_betweenness is corpus-wide; the
        # selection happens at fetch time on the routers side).
        betweenness = getattr(metrics, "entity_betweenness", None) or {}
        if betweenness:
            # Build a quick name lookup from top_pagerank if available
            # (entity_betweenness is just id→float; top_pagerank carries
            # canonical_name which is the best display label we have).
            name_map: dict[str, str] = {}
            for entry in getattr(metrics, "top_pagerank", None) or []:
                eid = entry.get("entity_id")
                if eid:
                    name_map[eid] = entry.get("canonical_name", "")
            # Sort by betweenness desc, skip seeds, cap at `limit`.
            seen_in_elite = {e["entity_id"] for e in elite}
            ranked = sorted(
                (
                    (eid, score) for eid, score in betweenness.items()
                    if eid not in seed_set and eid not in seen_in_elite
                ),
                key=lambda kv: kv[1],
                reverse=True,
            )
            for eid, score in ranked[: limit * 2]:
                if score <= 0:
                    continue
                elite.append({
                    "entity_id": eid,
                    "display_name": name_map.get(eid, ""),
                    "entity_type": "other",
                    "connected_seed_count": 0,  # betweenness is global, not seed-anchored
                    "connected_seeds": [],
                    "betweenness": float(score),
                    "source": "betweenness",
                })

        if elite:
            # Stable ordering: fragile first (lower-volume, higher-value),
            # then betweenness by score.
            elite.sort(
                key=lambda e: (
                    0 if e["source"] == "fragile" else 1,
                    -float(e.get("betweenness", 0.0)),
                )
            )
            logger.info(
                "find_bridges: elite path n=%d (fragile=%d, betweenness=%d)",
                len(elite),
                sum(1 for e in elite if e["source"] == "fragile"),
                sum(1 for e in elite if e["source"] == "betweenness"),
            )
            return elite[: int(limit)]

    # Pre-Phase-2 path-count fallback. Single-seed queries can't
    # produce path-count bridges (need ≥2 seeds) — short-circuit.
    if len(entity_ids) < 2:
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
        rows = [dict(r) async for r in result]
        # Stamp source so callers can distinguish path-count results
        # from the elite path.
        for r in rows:
            r["source"] = "path_count"
        return rows


def find_hubs(
    nodes: list[dict],
    links: list[dict],
    top_n: int = 8,
    metrics=None,
) -> list[dict]:
    """
    Hub detection — Phase 2 hybrid (analytics-aware, degree fallback).

    Pre-Phase-2 behavior (still active when `metrics` is None or the
    cache misses the current subgraph): pure-Python unique-neighbor
    degree count on the returned subgraph. Cheap, deterministic, no
    cache dependency. Cheaper than a Cypher round-trip because the
    subgraph is already in memory.

    Phase 2 elite path (active when `metrics` is provided AND its
    `top_pagerank` list intersects the current subgraph's nodes): rank
    hubs by precomputed PageRank centrality. PageRank measures
    structural importance across the FULL corpus graph, so a node with
    50 weak external references outranks a node with 100 local self-
    loops — the limitation that pure degree count produces. Each
    returned row carries `pagerank_score` so downstream surfaces (Mode
    A, frontend hub chip) can show *why* an entity ranks where it does.

    Result shape (additive only — every legacy field still present):
        {entity_id, display_name, entity_type, degree, is_seed,
         pagerank_score?, source: "pagerank" | "degree"}
    """
    if not nodes:
        return []

    index = {n["id"]: n for n in nodes}

    # Phase 2 elite path — PageRank-backed ranking when the corpus
    # metrics cache is warm AND covers the current subgraph.
    if metrics is not None:
        top_pr = getattr(metrics, "top_pagerank", None) or []
        if top_pr:
            # Intersect the cached PageRank list with the current
            # subgraph node set. The metrics are corpus-scoped (whole
            # graph), the subgraph is query-scoped — only the overlap
            # is meaningful for this hub query.
            pr_in_scope = [
                entry for entry in top_pr if entry.get("entity_id") in index
            ]
            if pr_in_scope:
                # Compute local degree alongside PageRank so the row
                # carries both signals (degree is still useful as a
                # secondary tie-break / context hint).
                local_degree: Counter[str] = Counter()
                for link in links:
                    local_degree[link["source"]] += 1
                    local_degree[link["target"]] += 1
                scored = [
                    {
                        "entity_id": entry["entity_id"],
                        "display_name": (
                            index[entry["entity_id"]].get("display_name", "")
                            or entry.get("canonical_name", "")
                        ),
                        "entity_type": index[entry["entity_id"]].get(
                            "entity_type", "other"
                        ),
                        "degree": local_degree.get(entry["entity_id"], 0),
                        "is_seed": index[entry["entity_id"]].get(
                            "is_seed", False
                        ),
                        "pagerank_score": float(entry.get("score", 0.0)),
                        "source": "pagerank",
                    }
                    for entry in pr_in_scope[:top_n]
                ]
                logger.info(
                    "find_hubs: pagerank path n=%d (top_pr=%d, in_scope=%d)",
                    len(scored), len(top_pr), len(pr_in_scope),
                )
                return scored

    # Pre-Phase-2 degree fallback — also serves when `metrics` is None,
    # the cache is cold, or the cache has zero overlap with the
    # subgraph (e.g., query landed in a frontier region not present
    # in top_pagerank's bounded list).
    if not links:
        return []

    degree: Counter[str] = Counter()
    for link in links:
        degree[link["source"]] += 1
        degree[link["target"]] += 1

    scored = [
        {
            "entity_id": nid,
            "display_name": index.get(nid, {}).get("display_name", ""),
            "entity_type": index.get(nid, {}).get("entity_type", "other"),
            "degree": deg,
            "is_seed": index.get(nid, {}).get("is_seed", False),
            "source": "degree",
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
