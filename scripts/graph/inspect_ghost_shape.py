#!/usr/bin/env python3
import asyncio, json, os
from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings

CORPUS = "6a766597-29f3-4a3e-8918-5de10f0053b3"


async def main() -> None:
    s = get_settings()
    db = AsyncIOMotorClient(os.environ["MONGODB_URI"])[s.MONGODB_DATABASE]
    g = await db.ghost_b_extractions.find_one(
        {"corpus_id": CORPUS, "relations.0": {"$exists": True}}
    )
    if not g:
        g = await db.ghost_b_extractions.find_one({"corpus_id": CORPUS})
    ents = g.get("entities") or []
    rels = g.get("relations") or []
    print("ent0", json.dumps(ents[0] if ents else {}, indent=2, default=str)[:900])
    print("rel0", json.dumps(rels[0] if rels else {}, indent=2, default=str)[:900])
    doc = await db.documents.find_one(
        {"corpus_id": CORPUS}, {"write_state": 1, "enrichment_status": 1, "doc_id": 1}
    )
    print("doc", json.dumps(doc, indent=2, default=str)[:800])


if __name__ == "__main__":
    asyncio.run(main())
