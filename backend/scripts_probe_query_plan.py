"""Run the deployed QueryPlanV2 path against real corpus artifacts.

Usage inside the backend container:
    python scripts_probe_query_plan.py "question" corpus_id[,corpus_id] [tier]

The output is intentionally credential-free and limited to planning,
hierarchy, coverage, timing, and selected evidence diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys


async def main(query: str, corpus_ids: list[str], tier: str) -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from models.schemas import RetrievalTier
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2

    settings = get_settings()
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    database = client[settings.MONGODB_DATABASE]
    await ingestion_service.connect(database)
    conversation_service._db = database

    plan = build_query_plan_v2(query, corpus_ids=corpus_ids)
    result = await retriever_orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=corpus_ids,
        retrieval_tier=RetrievalTier(tier),
        retrieval_k=85,
        top_k_summary=24,
        rerank_enabled=True,
        rerank_top_n=40,
        final_top_k=8,
    )
    diagnostics = dict(result.diagnostics or {})
    document_routing = dict(diagnostics.get("document_routing") or {})
    document_routing["routes"] = {
        lane_id: [
            {
                "corpus_id": route.get("corpus_id"),
                "doc_id": route.get("doc_id"),
                "score": route.get("score"),
                "title": route.get("title"),
            }
            for route in routes
        ]
        for lane_id, routes in (document_routing.get("routes") or {}).items()
    }
    summary_tree_routing = dict(diagnostics.get("summary_tree_routing") or {})
    summary_tree_routing["routes"] = {
        lane_id: [
            {
                "corpus_id": route.get("corpus_id"),
                "doc_id": route.get("doc_id"),
                "section_count": len(route.get("section_ids") or []),
                "rollup_count": len(route.get("rollup_ids") or []),
                "parent_count": route.get("parent_count"),
            }
            for route in routes
        ]
        for lane_id, routes in (summary_tree_routing.get("routes") or {}).items()
    }
    lane_diagnostics = [
        {
            key: value
            for key, value in dict(lane or {}).items()
            if key
            in {
                "lane_id",
                "role",
                "query",
                "routed_doc_ids",
                "descent",
                "counts",
                "duration_s",
            }
        }
        for lane in (diagnostics.get("lanes") or [])
    ]
    output = {
        "plan": {
            "complexity": plan.complexity,
            "answer_shape": plan.answer_shape,
            "probes": [
                {
                    "probe_id": probe.probe_id,
                    "question": probe.question,
                    "required": probe.required,
                }
                for probe in plan.probes
            ],
        },
        "retrieval": {
            "status": diagnostics.get("status"),
            "effective_tier": diagnostics.get("effective_tier"),
            "quality_first": diagnostics.get("quality_first"),
            "limits": diagnostics.get("limits"),
            "coverage": diagnostics.get("required_concept_coverage"),
            "document_routing": document_routing,
            "summary_tree_routing": summary_tree_routing,
            "lanes": lane_diagnostics,
            "grounding_filter": diagnostics.get("grounding_filter"),
            "reservations": diagnostics.get("reservations"),
            "selection": diagnostics.get("selection"),
            "timings_s": diagnostics.get("timings_s"),
            "total_s": diagnostics.get("total_s"),
            "final_distribution": diagnostics.get("final_distribution"),
            "counts": diagnostics.get("counts"),
            "graph_evidence": diagnostics.get("graph_evidence"),
            "failures": diagnostics.get("lane_failures"),
        },
        "evidence": [
            {
                "score": round(float(chunk.score or 0.0), 4),
                "corpus_id": chunk.corpus_id,
                "doc_id": chunk.doc_id,
                "doc_name": chunk.doc_name,
                "heading_path": chunk.heading_path,
                "source_tier": chunk.source_tier,
                "text": " ".join(str(chunk.text or "").split())[:320],
            }
            for chunk in result.chunks
        ],
    }
    print(json.dumps(output, indent=2, default=str))
    client.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    selected = [value for value in sys.argv[2].split(",") if value]
    selected_tier = sys.argv[3] if len(sys.argv) > 3 else "qdrant_mongo"
    asyncio.run(main(sys.argv[1], selected, selected_tier))
