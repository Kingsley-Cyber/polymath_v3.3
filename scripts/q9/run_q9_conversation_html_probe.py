#!/usr/bin/env python3
"""q9 step 16 — conversation-grounded HTML-test follow-up retrieval probe."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path("/app") if Path("/app/services").is_dir() else Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(ROOT))


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from models.schemas import RetrievalTier
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2

    cid = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
    settings = get_settings()
    db = AsyncIOMotorClient(os.environ["MONGODB_URI"])[settings.MONGODB_DATABASE]
    await ingestion_service.connect(db)
    conversation_service._db = db

    q1 = "What is RAG and how does it relate to Information Retrieval?"
    plan1 = build_query_plan_v2(q1, corpus_ids=[cid])
    t0 = time.perf_counter()
    r1 = await retriever_orchestrator.retrieve_planned(
        plan=plan1,
        corpus_ids=[cid],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        final_top_k=6,
    )
    t1 = time.perf_counter() - t0

    standalone = (
        "create an HTML test about Retrieval-Augmented Generation and Information Retrieval"
    )
    q2 = "create an HTML test about that"
    plan2 = build_query_plan_v2(q2, corpus_ids=[cid], standalone_query=standalone)
    t0 = time.perf_counter()
    r2 = await retriever_orchestrator.retrieve_planned(
        plan=plan2,
        corpus_ids=[cid],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        final_top_k=6,
    )
    t2 = time.perf_counter() - t0

    a1 = (r1.diagnostics or {}).get("alias_retrieval") or {}
    a2 = (r2.diagnostics or {}).get("alias_retrieval") or {}
    out = {
        "turn1": {
            "query": q1,
            "latency_s": round(t1, 3),
            "final_count": len(r1.chunks or []),
            "ids": [c.chunk_id for c in (r1.chunks or [])],
            "alias_status": a1.get("status"),
            "schema_traces": (a1.get("query_report") or {}).get("schema_traces"),
        },
        "turn2_html_test": {
            "query": q2,
            "standalone_query": plan2.standalone_query,
            "latency_s": round(t2, 3),
            "final_count": len(r2.chunks or []),
            "ids": [c.chunk_id for c in (r2.chunks or [])],
            "alias_status": a2.get("status"),
            "grounded_not_literal_only": any(
                tok in (plan2.standalone_query or "")
                for tok in ("Retrieval-Augmented", "Information Retrieval", "RAG")
            ),
            "not_empty": bool(r2.chunks),
        },
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
