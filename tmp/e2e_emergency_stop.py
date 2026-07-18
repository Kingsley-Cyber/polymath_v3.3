"""Fail-closed durable stop for the active preregistered E2E batch.

This operator is intentionally corpus/batch-ID agnostic: it reads the fsynced
launch receipt, verifies the named strict failure, and cancels only unfinished
items in that one fresh batch after the owning worker process is stopped.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
EXPECTED_ERROR = "LocalExtractionV1 entity canonical label is empty"
STOP_REASON = f"E2E RED gate stop: {EXPECTED_ERROR}"


async def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        batch_filter = {"batch_id": batch_id, "corpus_id": corpus_id}
        batch = await db["ingest_batches"].find_one(batch_filter, {"_id": 0})
        if not batch:
            raise RuntimeError("launch-state batch is absent")
        failed = await db["ingest_batch_items"].find_one(
            {**batch_filter, "status": "failed", "error": EXPECTED_ERROR},
            {"_id": 0, "item_id": 1, "filename": 1, "error": 1},
        )
        if not failed:
            raise RuntimeError("named strict failure is not durable")
        before = {}
        for status in ("queued", "running", "cancelled", "done", "failed"):
            before[status] = await db["ingest_batch_items"].count_documents(
                {**batch_filter, "status": status}
            )
        now = datetime.utcnow()
        update = await db["ingest_batch_items"].update_many(
            {**batch_filter, "status": {"$in": ["queued", "running", "leased"]}},
            {
                "$set": {
                    "status": "cancelled",
                    "phase": "cancelled_red_gate",
                    "error": STOP_REASON,
                    "updated_at": now,
                    "completed_at": now,
                },
                "$unset": {
                    "lease_owner": "",
                    "lease_until": "",
                },
            },
        )
        batch_update = await db["ingest_batches"].update_one(
            batch_filter,
            {
                "$set": {
                    "status": "cancelled",
                    "error": STOP_REASON,
                    "updated_at": now,
                    "completed_at": now,
                }
            },
        )
        after = {}
        for status in ("queued", "running", "cancelled", "done", "failed"):
            after[status] = await db["ingest_batch_items"].count_documents(
                {**batch_filter, "status": status}
            )
        final_batch = await db["ingest_batches"].find_one(
            batch_filter, {"_id": 0, "status": 1, "error": 1}
        )
        if before["failed"] != 1:
            raise RuntimeError(f"unexpected failed-item count: {before['failed']}")
        if after["queued"] or after["running"]:
            raise RuntimeError(f"unfinished items remain after stop: {after}")
        if after["cancelled"] != before["queued"] + before["running"] + before["cancelled"]:
            raise RuntimeError(f"cancelled-item closure mismatch: before={before} after={after}")
        if batch_update.modified_count != 1 or (final_batch or {}).get("status") != "cancelled":
            raise RuntimeError("batch did not reach durable cancelled state")
        print(
            json.dumps(
                {
                    "schema_version": "e2e_red_stop.v1",
                    "corpus_id": corpus_id,
                    "batch_id": batch_id,
                    "worker_required_state": "stopped_before_operator",
                    "named_failure": {
                        "filename": failed.get("filename"),
                        "error": failed.get("error"),
                    },
                    "before": before,
                    "after": after,
                    "items_modified": update.modified_count,
                    "batch_status": (final_batch or {}).get("status"),
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
