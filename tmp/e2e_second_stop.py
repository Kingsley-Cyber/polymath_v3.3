"""Fail-closed stop for the incomplete-duplicate-skip E2E RED."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")
STOP_REASON = "E2E RED gate stop: incomplete document marked skipped duplicate"


async def main() -> None:
    state = json.loads(STATE.read_text())
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        scope = {"batch_id": batch_id, "corpus_id": corpus_id}
        skipped = await db["ingest_batch_items"].find_one(
            {**scope, "ordinal": 3, "status": "skipped"},
            {"_id": 0, "filename": 1, "doc_id": 1, "error": 1},
        )
        if not skipped or not skipped.get("doc_id"):
            raise RuntimeError("incomplete duplicate skip is not durable")
        document = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": skipped["doc_id"]},
            {"_id": 0, "write_state.verified": 1},
        )
        if (document or {}).get("write_state", {}).get("verified") is True:
            raise RuntimeError("skipped duplicate is already verified")
        before = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in ("queued", "running", "cancelled", "done", "failed", "skipped")
        }
        if before["failed"] or before["skipped"] != 1 or before["done"] != 3:
            raise RuntimeError(f"second-stop precondition drifted: {before}")
        digest = hashlib.sha256(corpus_id.encode()).hexdigest()
        rows = [
            json.loads(line)
            for line in (JOURNAL_ROOT / f"corpus-{digest}.jsonl").read_text().splitlines()
            if line.strip()
        ]
        submitted = {str(row.get("job_id") or "") for row in rows if row.get("event") == "submitted"}
        terminal = {str(row.get("job_id") or "") for row in rows if row.get("event") == "terminal"}
        if len(submitted) != 201 or submitted != terminal:
            raise RuntimeError("second-stop provider closure drifted")
        now = datetime.utcnow()
        changed = await db["ingest_batch_items"].update_many(
            {**scope, "status": {"$in": ["queued", "running", "leased"]}},
            {
                "$set": {
                    "status": "cancelled",
                    "phase": "cancelled_red_gate",
                    "error": STOP_REASON,
                    "updated_at": now,
                    "completed_at": now,
                },
                "$unset": {"lease_owner": "", "lease_until": ""},
            },
        )
        await db["ingest_batches"].update_one(
            scope,
            {
                "$set": {
                    "status": "cancelled",
                    "error": STOP_REASON,
                    "updated_at": now,
                    "completed_at": now,
                }
            },
        )
        after = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in ("queued", "running", "cancelled", "done", "failed", "skipped")
        }
        if after["queued"] or after["running"]:
            raise RuntimeError(f"unfinished items remain after second stop: {after}")
        print(
            json.dumps(
                {
                    "schema_version": "e2e_second_red_stop.v1",
                    "corpus_id": corpus_id,
                    "batch_id": batch_id,
                    "before": before,
                    "after": after,
                    "items_modified": changed.modified_count,
                    "skipped_unverified_filename": skipped.get("filename"),
                    "provider_submitted_terminal": len(submitted),
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
