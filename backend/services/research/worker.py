"""Runnable autoresearch worker loop."""

from __future__ import annotations

import logging
from typing import Any

from models.schemas import RetrievalTier
from services.ingestion_service import ingestion_service
from services.research.artifacts import research_service
from services.research.context import context_token_budget, pack_research_context
from services.research.evidence import chunk_to_evidence, dedupe_and_number_evidence
from services.research.orchestrator import ResearchLaneSpec, run_research_lanes
from services.research.planner import plan_subquestions
from services.research.renderers import (
    render_html_report,
    render_json_report,
    render_markdown_report,
)
from services.retriever import retriever_orchestrator

logger = logging.getLogger(__name__)


async def _empty_list() -> list[Any]:
    return []


async def _cancelled_job(db: Any, *, user_id: str, job_id: str, stage: str) -> Any | None:
    current = await research_service.get_job(db, user_id=user_id, job_id=job_id)
    if current is None or current.status != "cancelled":
        return None
    await research_service.add_event(
        db,
        user_id=user_id,
        job_id=job_id,
        stage=stage,
        status="cancelled",
        message="Research job cancellation acknowledged before continuing.",
        metadata={},
    )
    return current


async def _run_retrieval_lane(
    *,
    job,
    subquestion: dict[str, Any],
    retriever: Any,
) -> list[dict[str, Any]]:
    result = await retriever.retrieve(
        query=subquestion["question"],
        corpus_ids=job.corpus_ids,
        retrieval_tier=RetrievalTier.qdrant_mongo_graph,
        collections=None,
        retrieval_k=None,
        rerank_enabled=True,
        top_k_summary=None,
        rerank_top_n=None,
        similarity_threshold=None,
        neo4j_expansion_cap=None,
        max_corpora_per_query=None,
        final_top_k=max(3, min(12, job.budgets.max_evidence_items)),
        fact_seed_limit=None,
        search_mode="auto",
    )
    evidence: list[dict[str, Any]] = []
    for rank, chunk in enumerate(getattr(result, "chunks", []) or [], 1):
        item = chunk_to_evidence(chunk, subquestion_id=subquestion["id"], rank=rank)
        if item is not None:
            evidence.append(item)
    return evidence


async def _run_graph_lane(
    *,
    job,
    subquestion: dict[str, Any],
    graph_driver: Any,
    qdrant: Any,
) -> list[dict[str, Any]]:
    if graph_driver is None or job.budgets.max_graph_hops <= 0:
        return []
    from services.graph.graph_query import expand_subgraph, extract_query_entities

    traces: list[dict[str, Any]] = []
    for corpus_id in job.corpus_ids:
        seeds = await extract_query_entities(
            subquestion["question"],
            corpus_id,
            graph_driver,
            limit_per_token=2,
            qdrant=qdrant,
        )
        seed_ids = [str(seed.get("entity_id")) for seed in seeds if seed.get("entity_id")]
        graph = await expand_subgraph(
            seed_ids[:8],
            corpus_id,
            graph_driver,
            max_hops=job.budgets.max_graph_hops,
            limit=80,
            entity_scores={
                str(seed.get("entity_id")): float(seed.get("score") or 0.0)
                for seed in seeds
                if seed.get("entity_id")
            },
        )
        traces.append(
            {
                "subquestion_id": subquestion["id"],
                "corpus_id": corpus_id,
                "status": "ok",
                "seed_count": len(seed_ids),
                "node_count": len(graph.get("nodes") or []),
                "edge_count": len(graph.get("links") or []),
                "seeds": seeds[:8],
                "graph": {
                    "nodes": (graph.get("nodes") or [])[:80],
                    "links": (graph.get("links") or [])[:120],
                },
            }
        )
    return traces


async def run_research_job(
    db: Any,
    *,
    user_id: str,
    job_id: str,
    retriever: Any | None = None,
    graph_driver: Any | None = None,
    qdrant: Any | None = None,
) -> Any:
    """Run one research job to Markdown, HTML, and JSON artifacts."""
    retriever = retriever or retriever_orchestrator
    if graph_driver is None:
        graph_driver = ingestion_service.neo4j_driver
    if qdrant is None:
        qdrant = ingestion_service.qdrant_client
    job = await research_service.get_job(db, user_id=user_id, job_id=job_id)
    if job is None:
        raise FileNotFoundError("research job not found")
    if job.status == "cancelled":
        return job

    subquestions: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    graph_traces: list[dict[str, Any]] = []
    caveats: list[str] = []
    try:
        await research_service.update_job_status(
            db,
            user_id=user_id,
            job_id=job_id,
            status="running",
        )
        job = await research_service.get_job(db, user_id=user_id, job_id=job_id)
        if job is None:
            raise FileNotFoundError("research job not found")
        subquestions = plan_subquestions(job.question, job.budgets)
        await research_service.add_event(
            db,
            user_id=user_id,
            job_id=job_id,
            stage="planner",
            status="done",
            message=f"Planned {len(subquestions)} research subquestions.",
            metadata={"subquestions": subquestions},
        )
        tool_calls = 0
        for subquestion in subquestions:
            cancelled = await _cancelled_job(
                db,
                user_id=user_id,
                job_id=job_id,
                stage="control",
            )
            if cancelled is not None:
                return cancelled
            if tool_calls >= job.budgets.max_tool_calls:
                caveats.append("Tool-call budget reached before all subquestions completed.")
                break
            lanes: list[ResearchLaneSpec] = [
                ResearchLaneSpec(
                    name="retrieval",
                    run=lambda subquestion=subquestion: _run_retrieval_lane(
                        job=job,
                        subquestion=subquestion,
                        retriever=retriever,
                    ),
                    fallback=lambda: _empty_list(),
                    max_attempts=2,
                    timeout_seconds=30.0,
                    required=True,
                )
            ]
            graph_enabled = graph_driver is not None and job.budgets.max_graph_hops > 0
            if graph_enabled and tool_calls + len(lanes) < job.budgets.max_tool_calls:
                lanes.append(
                    ResearchLaneSpec(
                        name="graph",
                        run=lambda subquestion=subquestion: _run_graph_lane(
                            job=job,
                            subquestion=subquestion,
                            graph_driver=graph_driver,
                            qdrant=qdrant,
                        ),
                        fallback=lambda: _empty_list(),
                        max_attempts=1,
                        timeout_seconds=20.0,
                        required=False,
                    )
                )

            outcomes = await run_research_lanes(
                lanes,
                max_concurrency=min(2, max(1, len(lanes))),
            )
            tool_calls += len(lanes)
            for outcome in outcomes:
                receipt = {
                    **outcome.receipt(),
                    "subquestion_id": subquestion["id"],
                }
                if outcome.name == "retrieval":
                    rows = outcome.result if isinstance(outcome.result, list) else []
                    evidence_rows.extend(rows)
                    if outcome.status != "success":
                        caveats.append(
                            f"Retrieval lane {outcome.status} for {subquestion['id']}: "
                            f"{outcome.error_type or 'empty fallback'}"
                        )
                    await research_service.add_event(
                        db,
                        user_id=user_id,
                        job_id=job_id,
                        stage="retrieval",
                        status="done" if outcome.status == "success" else outcome.status,
                        message=(
                            f"Retrieved {len(rows)} evidence rows for {subquestion['id']}."
                            if outcome.status == "success"
                            else f"Retrieval lane used {outcome.status} for {subquestion['id']}."
                        ),
                        metadata=receipt,
                    )
                elif outcome.name == "graph":
                    traces = outcome.result if isinstance(outcome.result, list) else []
                    graph_traces.extend(traces)
                    if outcome.status != "success":
                        caveats.append(
                            f"Graph lane {outcome.status} for {subquestion['id']}: "
                            f"{outcome.error_type or 'empty fallback'}"
                        )
                    await research_service.add_event(
                        db,
                        user_id=user_id,
                        job_id=job_id,
                        stage="graph",
                        status=(
                            "done"
                            if outcome.status == "success" and traces
                            else "skipped"
                            if outcome.status == "success"
                            else outcome.status
                        ),
                        message=(
                            f"Graph traversal produced {len(traces)} packet(s)."
                            if outcome.status == "success" and traces
                            else "Graph traversal skipped."
                            if outcome.status == "success"
                            else f"Graph lane used {outcome.status} for {subquestion['id']}."
                        ),
                        metadata=receipt,
                    )
            cancelled = await _cancelled_job(
                db,
                user_id=user_id,
                job_id=job_id,
                stage="control",
            )
            if cancelled is not None:
                return cancelled

        evidence = dedupe_and_number_evidence(
            evidence_rows,
            limit=job.budgets.max_evidence_items,
        )
        packed_context = pack_research_context(
            evidence=evidence,
            graph_traces=graph_traces,
            token_budget=context_token_budget(job.budgets.max_output_tokens),
        )
        evidence = packed_context.evidence
        graph_traces = packed_context.graph_traces
        receipt = packed_context.receipt
        if receipt["dropped_evidence"] or receipt["dropped_graph_traces"]:
            caveats.append(
                "Context window packing dropped "
                f"{receipt['dropped_evidence']} evidence row(s) and "
                f"{receipt['dropped_graph_traces']} graph trace(s)."
            )
        await research_service.add_event(
            db,
            user_id=user_id,
            job_id=job_id,
            stage="context",
            status="done",
            message=(
                "Packed research context with "
                f"{receipt['included_evidence']} evidence row(s) and "
                f"{receipt['included_graph_traces']} graph trace(s)."
            ),
            metadata=receipt,
        )
        if not evidence:
            caveats.append("No retrieval evidence was returned.")
        cancelled = await _cancelled_job(
            db,
            user_id=user_id,
            job_id=job_id,
            stage="control",
        )
        if cancelled is not None:
            return cancelled
        await research_service.update_job_status(
            db,
            user_id=user_id,
            job_id=job_id,
            status="rendering",
        )
        markdown = render_markdown_report(
            job=job,
            subquestions=subquestions,
            evidence=evidence,
            graph_traces=graph_traces,
            caveats=caveats,
        )
        report_json = render_json_report(
            job=job,
            subquestions=subquestions,
            evidence=evidence,
            graph_traces=graph_traces,
            caveats=caveats,
        )
        html = render_html_report(
            job=job,
            subquestions=subquestions,
            evidence=evidence,
            graph_traces=graph_traces,
            caveats=caveats,
        )
        await research_service.store_artifact(
            db,
            user_id=user_id,
            job_id=job_id,
            filename="research-report.md",
            content=markdown.encode("utf-8"),
            artifact_format="markdown",
            mime_type="text/markdown; charset=utf-8",
        )
        await research_service.store_artifact(
            db,
            user_id=user_id,
            job_id=job_id,
            filename="research-report.html",
            content=html.encode("utf-8"),
            artifact_format="html",
            mime_type="text/html; charset=utf-8",
        )
        await research_service.store_artifact(
            db,
            user_id=user_id,
            job_id=job_id,
            filename="research-trace.json",
            content=report_json.encode("utf-8"),
            artifact_format="json",
            mime_type="application/json",
        )
        return await research_service.update_job_status(
            db,
            user_id=user_id,
            job_id=job_id,
            status="done",
        )
    except Exception as exc:
        logger.exception("research job failed: %s", job_id)
        return await research_service.update_job_status(
            db,
            user_id=user_id,
            job_id=job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
