#!/usr/bin/env python3
"""Backfill Stage D FACTS onto chunks the pod lane left empty.

WHY THIS EXISTS
    runpod_local_extraction.py hardcodes `relations=[]`. MEASURED 2026-07-30:
    357,846 of 362,759 chunks (98.6%) hold zero relations, and every one was
    extracted by that lane. The graph is empty because the output was never
    WRITTEN -- not because the extractor is quiet (it MEASURES 0.5476
    relations/chunk live).

WHY NO POD IS NEEDED
    The pod already stored everything the relation lane consumes: `text` plus
    span-validated `local_extraction.entities` WITH char offsets. Relations are
    a pure function of those two. So this is a local recompute at ZERO pod cost
    and zero re-extraction -- no re-parse, no re-embed, no LLM call, no
    summary spend. It is not a re-ingest.

SAFETY
    - ADDITIVE ONLY: writes solely to chunks whose `relations` array is empty.
      Never overwrites an existing relation.
    - IDEMPOTENT: stamps `fact_backfill.version`; re-running skips stamped
      chunks. Safe to interrupt and resume.
    - REVERSIBLE: every touched chunk carries the stamp, so the exact write set
      is queryable and can be undone by version.
    - DRY-RUN BY DEFAULT. `--apply` is required to write.

Usage (inside polymath_v33-backend-1, which has MONGODB_URI and /app/config):
    python backfill_relations.py --corpus <id> --limit 500          # dry run
    python backfill_relations.py --corpus <id> --apply              # write
    python backfill_relations.py --all --apply                      # everything
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import pymongo
from pymongo import UpdateOne

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.extraction.dep_path_extractor import (  # noqa: E402
    ALL_COUNTER_KEYS, new_counters,
)
from services.extraction.spacy_relation_adapter import (  # noqa: E402
    get_spacy_extractor,
)

# Bump ONLY when the relation output for identical input would change
# (extractor logic, ontology, or predicate_synonyms). Chunks stamped with an
# older version are re-eligible, which is how a ladder rung gets rolled out.
# Bumped 2026-07-30 when the pairing model was rebuilt: the engine is no longer
# shortest-path dep-path but the frame-licensed extractor, gated at
# spacy_relation_gate_v2 = 0.8015 [0.725, 0.861]. A stamp naming the wrong
# engine would misattribute every row written under it.
BACKFILL_VERSION = "facts.v1.staged"
BACKFILL_ENGINE = "enrich_stage_d"
GATE_RECEIPT = "deterministic rule extractor; no model, no provider call"
MAX_RELATED = 10


def _facts_for(payload: dict) -> list[dict]:
    """Stage D rules for one chunk. Confidence sentinels match ghost_b_local:
    1.0 deterministic (quantity/timestamp/threshold/property), 0.9 qualitative."""
    from services.ingestion.enrich import extract_facts, extract_qualitative_facts
    text = payload.get("text") or ""
    ents = [
        {"canonical_name": e.get("canonical_name") or e.get("surface") or "",
         "surface_form": e.get("surface") or "",
         "entity_type": e.get("entity_type") or ""}
        for e in payload.get("entities") or []
    ]
    if not text.strip() or not ents:
        return []
    # Document furniture reaches the fact rules the same way it reached the
    # relation lane: the upstream tagger emits "ch04", "Page 135", bare "text"
    # as entities, and the rules faithfully attach facts to them. MEASURED in
    # the 2,000-chunk canary: "<!-- section:ch04.html -->" produced
    # [threshold] ch04 = < 04. Filtered at this boundary rather than in
    # enrich.py, which is shared with the local lane and is not in scope here.
    from services.extraction.frame_extractor import _is_structural_artifact

    rows: list[dict] = []
    for f in (extract_facts(text, ents) or []):
        if _is_structural_artifact(f.get("subject", "")):
            continue
        rows.append({**f, "confidence": 1.0})
    for f in (extract_qualitative_facts(text, ents) or []):
        if _is_structural_artifact(f.get("subject", "")):
            continue
        rows.append({**f, "confidence": 0.9})
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backfill(
    corpus_id: str | None,
    apply: bool,
    limit: int | None,
    batch_size: int,
) -> dict:
    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
    coll = db.ghost_b_extractions
    ext = get_spacy_extractor()

    query: dict = {
        # Additive only: untouched chunks with nothing stored.
        "$or": [{"facts": {"$size": 0}}, {"facts": {"$exists": False}}],
        "local_extraction.entities.1": {"$exists": True},
        "fact_backfill.version": {"$ne": BACKFILL_VERSION},
    }
    if corpus_id:
        query["corpus_id"] = corpus_id

    eligible = coll.count_documents(query)
    print(f"eligible chunks: {eligible}", file=sys.stderr)
    if limit:
        print(f"  (limited to {limit})", file=sys.stderr)

    cursor = coll.find(
        query,
        {"chunk_id": 1, "doc_id": 1, "corpus_id": 1, "text": 1,
         "local_extraction.entities": 1},
    ).sort("chunk_id", 1)
    if limit:
        cursor = cursor.limit(limit)

    totals = dict.fromkeys(ALL_COUNTER_KEYS, 0)
    predicate_dist: Counter = Counter()
    processed = written = relations_made = chunks_with_rels = 0
    pending: list[UpdateOne] = []
    batch: list[dict] = []
    t0 = time.time()

    def flush_batch() -> None:
        nonlocal processed, relations_made, chunks_with_rels, pending
        if not batch:
            return
        payload = [
            {
                "chunk_id": d.get("chunk_id", ""),
                "doc_id": d.get("doc_id", ""),
                "text": d.get("text") or "",
                "entities": [
                    {
                        "surface": e.get("text") or "",
                        "start_char": int(e.get("start_char") or 0),
                        "end_char": int(e.get("end_char") or 0),
                        # Casing normalized inside the adapter boundary.
                        "entity_type": e.get("entity_type") or "",
                        "canonical_name": e.get("canonical_label") or "",
                    }
                    for e in ((d.get("local_extraction") or {}).get("entities") or [])
                ],
            }
            for d in batch
        ]
        ctrs = [new_counters() for _ in payload]
        edge_lists = [_facts_for(p) for p in payload]
        for doc, edges, ctr in zip(batch, edge_lists, ctrs):
            processed += 1
            for k, v in ctr.items():
                totals[k] = totals.get(k, 0) + v
            relations = edges
            relations_made += len(relations)
            if relations:
                chunks_with_rels += 1
            for r in relations:
                predicate_dist[r["fact_type"]] += 1
            pending.append(UpdateOne(
                {"chunk_id": doc["chunk_id"],
                 # Re-assert additive-only at write time: if anything wrote
                 # relations between our read and our write, we lose the race
                 # deliberately rather than clobber it.
                 "$or": [{"facts": {"$size": 0}},
                         {"facts": {"$exists": False}}]},
                {"$set": {
                    "facts": relations,
                    "extraction_counters": {k: v for k, v in ctr.items() if v},
                    "fact_backfill": {
                        "version": BACKFILL_VERSION,
                        "at": _now(),
                        "engine": BACKFILL_ENGINE,
                        "gate_receipt": GATE_RECEIPT,
                        "n_relations": len(relations),
                        "source": "local_extraction.entities",
                        "pod_cost": 0,
                    },
                }},
            ))
        batch.clear()

    def flush_writes() -> None:
        nonlocal written, pending
        if not pending:
            return
        if apply:
            res = coll.bulk_write(pending, ordered=False)
            written += res.modified_count
        pending = []

    for doc in cursor:
        if not (doc.get("text") or "").strip():
            continue
        batch.append(doc)
        if len(batch) >= batch_size:
            flush_batch()
            if len(pending) >= batch_size * 2:
                flush_writes()
            if processed % 2000 == 0:
                rate = processed / max(1e-9, time.time() - t0)
                print(f"  {processed} processed, {relations_made} relations "
                      f"({rate:.0f} chunk/s)", file=sys.stderr)
    flush_batch()
    flush_writes()

    elapsed = time.time() - t0
    return {
        "mode": "APPLIED" if apply else "DRY_RUN",
        "backfill_version": BACKFILL_VERSION,
        "corpus_id": corpus_id or "ALL",
        "eligible_total": eligible,
        "processed": processed,
        "db_modified": written,
        "relations_created": relations_made,
        "chunks_with_relations": chunks_with_rels,
        "relations_per_chunk": round(relations_made / processed, 4) if processed else 0.0,
        "rel_bearing_share": round(chunks_with_rels / processed, 4) if processed else 0.0,
        "elapsed_s": round(elapsed, 1),
        "chunks_per_s": round(processed / elapsed, 1) if elapsed else 0.0,
        "predicate_distribution": dict(predicate_dist.most_common(20)),
        "suppression_counters": {k: v for k, v in totals.items() if v},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=200)
    args = ap.parse_args()

    if not args.corpus and not args.all:
        print("refusing to run: pass --corpus <id> or --all", file=sys.stderr)
        return 2

    report = backfill(args.corpus, args.apply, args.limit, args.batch_size)
    print(json.dumps(report, indent=2))
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
