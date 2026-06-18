#!/usr/bin/env python3
"""B1 backfill — reclassify already-ingested `body` chunks with the extended
section classifier (R1: links / unheaded reference lists / publisher
boilerplate) and propagate the new chunk_kind to Mongo + every Qdrant lane so
the NOISY_KINDS retrieval filter excludes them.

Only ever changes body -> noisy (never the reverse), so it can only *hide* junk,
never expose it. Dry-run by default; pass --apply to write. Changed chunk_ids
are journaled to /tmp/backfill_changes.json for auditing/rollback.

Run inside the backend container:
  docker exec -w /app polymath_v33-backend-1 python /app/scripts/backfill_chunk_kinds.py <corpus_id> [--apply]
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
from pymongo import MongoClient  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client import models as qm  # noqa: E402
from services.ingestion.section_classifier import (  # noqa: E402
    ChunkKind,
    classify_heading,
    is_noisy,
    _is_link_list,
    _is_reference_list,
    _is_resources_list,
    _TOC_LINE_RE,
)


def reclassify(heading_path, text):
    """High-precision only — noisy heading rules + reference-list / link-list /
    dot-leader TOC. Deliberately SKIPS the fuzzy fragment heuristics
    (_is_partial_index, glossary, citation-density) which false-positive on the
    small child chunks this backfill scans (a prose fragment with a few short
    lines is not an index)."""
    hk = classify_heading(heading_path)
    if is_noisy(hk):
        return hk
    if _is_resources_list(heading_path, text):
        return ChunkKind.LINKS
    if _is_reference_list(text):
        return ChunkKind.BIBLIOGRAPHY
    lines = [ln for ln in (text or "")[:2000].split("\n") if ln.strip()]
    if _is_link_list(lines):
        return ChunkKind.LINKS
    if len(lines) >= 5:
        toc_hits = sum(1 for ln in lines if _TOC_LINE_RE.search(ln))
        if toc_hits / len(lines) >= 0.30:
            return ChunkKind.TOC
    return ChunkKind.BODY

CORPUS = next((a for a in sys.argv[1:] if not a.startswith("-")), "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
APPLY = "--apply" in sys.argv
MONGO = os.environ["MONGODB_URI"]
QDRANT = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTIONS = [f"corpus_{CORPUS[:8]}_{k}" for k in ("naive", "hrag", "graph")]

chunks = MongoClient(MONGO).get_database().chunks

changes: dict[str, list[str]] = defaultdict(list)
samples: dict[str, list] = defaultdict(list)
scanned = 0
cur = chunks.find(
    {"corpus_id": CORPUS, "chunk_kind": {"$in": ["body", None]}},
    {"chunk_id": 1, "heading_path": 1, "text": 1},
    no_cursor_timeout=True,
)
try:
    for c in cur:
        scanned += 1
        nk = reclassify(c.get("heading_path"), c.get("text"))
        if is_noisy(nk):
            changes[nk].append(c["chunk_id"])
            if len(samples[nk]) < 5:
                samples[nk].append((c.get("heading_path"), (c.get("text") or "")[:130]))
finally:
    cur.close()

total = sum(len(v) for v in changes.values())
print(f"scanned {scanned} body chunks; would reclassify {total} ({100*total/max(1,scanned):.2f}%) as noisy:")
for k in sorted(changes, key=lambda k: -len(changes[k])):
    print(f"  {k}: {len(changes[k])}")
    for hp, tx in samples[k]:
        print(f"     e.g. {hp} :: {tx!r}")

json.dump({k: v for k, v in changes.items()}, open("/tmp/backfill_changes.json", "w"))
print("journaled changed chunk_ids -> /tmp/backfill_changes.json")

if not APPLY:
    print("\nDRY-RUN — re-run with --apply to write Mongo + Qdrant.")
    sys.exit(0)

# Mongo
for k, ids in changes.items():
    for i in range(0, len(ids), 2000):
        chunks.update_many(
            {"corpus_id": CORPUS, "chunk_id": {"$in": ids[i:i + 2000]}},
            {"$set": {"chunk_kind": k}},
        )
print("mongo: updated.")

# Qdrant — set_payload filtered by chunk_id (robust to point-id scheme), per lane
qc = QdrantClient(url=QDRANT)
for col in COLLECTIONS:
    try:
        for k, ids in changes.items():
            for i in range(0, len(ids), 256):
                qc.set_payload(
                    collection_name=col,
                    payload={"chunk_kind": k},
                    points=qm.Filter(must=[qm.FieldCondition(
                        key="chunk_id", match=qm.MatchAny(any=ids[i:i + 256]))]),
                    wait=True,
                )
        print(f"qdrant {col}: updated.")
    except Exception as exc:  # noqa: BLE001
        print(f"qdrant {col}: FAILED — {exc}")
print("DONE.")
