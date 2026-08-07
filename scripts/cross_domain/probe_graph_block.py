#!/usr/bin/env python3
"""Live canary: Graph request on q9 must block with qualified_graph_evidence_unavailable."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

CORPUS = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
OUT = Path(os.environ.get("OUT", "/app/_graph_block_probe.json"))


async def main() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from models.schemas import RetrievalTier
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2

    settings = get_settings()
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client[settings.MONGODB_DATABASE]
    await ingestion_service.connect(db)
    conversation_service._db = db

    plan = build_query_plan_v2(
        "How does Retrieval-Augmented Generation relate to Information Retrieval?"
    )
    result = await retriever_orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=[CORPUS],
        retrieval_tier=RetrievalTier.qdrant_mongo_graph,
    )
    out = {
        "corpus_id": CORPUS,
        "chunk_count": len(result.chunks or []),
        "facts_count": len(result.facts or []),
        "requested_tier": str(result.requested_tier),
        "effective_tier": str(result.effective_tier),
        "diagnostics": {
            k: result.diagnostics.get(k)
            for k in (
                "status",
                "reason",
                "requested_mode",
                "available_modes",
                "facts_used",
                "graph_authority",
            )
            if k in (result.diagnostics or {})
        },
        "pass": (
            len(result.chunks or []) == 0
            and (result.diagnostics or {}).get("status") == "blocked"
            and (result.diagnostics or {}).get("reason")
            == "qualified_graph_evidence_unavailable"
        ),
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
