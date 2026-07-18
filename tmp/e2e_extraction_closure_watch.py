"""Durable-ID extraction closure watcher for the 15-document E2E."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.summary_cost_control import summary_cost_snapshot


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=10.0)
    args = parser.parse_args()
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    corpus_hash = hashlib.sha256(corpus_id.encode()).hexdigest()
    journal_path = JOURNAL_ROOT / f"corpus-{corpus_hash}.jsonl"
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        while True:
            batch = await database["ingest_batches"].find_one(
                {"batch_id": batch_id, "corpus_id": corpus_id}, {"_id": 0}
            )
            if not batch:
                raise RuntimeError("E2E batch disappeared")
            items = await database["ingest_batch_items"].find(
                {"batch_id": batch_id, "corpus_id": corpus_id},
                {"_id": 0, "status": 1, "phase": 1, "filename": 1},
            ).to_list(length=20)
            counts = Counter(str(row.get("status") or "") for row in items)
            active = next(
                (row for row in items if str(row.get("status") or "") == "running"),
                {},
            )
            failures = [
                row
                for row in items
                if str(row.get("status") or "")
                in {"failed", "failed_recoverable", "cancelled", "skipped"}
            ]
            documents = await database["documents"].find(
                {"corpus_id": corpus_id},
                {
                    "_id": 0,
                    "doc_id": 1,
                    "original_filename": 1,
                    "filename": 1,
                    "ghost_b_metrics": 1,
                },
            ).to_list(length=20)
            rows = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            submitted = [row for row in rows if row.get("event") == "submitted"]
            terminal = [row for row in rows if row.get("event") == "terminal"]
            submitted_ids = {str(row.get("job_id") or "") for row in submitted}
            terminal_ids = {str(row.get("job_id") or "") for row in terminal}
            preflights = sum(1 for row in rows if row.get("event") == "journal_preflight")
            if (
                "" in submitted_ids
                or "" in terminal_ids
                or len(submitted_ids) != len(submitted)
                or len(terminal_ids) != len(terminal)
                or not terminal_ids.issubset(submitted_ids)
                or any(str(row.get("status") or "") != "COMPLETED" for row in terminal)
            ):
                raise RuntimeError("RunPod journal identity/status closure failed")
            durable_ids: set[str] = set()
            incomplete_documents: list[str] = []
            for document in documents:
                metrics = document.get("ghost_b_metrics") or {}
                remote_jobs = metrics.get("remote_jobs") or []
                job_ids = [str(row.get("job_id") or "") for row in remote_jobs]
                if (
                    not job_ids
                    or "" in job_ids
                    or len(job_ids) != len(set(job_ids))
                    or int(metrics.get("request_batches") or 0) != len(job_ids)
                    or int(metrics.get("failed_chunks") or 0) != 0
                    or any(job_id not in terminal_ids for job_id in job_ids)
                ):
                    incomplete_documents.append(
                        str(
                            document.get("original_filename")
                            or document.get("filename")
                            or document.get("doc_id")
                            or ""
                        )
                    )
                    continue
                overlap = durable_ids.intersection(job_ids)
                if overlap:
                    raise RuntimeError("RunPod job identity maps to multiple documents")
                durable_ids.update(job_ids)
            summary = await summary_cost_snapshot(database, batch_id)
            execution_seconds = sum(
                float(row.get("execution_time_ms") or 0.0) for row in terminal
            ) / 1000.0
            result = {
                "at": datetime.now(timezone.utc).isoformat(),
                "batch_status": batch.get("status"),
                "done": int(counts.get("done", 0)),
                "queued": int(counts.get("queued", 0)),
                "running": int(counts.get("running", 0)),
                "active_file": active.get("filename"),
                "active_phase": active.get("phase"),
                "documents": len(documents),
                "documents_with_closed_remote_jobs": len(documents)
                - len(incomplete_documents),
                "incomplete_documents": incomplete_documents,
                "journal_preflights": preflights,
                "jobs_submitted": len(submitted),
                "jobs_terminal": len(terminal),
                "durable_job_ids": len(durable_ids),
                "durable_ids_equal_journal": durable_ids == submitted_ids,
                "runpod_conservative_cost_usd": round(
                    execution_seconds * 0.00031 * 1.5, 9
                ),
                "summary_calls_refused": int(summary.get("calls_refused") or 0),
            }
            print(json.dumps(result, sort_keys=True), flush=True)
            if failures or str(batch.get("status") or "") in {"failed", "cancelled"}:
                raise RuntimeError("E2E batch entered a failure state")
            if result["runpod_conservative_cost_usd"] >= 5.0:
                raise RuntimeError("RunPod conservative cost authority reached")
            if result["summary_calls_refused"]:
                raise RuntimeError("summary cost controller refused a call")
            extraction_complete = (
                len(documents) == 15
                and not incomplete_documents
                and durable_ids == submitted_ids
                and preflights >= 15
                and int(counts.get("queued", 0)) == 0
                and str(active.get("phase") or "") not in {"", "queued", "ghosts"}
            )
            if extraction_complete:
                print("WATCH_TERMINAL=EXTRACTION_COMPLETE_DURABLE_IDS", flush=True)
                return
            await asyncio.sleep(max(5.0, args.interval))
    finally:
        client.close()


asyncio.run(main())
