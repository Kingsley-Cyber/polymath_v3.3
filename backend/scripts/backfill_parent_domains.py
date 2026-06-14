"""Backfill `domain` onto parent_chunks from the graph_domain_cache.

Domain-aware final selection (select_facet_final max_per_domain, enforced for
BROAD/global chat queries) reads `chunk.domain`, which hydrate.py attaches from
the parent_chunks `domain` field. Until Ghost A emits a domain at summary time,
this script seeds that field from the already-computed per-document cluster
assignments in `graph_domain_cache` (produced by graph.analytics.emerge_domains).

Idempotent and additive — re-run any time after re-clustering. Run in-container:

    docker exec -i polymath_v33-backend-1 python scripts/backfill_parent_domains.py
"""
import asyncio
import logging

from pymongo import UpdateMany

from services.conversation import conversation_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_parent_domains")


async def backfill_parent_domains() -> dict:
    await conversation_service.connect()
    db = conversation_service._db
    if db is None:
        raise RuntimeError("Mongo not connected")

    # doc_id -> domain label, from every per-corpus cache document.
    domain_map: dict[str, str] = {}
    async for cache in db["graph_domain_cache"].find({}):
        for doc_id, info in (cache.get("doc_assignments") or {}).items():
            name = str((info or {}).get("cluster_name") or "").strip()
            if doc_id and name:
                domain_map[str(doc_id)] = name

    distinct = sorted(set(domain_map.values()))
    logger.info("doc->domain assignments: %d docs, %d labels", len(domain_map), len(distinct))
    if not domain_map:
        logger.warning("graph_domain_cache empty — run graph analytics emerge_domains first")
        return {"docs": 0, "modified": 0, "labels": []}

    ops = [UpdateMany({"doc_id": d}, {"$set": {"domain": dom}}) for d, dom in domain_map.items()]
    modified = 0
    for i in range(0, len(ops), 500):
        res = await db["parent_chunks"].bulk_write(ops[i : i + 500], ordered=False)
        modified += res.modified_count

    total = await db["parent_chunks"].count_documents({})
    with_dom = await db["parent_chunks"].count_documents({"domain": {"$exists": True, "$ne": None}})
    logger.info(
        "parent_chunks modified=%d total=%d with_domain=%d (%d%%)",
        modified, total, with_dom, (100 * with_dom // max(total, 1)),
    )
    return {"docs": len(domain_map), "modified": modified, "labels": distinct,
            "total": total, "with_domain": with_dom}


if __name__ == "__main__":
    asyncio.run(backfill_parent_domains())
