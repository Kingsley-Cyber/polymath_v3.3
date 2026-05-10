"""
Neo4j reader — read-only graph queries for the Extraction API (Phase 9).

All Document/Chunk matches scope by corpus_id.
Entity nodes are global but accessed only through corpus-scoped chunk traversal.
"""
import logging
from typing import Optional

from neo4j import AsyncDriver

logger = logging.getLogger(__name__)


async def get_entities(
    driver: AsyncDriver,
    corpus_id: str,
    q: str = "",
    limit: int = 20,
    doc_id: Optional[str] = None,
) -> list[dict]:
    """
    Search entities mentioned by chunks in this corpus.
    Optional doc_id narrows to a single document.
    """
    if doc_id:
        cypher = """
        MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})
              -[:HAS_CHUNK]->(c:Chunk)-[m:MENTIONS]->(e:Entity)
        WHERE $q = '' OR toLower(e.normalized_name) CONTAINS toLower($q)
               OR toLower(e.display_name) CONTAINS toLower($q)
        RETURN e.entity_id         AS entity_id,
               e.normalized_name   AS normalized_name,
               e.display_name      AS display_name,
               coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
               max(m.confidence)   AS confidence,
               count(DISTINCT c)   AS mention_count
        ORDER BY mention_count DESC
        LIMIT $limit
        """
        params = {"corpus_id": corpus_id, "doc_id": doc_id, "q": q, "limit": limit}
    else:
        cypher = """
        MATCH (c:Chunk {corpus_id: $corpus_id})-[m:MENTIONS]->(e:Entity)
        WHERE $q = '' OR toLower(e.normalized_name) CONTAINS toLower($q)
               OR toLower(e.display_name) CONTAINS toLower($q)
        RETURN e.entity_id         AS entity_id,
               e.normalized_name   AS normalized_name,
               e.display_name      AS display_name,
               coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
               max(m.confidence)   AS confidence,
               count(DISTINCT c)   AS mention_count
        ORDER BY mention_count DESC
        LIMIT $limit
        """
        params = {"corpus_id": corpus_id, "q": q, "limit": limit}

    async with driver.session() as session:
        result = await session.run(cypher, **params)
        return [dict(record) async for record in result]


async def get_chunk_extraction(
    driver: AsyncDriver,
    corpus_id: str,
    chunk_id: str,
) -> dict:
    """Return all entities and relations extracted from a single chunk."""
    entity_cypher = """
    MATCH (c:Chunk {chunk_id: $chunk_id, corpus_id: $corpus_id})
          -[m:MENTIONS]->(e:Entity)
    RETURN e.entity_id       AS entity_id,
           e.normalized_name AS normalized_name,
           e.display_name    AS display_name,
           coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
           m.confidence      AS confidence,
           1                 AS mention_count
    ORDER BY m.confidence DESC
    """
    relation_cypher = """
    MATCH (c:Chunk {chunk_id: $chunk_id, corpus_id: $corpus_id})
          -[:MENTIONS]->(e1:Entity)-[r:RELATES_TO]->(e2:Entity)
    RETURN e1.entity_id   AS subject_id,
           e1.display_name AS subject_name,
           r.predicate     AS predicate,
           coalesce(r.relation_family, 'WeakAssociation') AS relation_family,
           e2.entity_id   AS object_id,
           e2.display_name AS object_name,
           r.confidence    AS confidence
    ORDER BY r.confidence DESC
    """
    params = {"chunk_id": chunk_id, "corpus_id": corpus_id}
    async with driver.session() as session:
        entity_result = await session.run(entity_cypher, **params)
        entities = [dict(r) async for r in entity_result]
        relation_result = await session.run(relation_cypher, **params)
        relations = [dict(r) async for r in relation_result]

    return {
        "chunk_id": chunk_id,
        "corpus_id": corpus_id,
        "entities": entities,
        "relations": relations,
    }


async def get_doc_extraction_summary(
    driver: AsyncDriver,
    corpus_id: str,
    doc_id: str,
) -> list[dict]:
    """Return per-chunk entity + relation counts for a document."""
    cypher = """
    MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})-[:HAS_CHUNK]->(c:Chunk)
    OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
    OPTIONAL MATCH (c)-[:MENTIONS]->(ea:Entity)-[:RELATES_TO]->(:Entity)
    RETURN c.chunk_id             AS chunk_id,
           count(DISTINCT e)      AS entity_count,
           count(DISTINCT ea)     AS relation_count
    ORDER BY chunk_id
    """
    async with driver.session() as session:
        result = await session.run(cypher, doc_id=doc_id, corpus_id=corpus_id)
        return [dict(r) async for r in result]


async def get_full_corpus_graph(
    driver: AsyncDriver,
    corpus_id: str,
    max_nodes: int = 20000,
    max_edges: int = 60000,
) -> dict:
    """Return every entity + every RELATES_TO edge scoped to a corpus, in a
    shape ready for client-side graph rendering (sigma / cosmos / d3).

    Phase K — the new WebGL GraphView consumes this instead of stitching
    together per-entity calls.

    Returns:
        {
          "nodes": [ {id, display_name, entity_type, mention_count, facets...}, ... ],
          "edges": [ {source, target, predicate, confidence}, ... ],
          "truncated": bool,
        }
    """
    # Nodes — every entity mentioned anywhere in the corpus, with a cheap
    # mention_count we can use for degree-centrality-like sizing on the client.
    nodes_cypher = """
    MATCH (c:Chunk {corpus_id: $corpus_id})-[m:MENTIONS]->(e:Entity)
    WITH e, count(DISTINCT c) AS mention_count
    RETURN e.entity_id       AS id,
           e.display_name    AS display_name,
           coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
           e.observed_entity_types AS observed_entity_types,
           e.object_kind     AS object_kind,
           e.object_kind_parent AS object_kind_parent,
           e.object_kind_root AS object_kind_root,
           e.domain_type AS domain_type,
           e.domain_type_parent AS domain_type_parent,
           e.domain_type_root AS domain_type_root,
           e.canonical_family AS canonical_family,
           e.ontology_version AS ontology_version,
           mention_count
    ORDER BY mention_count DESC
    LIMIT $max_nodes
    """
    # Edges — every RELATES_TO between two in-corpus entities.
    edges_cypher = """
    MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(a:Entity)
    MATCH (c2:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(b:Entity)
    WHERE a <> b
    MATCH (a)-[r:RELATES_TO]->(b)
    WHERE $corpus_id IN coalesce(r.corpus_ids, [])
       OR EXISTS {
           MATCH (ec:Chunk {corpus_id: $corpus_id})
           WHERE ec.chunk_id IN coalesce(r.evidence_chunk_ids, [])
       }
    RETURN DISTINCT a.entity_id AS source,
                    b.entity_id AS target,
                    r.predicate AS predicate,
                    coalesce(r.relation_family, 'WeakAssociation') AS relation_family,
                    r.confidence AS confidence
    LIMIT $max_edges
    """
    async with driver.session() as session:
        nodes_res = await session.run(
            nodes_cypher, corpus_id=corpus_id, max_nodes=max_nodes
        )
        nodes = [dict(r) async for r in nodes_res]
        edges_res = await session.run(
            edges_cypher, corpus_id=corpus_id, max_edges=max_edges
        )
        edges = [dict(r) async for r in edges_res]
    # Filter out edges pointing at nodes we didn't return (when node cap hit).
    node_ids = {n["id"] for n in nodes}
    edges = [
        e for e in edges if e["source"] in node_ids and e["target"] in node_ids
    ]
    truncated = len(nodes) == max_nodes or len(edges) == max_edges
    return {"nodes": nodes, "edges": edges, "truncated": truncated}


async def get_document_clusters_overview(
    driver: AsyncDriver,
    corpus_ids: list[str],
) -> list[dict]:
    """Cluster-only overview for the books-as-clusters graph.

    Returns one row per Document scoped to the given corpora, with cheap
    aggregates (entity count, mention count, top label entities) and ZERO
    node/edge payload. Suitable for the 500-book overview where rendering
    every internal entity at once would freeze the browser.

    Returns: [{cluster_id, corpus_id, entity_count, total_mentions,
               top_entity_ids, top_entity_names}]
    """
    if not corpus_ids:
        return []
    cypher = """
    MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
    WHERE c.corpus_id IN $corpus_ids
    WITH c.doc_id AS doc_id, c.corpus_id AS corpus_id, e,
         count(c) AS mention_count
    WITH doc_id, corpus_id, collect({
        eid: e.entity_id,
        name: coalesce(e.display_name, e.canonical_name),
        mc: mention_count
    }) AS entries
    RETURN doc_id, corpus_id,
           size(entries) AS entity_count,
           reduce(t = 0, x IN entries | t + x.mc) AS total_mentions,
           [x IN entries | x.eid][..12] AS top_entity_ids,
           [x IN entries | x.name][..12] AS top_entity_names
    """
    async with driver.session() as session:
        res = await session.run(cypher, corpus_ids=corpus_ids)
        rows = [dict(r) async for r in res]
    rows.sort(key=lambda r: r.get("entity_count", 0), reverse=True)
    return rows


async def get_documents_as_clusters(
    driver: AsyncDriver,
    corpus_ids: list[str],
    *,
    min_entity_mentions: int = 2,
    max_nodes: int = 20000,
    max_edges: int = 60000,
    top_entities_per_cluster: int = 200,
    drill_doc_id: str | None = None,
    bridge_neighbor_cap: int = 100,
) -> dict:
    """Build a books-as-clusters view: each Document is a cluster, entities
    that appear in multiple Documents become bridges between clusters.

    Returns:
        {
          "clusters": [
              {
                "cluster_id": doc_id,
                "corpus_id": corpus_id,
                "label": filename or doc_id,
                "entity_count": int,
                "top_entities": [entity_id, ...]
              }, ...
          ],
          "nodes": [
              {
                "id": entity_id,
                "display_name": str,
                "entity_type": str,
                "primary_doc_id": doc_id,
                "bridge_doc_ids": [doc_id, ...],   // other docs that mention it
                "total_mentions": int,
                "per_doc_mentions": {doc_id: count, ...}
              }, ...
          ],
          "edges": [
              {source, target, predicate, relation_family, confidence,
               source_doc_id?, cross_cluster: bool}, ...
          ],
          "truncated": bool,
        }

    `cross_cluster` on an edge is true when source and target have different
    primary_doc_id — those are the visual bridges between book clusters.
    """
    if not corpus_ids:
        return {"clusters": [], "nodes": [], "edges": [], "truncated": False}

    # Per-(doc, entity) mention counts. We filter min_entity_mentions
    # in Python after the aggregate so we keep entities that are below the
    # threshold in one doc but cross it via bridges to other docs.
    #
    # When drill_doc_id is set, restrict the universe to: that doc's entities
    # PLUS any other doc that mentions one of those entities (the bridge
    # neighbours). This is what powers a click-to-drill cluster expansion
    # without dragging in the whole corpus.
    if drill_doc_id:
        mention_cypher = """
        MATCH (c0:Chunk {doc_id: $drill_doc_id})-[:MENTIONS]->(target:Entity)
        WHERE c0.corpus_id IN $corpus_ids
        WITH collect(DISTINCT target.entity_id) AS drill_entity_ids

        MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
        WHERE c.corpus_id IN $corpus_ids
          AND e.entity_id IN drill_entity_ids
        WITH c.doc_id AS doc_id, c.corpus_id AS corpus_id, e,
             count(c) AS mention_count
        RETURN doc_id, corpus_id,
               e.entity_id AS entity_id,
               coalesce(e.display_name, e.canonical_name) AS display_name,
               coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
               mention_count
        """
    else:
        mention_cypher = """
        MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
        WHERE c.corpus_id IN $corpus_ids
        WITH c.doc_id AS doc_id, c.corpus_id AS corpus_id, e,
             count(c) AS mention_count
        RETURN doc_id, corpus_id,
               e.entity_id AS entity_id,
               coalesce(e.display_name, e.canonical_name) AS display_name,
               coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
               mention_count
        """

    edges_cypher = """
    MATCH (c:Chunk)-[:MENTIONS]->(a:Entity)
    WHERE c.corpus_id IN $corpus_ids
    MATCH (c2:Chunk)-[:MENTIONS]->(b:Entity)
    WHERE c2.corpus_id IN $corpus_ids AND a <> b
    MATCH (a)-[r:RELATES_TO]->(b)
    WHERE any(cid IN $corpus_ids WHERE cid IN coalesce(r.corpus_ids, []))
    RETURN DISTINCT a.entity_id AS source,
                    b.entity_id AS target,
                    r.predicate AS predicate,
                    coalesce(r.relation_family, 'WeakAssociation') AS relation_family,
                    r.confidence AS confidence
    LIMIT $max_edges
    """

    async with driver.session() as session:
        mention_params: dict[str, object] = {"corpus_ids": corpus_ids}
        if drill_doc_id:
            mention_params["drill_doc_id"] = drill_doc_id
        mention_res = await session.run(mention_cypher, **mention_params)
        mention_rows = [dict(r) async for r in mention_res]
        edges_res = await session.run(
            edges_cypher, corpus_ids=corpus_ids, max_edges=max_edges
        )
        edges = [dict(r) async for r in edges_res]

    # Aggregate per-entity stats: which doc has the most mentions = primary,
    # all other docs = bridges. Track per-doc mention counts for sizing.
    by_entity: dict[str, dict] = {}
    cluster_entity_lists: dict[str, list[tuple[str, int]]] = {}
    cluster_meta: dict[str, dict] = {}

    for row in mention_rows:
        eid = row["entity_id"]
        did = row["doc_id"]
        cid = row["corpus_id"]
        mc = int(row["mention_count"] or 0)

        ent = by_entity.setdefault(
            eid,
            {
                "id": eid,
                "display_name": row["display_name"],
                "entity_type": row["entity_type"],
                "per_doc_mentions": {},
                "total_mentions": 0,
                "primary_doc_id": None,
                "primary_doc_count": 0,
                "bridge_doc_ids": [],
            },
        )
        ent["per_doc_mentions"][did] = ent["per_doc_mentions"].get(did, 0) + mc
        ent["total_mentions"] += mc
        if ent["per_doc_mentions"][did] > ent["primary_doc_count"]:
            ent["primary_doc_count"] = ent["per_doc_mentions"][did]
            ent["primary_doc_id"] = did

        cluster_meta.setdefault(
            did, {"cluster_id": did, "corpus_id": cid, "entity_count": 0}
        )
        cluster_entity_lists.setdefault(did, []).append((eid, mc))

    # Compute bridge_doc_ids and apply min_entity_mentions threshold.
    nodes: list[dict] = []
    for ent in by_entity.values():
        if ent["total_mentions"] < min_entity_mentions:
            continue
        ent["bridge_doc_ids"] = [
            d for d in ent["per_doc_mentions"].keys() if d != ent["primary_doc_id"]
        ]
        ent.pop("primary_doc_count", None)
        nodes.append(ent)

    # Sort nodes by total_mentions descending for the truncation cap.
    nodes.sort(key=lambda n: n["total_mentions"], reverse=True)
    truncated_nodes = len(nodes) > max_nodes
    if truncated_nodes:
        nodes = nodes[:max_nodes]
    surviving_node_ids = {n["id"] for n in nodes}

    # Compute per-cluster top entities + entity counts using only surviving nodes.
    for did, ents in cluster_entity_lists.items():
        ents_alive = [(eid, mc) for eid, mc in ents if eid in surviving_node_ids]
        ents_alive.sort(key=lambda t: t[1], reverse=True)
        cluster_meta[did]["entity_count"] = len(ents_alive)
        cluster_meta[did]["top_entities"] = [
            eid for eid, _ in ents_alive[:top_entities_per_cluster]
        ]

    # Drop edges that point at nodes we discarded.
    edges = [
        e
        for e in edges
        if e["source"] in surviving_node_ids and e["target"] in surviving_node_ids
    ]
    truncated_edges = len(edges) >= max_edges

    # Annotate each edge as cross_cluster vs intra_cluster using primary_doc_id.
    primary_by_id = {n["id"]: n["primary_doc_id"] for n in nodes}
    for e in edges:
        sp = primary_by_id.get(e["source"])
        tp = primary_by_id.get(e["target"])
        e["cross_cluster"] = bool(sp and tp and sp != tp)

    clusters = list(cluster_meta.values())
    clusters.sort(key=lambda c: c["entity_count"], reverse=True)

    return {
        "clusters": clusters,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated_nodes or truncated_edges,
    }


async def get_entity_relations(
    driver: AsyncDriver,
    corpus_id: str,
    entity_id: Optional[str] = None,
    canonical_name: Optional[str] = None,
    depth: int = 1,
    limit: int = 20,
) -> list[dict]:
    """
    Return outgoing + incoming RELATES_TO edges for an entity.
    Corpus-scoped: entity must be mentioned by at least one chunk in the corpus.
    """
    if entity_id:
        match_clause = "MATCH (e:Entity {entity_id: $lookup})"
        lookup = entity_id
    elif canonical_name:
        match_clause = "MATCH (e:Entity {normalized_name: $lookup})"
        lookup = canonical_name.lower().strip()
    else:
        return []

    cypher = f"""
    {match_clause}
    WHERE EXISTS {{
        MATCH (c:Chunk {{corpus_id: $corpus_id}})-[:MENTIONS]->(e)
    }}
    CALL {{
        WITH e
        MATCH (e)-[r:RELATES_TO]->(e2:Entity)
        WHERE EXISTS {{
            MATCH (c2:Chunk {{corpus_id: $corpus_id}})-[:MENTIONS]->(e2)
        }}
          AND (
              $corpus_id IN coalesce(r.corpus_ids, [])
              OR EXISTS {{
                  MATCH (ec:Chunk {{corpus_id: $corpus_id}})
                  WHERE ec.chunk_id IN coalesce(r.evidence_chunk_ids, [])
              }}
          )
    RETURN e.entity_id    AS subject_id,
           e.display_name  AS subject_name,
           r.predicate     AS predicate,
           coalesce(r.relation_family, 'WeakAssociation') AS relation_family,
           e2.entity_id   AS object_id,
           e2.display_name AS object_name,
           r.confidence    AS confidence
        LIMIT $limit
        UNION ALL
        MATCH (e2:Entity)-[r:RELATES_TO]->(e)
        WHERE EXISTS {{
            MATCH (c2:Chunk {{corpus_id: $corpus_id}})-[:MENTIONS]->(e2)
        }}
          AND (
              $corpus_id IN coalesce(r.corpus_ids, [])
              OR EXISTS {{
                  MATCH (ec:Chunk {{corpus_id: $corpus_id}})
                  WHERE ec.chunk_id IN coalesce(r.evidence_chunk_ids, [])
              }}
          )
    RETURN e2.entity_id   AS subject_id,
           e2.display_name AS subject_name,
           r.predicate     AS predicate,
           coalesce(r.relation_family, 'WeakAssociation') AS relation_family,
           e.entity_id    AS object_id,
           e.display_name  AS object_name,
           r.confidence    AS confidence
        LIMIT $limit
    }}
    RETURN subject_id, subject_name, predicate, relation_family, object_id, object_name, confidence
    ORDER BY confidence DESC
    LIMIT $limit
    """
    async with driver.session() as session:
        result = await session.run(
            cypher,
            lookup=lookup,
            corpus_id=corpus_id,
            limit=limit,
        )
        return [dict(r) async for r in result]
