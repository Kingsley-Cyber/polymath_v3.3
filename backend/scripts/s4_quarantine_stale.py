"""S4 driver: quarantine capture-stale summaries (valid summary, no latent_concepts)
so the scoped backfill regenerates them through the new capture contract.
Backup-first (full rows, JSONL). Usage: python s4_quarantine_stale.py <corpus_regex> <limit|all> [--apply]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from config import get_settings


async def main() -> int:
    pat = sys.argv[1]
    limit = None if sys.argv[2] == "all" else int(sys.argv[2])
    apply = "--apply" in sys.argv
    s = get_settings()
    db = AsyncIOMotorClient(s.MONGODB_URI)[s.MONGODB_DATABASE]
    c = await db.corpora.find_one(
        {"name": {"$regex": pat, "$options": "i"}}, {"corpus_id": 1, "name": 1}
    )
    cid = c["corpus_id"]
    sel = {
        "corpus_id": cid,
        "summary": {"$type": "string", "$ne": ""},
        "latent_concepts": {"$exists": False},
        "summary_quarantined_at": {"$exists": False},
    }
    n = await db.parent_chunks.count_documents(sel)
    print(f"[{c['name']}] capture-stale candidates: {n} (limit={limit})", flush=True)
    if not apply:
        print("DRY RUN — pass --apply")
        return 0
    backup = Path(f"/data/ingest-files/backups/s4-capture-regen-{int(time.time())}_{cid[:8]}.jsonl")
    backup.parent.mkdir(parents=True, exist_ok=True)
    cur = db.parent_chunks.find(sel)
    if limit:
        cur = cur.limit(limit)
    ids, wrote = [], 0
    with backup.open("w") as f:
        async for row in cur:
            row["_id"] = str(row["_id"])
            f.write(json.dumps(row, default=str) + "\n")
            ids.append(row["parent_id"])
            wrote += 1
    res = await db.parent_chunks.update_many(
        {"corpus_id": cid, "parent_id": {"$in": ids}},
        {
            "$set": {
                "summary_quarantined_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "summary_quarantine_reason": "capture_stale_regen_s4",
            },
            "$unset": {"summary": "", "summary_model": ""},
        },
    )
    print(f"backed up {wrote} rows -> {backup}")
    print(f"quarantined+blanked {res.modified_count} rows for regen")
    return 0


raise SystemExit(asyncio.run(main()))
