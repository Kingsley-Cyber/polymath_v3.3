"""Compact fail-closed watcher for extraction or full-batch completion."""

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
    parser.add_argument("--until", choices=("extraction", "batch"), required=True)
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    corpus_hash = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
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
                {"_id": 0, "status": 1, "phase": 1, "filename": 1, "error": 1},
            ).to_list(length=20)
            counts = Counter(str(row.get("status") or "") for row in items)
            failures = [
                row
                for row in items
                if str(row.get("status") or "")
                in {"failed", "failed_recoverable", "cancelled"}
            ]
            active = next(
                (row for row in items if str(row.get("status") or "") == "running"),
                {},
            )
            journal_rows = []
            if journal_path.exists():
                journal_rows = [
                    json.loads(line)
                    for line in journal_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            submitted = [row for row in journal_rows if row.get("event") == "submitted"]
            terminal = [row for row in journal_rows if row.get("event") == "terminal"]
            preflights = sum(
                1 for row in journal_rows if row.get("event") == "journal_preflight"
            )
            submitted_ids = {str(row.get("job_id") or "") for row in submitted}
            terminal_ids = {str(row.get("job_id") or "") for row in terminal}
            if "" in submitted_ids or "" in terminal_ids:
                raise RuntimeError("empty RunPod journal job identity")
            if len(submitted_ids) != len(submitted):
                raise RuntimeError("duplicate RunPod submitted job identity")
            if not terminal_ids.issubset(submitted_ids):
                raise RuntimeError("RunPod terminal lacks submitted receipt")
            if any(str(row.get("status") or "") != "COMPLETED" for row in terminal):
                raise RuntimeError("RunPod journal has a failed terminal")
            execution_seconds = sum(
                float(row.get("execution_time_ms") or 0.0) for row in terminal
            ) / 1000.0
            conservative_cost = execution_seconds * 0.00031 * 1.5
            summary = await summary_cost_snapshot(database, batch_id)
            calls_refused = int((summary or {}).get("calls_refused") or 0)
            line = {
                "at": datetime.now(timezone.utc).isoformat(),
                "batch_status": batch.get("status"),
                "done": int(counts.get("done", 0)),
                "queued": int(counts.get("queued", 0)),
                "running": int(counts.get("running", 0)),
                "active_file": active.get("filename"),
                "active_phase": active.get("phase"),
                "journal_preflights": preflights,
                "jobs_submitted": len(submitted),
                "jobs_terminal": len(terminal),
                "jobs_outstanding": len(submitted_ids - terminal_ids),
                "worker_seconds": round(execution_seconds, 3),
                "runpod_conservative_cost_usd": round(conservative_cost, 9),
                "summary_accounted_cost_usd": (summary or {}).get(
                    "accounted_cost_usd"
                ),
                "summary_calls_refused": calls_refused,
            }
            print(json.dumps(line, sort_keys=True), flush=True)
            if failures or str(batch.get("status") or "") in {"failed", "cancelled"}:
                raise RuntimeError("E2E batch entered a failure state")
            if conservative_cost >= 5.0:
                raise RuntimeError("RunPod conservative cost authority reached")
            if calls_refused:
                raise RuntimeError("summary cost controller refused a call")
            if args.until == "extraction":
                extraction_complete = (
                    preflights == 15
                    and len(submitted_ids - terminal_ids) == 0
                    and int(counts.get("queued", 0)) == 0
                    and str(active.get("phase") or "") not in {"", "queued", "ghosts"}
                )
                if extraction_complete:
                    print("WATCH_TERMINAL=EXTRACTION_COMPLETE", flush=True)
                    return
            elif str(batch.get("status") or "") == "done":
                if int(counts.get("done", 0)) != 15:
                    raise RuntimeError("done batch does not close at 15 done items")
                print("WATCH_TERMINAL=BATCH_COMPLETE", flush=True)
                return
            await asyncio.sleep(max(5.0, args.interval))
    finally:
        client.close()


asyncio.run(main())
