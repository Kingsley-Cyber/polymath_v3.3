#!/usr/bin/env python3
"""Run extraction coverage checkpoints and persist receipts.

Answers the question nothing was asking: DID EACH ORGAN ACTUALLY FIRE?

Writes one receipt per corpus to `extraction_coverage` so the control plane can
read organ health without recomputing, and exits NON-ZERO when any organ is dead
or below floor — so it is usable as a gate in CI or after any ingest.

    python extraction_coverage_check.py                 # all corpora
    python extraction_coverage_check.py --corpus <id>
    python extraction_coverage_check.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.extraction.coverage_checkpoint import (  # noqa: E402
    CHECKPOINT_VERSION, STATUS_DEAD, build_pipeline, coverage_from_row,
)


def main() -> int:
    import pymongo

    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()

    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
    names = {
        (r.get("corpus_id") or str(r.get("_id"))): (r.get("name") or "")
        for r in db.corpora.find({}, {"corpus_id": 1, "name": 1, "_id": 1})
    }

    pipeline = build_pipeline()
    if args.corpus:
        pipeline = [{"$match": {"corpus_id": args.corpus}}] + pipeline

    reports = [
        coverage_from_row(row, names.get(str(row.get("_id")), ""))
        for row in db.ghost_b_extractions.aggregate(pipeline, allowDiskUse=True)
    ]
    reports.sort(key=lambda r: -r.chunks)

    if not args.no_persist:
        for rep in reports:
            db.extraction_coverage.update_one(
                {"corpus_id": rep.corpus_id,
                 "checkpoint_version": CHECKPOINT_VERSION},
                {"$set": rep.to_doc()},
                upsert=True,
            )

    if args.json:
        print(json.dumps([r.to_doc() for r in reports], indent=2))
    else:
        organs = ["entities", "facets", "relations", "facts", "claims"]
        print(f"{'corpus':<26}{'chunks':>9}  " +
              "  ".join(f"{o[:9]:>9}" for o in organs))
        print("-" * 84)
        for rep in reports:
            by = {o.organ: o for o in rep.organs}
            cells = []
            for o in organs:
                oc = by[o]
                mark = "!!" if oc.status == STATUS_DEAD else (
                    " ?" if oc.failed else "  ")
                cells.append(f"{mark}{oc.value:>7.3f}")
            nm = (rep.corpus_name or rep.corpus_id[:8])[:24]
            print(f"{nm:<26}{rep.chunks:>9,}  " + "  ".join(cells))
        print("-" * 84)
        print("  !! = DEAD ORGAN (exactly zero)   ? = below floor")

    dead = [(r.corpus_id, r.dead_organs) for r in reports if r.dead_organs]
    failing = [(r.corpus_id, r.failing_organs) for r in reports if r.failing_organs]

    if dead:
        print("\nDEAD ORGANS — an organ produced EXACTLY ZERO for a whole corpus.",
              file=sys.stderr)
        for cid, organs_ in dead:
            print(f"  {names.get(cid, cid)[:30]}: {', '.join(organs_)}",
                  file=sys.stderr)
    if failing:
        print(f"\nFAIL: {len(failing)} corpus/corpora with a failing organ.",
              file=sys.stderr)
        return 1
    print("\nOK: every organ fired above its floor in every corpus.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
