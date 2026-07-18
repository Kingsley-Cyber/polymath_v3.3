"""Launch-state-bound isolated requeue for E2E ordinal 3 after dedup fix."""

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


def _journal_closure(corpus_id: str) -> dict[str, int]:
    digest = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
    rows = [
        json.loads(line)
        for line in (JOURNAL_ROOT / f"corpus-{digest}.jsonl").read_text().splitlines()
        if line.strip()
    ]
    submitted = {
        str(row.get("job_id") or "")
        for row in rows
        if row.get("event") == "submitted"
    }
    terminal = {
        str(row.get("job_id") or "")
        for row in rows
        if row.get("event") == "terminal"
    }
    return {
        "submitted": len(submitted),
        "terminal": len(terminal),
        "reused_terminal_output": sum(
            row.get("event") == "reused_terminal_output" for row in rows
        ),
        "closure_equal": submitted == terminal,
    }


async def main() -> None:
    state = json.loads(STATE.read_text())
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        scope = {"batch_id": batch_id, "corpus_id": corpus_id}
        before = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in (
                "queued",
                "running",
                "cancelled",
                "done",
                "failed",
                "skipped",
            )
        }
        expected_before = {
            "queued": 0,
            "running": 0,
            "cancelled": 11,
            "done": 3,
            "failed": 0,
            "skipped": 1,
        }
        if before != expected_before:
            raise RuntimeError(f"isolated requeue precondition drifted: {before}")
        item = await db["ingest_batch_items"].find_one(
            {**scope, "ordinal": 3, "status": "skipped"},
            {"_id": 0, "doc_id": 1, "error": 1},
        )
        if not item or "Exact source duplicate skipped" not in str(
            item.get("error") or ""
        ):
            raise RuntimeError("ordinal 3 is not the sealed exact-source skip")
        document = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": item.get("doc_id")},
            {"_id": 0, "write_state": 1, "ghost_b_metrics": 1},
        )
        ws = (document or {}).get("write_state") or {}
        metrics = (document or {}).get("ghost_b_metrics") or {}
        staged = await db["ghost_b_extractions"].count_documents(
            {"corpus_id": corpus_id, "doc_id": item.get("doc_id")}
        )
        if ws.get("verified") is True or ws.get("neo4j_written") is True:
            raise RuntimeError("ordinal 3 no longer has the diagnosed incomplete state")
        if metrics.get("request_batches") != 50 or staged != 1575:
            raise RuntimeError(
                f"stored extraction closure drifted: batches={metrics.get('request_batches')} "
                f"rows={staged}"
            )
        journal = _journal_closure(corpus_id)
        if journal != {
            "submitted": 201,
            "terminal": 201,
            "reused_terminal_output": 66,
            "closure_equal": True,
        }:
            raise RuntimeError(f"provider closure drifted before requeue: {journal}")
        now = datetime.utcnow()
        changed = await db["ingest_batch_items"].update_one(
            {**scope, "ordinal": 3, "status": "skipped"},
            {
                "$set": {"status": "queued", "phase": "queued", "updated_at": now},
                "$unset": {
                    "error": "",
                    "failure_stage": "",
                    "completed_at": "",
                    "lease_owner": "",
                    "lease_until": "",
                },
            },
        )
        if changed.modified_count != 1:
            raise RuntimeError(f"isolated requeue modified {changed.modified_count}")
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
            for status in (
                "queued",
                "running",
                "cancelled",
                "done",
                "failed",
                "skipped",
            )
        }
        if after != {
            "queued": 1,
            "running": 0,
            "cancelled": 11,
            "done": 3,
            "failed": 0,
            "skipped": 0,
        }:
            raise RuntimeError(f"isolated requeue postcondition drifted: {after}")
        print(
            json.dumps(
                {
                    "schema_version": "e2e_dedup_resume_requeue.v1",
                    "corpus_id": corpus_id,
                    "batch_id": batch_id,
                    "ordinal": 3,
                    "before": before,
                    "after": after,
                    "stored_extraction_rows": staged,
                    "stored_request_batches": metrics.get("request_batches"),
                    "journal_before": journal,
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
