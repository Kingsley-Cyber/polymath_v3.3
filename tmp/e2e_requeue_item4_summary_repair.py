"""Isolated canonical-direction item-4 requeue after summary-clobber fix."""

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
VERIFY_ERROR = (
    "corpus_2c894530_naive: 174 summary payload(s) missing Mongo text; "
    "corpus_2c894530_hrag: 174 summary payload(s) missing Mongo text"
)


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
        "reused_terminal_output": sum(
            row.get("event") == "reused_terminal_output" for row in rows
        ),
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
            for status in ("queued", "running", "cancelled", "done", "failed", "skipped")
        }
        if before != {
            "queued": 0,
            "running": 0,
            "cancelled": 11,
            "done": 3,
            "failed": 1,
            "skipped": 0,
        }:
            raise RuntimeError(f"summary repair precondition drifted: {before}")
        item = await db["ingest_batch_items"].find_one(
            {**scope, "ordinal": 3, "status": "failed", "error": VERIFY_ERROR},
            {"_id": 0, "doc_id": 1},
        )
        if not item or not item.get("doc_id"):
            raise RuntimeError("sealed item-4 verification failure is absent")
        doc_id = str(item["doc_id"])
        document = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "write_state": 1, "ghost_b_metrics": 1},
        )
        ws = (document or {}).get("write_state") or {}
        metrics = (document or {}).get("ghost_b_metrics") or {}
        parents = await db["parent_chunks"].count_documents(
            {"corpus_id": corpus_id, "doc_id": doc_id}
        )
        nonempty_summaries = await db["parent_chunks"].count_documents(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "summary": {"$exists": True, "$nin": [None, ""]},
            }
        )
        if parents != 174 or nonempty_summaries != 0:
            raise RuntimeError(
                f"canonical summary repair shape drifted: parents={parents} "
                f"nonempty={nonempty_summaries}"
            )
        if (
            ws.get("verified") is not False
            or ws.get("neo4j_written") is not True
            or metrics.get("request_batches") != 50
        ):
            raise RuntimeError("item-4 durable state drifted before repair")
        journal = _journal(corpus_id)
        if journal != {
            "submitted": 201,
            "terminal": 201,
            "closure_equal": True,
            "reused_terminal_output": 66,
        }:
            raise RuntimeError(f"provider closure drifted: {journal}")
        cost = await summary_cost_snapshot(db, batch_id)
        if cost.get("calls_refused") != 0 or cost.get("outstanding_reserved_usd") != "0.000000000":
            raise RuntimeError(f"summary ledger is not settled before repair: {cost}")
        now = datetime.utcnow()
        changed = await db["ingest_batch_items"].update_one(
            {**scope, "ordinal": 3, "status": "failed", "error": VERIFY_ERROR},
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
            raise RuntimeError(f"isolated repair requeued {changed.modified_count}")
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
            for status in ("queued", "running", "cancelled", "done", "failed", "skipped")
        }
        if after != {
            "queued": 1,
            "running": 0,
            "cancelled": 11,
            "done": 3,
            "failed": 0,
            "skipped": 0,
        }:
            raise RuntimeError(f"summary repair postcondition drifted: {after}")
        print(
            json.dumps(
                {
                    "schema_version": "e2e_summary_repair_requeue.v1",
                    "corpus_id": corpus_id,
                    "batch_id": batch_id,
                    "ordinal": 3,
                    "before": before,
                    "after": after,
                    "parents_requiring_canonical_regeneration": parents,
                    "summary_ceiling_basis_before_usd": cost.get("ceiling_basis_usd"),
                    "summary_remaining_authority_before_usd": cost.get("remaining_authority_usd"),
                    "journal": journal,
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
