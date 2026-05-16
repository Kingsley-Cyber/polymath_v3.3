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

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from neo4j import AsyncDriver

logger = logging.getLogger(__name__)

# Brain View is an overview, not a forensic edge dump. Cap the number of
# high-mention entities per document that participate in the book-to-book
# bridge traversal so large corpora don't run an all-entities cross product
# just to draw the first canvas.
BRAIN_VIEW_BRIDGE_ENTITY_CAP = 64


# Pt 7d — Entity stop-list for Brain View bridges.
#
# The brain-view bridge query joins (book → entity → entity → book), so any
# generic / structural / type-leak entity name (e.g. "Index", "this book",
# "Person", "users", "k") creates spurious cross-book bridges. The audit at
# Pt 7c revealed roughly half the top-30 highest-degree entities were noise.
#
# This loader reads `entity_stoplist.json` (sibling file) and exposes a
# compiled (exact_set, combined_regex) tuple used by `_BRAIN_VIEW_CYPHER` to
# exclude those entities from contributing to bridges. Drill-down queries
# are intentionally NOT filtered — in-book entity lists keep everything.
ENTITY_STOPLIST_PATH = Path(__file__).with_name("entity_stoplist.json")


@lru_cache(maxsize=1)
def _load_entity_stoplist() -> tuple[list[str], str]:
    """Return (exact_lowercase_list, combined_pattern_regex).

    The exact list is what gets passed to Cypher as `$stop_exact` — Neo4j
    handles the IN-membership test. The pattern is a single Java regex
    string (alternation of the JSON `patterns` entries) passed as
    `$stop_pattern` and matched with `=~`.

    Both inputs are already lowercased / anchored; the Cypher caller
    lowercases `e.display_name` before testing. Returns ([], "$^") (a
    never-matching regex) if the JSON is missing — safer to bridge-with-
    noise than to silently filter nothing.
    """
    try:
        data = json.loads(ENTITY_STOPLIST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Entity stop-list missing at %s — running with no filter", ENTITY_STOPLIST_PATH)
        return [], "$^"
    except Exception as exc:
        logger.warning("Entity stop-list failed to load (%s) — running with no filter", exc)
        return [], "$^"

    exact_raw = data.get("exact_lowercase") or []
    exact_list = sorted({str(s).strip().lower() for s in exact_raw if isinstance(s, str) and s.strip()})

    patterns = data.get("patterns") or []
    pattern_parts = [str(p).strip() for p in patterns if isinstance(p, str) and p.strip()]
    # Combine via | so a single =~ call covers all patterns. "$^" never
    # matches and serves as the "no patterns" sentinel.
    combined = "|".join(f"(?:{p})" for p in pattern_parts) if pattern_parts else "$^"
    return exact_list, combined


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

// 1. Total chunk count for this anchor.
OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
WITH d, count(DISTINCT c) AS actual_chunk_count

// 2. Pt 6 scaling fix: dominant_family + dominant_entity_type are now
//    pre-computed at ingest and stored on the :Document node. Read them
//    DIRECTLY off the node — one indexed property lookup per anchor —
//    instead of OPTIONAL MATCH'ing every Chunk → Entity per anchor on
//    every query. This drops a O(books × chunks × entities) traversal
//    to O(books). Brain View now scales linearly.
//
//    LEGACY FALLBACK: if a Document was ingested before Pt 6, its
//    dominant_* properties are null. The fallback OPTIONAL MATCH legs
//    below run only for those rows (gated via d.dominant_* IS NULL
//    inside the WHERE) so legacy docs still get a color until the
//    backfill catches up. Final pick happens at RETURN via COALESCE.
OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c_fam:Chunk)-[:MENTIONS]->(e_fam:Entity)
WHERE d.dominant_family IS NULL
  AND e_fam.canonical_family IS NOT NULL
WITH d, actual_chunk_count,
     e_fam.canonical_family AS fam_name, count(*) AS fam_n
  ORDER BY fam_n DESC
WITH d, actual_chunk_count,
     collect(fam_name)[0] AS computed_family

OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c_typ:Chunk)-[:MENTIONS]->(e_typ:Entity)
WHERE d.dominant_entity_type IS NULL
  AND e_typ.primary_entity_type IS NOT NULL
WITH d, actual_chunk_count, computed_family,
     e_typ.primary_entity_type AS type_name, count(*) AS type_n
  ORDER BY type_n DESC
WITH d, actual_chunk_count, computed_family,
     collect(type_name)[0] AS computed_type

WITH d, actual_chunk_count,
     coalesce(d.dominant_family, computed_family) AS dominant_family,
     coalesce(d.dominant_entity_type, computed_type) AS dominant_entity_type
WITH d, actual_chunk_count, dominant_family, dominant_entity_type,
     $stop_exact AS stop_exact,
     $stop_pattern AS stop_pattern,
     $bridge_entity_cap AS bridge_entity_cap,
     $corpus_ids AS corpus_ids

// 3. Pick the top local entities once. The old Brain View traversed every
//    entity on every document when computing bridges, which made the initial
//    overview far too expensive for large corpora. For the top-level canvas,
//    high-mention entities carry the useful signal; drill-down still exposes
//    the full local neighborhood for a selected book.
CALL {
    WITH d, stop_exact, stop_pattern, bridge_entity_cap
    OPTIONAL MATCH (d)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e_seed:Entity)
    WHERE NOT toLower(coalesce(e_seed.display_name, '')) IN stop_exact
      AND NOT toLower(coalesce(e_seed.display_name, '')) =~ stop_pattern
    WITH e_seed, bridge_entity_cap, count(*) AS mentions
    WHERE e_seed IS NOT NULL
    ORDER BY mentions DESC
    WITH collect(e_seed) AS ranked_entities,
         collect(coalesce(e_seed.display_name, e_seed.entity_id)) AS ranked_names,
         count(e_seed) AS entity_count,
         bridge_entity_cap
    RETURN ranked_entities[..bridge_entity_cap] AS bridge_entities,
           ranked_names[..8] AS top_entities,
           entity_count
}

// 4. Bridges to other selected anchors via the capped entity set + RELATES_TO.
//    Pt 5 — also collects the relation_family list per bridge so we can
//    pick a dominant family for edge coloring on the frontend.
//    Pt 7d — both sides of the bridge are filtered through the entity
//    stop-list ($stop_exact + $stop_pattern, loaded from
//    entity_stoplist.json) so structural / generic / type-leak entities
//    (Index, "this book", Person, users, k, …) don't manufacture spurious
//    cross-book bridges. Drill-down queries are NOT filtered.
CALL {
    WITH d, bridge_entities, corpus_ids, stop_exact, stop_pattern
    UNWIND CASE
        WHEN size(bridge_entities) = 0 THEN [null]
        ELSE bridge_entities
    END AS e
    OPTIONAL MATCH (e)-[r:RELATES_TO]->(e2:Entity)<-[:MENTIONS]-(:Chunk)<-[:HAS_CHUNK]-(d2:Document)
    WHERE e IS NOT NULL
      AND d2.corpus_id IN corpus_ids
      AND d2.is_cluster_anchor = true
      AND d2.doc_id <> d.doc_id
      AND NOT toLower(coalesce(e2.display_name, '')) IN stop_exact
      AND NOT toLower(coalesce(e2.display_name, '')) =~ stop_pattern
    WITH d2,
         count(DISTINCT e) AS shared_entities,
         count(r) AS edge_count,
         collect(r.relation_family) AS bridge_families,
         // Pt 7c: distinct concept names that form this bridge — drives the
         // on-edge label so users see what connects two books without clicking.
         collect(DISTINCT coalesce(e.display_name, e.entity_id)) AS shared_entity_names
    WHERE d2 IS NOT NULL AND shared_entities > 0
    RETURN collect({
        target_doc_id: d2.doc_id,
        target_filename: coalesce(d2.filename, d2.doc_id),
        target_corpus_id: d2.corpus_id,
        shared_entities: shared_entities,
        strength: edge_count,
        // Pt 5: first non-null relation_family seen between this pair of books.
        dominant_relation_family: head([f IN bridge_families WHERE f IS NOT NULL]),
        // Pt 7c: top 3 shared concept names.
        top_shared_entities: shared_entity_names[..3]
    }) AS raw_bridges
}

WITH d, actual_chunk_count, dominant_family, dominant_entity_type,
     coalesce(entity_count, 0) AS entity_count,
     coalesce(top_entities, []) AS top_entities,
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
    dominant_family,
    dominant_entity_type,
    d.ghost_b_success_rate AS ghost_b_success_rate,
    d.ghost_b_extracted    AS ghost_b_extracted,
    d.ghost_b_total        AS ghost_b_total,
    d.schema_lens_id  AS schema_lens_id,
    d.source_tier     AS source_tier,
    d.ingested_at     AS ingested_at,
    d.updated_at      AS updated_at,
    size(bridges)     AS bridge_count,
    bridges,
    entity_count,
    top_entities
ORDER BY size(bridges) DESC, label ASC
LIMIT $limit
"""


async def get_brain_view(
    driver: AsyncDriver,
    corpus_ids: list[str],
    *,
    limit: int = 2000,
    bridge_entity_cap: int = BRAIN_VIEW_BRIDGE_ENTITY_CAP,
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
            {source: doc_id, target: target_doc_id, strength, shared_entities,
             dominant_relation_family, top_shared_entities: [name, ...]},
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

    stop_exact, stop_pattern = _load_entity_stoplist()
    try:
        async with driver.session() as session:
            result = await session.run(
                _BRAIN_VIEW_CYPHER,
                corpus_ids=list(corpus_ids),
                limit=int(limit),
                bridge_entity_cap=max(1, int(bridge_entity_cap)),
                stop_exact=stop_exact,
                stop_pattern=stop_pattern,
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
            # Pt 5: extraction-schema facets drive deterministic frontend colors.
            "dominant_family": record.get("dominant_family"),
            "dominant_entity_type": record.get("dominant_entity_type"),
            "ghost_b_success_rate": record.get("ghost_b_success_rate"),
            "ghost_b_extracted": record.get("ghost_b_extracted"),
            "ghost_b_total": record.get("ghost_b_total"),
            "schema_lens_id": record.get("schema_lens_id"),
            "source_tier": record.get("source_tier"),
            "ingested_at": _iso_or_none(record.get("ingested_at")),
            "updated_at": _iso_or_none(record.get("updated_at")),
            "bridge_count": record.get("bridge_count") or 0,
            "bridges": record.get("bridges") or [],
            # Octopus mode — top N entity names + total entity count per
            # anchor. Capped at 8 by the Cypher; the frontend spawns these
            # as orbiting satellites for spotlight (top-bridge-count) docs.
            "entity_count": int(record.get("entity_count") or 0),
            "top_entities": list(record.get("top_entities") or []),
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
                    # Pt 5: edge color on the frontend keys off this family.
                    "dominant_relation_family": b.get("dominant_relation_family"),
                    # Pt 7c: top 3 shared concept names — drives the on-edge
                    # label so users see what connects two books at a glance.
                    "top_shared_entities": list(b.get("top_shared_entities") or []),
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
            "bridge_entity_cap": max(1, int(bridge_entity_cap)),
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
