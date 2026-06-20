#!/usr/bin/env python
"""Corrective reclassification of body-misclassified citation/reference chunks.

The strengthened section_classifier (preventive) catches academic reference
blocks for NEW ingests; this tool CORRECTS the chunks already in the corpus.
Reference blocks tagged chunk_kind=body slip past NOISY_KINDS on every retrieval
lane (funnels filter chunk_kind on the QDRANT PAYLOAD; lexical/mode_a on Mongo),
so this updates BOTH stores. Gates on is_reference_block (the HIGH-PRECISION
structural reference-list detector shared with ingestion) — NOT classify_content,
whose inline-citation density rule false-positives on citation-dense body prose.

NON-DESTRUCTIVE: dry-run by default (no writes); --apply backs up every changed
chunk's old kind to `citation_reclass_backup` first; --restore reverts from that
backup. Idempotent and batched.

Run inside the backend container:
  docker exec -w /app polymath_v33-backend-1 python scripts/reclassify_citation_chunks.py --corpus <id>           # dry-run
  docker exec -w /app polymath_v33-backend-1 python scripts/reclassify_citation_chunks.py --corpus <id> --apply
  docker exec -w /app polymath_v33-backend-1 python scripts/reclassify_citation_chunks.py --corpus <id> --restore
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

BACKUP = "citation_reclass_backup"
BATCH = 1000
NEW_KIND = "bibliography"


async def _qdrant_collections(qdrant, corpus_id: str) -> list[str]:
    short = corpus_id.split("-")[0]
    cols = (await qdrant.get_collections()).collections
    return [c.name for c in cols if c.name.startswith(f"corpus_{short}_")]


async def _set_qdrant_kind(qdrant, collections, chunk_ids, kind):
    from qdrant_client import models

    calls = 0
    for col in collections:
        try:
            await qdrant.set_payload(
                collection_name=col,
                payload={"chunk_kind": kind},
                points=models.Filter(
                    must=[models.FieldCondition(
                        key="chunk_id", match=models.MatchAny(any=chunk_ids))]
                ),
                wait=True,
            )
            calls += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] qdrant set_payload {col} failed: {exc}", flush=True)
    return calls


async def run(corpus_id: str, mode: str) -> int:
    from services.ingestion_service import ingestion_service
    from services.ingestion.section_classifier import is_reference_block
    from pymongo import UpdateOne

    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    await ingestion_service.connect(db)
    qdrant = ingestion_service.qdrant_client

    if mode == "restore":
        ids = [d["chunk_id"] async for d in db[BACKUP].find({"corpus_id": corpus_id}, {"chunk_id": 1})]
        print(f"[restore] {len(ids)} chunks to revert to body", flush=True)
        if not ids:
            return 0
        cols = await _qdrant_collections(qdrant, corpus_id)
        for i in range(0, len(ids), BATCH):
            batch = ids[i:i + BATCH]
            await db["chunks"].update_many({"chunk_id": {"$in": batch}}, {"$set": {"chunk_kind": "body"}})
            await _set_qdrant_kind(qdrant, cols, batch, "body")
        await db[BACKUP].delete_many({"corpus_id": corpus_id})
        print(f"[restore] reverted {len(ids)} chunks (Mongo + Qdrant), backup cleared", flush=True)
        return 0

    # detect candidates (reuse the ingestion detector verbatim)
    cands: list[str] = []
    scanned = 0
    async for r in db["chunks"].find(
        {"corpus_id": corpus_id, "chunk_kind": "body"}, {"_id": 0, "chunk_id": 1, "text": 1}
    ):
        scanned += 1
        if is_reference_block(r.get("text")):
            cands.append(r["chunk_id"])
    print(f"[scan] body chunks scanned={scanned}  reclassify body->bibliography={len(cands)}", flush=True)

    if mode == "dry-run":
        for cid in cands[:6]:
            d = await db["chunks"].find_one({"chunk_id": cid}, {"_id": 0, "text": 1})
            print(f"  e.g. {cid[:34]} :: {(d.get('text') or '')[:90].strip()!r}", flush=True)
        print("[dry-run] no writes. Re-run with --apply to correct (backed up + reversible).", flush=True)
        return 0

    # apply
    cols = await _qdrant_collections(qdrant, corpus_id)
    print(f"[apply] qdrant collections: {cols}", flush=True)
    mongo_mod = 0
    qcalls = 0
    for i in range(0, len(cands), BATCH):
        batch = cands[i:i + BATCH]
        # backup old kind first (idempotent; never overwrites an existing backup)
        await db[BACKUP].bulk_write(
            [UpdateOne({"chunk_id": cid},
                       {"$setOnInsert": {"chunk_id": cid, "corpus_id": corpus_id,
                                         "old_kind": "body", "new_kind": NEW_KIND}},
                       upsert=True) for cid in batch],
            ordered=False,
        )
        res = await db["chunks"].update_many({"chunk_id": {"$in": batch}}, {"$set": {"chunk_kind": NEW_KIND}})
        mongo_mod += res.modified_count
        qcalls += await _set_qdrant_kind(qdrant, cols, batch, NEW_KIND)
        print(f"  ...{min(i + BATCH, len(cands))}/{len(cands)}", flush=True)

    # verify
    remaining = 0
    async for r in db["chunks"].find({"corpus_id": corpus_id, "chunk_kind": "body"}, {"_id": 0, "text": 1}):
        if is_reference_block(r.get("text")):
            remaining += 1
    print(f"[apply] mongo_modified={mongo_mod} qdrant_setpayload_calls={qcalls} "
          f"backup={len(cands)} | VERIFY remaining body-misclassified={remaining}", flush=True)
    return 0 if remaining == 0 else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--restore", action="store_true")
    a = ap.parse_args()
    mode = "restore" if a.restore else ("apply" if a.apply else "dry-run")
    sys.exit(asyncio.run(run(a.corpus, mode)))


if __name__ == "__main__":
    main()
