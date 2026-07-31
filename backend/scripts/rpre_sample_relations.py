#!/usr/bin/env python3
"""Dump extracted relations with evidence for hand precision judging.

Read-only. Writes nothing to Mongo. Feeds the 20-edge spot-check that repo law
requires before any corpus-scale backfill, and the per-genre precision split
that R7 requires be measured IN ISOLATION.

    PYTHONPATH=backend local_ghost_b/.venv/bin/python \
        backend/scripts/rpre_sample_relations.py --in sample.jsonl --genre book -n 20
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from services.extraction.dep_path_extractor import new_counters  # noqa: E402
from services.extraction.spacy_relation_adapter import (  # noqa: E402
    get_spacy_extractor,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--genre", default=None)
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--max-chunks", type=int, default=1200)
    args = ap.parse_args()

    chunks = [json.loads(l) for l in open(args.inp, encoding="utf-8") if l.strip()]
    if args.genre:
        chunks = [c for c in chunks if c.get("genre") == args.genre]
    chunks = chunks[:args.max_chunks]

    ext = get_spacy_extractor()
    rows = []
    for start in range(0, len(chunks), 200):
        batch = chunks[start:start + 200]
        payload = [{"chunk_id": c["chunk_id"], "doc_id": c["doc_id"],
                    "text": c["text"], "entities": c["entities"]} for c in batch]
        ctrs = [new_counters() for _ in payload]
        for c, edges in zip(batch, ext.extract_chunks(
                payload, max_related=10, suppression_counters_list=ctrs)):
            for e in edges:
                rows.append((c["chunk_id"], e))

    print(f"genre={args.genre or 'ALL'} chunks={len(chunks)} "
          f"relations={len(rows)} rel/chunk={len(rows)/max(1,len(chunks)):.4f}\n")
    if not rows:
        return 0
    step = max(1, len(rows) // args.n)
    for i, (cid, e) in enumerate([rows[j] for j in range(0, len(rows), step)][:args.n], 1):
        ev = " ".join((e.get("ev") or "").split())
        print(f"{i:2}. ({e['sub']}) --{e['pred']}--> ({e['obj']})  cf={e['score']}")
        print(f"    EV: {ev[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
