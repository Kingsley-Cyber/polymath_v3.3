#!/usr/bin/env python3
"""Prove Graph runs on assertion plane (not blocked, not silent Hybrid)."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

CORPUS = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
OUT = Path(os.environ.get("OUT", "/app/_graph_assertion_probe.json"))


async def main() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from models.schemas import RetrievalTier
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2

    settings = get_settings()
    settings.CROSS_DOMAIN_GRAPH_REQUIRE_QUALIFIED_FACTS = False
    import services.retriever as rm

    rm.settings = settings

    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client[settings.MONGODB_DATABASE]
    await ingestion_service.connect(db)
    conversation_service._db = db

    queries = [
        "How does Retrieval-Augmented Generation relate to Information Retrieval?",
        "What relationships connect Benesh Movement Notation and movement writing?",
        "How do human users relate to the MCP service in Trail Signal?",
    ]
    rows = []
    for q in queries:
        plan = build_query_plan_v2(q)
        t0 = time.perf_counter()
        hybrid = await retriever_orchestrator.retrieve_planned(
            plan=plan,
            corpus_ids=[CORPUS],
            retrieval_tier=RetrievalTier.qdrant_mongo,
        )
        graph = await retriever_orchestrator.retrieve_planned(
            plan=plan,
            corpus_ids=[CORPUS],
            retrieval_tier=RetrievalTier.qdrant_mongo_graph,
        )
        elapsed = time.perf_counter() - t0
        h_ids = {c.chunk_id for c in (hybrid.chunks or []) if c.chunk_id}
        g_ids = {c.chunk_id for c in (graph.chunks or []) if c.chunk_id}
        gdiag = graph.diagnostics or {}
        rows.append(
            {
                "query": q,
                "latency_s": round(elapsed, 3),
                "hybrid_count": len(h_ids),
                "graph_count": len(g_ids),
                "graph_status": gdiag.get("status"),
                "graph_capability": gdiag.get("graph_capability"),
                "qualified_facts_available": gdiag.get("qualified_facts_available"),
                "relation_assertions_available": gdiag.get(
                    "relation_assertions_available"
                ),
                "graph_capabilities": gdiag.get("graph_capabilities"),
                "facts_used": (gdiag.get("graph_evidence") or {}).get("facts_used"),
                "graph_only_ids": sorted(g_ids - h_ids),
                "graph_added_beyond_hybrid": len(g_ids - h_ids),
                "silent_hybrid": (
                    gdiag.get("status") not in {"blocked", None}
                    and gdiag.get("graph_capability") in (None, "blocked")
                    and len(g_ids) > 0
                    and g_ids == h_ids
                ),
            }
        )

    acceptance = {
        "rows": rows,
        "graph_ran_not_blocked": all(
            r["graph_status"] != "blocked" and r["graph_count"] > 0 for r in rows
        ),
        "qualified_facts_zero_allowed": all(
            r.get("qualified_facts_available") is False for r in rows
        ),
        "assertion_capability": all(
            (r.get("graph_capability") or "").startswith("graph_") for r in rows
        ),
        "any_graph_added_evidence": any(r["graph_added_beyond_hybrid"] > 0 for r in rows),
        "silent_hybrid_count": sum(1 for r in rows if r["silent_hybrid"]),
    }
    OUT.write_text(json.dumps(acceptance, indent=2, default=str))
    print(json.dumps(acceptance, indent=2, default=str)[:4000])


if __name__ == "__main__":
    asyncio.run(main())
