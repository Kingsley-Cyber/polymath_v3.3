#!/usr/bin/env python3
"""Project RelationAssertion plane for q9 corpus + mark stuck extraction jobs."""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CORPUS = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
OUT = Path(os.environ.get("OUT", "/app/_q9_assertion_projection.json"))


async def main() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase

    from config import get_settings
    from services.graph.assertion_projector import project_document_assertions_from_mongo
    from services.retriever.graph_authority import inspect_graph_capabilities

    settings = get_settings()
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client[settings.MONGODB_DATABASE]
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )

    docs = await db.documents.find(
        {"corpus_id": CORPUS}, {"doc_id": 1}
    ).to_list(100)
    per_doc = []
    totals = Counter()
    async with driver:
        for d in docs:
            doc_id = str(d.get("doc_id") or "")
            if not doc_id:
                continue
            result = await project_document_assertions_from_mongo(
                db, driver, corpus_id=CORPUS, doc_id=doc_id
            )
            per_doc.append(result)
            totals["ghost_rows"] += int(result.get("ghost_rows") or 0)
            totals["assertion_nodes"] += int(result.get("assertion_nodes") or 0)
            totals["support_edges"] += int(result.get("support_edges") or 0)

    # Mark stuck extraction jobs: queued/running with no ghost_b row for chunk,
    # or already covered by ghost_b (should be promoted).
    ghost_chunks = set()
    async for g in db.ghost_b_extractions.find(
        {"corpus_id": CORPUS}, {"chunk_id": 1}
    ):
        ghost_chunks.add(str(g.get("chunk_id") or ""))

    jobs = await db.extraction_jobs.find(
        {"corpus_id": CORPUS, "status": {"$in": ["queued", "running", "leased"]}},
        {"_id": 1, "chunk_id": 1, "status": 1, "attempts": 1},
    ).to_list(10000)

    covered = []
    stuck = []
    for j in jobs:
        cid = str(j.get("chunk_id") or "")
        if cid in ghost_chunks:
            covered.append(j["_id"])
        else:
            stuck.append(j["_id"])

    now = datetime.now(timezone.utc)
    marked_covered = 0
    marked_stuck = 0
    if covered:
        r = await db.extraction_jobs.update_many(
            {"_id": {"$in": covered}},
            {
                "$set": {
                    "status": "promoted",
                    "reason": "ghost_b_already_present",
                    "updated_at": now,
                }
            },
        )
        marked_covered = int(r.modified_count or 0)
    if stuck:
        r = await db.extraction_jobs.update_many(
            {"_id": {"$in": stuck}},
            {
                "$set": {
                    "status": "stuck_orphan",
                    "reason": "no_ghost_b_row_after_queryable",
                    "stuck_at": now,
                    "updated_at": now,
                }
            },
        )
        marked_stuck = int(r.modified_count or 0)

    # Fix enrichment_status honesty: graph complete only when neo4j_written AND
    # (entity mentions exist OR we leave structural). Keep pending if not written.
    # Capability certificate stored on corpus.
    caps = await inspect_graph_capabilities([CORPUS])
    await db.corpora.update_one(
        {"corpus_id": CORPUS},
        {
            "$set": {
                "graph_capabilities": caps,
                "graph_capabilities_updated_at": now,
            }
        },
    )

    # Recompute doc enrichment_status.graph from write_state + capability
    for d in docs:
        doc_id = str(d.get("doc_id") or "")
        full = await db.documents.find_one(
            {"doc_id": doc_id, "corpus_id": CORPUS},
            {"write_state": 1},
        )
        ws = (full or {}).get("write_state") or {}
        neo = bool(ws.get("neo4j_written"))
        # Capability-aware: neo4j_written alone is not enough if assertion/entity
        # plane empty for corpus — but here corpus has entities; mark complete
        # only when neo4j_written True.
        graph_status = "complete" if neo and caps.get("entity_ready") else (
            "pending" if not neo else "structural_only"
        )
        await db.documents.update_one(
            {"doc_id": doc_id, "corpus_id": CORPUS},
            {"$set": {"enrichment_status.graph": graph_status}},
        )

    job_status = Counter()
    async for j in db.extraction_jobs.find({"corpus_id": CORPUS}, {"status": 1}):
        job_status[str(j.get("status"))] += 1

    out = {
        "corpus_id": CORPUS,
        "docs": len(docs),
        "projection_totals": dict(totals),
        "per_doc": per_doc,
        "jobs_marked_covered": marked_covered,
        "jobs_marked_stuck": marked_stuck,
        "job_status_after": dict(job_status),
        "graph_capabilities": caps,
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({
        "projection_totals": dict(totals),
        "jobs": dict(job_status),
        "capabilities": {
            k: caps.get(k)
            for k in (
                "advertised_mode",
                "entity_ready",
                "assertion_ready",
                "qualified_fact_ready",
                "counts",
            )
        },
    }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
