#!/usr/bin/env python3
"""Promotion-boundary filter: the graph keeps only edges between real nodes.

EXTRACT BROADLY, PROMOTE NARROWLY
    Relation extraction uses the RELAXED entity tier (hard rules only), because
    applying the strict node gate as a precondition collapsed relation yield
    34x. A relation whose object is "strategy" is informative when its subject
    is "Kotler" -- so it belongs in the claims/evidence path.

    A NODE called "strategy" is not informative. The GRAPH is where the strict
    tier belongs, and this is that boundary.

    MEASURED on 17,185 stored relations: 21.4% have BOTH endpoints
    graph_eligible, 43.1% have one, 35.5% have neither.

WHAT THIS TOUCHES
    ONLY edges this program promoted (promote_version =
    polymath.promote.frame_backfill.v1). Legacy edges from other lanes are left
    alone -- they were not promoted under this contract and removing them would
    be an unrelated decision made silently.

SAFETY
    Exports every edge before deleting, because Neo4j deletes are irreversible
    without re-ingest. Dry-run by default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/app")

PROMOTE_VERSION = "polymath.promote.frame_backfill.v1"


def _eligibility_by_chunk(db) -> dict[str, set[str]]:
    """chunk_id -> the names the STRICT gate cleared IN THAT CHUNK.

    PER-CHUNK, NOT A GLOBAL UNION. An earlier version unioned eligible surfaces
    corpus-wide, which meant ONE eligible mention made a surface eligible
    EVERYWHERE. That inverted the filter in practice: it kept
    (customer)-affiliated_with->(company) because "customer" was eligible in
    some unrelated chunk, while dropping
    (world wide web)-created_by->(tim berners-lee) because that particular
    mention had not been cleared. Eligibility is a property of a MENTION, and
    aggregating it away destroys the signal being filtered on.
    """
    by_chunk: dict[str, set[str]] = {}
    n = 0
    for d in db.ghost_b_extractions.find(
        {"entity_quality_annotated": {"$exists": True}},
        {"chunk_id": 1, "entities": 1, "_id": 0},
    ):
        n += 1
        names = {
            k.strip().lower()
            for e in (d.get("entities") or []) if e.get("graph_eligible")
            for k in (e.get("canonical_name"), e.get("surface_form")) if k
        }
        if names:
            by_chunk[d["chunk_id"]] = names
    print(f"scanned {n:,} annotated chunks -> {len(by_chunk):,} with eligible "
          f"entities", file=sys.stderr)
    return by_chunk


def main() -> int:
    import pymongo
    from neo4j import GraphDatabase

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--export", default="/tmp/ineligible_edges.jsonl")
    args = ap.parse_args()

    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
    eligible = _eligibility_by_chunk(db)
    if not eligible:
        print("refusing: no eligible surfaces resolved (run the annotator first)",
              file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    t0 = time.time()
    with driver.session() as s:
        rows = list(s.run(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
            "WHERE r.promote_version = $pv "
            "RETURN id(r) AS rid, a.canonical_name AS s, b.canonical_name AS o, "
            "       r.predicate AS p, r.evidence_chunk_ids AS chunks, "
            "       properties(r) AS props",
            pv=PROMOTE_VERSION,
        ))
        total = len(rows)
        keep, drop = [], []
        for row in rows:
            subj = (row["s"] or "").strip().lower()
            obj = (row["o"] or "").strip().lower()
            # An edge survives when SOME chunk that evidences it cleared BOTH
            # endpoints. Scoped to the edge's own evidence, never global.
            ok = False
            for cid in (row.get("chunks") or []):
                names = eligible.get(cid)
                if names and subj in names and obj in names:
                    ok = True
                    break
            (keep if ok else drop).append(row)

        report = {
            "promoted_edges": total,
            "both_endpoints_eligible_KEEP": len(keep),
            "at_least_one_ineligible_DROP": len(drop),
            "keep_share": round(len(keep) / total, 4) if total else 0.0,
        }
        print(json.dumps(report, indent=2))
        if not args.apply:
            print("\nDRY RUN — nothing deleted.", file=sys.stderr)
            print("\nexamples that would be DROPPED:", file=sys.stderr)
            for row in drop[:8]:
                print(f"   ({row['s']}) -{row['p']}-> ({row['o']})", file=sys.stderr)
            print("\nexamples that would be KEPT:", file=sys.stderr)
            for row in keep[:8]:
                print(f"   ({row['s']}) -{row['p']}-> ({row['o']})", file=sys.stderr)
            return 0

        with open(args.export, "w", encoding="utf-8") as fh:
            for row in drop:
                fh.write(json.dumps({
                    "subject": row["s"], "object": row["o"],
                    "predicate": row["p"], "props": row["props"],
                }, default=str) + "\n")
        report["exported"] = len(drop)

        deleted = 0
        ids = [row["rid"] for row in drop]
        for i in range(0, len(ids), 5000):
            n = s.run(
                "UNWIND $ids AS rid MATCH ()-[r:RELATES_TO]->() "
                "WHERE id(r) = rid DELETE r RETURN count(*) AS n",
                ids=ids[i:i + 5000],
            ).single()["n"]
            deleted += n
        after = s.run(
            "MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS c").single()["c"]
        report.update({"deleted": deleted, "graph_edges_after": after,
                       "elapsed_s": round(time.time() - t0, 1)})
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
