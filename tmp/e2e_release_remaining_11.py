"""Release exactly the eleven remaining E2E items after item-4 verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.summary_cost_control import summary_cost_snapshot


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")


def _journal(corpus_id: str) -> dict[str, int | bool]:
    digest = hashlib.sha256(corpus_id.encode()).hexdigest()
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
        statuses = ("queued", "running", "cancelled", "done", "failed", "skipped")
        before = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in statuses
        }
        expected = {
            "queued": 0,
            "running": 0,
            "cancelled": 11,
            "done": 4,
            "failed": 0,
            "skipped": 0,
        }
        if before != expected:
            raise RuntimeError(f"remaining-item release precondition drifted: {before}")
        items = await db["ingest_batch_items"].find(
            scope,
            {"_id": 0, "ordinal": 1, "status": 1, "doc_id": 1},
        ).sort("ordinal", 1).to_list(length=20)
        done_ordinals = [int(row["ordinal"]) for row in items if row["status"] == "done"]
        cancelled_ordinals = [
            int(row["ordinal"]) for row in items if row["status"] == "cancelled"
        ]
        if done_ordinals != [0, 1, 2, 3] or cancelled_ordinals != list(range(4, 15)):
            raise RuntimeError(
                f"remaining-item ordinal fence drifted: done={done_ordinals} "
                f"cancelled={cancelled_ordinals}"
            )
        verified_docs = await db["documents"].count_documents(
            {"corpus_id": corpus_id, "write_state.verified": True}
        )
        if verified_docs != 4:
            raise RuntimeError(f"verified-document release fence drifted: {verified_docs}")
        journal = _journal(corpus_id)
        if journal != {"submitted": 201, "terminal": 201, "closure_equal": True}:
            raise RuntimeError(f"provider release fence drifted: {journal}")
        summary = await summary_cost_snapshot(db, batch_id)
        if summary.get("calls_refused") != 0 or summary.get("outstanding_reserved_usd") != "0.000000000":
            raise RuntimeError(f"summary release fence drifted: {summary}")
        now = datetime.utcnow()
        changed = await db["ingest_batch_items"].update_many(
            {
                **scope,
                "ordinal": {"$in": cancelled_ordinals},
                "status": "cancelled",
            },
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
        if changed.modified_count != 11:
            raise RuntimeError(f"remaining-item release modified {changed.modified_count}")
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
            for status in statuses
        }
        if after != {
            "queued": 11,
            "running": 0,
            "cancelled": 0,
            "done": 4,
            "failed": 0,
            "skipped": 0,
        }:
            raise RuntimeError(f"remaining-item release postcondition drifted: {after}")
        print(
            json.dumps(
                {
                    "schema_version": "e2e_remaining_11_release.v1",
                    "corpus_id": corpus_id,
                    "batch_id": batch_id,
                    "before": before,
                    "after": after,
                    "released_ordinals": cancelled_ordinals,
                    "verified_documents_before": verified_docs,
                    "journal_before": journal,
                    "summary_ceiling_basis_before_usd": summary.get("ceiling_basis_usd"),
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
