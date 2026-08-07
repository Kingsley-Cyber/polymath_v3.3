#!/usr/bin/env python3
import asyncio
import json

from config import get_settings
from models.schemas import RetrievalTier
from services.retriever import retriever_orchestrator
from services.retriever.query_plan import build_query_plan_v2


async def main() -> None:
    s = get_settings()
    print(
        "fixture",
        s.COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED,
        "planner",
        s.COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED,
    )
    plan = build_query_plan_v2(
        "How should a C++ combat update loop be translated into Roblox Luau "
        "while preserving the original mechanics?",
        corpus_ids=["gsem-e2e-20260804a"],
    )
    result = await retriever_orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=["gsem-e2e-20260804a"],
        retrieval_tier=RetrievalTier.qdrant_mongo_graph,
        rerank_enabled=False,
        final_top_k=8,
    )
    cq = (result.diagnostics or {}).get("complex_query") or {}
    print(
        json.dumps(
            {
                k: cq.get(k)
                for k in [
                    "enabled",
                    "execution_mode",
                    "complex_query_executor_ran",
                    "error",
                    "graph_paths_used",
                    "root_embedding_calls_in_cq",
                    "fixture_scope_enforced",
                    "planner_global_enable",
                    "path_ids",
                    "root_plan_hash",
                ]
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
