#!/usr/bin/env python3
"""Targeted relation-only promotion into Neo4j.

WHY NOT backfill_failed_graph_chunks
    That path re-writes the ENTIRE document graph — every chunk, entity and
    MENTIONS edge. MEASURED: >600s for a single 8,651-chunk document, which is
    ~20 hours for one corpus and rewrites data that is already correct. We only
    need the relations.

WHAT THIS DOES INSTEAD
    Drives neo4j_writer._upsert_relation — the SAME sanctioned edge writer,
    with its entity-id resolution, redirect handling, predicate refinement,
    relation-family and edge-strength logic — over just the backfilled
    relations. No hand-written Cypher, so promoted edges are indistinguishable
    from natively-written ones.

THE BINDING CONSTRAINT, MEASURED FIRST
    _upsert_relation does `MATCH (s:Entity {entity_id: ...})` — it MATCHES and
    never MERGEs entity nodes. An edge lands only if BOTH endpoints already
    exist as Entity nodes. Relation surfaces come from the frame extractor and
    may not correspond to any node the original ingest created. This script
    therefore reports attempted / landed / missing-endpoint so the yield is a
    measurement rather than an assumption.

DRY-RUN BY DEFAULT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/app")

BACKFILL_VERSION = "r8a.v2.frame"


async def _run(corpus_id: str | None, apply: bool, doc_limit: int | None) -> dict:
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase

    from services.ghost_b import RelationItem
    from services.graph.neo4j_writer import (
        _upsert_relation,
        canonicalize_entity_name,
        entity_id_from_name,
    )
    from services.ghost_b import SchemaContext

    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_database("polymath")
    driver = AsyncGraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )

    match: dict = {"relation_backfill.version": BACKFILL_VERSION,
                   "relations.0": {"$exists": True}}
    if corpus_id:
        match["corpus_id"] = corpus_id

    docs = [
        r["_id"] async for r in db["ghost_b_extractions"].aggregate([
            {"$match": match},
            {"$group": {"_id": {"c": "$corpus_id", "d": "$doc_id"},
                        "n": {"$sum": {"$size": "$relations"}}}},
            {"$sort": {"n": -1}},
        ])
    ]
    if doc_limit:
        docs = docs[:doc_limit]
    print(f"documents: {len(docs)}", file=sys.stderr)

    attempted = landed = missing_endpoint = 0
    miss_reasons: Counter = Counter()
    t0 = time.time()

    async def edge_count() -> int:
        async with driver.session() as s:
            res = await s.run("MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS c")
            row = await res.single()
            return int(row["c"])

    before = await edge_count()

    for i, key in enumerate(docs, 1):
        cid, did = key["c"], key["d"]
        # name_to_type from ALL entities of this doc, matching how the native
        # writer builds it.
        name_to_type: dict[str, str] = {}
        relations: list[RelationItem] = []
        async for row in db["ghost_b_extractions"].find(
            {"corpus_id": cid, "doc_id": did},
            {"entities": 1, "relations": 1, "chunk_id": 1, "_id": 0},
        ):
            for e in row.get("entities") or []:
                nm = e.get("canonical_name") or e.get("surface_form") or ""
                if nm:
                    name_to_type.setdefault(
                        canonicalize_entity_name(nm), e.get("entity_type") or ""
                    )
            chunk_id = row.get("chunk_id") or ""
            for r in row.get("relations") or []:
                relations.append((chunk_id, RelationItem(
                    subject=r.get("subject", ""),
                    predicate=r.get("predicate", ""),
                    object=r.get("object", ""),
                    object_kind=r.get("object_kind", "entity"),
                    confidence=float(r.get("confidence") or 0.0),
                    evidence_phrase=r.get("evidence_phrase", "") or "",
                )))

        # Pre-check endpoint existence so a miss is DIAGNOSED, not silent.
        ids: set[str] = set()
        for _cid_unused, r in relations:
            for nm in (r.subject, r.object):
                t = name_to_type.get(
                    canonicalize_entity_name(nm), SchemaContext.ENTITY_SENTINEL
                )
                ids.add(entity_id_from_name(nm, t))
        async with driver.session() as s:
            res = await s.run(
                "MATCH (e:Entity) WHERE e.entity_id IN $ids RETURN collect(e.entity_id) AS f",
                ids=list(ids),
            )
            row = await res.single()
            present = set(row["f"] or [])

        prov_rows: list[dict] = []
        for chunk_id, r in relations:
            attempted += 1
            st = name_to_type.get(
                canonicalize_entity_name(r.subject), SchemaContext.ENTITY_SENTINEL
            )
            ot = name_to_type.get(
                canonicalize_entity_name(r.object), SchemaContext.ENTITY_SENTINEL
            )
            sid = entity_id_from_name(r.subject, st)
            oid = entity_id_from_name(r.object, ot)
            if sid not in present or oid not in present:
                missing_endpoint += 1
                miss_reasons["subject" if sid not in present else "object"] += 1
                continue
            if apply:
                await _upsert_relation(driver, r, name_to_type)
                prov_rows.append({
                    "subject_id": sid, "object_id": oid, "predicate": r.predicate,
                    "corpus_id": cid, "doc_id": did, "chunk_id": chunk_id,
                    "evidence": r.evidence_phrase[:500],
                })
            landed += 1

        # PROVENANCE — the step that makes a promoted edge RETRIEVABLE.
        # _upsert_relation sets the edge's semantics but NOT corpus_ids; that is
        # normally done by the outer document-graph writer. Retrieval filters on
        # r.corpus_ids, so an edge without it is invisible to every
        # corpus-scoped query — written, but unreachable. MEASURED: the first
        # promotion pass produced 12,751 edges with corpus_ids = <none>.
        # Additive and membership-guarded, so re-running is idempotent.
        if apply and prov_rows:
            async with driver.session() as s:
                await s.run(
                    """
                    UNWIND $rows AS row
                    MATCH (a:Entity {entity_id: row.subject_id})
                          -[r:RELATES_TO {predicate: row.predicate}]->
                          (b:Entity {entity_id: row.object_id})
                    SET r.corpus_ids = CASE
                            WHEN r.corpus_ids IS NULL THEN [row.corpus_id]
                            WHEN row.corpus_id IN r.corpus_ids THEN r.corpus_ids
                            ELSE r.corpus_ids + [row.corpus_id] END,
                        r.evidence_doc_ids = CASE
                            WHEN r.evidence_doc_ids IS NULL THEN [row.doc_id]
                            WHEN row.doc_id IN r.evidence_doc_ids THEN r.evidence_doc_ids
                            ELSE r.evidence_doc_ids + [row.doc_id] END,
                        r.evidence_chunk_ids = CASE
                            WHEN r.evidence_chunk_ids IS NULL THEN [row.chunk_id]
                            WHEN row.chunk_id IN r.evidence_chunk_ids THEN r.evidence_chunk_ids
                            ELSE r.evidence_chunk_ids + [row.chunk_id] END,
                        r.evidence_phrases = CASE
                            WHEN r.evidence_phrases IS NULL THEN [row.evidence]
                            WHEN row.evidence IN r.evidence_phrases THEN r.evidence_phrases
                            ELSE r.evidence_phrases + [row.evidence] END,
                        r.latest_doc_id = row.doc_id,
                        r.support_count = size(coalesce(r.evidence_chunk_ids, [])),
                        r.promote_version = 'polymath.promote.frame_backfill.v1',
                        r.extract_schema_version = 'polymath.extract.local_extraction.v1'
                    """,
                    rows=prov_rows,
                )
        if i % 10 == 0:
            print(f"  {i}/{len(docs)} docs, attempted={attempted} "
                  f"landable={landed} ({time.time()-t0:.0f}s)", file=sys.stderr)

    after = await edge_count()
    await driver.close()
    return {
        "mode": "APPLIED" if apply else "DRY_RUN",
        "documents": len(docs),
        "relations_attempted": attempted,
        "relations_landable": landed,
        "missing_endpoint": missing_endpoint,
        "missing_endpoint_pct": round(missing_endpoint / attempted, 4) if attempted else 0.0,
        "missing_side": dict(miss_reasons),
        "graph_edges_before": before,
        "graph_edges_after": after,
        "graph_edge_delta": after - before,
        "elapsed_s": round(time.time() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--doc-limit", type=int, default=None)
    args = ap.parse_args()
    if not args.corpus and not args.all:
        print("refusing to run: pass --corpus <id> or --all", file=sys.stderr)
        return 2
    print(json.dumps(asyncio.run(_run(args.corpus, args.apply, args.doc_limit)), indent=2))
    if not args.apply:
        print("\nDRY RUN — nothing written.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
