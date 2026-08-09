#!/usr/bin/env python3
"""Purge RELATES_TO edges belonging only to corpora that no longer exist.

WHY THIS IS A CORRECTNESS FIX, NOT HOUSEKEEPING
    Entity nodes are GLOBAL. Two hot retrieval paths anchor on an entity and
    then walk RELATES_TO with no corpus filter on the edge:
      services/retriever/graph_decoration.py  MATCH (seed)-[r:RELATES_TO]-(neighbor)
      services/retriever/graph_rerank.py      OPTIONAL MATCH (e)-[r:RELATES_TO]-()
    So an entity mentioned in a LIVE corpus chunk drags in edges from DELETED
    corpora, which then decorate winners and inflate the degree used to rerank.

    MEASURED on ecommerce_meta (3,000 seed entities): 111,180 reachable
    RELATES_TO edges, of which 105,461 (94.9%) come from dead corpora.

WHAT COUNTS AS LIVE
    A corpus with a row in `corpora` AND at least one chunk in
    `ghost_b_extractions`. A corpus record with no content is NOT live — that
    is exactly polymath_v2's situation (290,266 edges, 0 documents).

SAFETY
    - Deletes ONLY edges whose corpus_ids is NON-EMPTY and contains no live
      corpus. Edges with null/empty corpus_ids are REPORTED, never deleted:
      they cannot be proven orphaned, and one of our own promotion passes
      briefly produced some.
    - Exports every edge it will delete to JSONL first. Neo4j deletes are not
      reversible without re-ingest, so the export is the receipt and the
      only undo material.
    - DRY-RUN BY DEFAULT.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/app")


def _live_corpus_ids() -> list[str]:
    import pymongo
    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
    registered = {
        (r.get("corpus_id") or str(r.get("_id")))
        for r in db.corpora.find({}, {"corpus_id": 1, "_id": 1})
    }
    with_content = set(db.ghost_b_extractions.distinct("corpus_id"))
    live = sorted(registered & with_content)
    print(f"registered corpora: {len(registered)}  with content: {len(with_content)}  "
          f"LIVE: {len(live)}", file=sys.stderr)
    return live


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--export", default="/tmp/orphan_edges_export.jsonl")
    ap.add_argument("--batch", type=int, default=10000)
    args = ap.parse_args()

    from neo4j import GraphDatabase

    live = _live_corpus_ids()
    if not live:
        print("refusing to purge: no live corpora resolved", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    report: dict = {"live_corpora": len(live)}
    t0 = time.time()
    with driver.session() as s:
        total = s.run("MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS c").single()["c"]
        unstamped = s.run(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.corpus_ids IS NULL OR size(r.corpus_ids)=0 "
            "RETURN count(r) AS c"
        ).single()["c"]
        orphan = s.run(
            "MATCH ()-[r:RELATES_TO]->() "
            "WHERE r.corpus_ids IS NOT NULL AND size(r.corpus_ids)>0 "
            "  AND NOT any(c IN r.corpus_ids WHERE c IN $live) "
            "RETURN count(r) AS c", live=live
        ).single()["c"]
        report.update({
            "edges_total": total,
            "edges_unstamped_REPORTED_NOT_DELETED": unstamped,
            "edges_orphaned_to_delete": orphan,
            "edges_retained": total - orphan,
        })
        print(json.dumps(report, indent=2))

        if not args.apply:
            print("\nDRY RUN — nothing deleted.", file=sys.stderr)
            return 0

        # Export first: the only undo material for a Neo4j delete.
        print(f"exporting {orphan:,} edges to {args.export} …", file=sys.stderr)
        written = 0
        with open(args.export, "w", encoding="utf-8") as fh:
            res = s.run(
                "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
                "WHERE r.corpus_ids IS NOT NULL AND size(r.corpus_ids)>0 "
                "  AND NOT any(c IN r.corpus_ids WHERE c IN $live) "
                "RETURN a.entity_id AS s, b.entity_id AS o, properties(r) AS p",
                live=live,
            )
            for row in res:
                fh.write(json.dumps({"subject_id": row["s"], "object_id": row["o"],
                                     "props": row["p"]}, default=str) + "\n")
                written += 1
        report["exported"] = written

        deleted = 0
        while True:
            n = s.run(
                "MATCH ()-[r:RELATES_TO]->() "
                "WHERE r.corpus_ids IS NOT NULL AND size(r.corpus_ids)>0 "
                "  AND NOT any(c IN r.corpus_ids WHERE c IN $live) "
                "WITH r LIMIT $batch DELETE r RETURN count(*) AS n",
                live=live, batch=args.batch,
            ).single()["n"]
            deleted += n
            print(f"  deleted {deleted:,}", file=sys.stderr)
            if n == 0:
                break
        after = s.run("MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS c").single()["c"]
        report.update({"deleted": deleted, "edges_after": after,
                       "elapsed_s": round(time.time() - t0, 1)})
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
