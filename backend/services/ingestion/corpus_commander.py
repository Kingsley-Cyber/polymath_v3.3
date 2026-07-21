"""Resident-style corpus commander cycle.

The commander is the missing verb around the existing queues. A cycle compares
desired corpus readiness against durable queue/vector state, materializes the
full missing keyspace, reclaims expired work, repairs deterministic vector
stranding, and persists a fresh readiness receipt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from services.ingestion.graph_promotion_jobs import plan_graph_promotion_jobs
from services.ingestion.job_leases import (
    corpus_lane_lease,
    reclaim_expired_running_jobs,
)
from services.ingestion.readiness import materialize_corpus_readiness
from services.ingestion.summary_jobs import (
    plan_summary_jobs,
    reconcile_satisfied_summary_jobs,
)
from services.ingestion.summary_vector_reconcile import (
    audit_parent_summary_vector_integrity,
    repair_parent_summary_vector_integrity,
)

Runner = Callable[..., Awaitable[dict[str, Any]]]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _changed(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    for key in (
        "planned",
        "claimed",
        "reclaimed",
        "written",
        "docs_updated",
        "artifact_reconciled",
        "superseded",
    ):
        if _int(result.get(key)):
            return True
    counts = result.get("counts") or {}
    return any(_int(value) for value in counts.values())


async def _plan_full_summary_keyspace(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None,
    apply: bool,
    limit: int,
    max_pages: int,
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    total_planned = 0
    total_reconciled = 0
    total_superseded = 0
    for _ in range(max(1, int(max_pages or 1))):
        page = await plan_summary_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=limit,
        )
        pages.append(page)
        planned = _int(page.get("planned"))
        total_planned += planned
        total_reconciled += _int(page.get("artifact_reconciled"))
        total_superseded += _int(page.get("superseded"))
        if planned == 0:
            break
    return {
        "status": "complete" if apply else "planned",
        "pages": len(pages),
        "planned": total_planned,
        "artifact_reconciled": total_reconciled,
        "superseded": total_superseded,
        "last_page": pages[-1] if pages else {},
    }


async def _plan_full_graph_keyspace(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None,
    apply: bool,
    limit: int,
    max_pages: int,
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    total_planned = 0
    for _ in range(max(1, int(max_pages or 1))):
        page = await plan_graph_promotion_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=limit,
        )
        pages.append(page)
        planned = _int(page.get("planned"))
        total_planned += planned
        if planned == 0:
            break
    return {
        "status": "complete" if apply else "planned",
        "pages": len(pages),
        "planned": total_planned,
        "last_page": pages[-1] if pages else {},
    }


async def _run_slices(
    runner: Runner | None,
    *,
    slices: int,
    limit: int,
) -> dict[str, Any]:
    if runner is None or int(slices or 0) <= 0:
        return {"status": "skipped", "slices": 0, "claimed": 0, "results": []}
    results: list[dict[str, Any]] = []
    claimed = 0
    for _ in range(max(0, int(slices or 0))):
        result = await runner(limit=limit)
        results.append(result)
        claimed += _int(result.get("claimed"))
        if _int(result.get("claimed")) <= 0 and result.get("status") in {
            "empty",
            "paused_pressure",
        }:
            break
    return {
        "status": "complete",
        "slices": len(results),
        "claimed": claimed,
        "results": results,
    }


async def run_corpus_commander_cycle(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    qdrant_client: Any = None,
    apply: bool = True,
    owner: str | None = None,
    summary_plan_limit: int = 500,
    graph_plan_limit: int = 500,
    max_plan_pages: int = 100,
    summary_runner: Runner | None = None,
    summary_run_slices: int = 0,
    summary_run_limit: int = 25,
    graph_runner: Runner | None = None,
    graph_run_slices: int = 0,
    graph_run_limit: int = 5,
    repair_summary_vectors: bool = True,
) -> dict[str, Any]:
    """Run one crash-safe commander reconcile cycle for a corpus."""

    started = datetime.utcnow()
    owner = owner or f"corpus_commander:{uuid4().hex[:12]}"
    async with corpus_lane_lease(
        db,
        corpus_id=corpus_id,
        lane="commander",
        owner=owner,
    ) as lease:
        if not lease:
            return {
                "status": "busy",
                "corpus_id": corpus_id,
                "owner": owner,
                "reason": "commander_lane_already_owned",
            }

        steps: list[dict[str, Any]] = []
        reclaimed_summary = await reclaim_expired_running_jobs(
            db,
            collection_name="summary_jobs",
            corpus_id=corpus_id,
            user_id=user_id,
        )
        reclaimed_graph = await reclaim_expired_running_jobs(
            db,
            collection_name="graph_promotion_jobs",
            corpus_id=corpus_id,
            user_id=user_id,
        )
        steps.append(
            {
                "name": "lease_reclaim",
                "status": "complete",
                "changed": bool(reclaimed_summary or reclaimed_graph),
                "result": {
                    "summary_jobs_reclaimed": reclaimed_summary,
                    "graph_jobs_reclaimed": reclaimed_graph,
                },
            }
        )

        artifact_reconciled = await reconcile_satisfied_summary_jobs(
            db,
            corpus_id=corpus_id,
        )
        steps.append(
            {
                "name": "summary_artifact_reconcile",
                "status": "complete",
                "changed": bool(artifact_reconciled),
                "result": {"artifact_reconciled": artifact_reconciled},
            }
        )

        summary_plan = await _plan_full_summary_keyspace(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=summary_plan_limit,
            max_pages=max_plan_pages,
        )
        steps.append(
            {
                "name": "summary_plan_full_keyspace",
                "status": summary_plan.get("status"),
                "changed": _changed(summary_plan),
                "result": summary_plan,
            }
        )

        graph_plan = await _plan_full_graph_keyspace(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=apply,
            limit=graph_plan_limit,
            max_pages=max_plan_pages,
        )
        steps.append(
            {
                "name": "graph_plan_full_keyspace",
                "status": graph_plan.get("status"),
                "changed": _changed(graph_plan),
                "result": graph_plan,
            }
        )

        summary_run = await _run_slices(
            summary_runner,
            slices=summary_run_slices,
            limit=summary_run_limit,
        )
        steps.append(
            {
                "name": "summary_run_slices",
                "status": summary_run.get("status"),
                "changed": _changed(summary_run),
                "result": summary_run,
            }
        )

        graph_run = await _run_slices(
            graph_runner,
            slices=graph_run_slices,
            limit=graph_run_limit,
        )
        steps.append(
            {
                "name": "graph_run_slices",
                "status": graph_run.get("status"),
                "changed": _changed(graph_run),
                "result": graph_run,
            }
        )

        vector_receipt: dict[str, Any] = {
            "status": "skipped",
            "reason": "qdrant_client_unavailable",
        }
        if qdrant_client is not None:
            if apply and repair_summary_vectors:
                vector_receipt = await repair_parent_summary_vector_integrity(
                    db,
                    qdrant_client,
                    corpus_id=corpus_id,
                    user_id=user_id,
                )
            else:
                vector_receipt = await audit_parent_summary_vector_integrity(
                    db,
                    qdrant_client,
                    corpus_id=corpus_id,
                )
        steps.append(
            {
                "name": "parent_summary_vector_id_join",
                "status": vector_receipt.get("status"),
                "changed": _changed(vector_receipt),
                "result": vector_receipt,
            }
        )

        readiness = await materialize_corpus_readiness(db, corpus_id)
        return {
            "status": "complete",
            "corpus_id": corpus_id,
            "owner": owner,
            "apply": bool(apply),
            "started_at": started,
            "completed_at": datetime.utcnow(),
            "changed": any(bool(step.get("changed")) for step in steps),
            "steps": steps,
            "readiness": readiness,
        }
