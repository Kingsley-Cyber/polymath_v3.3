"""One secret-free, fail-closed progress snapshot for the active 15-doc E2E."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.summary_cost_control import summary_cost_snapshot


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")
RUNPOD_RATE = 0.00031
RUNPOD_OVERHEAD = 1.5
RUNPOD_AUTHORITY = 5.0


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _journal(corpus_id: str) -> tuple[Path, list[dict[str, Any]]]:
    corpus_hash = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
    path = JOURNAL_ROOT / f"corpus-{corpus_hash}.jsonl"
    if not path.exists():
        return path, []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return path, rows


async def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        batch = await database["ingest_batches"].find_one(
            {"batch_id": batch_id, "corpus_id": corpus_id}, {"_id": 0}
        )
        if not batch:
            raise RuntimeError("durable E2E batch is absent")
        items = await database["ingest_batch_items"].find(
            {"batch_id": batch_id, "corpus_id": corpus_id},
            {
                "_id": 0,
                "filename": 1,
                "ordinal": 1,
                "status": 1,
                "phase": 1,
                "attempts": 1,
                "error": 1,
                "doc_id": 1,
                "started_at": 1,
                "completed_at": 1,
            },
        ).sort("ordinal", 1).to_list(length=20)
        if len(items) != 15:
            raise RuntimeError(f"durable item closure drifted: {len(items)}")
        item_counts = Counter(str(row.get("status") or "unknown") for row in items)
        failed_items = [
            {
                "filename": row.get("filename"),
                "status": row.get("status"),
                "phase": row.get("phase"),
                "error": str(row.get("error") or "")[:300],
            }
            for row in items
            if str(row.get("status") or "")
            in {"failed", "failed_recoverable", "cancelled"}
        ]
        active_items = [
            {
                "filename": row.get("filename"),
                "ordinal": row.get("ordinal"),
                "status": row.get("status"),
                "phase": row.get("phase"),
                "attempts": row.get("attempts"),
            }
            for row in items
            if str(row.get("status") or "") == "running"
        ]
        mongo_counts = {
            "documents": await database["documents"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "parent_chunks": await database["parent_chunks"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "chunks": await database["chunks"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "ghost_b_extractions": await database[
                "ghost_b_extractions"
            ].count_documents({"corpus_id": corpus_id}),
        }
        documents = await database["documents"].find(
            {"corpus_id": corpus_id},
            {
                "_id": 0,
                "filename": 1,
                "doc_id": 1,
                "status": 1,
                "ghost_b_metrics": 1,
                "write_state.verified": 1,
            },
        ).to_list(length=20)
        summary = await summary_cost_snapshot(database, batch_id)
        journal_path, journal_rows = _journal(corpus_id)
        submitted = [row for row in journal_rows if row.get("event") == "submitted"]
        terminal = [row for row in journal_rows if row.get("event") == "terminal"]
        unique_submitted = {str(row.get("job_id") or "") for row in submitted}
        unique_terminal = {str(row.get("job_id") or "") for row in terminal}
        if "" in unique_submitted or "" in unique_terminal:
            raise RuntimeError("RunPod journal contains an empty job identity")
        if len(unique_submitted) != len(submitted):
            raise RuntimeError("RunPod journal contains duplicate submitted job IDs")
        if not unique_terminal.issubset(unique_submitted):
            raise RuntimeError("RunPod terminal job lacks submitted receipt")
        execution_ms = sum(float(row.get("execution_time_ms") or 0.0) for row in terminal)
        delay_ms = [float(row.get("delay_time_ms") or 0.0) for row in terminal]
        conservative_cost = execution_ms / 1000.0 * RUNPOD_RATE * RUNPOD_OVERHEAD
        route_submitted = Counter(str(row.get("account_name") or "") for row in submitted)
        route_terminal = Counter(str(row.get("account_name") or "") for row in terminal)
        terminal_status = Counter(str(row.get("status") or "") for row in terminal)
        first_dispatch = min(
            (_timestamp(str(row["timestamp_utc"])) for row in submitted),
            default=None,
        )
        last_terminal = max(
            (_timestamp(str(row["timestamp_utc"])) for row in terminal),
            default=None,
        )
        wall_seconds = (
            max(0.0, last_terminal - first_dispatch)
            if first_dispatch is not None and last_terminal is not None
            else 0.0
        )
        completed_documents = [
            {
                "filename": row.get("filename"),
                "verified": (row.get("write_state") or {}).get("verified"),
                "request_batches": ((row.get("ghost_b_metrics") or {}).get(
                    "request_batches"
                )),
                "requested_chunks": ((row.get("ghost_b_metrics") or {}).get(
                    "requested_chunks"
                )),
            }
            for row in documents
            if str(row.get("status") or "") in {"done", "ready"}
            or (row.get("write_state") or {}).get("verified") is True
        ]
        result = {
            "schema_version": "runpod_e2e_progress.v1",
            "batch_status": batch.get("status"),
            "batch_counts": batch.get("counts"),
            "item_status_counts": dict(sorted(item_counts.items())),
            "active_items": active_items,
            "failed_items": failed_items,
            "completed_document_count": len(completed_documents),
            "completed_documents": completed_documents,
            "mongo_counts": mongo_counts,
            "summary_cost": summary,
            "runpod": {
                "journal_path": str(journal_path),
                "journal_preflights": sum(
                    1 for row in journal_rows if row.get("event") == "journal_preflight"
                ),
                "submitted_jobs": len(submitted),
                "terminal_jobs": len(terminal),
                "outstanding_jobs": len(unique_submitted - unique_terminal),
                "terminal_status_counts": dict(sorted(terminal_status.items())),
                "submitted_by_account": dict(sorted(route_submitted.items())),
                "terminal_by_account": dict(sorted(route_terminal.items())),
                "aggregate_execution_seconds": round(execution_ms / 1000.0, 3),
                "conservative_cost_usd": round(conservative_cost, 9),
                "authority_usd": RUNPOD_AUTHORITY,
                "wall_seconds_first_dispatch_to_last_terminal": round(
                    wall_seconds, 3
                ),
                "completed_requests_per_minute": round(
                    (len(terminal) / wall_seconds * 60.0) if wall_seconds else 0.0,
                    3,
                ),
                "delay_seconds": {
                    "min": round(min(delay_ms, default=0.0) / 1000.0, 3),
                    "p50": round(median(delay_ms) / 1000.0, 3) if delay_ms else 0.0,
                    "p95": round(_percentile(delay_ms, 0.95) / 1000.0, 3),
                    "max": round(max(delay_ms, default=0.0) / 1000.0, 3),
                },
            },
        }
        print(json.dumps(result, default=str, indent=2, sort_keys=True))
        if failed_items or str(batch.get("status") or "") in {
            "failed",
            "cancelled",
        }:
            raise RuntimeError("E2E durable batch entered a failure state")
        if any(status != "COMPLETED" for status in terminal_status):
            raise RuntimeError("RunPod journal contains a non-COMPLETED terminal")
        if conservative_cost >= RUNPOD_AUTHORITY:
            raise RuntimeError("RunPod conservative cost authority reached")
        if summary and summary.get("ceiling_reached"):
            raise RuntimeError("summary cost ceiling reached")
    finally:
        client.close()


asyncio.run(main())
