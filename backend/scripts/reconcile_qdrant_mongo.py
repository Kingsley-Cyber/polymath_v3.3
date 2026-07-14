"""Mongo <-> Qdrant reconciliation: per-corpus counts. Read-only."""
import asyncio
import json
import os
import urllib.request

from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings


def q(path):
    url = (os.environ.get("QDRANT_URL") or "http://qdrant:6333").rstrip("/") + path
    req = urllib.request.Request(url)
    key = os.environ.get("QDRANT_API_KEY")
    if key:
        req.add_header("api-key", key)
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


async def main():
    s = get_settings()
    db = AsyncIOMotorClient(s.MONGODB_URI)[s.MONGODB_DATABASE]
    colls = [c["name"] for c in q("/collections")["result"]["collections"]]
    print(f"qdrant collections: {len(colls)}")
    counts = {name: q(f"/collections/{name}")["result"]["points_count"] for name in colls}
    mismatches = []
    async for c in db.corpora.find({}, {"corpus_id": 1, "name": 1}):
        cid = c["corpus_id"]
        cid8 = cid[:8]
        mine = {n: counts[n] for n in colls if cid8 in n}
        mongo_children = await db.chunks.count_documents({"corpus_id": cid})
        mongo_parents = await db.parent_chunks.count_documents({"corpus_id": cid})
        print(f"\n{c['name']} (cid8={cid8}) mongo children={mongo_children} parents={mongo_parents}")
        for n, pc in sorted(mine.items()):
            print(f"  qdrant {n}: {pc}")
            base = n.replace(f"corpus_{cid8}_", "")
            if base in ("children", "chunks") and pc != mongo_children:
                mismatches.append((c["name"], n, "mongo_children", mongo_children, pc))
            if base == "parents" and pc != mongo_parents:
                mismatches.append((c["name"], n, "mongo_parents", mongo_parents, pc))
    print(f"\nMISMATCHES: {len(mismatches)}")
    for m in mismatches:
        print("  ", m)


asyncio.run(main())
