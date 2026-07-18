"""Enforce isolated item-4 resume with no new RunPod dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")


def _journal(corpus_id: str) -> dict[str, int | bool]:
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
    started = time.monotonic()
    last = None
    try:
        db = client[settings.MONGODB_DATABASE]
        while time.monotonic() - started < 1800:
            item = await db["ingest_batch_items"].find_one(
                {"batch_id": batch_id, "corpus_id": corpus_id, "ordinal": 3},
                {"_id": 0, "status": 1, "phase": 1, "doc_id": 1, "error": 1},
            )
            if not item:
                raise RuntimeError("ordinal 3 is absent")
            journal = _journal(corpus_id)
            if journal != {
                "submitted": 201,
                "terminal": 201,
                "closure_equal": True,
                "reused_terminal_output": 66,
            }:
                raise RuntimeError(f"isolated resume changed provider closure: {journal}")
            marker = (item.get("status"), item.get("phase"))
            if marker != last:
                print(
                    json.dumps(
                        {
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "status": item.get("status"),
                            "phase": item.get("phase"),
                            "provider_submitted_terminal": journal["submitted"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                last = marker
            if item.get("status") in {"failed", "skipped", "cancelled"}:
                raise RuntimeError(f"ordinal 3 terminal RED: {item}")
            if item.get("status") == "done":
                document = await db["documents"].find_one(
                    {"corpus_id": corpus_id, "doc_id": item.get("doc_id")},
                    {"_id": 0, "ingest_stage": 1, "write_state": 1, "ghost_b_metrics": 1},
                )
                ws = (document or {}).get("write_state") or {}
                metrics = (document or {}).get("ghost_b_metrics") or {}
                staged = await db["ghost_b_extractions"].count_documents(
                    {"corpus_id": corpus_id, "doc_id": item.get("doc_id")}
                )
                result = {
                    "schema_version": "e2e_dedup_resume_receipt.v1",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "item_status": item.get("status"),
                    "item_phase": item.get("phase"),
                    "ingest_stage": (document or {}).get("ingest_stage"),
                    "verified": ws.get("verified"),
                    "neo4j_written": ws.get("neo4j_written"),
                    "stored_extraction_rows": staged,
                    "stored_request_batches": metrics.get("request_batches"),
                    "historical_new_request_batches": metrics.get("new_request_batches"),
                    "reused_request_batches": metrics.get("reused_request_batches"),
                    "failed_chunks": metrics.get("failed_chunks"),
                    "journal": journal,
                    "secret_values_emitted": 0,
                }
                if result["verified"] is not True or result["neo4j_written"] is not True:
                    raise RuntimeError(f"ordinal 3 is not verified complete: {result}")
                if result["stored_extraction_rows"] != 1575:
                    raise RuntimeError(f"stored extraction rows drifted: {result}")
                if result["stored_request_batches"] != 50:
                    raise RuntimeError(f"stored request batches drifted: {result}")
                if result["failed_chunks"] != 0:
                    raise RuntimeError(f"stored extraction failures present: {result}")
                print(json.dumps(result, indent=2, sort_keys=True))
                return
            await asyncio.sleep(2)
        raise TimeoutError("ordinal 3 did not finish within 1800 seconds")
    finally:
        client.close()


asyncio.run(main())
