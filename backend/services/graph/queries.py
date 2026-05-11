"""
Brain View Cypher queries — books-as-clusters + drill-down.

These queries key off `:Document {is_cluster_anchor: true}` written by
`neo4j_writer._upsert_document`. They are pure Cypher (no Python aggregation)
so Neo4j can use the composite `(corpus_id, is_cluster_anchor)` index and the
bridge detection scales linearly with anchor count.

Two entry points:
  • get_brain_view  — multi-corpus anchor + bridge overview (top-level canvas)
  • get_book_drilldown — single anchor's local entities + cross-book bridges
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import AsyncDriver

logger = logging.getLogger(__name__)


def _iso_or_none(value: Any) -> str | None:
    """Coerce Neo4j temporal types (DateTime / Date / Time) to ISO strings.

    FastAPI's default JSON serializer (pydantic_core) does not know how to
    handle `neo4j.time.DateTime`; without this coercion the Brain View route
    raises `PydanticSerializationError: Unable to serialize unknown type`.
    Returns the input unchanged when it is already None or a primitive.
    """
    if value is None:
        return None
    # neo4j.time.DateTime, Date, Time all expose iso_format()
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            return str(value)
    if isinstance(value, str):
        return value
    return str(value)


_BRAIN_VIEW_CYPHER = """
MATCH (d:Document)
WHERE d.corpus_id IN $corpus_ids
  AND d.is_cluster_anchor = true

OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
WITH d, count(DISTINCT c) AS actual_chunk_count

OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c1:Chunk)-[:MENTIONS]->(e:Entity)
OPTIONAL MATCH (e)-[r:RELATES_TO]->(e2:Entity)<-[:MENTIONS]-(c2:Chunk)<-[:HAS_CHUNK]-(d2:Document)
WHERE d2.corpus_id IN $corpus_ids
  AND d2.is_cluster_anchor = true
  AND d2.doc_id <> d.doc_id

WITH d, actual_chunk_count, d2,
     count(DISTINCT e) AS shared_entities,
     count(r) AS edge_count
WHERE d2 IS NULL OR shared_entities > 0

WITH d, actual_chunk_count,
     collect(
        CASE WHEN d2 IS NULL THEN NULL
             ELSE {
               target_doc_id: d2.doc_id,
               target_filename: coalesce(d2.filename, d2.doc_id),
               target_corpus_id: d2.corpus_id,
               shared_entities: shared_entities,
               strength: edge_count
             }
        END
     ) AS raw_bridges

WITH d, actual_chunk_count,
     [b IN raw_bridges WHERE b IS NOT NULL] AS bridges

RETURN
    d.doc_id          AS doc_id,
    d.corpus_id       AS corpus_id,
    coalesce(d.filename, d.doc_id) AS label,
    d.filename        AS filename,
    d.kind            AS kind,
    d.chunk_count     AS chunk_count,
    d.parent_count    AS parent_count,
    actual_chunk_count,
    d.ghost_b_success_rate AS ghost_b_success_rate,
    d.ghost_b_extracted    AS ghost_b_extracted,
    d.ghost_b_total        AS ghost_b_total,
    d.schema_lens_id  AS schema_lens_id,
    d.source_tier     AS source_tier,
    d.ingested_at     AS ingested_at,
    d.updated_at      AS updated_at,
    size(bridges)     AS bridge_count,
    bridges
ORDER BY size(bridges) DESC, label ASC
LIMIT $limit
"""


async def get_brain_view(
    driver: AsyncDriver,
    corpus_ids: list[str],
    *,
    limit: int = 2000,
) -> dict[str, Any]:
    """Top-level books-as-clusters view.

    Returns Document anchors (one per ingested book) plus pairwise bridge
    strengths computed from shared Entity mentions. Pure Cypher — uses
    `(corpus_id, is_cluster_anchor)` composite index for the anchor MATCH
    and `MENTIONS` + `RELATES_TO` traversal for bridges.

    Returns:
        {
          "documents": [
            {doc_id, corpus_id, label, filename, kind, chunk_count,
             actual_chunk_count, ghost_b_success_rate, ghost_b_extracted,
             ghost_b_total, schema_lens_id, source_tier, ingested_at,
             updated_at, bridge_count, bridges: [{target_doc_id,
             target_filename, target_corpus_id, shared_entities, strength}]},
            ...
          ],
          "bridges": [        # flattened, source/target pair view
            {source: doc_id, target: target_doc_id, strength, shared_entities},
            ...
          ],
          "meta": {corpus_count, total_documents, total_bridges, limit_applied},
        }
    """
    if not corpus_ids:
        return {
            "documents": [],
            "bridges": [],
            "meta": {"corpus_count": 0, "total_documents": 0, "total_bridges": 0, "limit_applied": limit},
        }

    try:
        async with driver.session() as session:
            result = await session.run(
                _BRAIN_VIEW_CYPHER, corpus_ids=list(corpus_ids), limit=int(limit)
            )
            records = [dict(r) async for r in result]
    except Exception as exc:
        logger.exception("Brain view query failed: %s", exc)
        return {
            "documents": [],
            "bridges": [],
            "meta": {"error": str(exc), "partial": True, "limit_applied": limit},
            "_error": True,
        }

    documents: list[dict] = []
    flat_bridges: list[dict] = []
    corpora_seen: set[str] = set()

    for record in records:
        doc: dict[str, Any] = {
            "doc_id": record["doc_id"],
            "corpus_id": record["corpus_id"],
            "label": record["label"],
            "filename": record.get("filename"),
            "kind": record.get("kind") or "book",
            "chunk_count": record.get("chunk_count") or 0,
            "parent_count": record.get("parent_count") or 0,
            "actual_chunk_count": record.get("actual_chunk_count") or 0,
            "ghost_b_success_rate": record.get("ghost_b_success_rate"),
            "ghost_b_extracted": record.get("ghost_b_extracted"),
            "ghost_b_total": record.get("ghost_b_total"),
            "schema_lens_id": record.get("schema_lens_id"),
            "source_tier": record.get("source_tier"),
            "ingested_at": _iso_or_none(record.get("ingested_at")),
            "updated_at": _iso_or_none(record.get("updated_at")),
            "bridge_count": record.get("bridge_count") or 0,
            "bridges": record.get("bridges") or [],
        }
        documents.append(doc)
        corpora_seen.add(doc["corpus_id"])
        for b in doc["bridges"]:
            flat_bridges.append(
                {
                    "source": doc["doc_id"],
                    "source_corpus_id": doc["corpus_id"],
                    "target": b["target_doc_id"],
                    "target_corpus_id": b.get("target_corpus_id"),
                    "strength": int(b.get("strength") or 0),
                    "shared_entities": int(b.get("shared_entities") or 0),
                }
            )

    return {
        "documents": documents,
        "bridges": flat_bridges,
        "meta": {
            "corpus_count": len(corpora_seen),
            "total_documents": len(documents),
            "total_bridges": len(flat_bridges),
            "limit_applied": limit,
        },
    }


_DRILLDOWN_CYPHER = """
MATCH (d:Document {doc_id: $doc_id})
WHERE d.is_cluster_anchor = true

OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)

WITH d, collect(DISTINCT c) AS chunks, collect(DISTINCT e) AS local_entities

UNWIND CASE WHEN size(local_entities) = 0 THEN [null] ELSE local_entities END AS le
OPTIONAL MATCH (le)-[r:RELATES_TO]->(le2:Entity)
WHERE le2 IS NOT NULL AND le2 IN local_entities
WITH d, chunks, local_entities,
     collect(DISTINCT {
        source_id: le.entity_id,
        target_id: le2.entity_id,
        predicate: r.predicate,
        relation_family: coalesce(r.relation_family, 'WeakAssociation'),
        confidence: r.confidence
     }) AS local_relations_raw

UNWIND CASE WHEN size(local_entities) = 0 THEN [null] ELSE local_entities END AS be
OPTIONAL MATCH (be)-[rb:RELATES_TO]->(bridge:Entity)<-[:MENTIONS]-(:Chunk)<-[:HAS_CHUNK]-(d2:Document)
WHERE d2.corpus_id IN $other_corpus_ids
  AND d2.doc_id <> $doc_id
  AND d2.is_cluster_anchor = true
WITH d, chunks, local_entities, local_relations_raw,
     d2, be, bridge,
     count(rb) AS strength
WITH d, chunks, local_entities, local_relations_raw,
     collect(DISTINCT
        CASE WHEN d2 IS NULL THEN NULL
             ELSE {
               via_entity_id: be.entity_id,
               bridge_entity_id: bridge.entity_id,
               bridge_entity_name: coalesce(bridge.display_name, bridge.canonical_name),
               target_doc_id: d2.doc_id,
               target_filename: coalesce(d2.filename, d2.doc_id),
               target_corpus_id: d2.corpus_id,
               strength: strength
             }
        END
     ) AS bridges_raw

RETURN
    d {
      .*,
      label: coalesce(d.filename, d.doc_id),
      node_kind: 'Book'
    } AS anchor,
    [e IN local_entities WHERE e IS NOT NULL | {
        entity_id: e.entity_id,
        display_name: coalesce(e.display_name, e.canonical_name),
        entity_type: coalesce(e.primary_entity_type, e.entity_type),
        object_kind: e.object_kind,
        canonical_family: e.canonical_family
    }][..$limit] AS local_entities,
    [r IN local_relations_raw WHERE r.source_id IS NOT NULL] AS local_relations,
    [b IN bridges_raw WHERE b IS NOT NULL] AS cross_book_bridges
"""


async def get_book_drilldown(
    driver: AsyncDriver,
    doc_id: str,
    other_corpus_ids: list[str],
    *,
    limit: int = 350,
) -> dict[str, Any]:
    """Single-book drill: local entities + cross-book bridges.

    Returns the anchor's local Entity neighborhood, intra-book RELATES_TO edges,
    and pointers to bridge entities that connect this book to other anchors in
    the selected corpora. `limit` caps the local_entities list — relations and
    bridges are derived from those entities so the cap controls total payload.
    """
    if not doc_id:
        return {
            "anchor": None,
            "local_entities": [],
            "local_relations": [],
            "cross_book_bridges": [],
            "meta": {"found": False},
        }

    try:
        async with driver.session() as session:
            result = await session.run(
                _DRILLDOWN_CYPHER,
                doc_id=doc_id,
                other_corpus_ids=list(other_corpus_ids or []),
                limit=int(limit),
            )
            record = await result.single()
    except Exception as exc:
        logger.exception("Drilldown query failed for doc_id=%s: %s", doc_id, exc)
        return {
            "anchor": None,
            "local_entities": [],
            "local_relations": [],
            "cross_book_bridges": [],
            "meta": {"found": False, "error": str(exc), "partial": True},
            "_error": True,
        }

    if not record or record["anchor"] is None:
        return {
            "anchor": None,
            "local_entities": [],
            "local_relations": [],
            "cross_book_bridges": [],
            "meta": {"found": False},
        }

    anchor = dict(record["anchor"]) if record["anchor"] is not None else None
    # Coerce Neo4j temporal types on the anchor so FastAPI can serialize.
    if anchor:
        for key in ("ingested_at", "updated_at"):
            if key in anchor:
                anchor[key] = _iso_or_none(anchor[key])
    local_entities = list(record["local_entities"] or [])
    local_relations = list(record["local_relations"] or [])
    cross_book_bridges = list(record["cross_book_bridges"] or [])

    return {
        "anchor": anchor,
        "local_entities": local_entities,
        "local_relations": local_relations,
        "cross_book_bridges": cross_book_bridges,
        "meta": {
            "found": True,
            "local_entity_count": len(local_entities),
            "local_relation_count": len(local_relations),
            "bridge_count": len(cross_book_bridges),
            "limit": limit,
        },
    }
