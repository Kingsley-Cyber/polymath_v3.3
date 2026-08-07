#!/usr/bin/env python3
"""Inspect q9 corpus enrichment terminal state (Mongo + Neo4j counts)."""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CORPUS = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
BATCH = os.environ.get("Q9_BATCH_ID", "119d253e-f058-41ed-93b2-c58b6b8ed381")
OUT = Path(os.environ.get("Q9_OUT", "/app/_q9_enrichment_terminal.json"))


async def main() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings

    settings = get_settings()
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client[settings.MONGODB_DATABASE]
    docs = await db.documents.find({"corpus_id": CORPUS}).to_list(200)
    doc_ids = [d.get("doc_id") or str(d.get("_id")) for d in docs]

    # Flexible status fields seen across eras
    status_keys = [
        "status",
        "enrichment_status",
        "graph_status",
        "queryable",
        "graph_extracted",
        "graph_promoted",
        "stage",
        "pipeline_stage",
    ]
    per_doc = []
    for d in docs:
        row = {"doc_id": d.get("doc_id") or str(d.get("_id"))}
        for k in status_keys:
            if k in d:
                row[k] = d.get(k)
        # nested readiness/certificate if present
        for k in ("readiness", "certificate", "retrieval_readiness", "graph"):
            if k in d:
                v = d.get(k)
                if isinstance(v, dict):
                    row[k] = {kk: v.get(kk) for kk in list(v)[:20]}
                else:
                    row[k] = v
        per_doc.append(row)

    # Collections of interest
    coll_stats = {}
    for coll in [
        "ghost_b_extractions",
        "graph_promotion_jobs",
        "extraction_jobs",
        "summary_jobs",
        "enrichment_jobs",
        "ingest_items",
        "batch_items",
    ]:
        try:
            n = await db[coll].count_documents({"corpus_id": CORPUS})
            sample = await db[coll].find({"corpus_id": CORPUS}).limit(1).to_list(1)
            keys = sorted(sample[0].keys()) if sample else []
            status_c = {}
            if sample and "status" in sample[0]:
                rows = await db[coll].find(
                    {"corpus_id": CORPUS}, {"status": 1}
                ).to_list(5000)
                status_c = dict(Counter(str(r.get("status")) for r in rows))
            coll_stats[coll] = {"count": n, "sample_keys": keys[:40], "status": status_c}
        except Exception as e:
            coll_stats[coll] = {"error": str(e)}

    # Batch lookup
    batch = None
    for coll, q in [
        ("ingest_batches", {"_id": BATCH}),
        ("ingest_batches", {"batch_id": BATCH}),
        ("batches", {"_id": BATCH}),
        ("batches", {"batch_id": BATCH}),
    ]:
        try:
            batch = await db[coll].find_one(q)
            if batch:
                batch = {
                    "coll": coll,
                    **{
                        k: batch.get(k)
                        for k in batch
                        if k != "_id" and not isinstance(batch.get(k), (bytes, bytearray))
                    },
                }
                # trim huge nested
                batch = {k: (v if not isinstance(v, (list, dict)) or k in ("progress", "options", "status", "phase") else type(v).__name__) for k, v in list(batch.items())[:60]}
                break
        except Exception:
            continue

    # ghost_b aggregate facts
    ghost_rows = await db["ghost_b_extractions"].find({"corpus_id": CORPUS}).to_list(500)
    accepted_entities = accepted_relations = accepted_facts = 0
    ghost_statuses = Counter()
    for g in ghost_rows:
        ghost_statuses[str(g.get("status"))] += 1
        for key, bucket in [
            ("entities", "e"),
            ("accepted_entities", "e"),
            ("relations", "r"),
            ("accepted_relations", "r"),
            ("facts", "f"),
            ("accepted_facts", "f"),
        ]:
            v = g.get(key)
            if isinstance(v, list):
                if bucket == "e" and key.startswith("accepted") or key == "entities":
                    if key.startswith("accepted") or (
                        key == "entities" and "accepted_entities" not in g
                    ):
                        accepted_entities += len(v) if key in ("accepted_entities", "entities") else 0
                if key in ("accepted_relations", "relations"):
                    if key.startswith("accepted") or (
                        key == "relations" and "accepted_relations" not in g
                    ):
                        accepted_relations += len(v)
                if key in ("accepted_facts", "facts"):
                    if key.startswith("accepted") or (
                        key == "facts" and "accepted_facts" not in g
                    ):
                        accepted_facts += len(v)

    # Better counting: prefer accepted_* when present
    accepted_entities = accepted_relations = accepted_facts = 0
    for g in ghost_rows:
        ents = g.get("accepted_entities")
        if ents is None:
            ents = g.get("entities") or []
        rels = g.get("accepted_relations")
        if rels is None:
            rels = g.get("relations") or []
        facts = g.get("accepted_facts")
        if facts is None:
            facts = g.get("facts") or []
        if isinstance(ents, list):
            accepted_entities += len(ents)
        if isinstance(rels, list):
            accepted_relations += len(rels)
        if isinstance(facts, list):
            accepted_facts += len(facts)

    # promotion jobs terminal
    promo = await db["graph_promotion_jobs"].find({"corpus_id": CORPUS}).to_list(5000)
    promo_status = dict(Counter(str(j.get("status")) for j in promo))
    pending_like = {"pending", "queued", "leased", "running", "in_progress", "None"}
    promo_pending = sum(v for k, v in promo_status.items() if k in pending_like)
    promo_complete = sum(
        v
        for k, v in promo_status.items()
        if k in {"done", "completed", "complete", "succeeded", "success", "terminal"}
    )

    # Neo4j
    neo = {"error": None, "fact_nodes": None, "rel_count": None}
    try:
        from services.graph.neo4j_client import get_driver  # type: ignore

        driver = await get_driver() if asyncio.iscoroutinefunction(get_driver) else get_driver()
        if asyncio.iscoroutine(driver):
            driver = await driver
        # try common patterns
        from neo4j import AsyncGraphDatabase
        import os as _os

        uri = _os.environ.get("NEO4J_URI") or _os.environ.get("NEO4J_URL") or "bolt://neo4j:7687"
        user = _os.environ.get("NEO4J_USER") or _os.environ.get("NEO4J_USERNAME") or "neo4j"
        password = _os.environ.get("NEO4J_PASSWORD") or ""
        drv = AsyncGraphDatabase.driver(uri, auth=(user, password))
        async with drv.session() as session:
            r1 = await session.run(
                "MATCH (f:Fact) WHERE f.corpus_id = $c RETURN count(f) AS n",
                c=CORPUS,
            )
            rec = await r1.single()
            neo["fact_nodes"] = int(rec["n"]) if rec else 0
            r2 = await session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE coalesce(a.corpus_id, r.corpus_id, b.corpus_id) = $c
                RETURN count(r) AS n
                """,
                c=CORPUS,
            )
            rec2 = await r2.single()
            neo["rel_count"] = int(rec2["n"]) if rec2 else 0
            # label inventory
            r3 = await session.run(
                """
                MATCH (n)
                WHERE n.corpus_id = $c
                RETURN labels(n) AS labs, count(*) AS n
                ORDER BY n DESC LIMIT 20
                """,
                c=CORPUS,
            )
            neo["labels"] = [
                {"labels": rec["labs"], "count": rec["n"]} async for rec in r3
            ]
        await drv.close()
    except Exception as e:
        neo["error"] = f"{type(e).__name__}: {e}"

    # pending jobs across common collections
    pending = {}
    for coll, statuses in [
        ("extraction_jobs", ["pending", "leased", "queued", "running"]),
        ("summary_jobs", ["pending", "leased", "queued", "running"]),
        ("graph_promotion_jobs", ["pending", "leased", "queued", "running", "in_progress"]),
    ]:
        try:
            pending[coll] = {
                s: await db[coll].count_documents({"corpus_id": CORPUS, "status": s})
                for s in statuses
            }
        except Exception as e:
            pending[coll] = {"error": str(e)}

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "corpus_id": CORPUS,
        "batch_id": BATCH,
        "documents_total": len(docs),
        "documents_sample": per_doc[:10],
        "batch": batch,
        "collections": coll_stats,
        "ghost_b": {
            "rows": len(ghost_rows),
            "status": dict(ghost_statuses),
            "accepted_entities": accepted_entities,
            "accepted_relations": accepted_relations,
            "accepted_facts": accepted_facts,
            "sample_keys": sorted(ghost_rows[0].keys())[:50] if ghost_rows else [],
        },
        "graph_promotion": {
            "jobs": len(promo),
            "status": promo_status,
            "pending_like": promo_pending,
            "complete_like": promo_complete,
        },
        "pending_jobs": pending,
        "neo4j": neo,
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({
        "documents_total": out["documents_total"],
        "ghost_b_facts": accepted_facts,
        "ghost_b_relations": accepted_relations,
        "promo": promo_status,
        "pending": pending,
        "neo4j_facts": neo.get("fact_nodes"),
        "neo4j_error": neo.get("error"),
        "out": str(OUT),
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
