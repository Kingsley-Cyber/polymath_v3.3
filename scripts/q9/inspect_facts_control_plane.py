#!/usr/bin/env python3
"""Diagnose why q9 has 0 accepted facts / graph promotion noop."""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path

CORPUS = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
OUT = Path(os.environ.get("Q9_OUT", "/app/_q9_facts_cp.json"))


async def main() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings

    settings = get_settings()
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client[settings.MONGODB_DATABASE]

    ghost = await db["ghost_b_extractions"].find({"corpus_id": CORPUS}).to_list(500)
    fact_lens = []
    drop = Counter()
    providers = Counter()
    models = Counter()
    promo_results = Counter()
    sample_facts = []
    sample_empty = None
    for g in ghost:
        facts = g.get("facts") or []
        fact_lens.append(len(facts) if isinstance(facts, list) else -1)
        providers[str(g.get("provider"))] += 1
        models[str(g.get("model"))] += 1
        promo_results[str(g.get("graph_promotion_result"))] += 1
        for k in (
            "fact_drop_count",
            "entity_drop_count",
            "relation_drop_count",
            "citation_drop_count",
            "evidence_drop_count",
        ):
            drop[k] += int(g.get(k) or 0)
        if isinstance(facts, list) and facts and len(sample_facts) < 3:
            sample_facts.append(facts[0])
        if sample_empty is None and isinstance(facts, list) and not facts:
            sample_empty = {
                "chunk_id": g.get("chunk_id"),
                "status": g.get("status"),
                "provider": g.get("provider"),
                "model": g.get("model"),
                "lane": g.get("lane"),
                "entities_n": len(g.get("entities") or []),
                "relations_n": len(g.get("relations") or []),
                "facts_n": 0,
                "extraction_counters": g.get("extraction_counters"),
                "claim_compilation": g.get("claim_compilation"),
                "graph_promotion_result": g.get("graph_promotion_result"),
                "local_extraction": g.get("local_extraction"),
            }

    # extraction job ages / leases
    jobs = await db["extraction_jobs"].find(
        {"corpus_id": CORPUS},
        {"status": 1, "updated_at": 1, "created_at": 1, "leased_at": 1, "attempts": 1, "doc_id": 1},
    ).to_list(5000)
    job_status = Counter(str(j.get("status")) for j in jobs)
    # docs enrichment vs jobs
    docs = await db.documents.find(
        {"corpus_id": CORPUS},
        {"doc_id": 1, "enrichment_status": 1, "queryable": 1, "ghost_b_metrics": 1},
    ).to_list(50)
    graph_status = Counter(
        str((d.get("enrichment_status") or {}).get("graph")) for d in docs
    )

    # promotion job details
    promo = await db["graph_promotion_jobs"].find({"corpus_id": CORPUS}).to_list(50)
    promo_detail = [
        {
            "doc_id": p.get("doc_id"),
            "status": p.get("status"),
            "reason": p.get("reason") or p.get("block_reason") or p.get("message"),
            "result": p.get("result"),
            "facts_promoted": p.get("facts_promoted") or p.get("promoted_facts"),
            "keys": sorted(p.keys())[:30],
        }
        for p in promo
    ]

    # Neo4j via settings
    neo = {}
    try:
        from neo4j import AsyncGraphDatabase

        drv = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        async with drv.session() as session:
            r = await session.run(
                "MATCH (f:Fact) WHERE f.corpus_id = $c RETURN count(f) AS n",
                c=CORPUS,
            )
            neo["fact_nodes"] = (await r.single())["n"]
            r = await session.run(
                "MATCH (e:Entity) WHERE e.corpus_id = $c RETURN count(e) AS n",
                c=CORPUS,
            )
            neo["entity_nodes"] = (await r.single())["n"]
            r = await session.run(
                """
                MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
                WHERE e.corpus_id = $c OR f.corpus_id = $c
                RETURN count(f) AS n
                """,
                c=CORPUS,
            )
            neo["has_fact_edges"] = (await r.single())["n"]
        await drv.close()
    except Exception as e:
        neo["error"] = f"{type(e).__name__}: {e}"

    out = {
        "corpus_id": CORPUS,
        "ghost_rows": len(ghost),
        "fact_len_hist": dict(Counter(fact_lens)),
        "providers": dict(providers),
        "models": dict(models),
        "promo_results_on_ghost": dict(promo_results),
        "drop_totals": dict(drop),
        "sample_facts": sample_facts,
        "sample_empty_row": sample_empty,
        "extraction_jobs": {"total": len(jobs), "status": dict(job_status)},
        "docs_graph_enrichment_status": dict(graph_status),
        "docs": [
            {
                "doc_id": d.get("doc_id"),
                "enrichment_status": d.get("enrichment_status"),
                "ghost_b_metrics": d.get("ghost_b_metrics"),
            }
            for d in docs
        ],
        "promotion_jobs": promo_detail,
        "neo4j": neo,
        "diagnosis_hint": (
            "Branch A if accepted_facts=0 and neo4j facts=0; "
            "control-plane honesty defect if docs mark graph complete while extraction_jobs still queued/running"
        ),
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({
        "fact_len_hist": out["fact_len_hist"],
        "drop_totals": out["drop_totals"],
        "extraction_jobs": out["extraction_jobs"],
        "docs_graph": out["docs_graph_enrichment_status"],
        "neo4j": out["neo4j"],
        "providers": out["providers"],
        "promo_jobs_status": Counter(str(p.get("status")) for p in promo),
        "sample_empty": sample_empty,
    }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
