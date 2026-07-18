"""Watch canonical Ghost-A regeneration and whole-document verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
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
        baseline = await summary_cost_snapshot(db, batch_id)
        while time.monotonic() - started < 2400:
            item = await db["ingest_batch_items"].find_one(
                {"batch_id": batch_id, "corpus_id": corpus_id, "ordinal": 3},
                {"_id": 0, "status": 1, "phase": 1, "doc_id": 1, "error": 1},
            )
            if not item:
                raise RuntimeError("item 4 is absent")
            journal = _journal(corpus_id)
            if journal != {
                "submitted": 201,
                "terminal": 201,
                "closure_equal": True,
                "reused_terminal_output": 66,
            }:
                raise RuntimeError(f"summary repair changed RunPod closure: {journal}")
            cost = await summary_cost_snapshot(db, batch_id)
            if cost.get("calls_refused") != 0:
                raise RuntimeError(f"summary ceiling refusal during repair: {cost}")
            marker = (
                item.get("status"),
                item.get("phase"),
                cost.get("calls_completed"),
                cost.get("calls_reserved"),
            )
            if marker != last:
                print(
                    json.dumps(
                        {
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "status": item.get("status"),
                            "phase": item.get("phase"),
                            "summary_calls_completed": cost.get("calls_completed"),
                            "summary_calls_reserved": cost.get("calls_reserved"),
                            "summary_ceiling_basis_usd": cost.get("ceiling_basis_usd"),
                            "runpod_submitted_terminal": journal["submitted"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                last = marker
            if item.get("status") in {"failed", "skipped", "cancelled"}:
                raise RuntimeError(f"item-4 canonical repair RED: {item}")
            if item.get("status") == "done":
                doc_id = str(item.get("doc_id") or "")
                document = await db["documents"].find_one(
                    {"corpus_id": corpus_id, "doc_id": doc_id},
                    {"_id": 0, "ingest_stage": 1, "write_state": 1, "ghost_b_metrics": 1},
                )
                ws = (document or {}).get("write_state") or {}
                metrics = (document or {}).get("ghost_b_metrics") or {}
                parents = await db["parent_chunks"].count_documents(
                    {"corpus_id": corpus_id, "doc_id": doc_id}
                )
                summaries = await db["parent_chunks"].count_documents(
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "summary": {"$exists": True, "$nin": [None, ""]},
                    }
                )
                retrieval_texts = await db["parent_chunks"].count_documents(
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "retrieval_text": {"$exists": True, "$nin": [None, ""]},
                    }
                )
                final_cost = await summary_cost_snapshot(db, batch_id)
                result = {
                    "schema_version": "e2e_summary_repair_receipt.v1",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "item_status": item.get("status"),
                    "item_phase": item.get("phase"),
                    "ingest_stage": (document or {}).get("ingest_stage"),
                    "verified": ws.get("verified"),
                    "verify_errors": ws.get("verify_errors"),
                    "summary_points": ws.get("summary_points"),
                    "summaries_indexed": ws.get("summaries_indexed"),
                    "parent_rows": parents,
                    "nonempty_summaries": summaries,
                    "nonempty_retrieval_texts": retrieval_texts,
                    "stored_request_batches": metrics.get("request_batches"),
                    "failed_chunks": metrics.get("failed_chunks"),
                    "summary_calls_completed_delta": int(final_cost.get("calls_completed") or 0)
                    - int(baseline.get("calls_completed") or 0),
                    "summary_ceiling_basis_before_usd": baseline.get("ceiling_basis_usd"),
                    "summary_ceiling_basis_after_usd": final_cost.get("ceiling_basis_usd"),
                    "summary_calls_refused": final_cost.get("calls_refused"),
                    "summary_outstanding_reserved_usd": final_cost.get("outstanding_reserved_usd"),
                    "journal": journal,
                    "secret_values_emitted": 0,
                }
                if result["verified"] is not True or result["verify_errors"]:
                    raise RuntimeError(f"whole-document verification failed: {result}")
                if summaries != 174 or retrieval_texts != 174 or parents != 174:
                    raise RuntimeError(f"canonical parent summary closure failed: {result}")
                if result["summary_points"] != 174 or result["summaries_indexed"] is not True:
                    raise RuntimeError(f"summary projection closure failed: {result}")
                if result["stored_request_batches"] != 50 or result["failed_chunks"] != 0:
                    raise RuntimeError(f"extraction closure drifted: {result}")
                if result["summary_calls_refused"] != 0 or result["summary_outstanding_reserved_usd"] != "0.000000000":
                    raise RuntimeError(f"summary ledger not settled: {result}")
                print(json.dumps(result, indent=2, sort_keys=True))
                return
            await asyncio.sleep(2)
        raise TimeoutError("item 4 canonical repair exceeded 2400 seconds")
    finally:
        client.close()


asyncio.run(main())
