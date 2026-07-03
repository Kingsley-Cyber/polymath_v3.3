"""B2 — promote(): the single P2→P3 crossing (POLYMATH_ARCHITECTURE §3.S5).

Turns a stored extraction row (ghost_b_extractions shape / ChunkExtraction)
into the ADDITIVE RetrievalPayload delta that makes extraction filterable at
the vector layer: concepts[] (names+aliases → recall), entity_ids[] (the
Qdrant↔Neo4j join), families/domains, relation + fact aggregates, and the
version stamps. Pure, deterministic, idempotent — same row ⇒ same delta.
Applied as an idempotent POST-ghost write (ghosts run parallel; children are
written before ghosts finish), and re-runnable as a backfill over existing
corpora with NO re-extraction.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

PROMOTE_VERSION = "polymath.promote.v1"
_PROMOTED_LIST_FIELDS = (
    "concepts",
    "entity_ids",
    "entity_families",
    "entity_domains",
    "relation_predicates",
    "relation_families",
    "fact_types",
)


def _default_entity_id(canonical_name: str) -> str:
    """Fallback slug — the backfill passes the REAL neo4j_writer fn so the
    vector-side entity_ids match graph identity exactly."""
    slug = re.sub(r"[^a-z0-9]+", "_", (canonical_name or "").lower()).strip("_")
    return f"entity:{slug}" if slug else ""


def _norm_term(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def promote(
    extraction: dict[str, Any],
    *,
    entity_id_fn: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    """Extraction row → additive RetrievalPayload delta (plain dict, ready for
    Qdrant set_payload / Mongo $set). Only promoted keys — never identity keys,
    so it cannot clobber corpus/doc/chunk/parent ids."""
    eid = entity_id_fn or _default_entity_id
    entities = extraction.get("entities") or []
    relations = extraction.get("relations") or []
    facts = extraction.get("facts") or []

    concepts: set[str] = set()
    entity_ids: set[str] = set()
    families: set[str] = set()
    domains: set[str] = set()
    for e in entities:
        name = _norm_term(e.get("canonical_name"))
        if not name:
            continue
        concepts.add(name)
        for alias in e.get("query_aliases") or []:
            a = _norm_term(alias)
            if a:
                concepts.add(a)
        ident = e.get("entity_id") or eid(name)
        if ident:
            entity_ids.add(ident)
        if e.get("canonical_family"):
            families.add(_norm_term(e["canonical_family"]))
        if e.get("domain_type"):
            domains.add(_norm_term(e["domain_type"]))

    predicates = {_norm_term(r.get("predicate")) for r in relations if r.get("predicate")}
    rel_families = {
        _norm_term(r.get("relation_family")) for r in relations if r.get("relation_family")
    }
    fact_types = {_norm_term(f.get("fact_type")) for f in facts if f.get("fact_type")}

    return {
        "concepts": sorted(concepts),
        "entity_ids": sorted(entity_ids),
        "entity_families": sorted(families),
        "entity_domains": sorted(domains),
        "relation_predicates": sorted(predicates),
        "relation_families": sorted(rel_families),
        "fact_types": sorted(fact_types),
        "has_relations": bool(relations),
        "extract_schema_version": str(
            extraction.get("schema_version") or "polymath.extract.v1"
        ),
        "promote_version": PROMOTE_VERSION,
    }


def promoted_index_fields() -> list[tuple[str, str]]:
    """(field, qdrant schema type) — the index ships in the SAME migration as
    the field (Stage-Contract rule)."""
    return [(f, "keyword") for f in _PROMOTED_LIST_FIELDS] + [("has_relations", "bool")]
