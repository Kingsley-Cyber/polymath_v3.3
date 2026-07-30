#!/usr/bin/env python3
"""R-pre — export a deterministic stratified chunk sample for guard measurement.

Runs INSIDE polymath_v33-backend-1 (it holds MONGODB_URI). Writes JSONL that
`rpre_measure.py` then consumes on the HOST, where config/*.yaml lives and the
dep-path extractor can actually load (Stage C's real venue is the host sidecar).

Sampling is deterministic: chunks are ordered by chunk_id and selected by a
fixed stride, so the same corpora produce the same sample on every run. No
$sample, no RNG — a measurement that cannot be reproduced is not a measurement.

Usage (from repo root):
    docker cp backend/scripts/rpre_export_sample.py polymath_v33-backend-1:/app/_rpre_export.py
    docker exec -w /app polymath_v33-backend-1 python _rpre_export.py --out /tmp/rpre_sample.jsonl
    docker cp polymath_v33-backend-1:/tmp/rpre_sample.jsonl <scratchpad>/
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pymongo

# Stratified across text genres. Counts chosen to clear the >=5,000 floor with
# real representation of ASR (the corpus the low-parse guard silently zeroes).
STRATA = [
    ("8dfb070a-5cb6-4f15-8769-b253df09d12c", "ecommerce_meta",   "book",       1500),
    ("f3849304-268f-46bb-838e-912d84a68e3c", "authentic_library_v2", "book",   1200),
    ("d18713f7-2d12-4b6d-8be8-436c136f03c7", "cybersecurity_study", "paper",   1000),
    ("149a2dcb-8c61-404e-8006-1df7e3791b5a", "video_generations_schools", "asr", 1200),
    ("91e2fd28-461a-437d-8949-9581507f9a86", "markbuildsbrands_transcripts", "asr", 600),
    ("65cae4a1-d011-4ae6-8f43-ca40e2e14420", "cpcs_local_extraction", "paper",  500),
]


def export(out_path: str) -> int:
    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
    coll = db.ghost_b_extractions

    written = 0
    per_corpus: dict[str, int] = {}
    with open(out_path, "w", encoding="utf-8") as fh:
        for corpus_id, name, genre, want in STRATA:
            # Only chunks that can even produce a relation: >=2 spans + text.
            query = {
                "corpus_id": corpus_id,
                "local_extraction.entities.1": {"$exists": True},
            }
            total = coll.count_documents(query)
            if total == 0:
                print(f"  {name}: 0 eligible chunks — SKIPPED", file=sys.stderr)
                continue
            stride = max(1, total // want)
            cursor = coll.find(
                query,
                {"chunk_id": 1, "doc_id": 1, "corpus_id": 1, "text": 1,
                 "local_extraction.entities": 1, "entities": 1},
            ).sort("chunk_id", 1)

            taken = 0
            for i, doc in enumerate(cursor):
                if i % stride:
                    continue
                if taken >= want:
                    break
                text = doc.get("text") or ""
                le_ents = ((doc.get("local_extraction") or {}).get("entities")) or []
                if not text.strip() or len(le_ents) < 2:
                    continue
                fh.write(json.dumps({
                    "chunk_id": doc.get("chunk_id", ""),
                    "doc_id": doc.get("doc_id", ""),
                    "corpus_id": corpus_id,
                    "corpus_name": name,
                    "genre": genre,
                    "text": text,
                    # Span-validated entities WITH char offsets.
                    "entities": [
                        {
                            "surface": e.get("text") or "",
                            "start_char": int(e.get("start_char") or 0),
                            "end_char": int(e.get("end_char") or 0),
                            "entity_type": e.get("entity_type") or "",
                            "canonical_name": e.get("canonical_label") or "",
                        }
                        for e in le_ents
                    ],
                }) + "\n")
                taken += 1
                written += 1
            per_corpus[name] = taken
            print(f"  {name} ({genre}): {taken} of {total} eligible "
                  f"(stride {stride})", file=sys.stderr)

    print(json.dumps({"written": written, "per_corpus": per_corpus}))
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/rpre_sample.jsonl")
    args = ap.parse_args()
    n = export(args.out)
    if n < 5000:
        print(f"WARNING: only {n} chunks exported (<5000 floor)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
