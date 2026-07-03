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


async def promote_doc(db, corpus_id: str, doc_id: str) -> dict[str, Any]:
    """Ingest-time promotion for ONE document (idempotent post-ghost write —
    POLYMATH_ARCHITECTURE §3.S5). Reads the doc's ok extraction rows, projects
    with promote(), ensures payload indexes, and additively set_payload's the
    child points across the per-corpus collections + mirrors onto Mongo
    children. Best-effort by contract: callers wrap it — it never raises past
    a logged failure count."""
    from config import get_settings
    from qdrant_client import AsyncQdrantClient
    from qdrant_client import models as qm
    from services.storage import qdrant_writer as qw

    def _eid(name: str) -> str:
        try:
            from services.graph import neo4j_writer as nw

            for cand in ("entity_id_for", "_entity_id_for", "_entity_id", "make_entity_id"):
                fn = getattr(nw, cand, None)
                if callable(fn):
                    try:
                        return fn(name)
                    except TypeError:
                        return fn(name, "")
        except Exception:
            pass
        return _default_entity_id(name)

    s = get_settings()
    client = AsyncQdrantClient(url=s.QDRANT_URL, timeout=30)
    try:
        cols = []
        for kind in ("naive", "hrag", "graph"):
            name = qw._col_for_corpus(corpus_id, kind)
            if await client.collection_exists(name):
                cols.append(name)
        for col in cols:
            for field_name, ftype in promoted_index_fields():
                try:
                    await client.create_payload_index(
                        collection_name=col,
                        field_name=field_name,
                        field_schema=qm.PayloadSchemaType.KEYWORD
                        if ftype == "keyword"
                        else qm.PayloadSchemaType.BOOL,
                    )
                except Exception:
                    pass  # exists — idempotent
        rows = await db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"}
        ).to_list(length=None)
        done = warn = 0
        for row in rows:
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            delta = promote(row, entity_id_fn=_eid)
            pid = qw._uuid_from_str(chunk_id)
            for col in cols:
                try:
                    await client.set_payload(collection_name=col, payload=delta, points=[pid])
                except Exception:
                    warn += 1
            await db["chunks"].update_one(
                {"corpus_id": corpus_id, "chunk_id": chunk_id}, {"$set": delta}
            )
            done += 1
        return {"promoted": done, "rows": len(rows), "payload_warns": warn}
    finally:
        try:
            await client.close()
        except Exception:
            pass
