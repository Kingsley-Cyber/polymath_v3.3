"""Two-stage, launch-state-bound requeue for the repaired E2E failure."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")
EXPECTED_ERROR = "LocalExtractionV1 entity canonical label is empty"
STOP_REASON = f"E2E RED gate stop: {EXPECTED_ERROR}"


def _journal_counts(corpus_id: str) -> dict[str, int]:
    corpus_hash = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
    path = JOURNAL_ROOT / f"corpus-{corpus_hash}.jsonl"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {
        "submitted": sum(row.get("event") == "submitted" for row in rows),
        "terminal": sum(row.get("event") == "terminal" for row in rows),
        "reused_terminal_output": sum(
            row.get("event") == "reused_terminal_output" for row in rows
        ),
    }


async def main(mode: str) -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        scope = {"batch_id": batch_id, "corpus_id": corpus_id}
        batch = await db["ingest_batches"].find_one(scope, {"_id": 0})
        if not batch:
            raise RuntimeError("launch-state batch is absent")
        before = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in ("queued", "running", "cancelled", "done", "failed")
        }
        journal_before = _journal_counts(corpus_id)
        if journal_before["submitted"] != 200 or journal_before["terminal"] != 200:
            raise RuntimeError(f"pre-requeue journal closure drifted: {journal_before}")
        now = datetime.utcnow()

        if mode == "failed_only":
            if batch.get("status") != "cancelled" or before != {
                "queued": 0,
                "running": 0,
                "cancelled": 12,
                "done": 2,
                "failed": 1,
            }:
                raise RuntimeError(f"failed-only precondition drifted: {before}")
            selector = {**scope, "status": "failed", "error": EXPECTED_ERROR}
            expected_modified = 1
        else:
            failed_item = await db["ingest_batch_items"].find_one(
                {**scope, "ordinal": 2}, {"_id": 0, "status": 1, "doc_id": 1}
            )
            if not failed_item or failed_item.get("status") != "done":
                raise RuntimeError("repaired failed item is not durable done")
            document = await db["documents"].find_one(
                {"corpus_id": corpus_id, "doc_id": failed_item.get("doc_id")},
                {"_id": 0, "write_state.verified": 1, "ghost_b_metrics": 1},
            )
            metrics = (document or {}).get("ghost_b_metrics") or {}
            if (document or {}).get("write_state", {}).get("verified") is not True:
                raise RuntimeError("repaired failed item is not verified")
            if metrics.get("reused_request_batches") != 66:
                raise RuntimeError(f"paid replay count drifted: {metrics}")
            if metrics.get("new_request_batches") != 0:
                raise RuntimeError(f"paid replay dispatched new work: {metrics}")
            if (metrics.get("mention_exclusion_counts") or {}).get(
                "empty_canonical_label"
            ) != 1:
                raise RuntimeError(f"empty-canonical exclusion drifted: {metrics}")
            if journal_before != {
                "submitted": 200,
                "terminal": 200,
                "reused_terminal_output": 66,
            }:
                raise RuntimeError(f"replay journal closure drifted: {journal_before}")
            selector = {**scope, "status": "cancelled", "error": STOP_REASON}
            expected_modified = 12

        result = await db["ingest_batch_items"].update_many(
            selector,
            {
                "$set": {
                    "status": "queued",
                    "phase": "queued",
                    "updated_at": now,
                },
                "$unset": {
                    "error": "",
                    "failure_stage": "",
                    "completed_at": "",
                    "lease_owner": "",
                    "lease_until": "",
                },
            },
        )
        if result.modified_count != expected_modified:
            raise RuntimeError(
                f"requeue modified {result.modified_count}, expected {expected_modified}"
            )
        await db["ingest_batches"].update_one(
            scope,
            {
                "$set": {
                    "status": "queued",
                    "run_requested_at": now,
                    "updated_at": now,
                },
                "$unset": {"error": "", "completed_at": ""},
            },
        )
        after = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in ("queued", "running", "cancelled", "done", "failed")
        }
        if after["failed"] or (mode == "failed_only" and after["cancelled"] != 12):
            raise RuntimeError(f"post-requeue item closure drifted: {after}")
        print(
            json.dumps(
                {
                    "schema_version": "e2e_requeue_after_fix.v1",
                    "mode": mode,
                    "corpus_id": corpus_id,
                    "batch_id": batch_id,
                    "before": before,
                    "after": after,
                    "items_requeued": result.modified_count,
                    "journal_before": journal_before,
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("failed_only", "cancelled_remaining"))
    args = parser.parse_args()
    asyncio.run(main(args.mode))
