#!/usr/bin/env python3
"""Annotate stored entity mentions with a graph-eligibility verdict.

NON-DESTRUCTIVE. Marks each entity rather than deleting it, so:
  - the tagger's raw output stays auditable,
  - relations/facts/claims referencing an entity are never orphaned,
  - the whole pass is revertible by unsetting one version stamp.

Consumers opt in via entity_quality.eligible_only(). Un-annotated rows pass
through, so a corpus that predates the gate is unaffected.
"""
from __future__ import annotations

import argparse, json, os, sys, time
from collections import Counter

sys.path.insert(0, "/app")
from services.extraction.entity_quality import (  # noqa: E402
    QUALITY_GATE_VERSION, annotate_entities, new_reject_counters,
)


def main() -> int:
    import pymongo
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--df", default="/tmp/entity_df.json")
    ap.add_argument("--max-df", type=float, default=0.002)
    args = ap.parse_args()

    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
    df = {}
    if os.path.exists(args.df):
        df = json.load(open(args.df))
    q = {"entities.0": {"$exists": True},
         "entity_quality_annotated": {"$ne": QUALITY_GATE_VERSION}}
    if args.corpus:
        q["corpus_id"] = args.corpus

    total = db.ghost_b_extractions.count_documents(q)
    print(f"chunks to annotate: {total:,}", file=sys.stderr)
    ctr = new_reject_counters()
    seen = kept = 0
    chunks = 0
    ops = []
    t0 = time.time()
    cur = db.ghost_b_extractions.find(q, {"chunk_id": 1, "entities": 1, "_id": 0})
    if args.limit:
        cur = cur.limit(args.limit)
    for d in cur:
        ann = annotate_entities(d.get("entities") or [], doc_frequency=df,
                                max_doc_frequency=args.max_df, counters=ctr)
        seen += len(ann)
        kept += sum(1 for e in ann if e["graph_eligible"])
        chunks += 1
        ops.append(pymongo.UpdateOne(
            {"chunk_id": d["chunk_id"]},
            {"$set": {"entities": ann,
                      "entity_quality_annotated": QUALITY_GATE_VERSION}}))
        if args.apply and len(ops) >= 500:
            db.ghost_b_extractions.bulk_write(ops, ordered=False); ops = []
        if chunks % 20000 == 0:
            print(f"  {chunks:,} chunks ({time.time()-t0:.0f}s)", file=sys.stderr)
    if args.apply and ops:
        db.ghost_b_extractions.bulk_write(ops, ordered=False)

    print(json.dumps({
        "mode": "APPLIED" if args.apply else "DRY_RUN",
        "chunks": chunks, "mentions": seen, "graph_eligible": kept,
        "eligible_share": round(kept / seen, 4) if seen else 0.0,
        "rejections": {k: v for k, v in ctr.items() if v},
        "elapsed_s": round(time.time() - t0, 1),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
