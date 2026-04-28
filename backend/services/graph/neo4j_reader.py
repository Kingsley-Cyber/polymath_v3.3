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
