"""Watch the isolated repaired item and enforce zero provider dispatch."""

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


def _journal(corpus_id: str) -> dict[str, int]:
    digest = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
    path = JOURNAL_ROOT / f"corpus-{digest}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {
        "submitted": sum(row.get("event") == "submitted" for row in rows),
        "terminal": sum(row.get("event") == "terminal" for row in rows),
        "reused_terminal_output": sum(
            row.get("event") == "reused_terminal_output" for row in rows
        ),
    }


async def main() -> None:
    state = json.loads(STATE.read_text())
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    started = time.monotonic()
    last = None
    try:
        db = mongo[settings.MONGODB_DATABASE]
        while time.monotonic() - started < 1800:
            item = await db["ingest_batch_items"].find_one(
                {"batch_id": batch_id, "corpus_id": corpus_id, "ordinal": 2},
                {"_id": 0, "status": 1, "phase": 1, "doc_id": 1, "error": 1},
            )
            if not item:
                raise RuntimeError("repaired item is absent")
            journal = _journal(corpus_id)
            if journal["submitted"] != 200 or journal["terminal"] != 200:
                raise RuntimeError(f"replay created or lost provider jobs: {journal}")
            marker = (item.get("status"), item.get("phase"), journal["reused_terminal_output"])
            if marker != last:
                print(
                    json.dumps(
                        {
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "status": item.get("status"),
                            "phase": item.get("phase"),
                            "reused_terminal_output": journal[
                                "reused_terminal_output"
                            ],
                            "submitted": journal["submitted"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                last = marker
            if item.get("status") == "failed":
                raise RuntimeError(f"repaired item failed: {item.get('error')}")
            if item.get("status") == "done":
                document = await db["documents"].find_one(
                    {"corpus_id": corpus_id, "doc_id": item.get("doc_id")},
                    {"_id": 0, "write_state.verified": 1, "ghost_b_metrics": 1},
                )
                metrics = (document or {}).get("ghost_b_metrics") or {}
                result = {
                    "schema_version": "e2e_paid_replay_receipt.v1",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "item_status": item.get("status"),
                    "item_phase": item.get("phase"),
                    "verified": (document or {}).get("write_state", {}).get(
                        "verified"
                    ),
                    "request_batches": metrics.get("request_batches"),
                    "reused_request_batches": metrics.get(
                        "reused_request_batches"
                    ),
                    "new_request_batches": metrics.get("new_request_batches"),
                    "empty_canonical_exclusions": (
                        metrics.get("mention_exclusion_counts") or {}
                    ).get("empty_canonical_label"),
                    "journal": journal,
                    "secret_values_emitted": 0,
                }
                if result["verified"] is not True:
                    raise RuntimeError(f"repaired item is not verified: {result}")
                if result["request_batches"] != 66:
                    raise RuntimeError(f"repaired request closure drifted: {result}")
                if result["reused_request_batches"] != 66:
                    raise RuntimeError(f"paid replay closure drifted: {result}")
                if result["new_request_batches"] != 0:
                    raise RuntimeError(f"paid replay dispatched new work: {result}")
                if result["empty_canonical_exclusions"] != 1:
                    raise RuntimeError(f"mention exclusion closure drifted: {result}")
                if journal["reused_terminal_output"] != 66:
                    raise RuntimeError(f"replay journal count drifted: {result}")
                print(json.dumps(result, indent=2, sort_keys=True))
                return
            await asyncio.sleep(2)
        raise TimeoutError("repaired item did not finish within 1800 seconds")
    finally:
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
