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

import logging
import re
import unicodedata
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PROMOTE_VERSION = "polymath.promote.v1"
_PROMOTED_LIST_FIELDS = (
    "mechanisms",
    "key_terms",
    "concepts",
    "entity_ids",
    "entity_families",
    "entity_domains",
    "relation_predicates",
    "relation_families",
    "fact_types",
    "related_entities",
    "graph_neighbors",
)


def _default_entity_id(canonical_name: str) -> str:
    """Fallback slug — a faithful replica of neo4j_writer.entity_id_from_name
    (lowercase -> NFKD -> strip punctuation -> collapse spaces -> hyphens),
    minus the alias map. Callers in-app get the REAL fn via _eid; this exists
    for dependency-free tests. Convention is HYPHENS: the graph writes
    entity:layered-indexing, and an underscore slug silently breaks the
    vector<->graph join for every multi-word entity."""
    name = unicodedata.normalize("NFKD", (canonical_name or "").lower().strip())
    name = re.sub(r"[^\w\s]", "", name)
    slug = re.sub(r"\s+", " ", name).strip().replace(" ", "-")
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

    # P1 — doc-local graph precompute: relation ENDPOINTS as entity ids.
    # A chunk's related_entities are the entities it asserts relations about —
    # the zero-Cypher first hop (POLYMATH_ARCHITECTURE §12.3/§12.5 P1).
    related: set[str] = set()
    for r in relations:
        s_name = _norm_term(r.get("subject"))
        if s_name:
            related.add(eid(s_name))
        if (r.get("object_kind") or "entity") == "entity":
            o_name = _norm_term(r.get("object"))
            if o_name:
                related.add(eid(o_name))
    related.discard("")

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
        "related_entities": sorted(related),
        "has_relations": bool(relations),
        "extract_schema_version": str(
            extraction.get("schema_version") or "polymath.extract.v1"
        ),
        "promote_version": PROMOTE_VERSION,
    }


def promoted_index_fields() -> list[tuple[str, str]]:
    """(field, qdrant schema type) — the index ships in the SAME migration as
    the field (Stage-Contract rule)."""
    return [(f, "keyword") for f in _PROMOTED_LIST_FIELDS] + [
        ("has_relations", "bool"),
        ("semantic_chunk_type", "keyword"),
        ("topic_key", "keyword"),
        ("neighbor_chunks", "keyword"),
        ("graph_degree", "integer"),
    ]


def doc_local_neighbor_chunks(
    chunk_eids: dict[str, list[str]], cap: int = 8
) -> dict[str, list[str]]:
    """§12.6 offline graph: chunks in the SAME doc sharing entities are
    graph-adjacent — computable in pure python from extraction rows, zero
    Cypher, deterministic (ranked by shared-entity count, then chunk_id)."""
    by_entity: dict[str, list[str]] = {}
    for cid, eids in chunk_eids.items():
        for e in eids:
            by_entity.setdefault(e, []).append(cid)
    out: dict[str, list[str]] = {}
    for cid, eids in chunk_eids.items():
        shared: dict[str, int] = {}
        for e in eids:
            for other in by_entity.get(e, []):
                if other != cid:
                    shared[other] = shared.get(other, 0) + 1
        ranked = sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))
        out[cid] = [c for c, _ in ranked[:cap]]
    return out


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

            for cand in ("entity_id_from_name", "entity_id_for", "make_entity_id"):
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
                        field_schema={
                            "keyword": qm.PayloadSchemaType.KEYWORD,
                            "bool": qm.PayloadSchemaType.BOOL,
                            "integer": qm.PayloadSchemaType.INTEGER,
                        }[ftype],
                    )
                except Exception:
                    pass  # exists — idempotent
        rows = await db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"}
        ).to_list(length=None)
        # §10.1 — parent semantics lift: child inherits its parent's
        # semantic_chunk_type / mechanisms / key_terms / topic_key.
        parent_sem: dict[str, dict] = {}
        async for pr in db["parent_chunks"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"parent_id": 1, "semantic_chunk_type": 1, "key_terms": 1,
             "mechanisms": 1, "topic_key": 1},
        ):
            parent_sem[str(pr["parent_id"])] = pr
        child_parent: dict[str, str] = {}
        async for cr in db["chunks"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"chunk_id": 1, "parent_id": 1},
        ):
            child_parent[str(cr["chunk_id"])] = str(cr.get("parent_id") or "")
        # P1 — cross-doc 1-hop neighborhood, ONE Cypher for the whole doc,
        # capped + sorted (deterministic), best-effort (Neo4j down → field
        # simply absent; Mode A falls back to live expansion).
        neighbor_map: dict[str, list[str]] = {}
        degree_map: dict[str, int] = {}
        foreign_chunks_map: dict[str, list[str]] = {}
        chunk_eids_local: dict[str, list[str]] = {
            str(row.get("chunk_id")): promote(row, entity_id_fn=_eid).get("entity_ids", [])
            for row in rows
            if row.get("chunk_id")
        }
        local_adjacency = doc_local_neighbor_chunks(chunk_eids_local)
        transient_driver = None
        try:
            from services.ingestion_service import ingestion_service as _ing

            driver = getattr(_ing, "neo4j_driver", None)
            if driver is None:
                # script/backfill process — app lifespan never connected the
                # service; build a transient driver from settings instead of
                # silently skipping every graph field.
                try:
                    from config import get_settings as _gs

                    _st = _gs()
                    if _st.NEO4J_ENABLED:
                        from neo4j import AsyncGraphDatabase

                        transient_driver = AsyncGraphDatabase.driver(
                            _st.NEO4J_URI,
                            auth=(_st.NEO4J_USER, _st.NEO4J_PASSWORD),
                        )
                        driver = transient_driver
                except Exception:
                    driver = None
            all_eids = sorted({
                e for row in rows
                for e in promote(row, entity_id_fn=_eid).get("entity_ids", [])
            })
            if driver is not None and all_eids:
                async with driver.session() as sess:
                    res = await sess.run(
                        "MATCH (e:Entity)-[r:RELATES_TO]-(n:Entity) "
                        "WHERE e.entity_id IN $ids "
                        "WITH e, n, r ORDER BY r.confidence DESC "
                        "RETURN e.entity_id AS eid, "
                        "collect(DISTINCT n.entity_id)[..12] AS nbrs, "
                        "count(DISTINCT n) AS deg",
                        ids=all_eids,
                    )
                    async for rec in res:
                        neighbor_map[rec["eid"]] = list(rec["nbrs"] or [])
                        degree_map[rec["eid"]] = int(rec["deg"] or 0)
                    res2 = await sess.run(
                        "MATCH (e:Entity)<-[:MENTIONS]-(n:Chunk) "
                        "WHERE e.entity_id IN $ids "
                        "AND NOT n.chunk_id STARTS WITH $doc_prefix "
                        "RETURN e.entity_id AS eid, "
                        "collect(DISTINCT n.chunk_id)[..8] AS chunks",
                        ids=all_eids,
                        doc_prefix=doc_id,
                    )
                    async for rec in res2:
                        foreign_chunks_map[rec["eid"]] = list(rec["chunks"] or [])
        except Exception as exc:  # noqa: BLE001 — hops fall back to live Cypher
            logger.warning("promote_doc graph pass failed (fields skipped): %s", exc)
            neighbor_map = {}
        finally:
            if transient_driver is not None:
                try:
                    await transient_driver.close()
                except Exception:  # noqa: BLE001
                    pass

        done = warn = 0
        for row in rows:
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            delta = promote(row, entity_id_fn=_eid)
            ps = parent_sem.get(child_parent.get(chunk_id, ""), {})
            if ps.get("semantic_chunk_type"):
                delta["semantic_chunk_type"] = ps["semantic_chunk_type"]
            if ps.get("topic_key"):
                delta["topic_key"] = ps["topic_key"]
            if ps.get("mechanisms"):
                delta["mechanisms"] = ps["mechanisms"]
            if ps.get("key_terms"):
                delta["key_terms"] = ps["key_terms"]
            own = set(delta.get("entity_ids") or [])
            if neighbor_map:
                nbrs: set[str] = set()
                for e in own:
                    nbrs.update(neighbor_map.get(e, []))
                nbrs -= own
                if nbrs:
                    delta["graph_neighbors"] = sorted(nbrs)[:12]
            # §12.6 — chunk-level adjacency: doc-local (python) first, then
            # cross-doc mention neighbors (Neo4j), deduped, capped 8.
            ncs: list[str] = list(local_adjacency.get(chunk_id, []))
            seen_nc = set(ncs)
            for e in sorted(own):
                for fc in foreign_chunks_map.get(e, []):
                    if fc not in seen_nc and fc != chunk_id:
                        ncs.append(fc)
                        seen_nc.add(fc)
            if ncs:
                delta["neighbor_chunks"] = ncs[:8]
            if degree_map and own:
                delta["graph_degree"] = max(degree_map.get(e, 0) for e in own)
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
        lexicon_result: dict[str, Any] = {}
        try:
            from services.ingestion.corpus_lexicon import (
                refresh_and_index_document_lexicon,
            )

            lexicon_result = await refresh_and_index_document_lexicon(
                db,
                client,
                corpus_id=corpus_id,
                doc_id=doc_id,
            )
        except Exception as exc:  # lexicon is enrichment, never queryability
            logger.warning(
                "promote_doc lexicon refresh failed doc=%s corpus=%s: %s",
                doc_id[:12],
                corpus_id[:8],
                exc,
            )
            try:
                await db["documents"].update_one(
                    {"corpus_id": corpus_id, "doc_id": doc_id},
                    {
                        "$set": {
                            "lexicon_state": "lexicon_pending",
                            "lexicon_last_error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    },
                )
            except Exception:
                pass
        return {
            "promoted": done,
            "rows": len(rows),
            "payload_warns": warn,
            "lexicon": lexicon_result,
        }
    finally:
        try:
            await client.close()
        except Exception:
            pass
