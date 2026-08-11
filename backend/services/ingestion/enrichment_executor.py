"""THE enrichment executor — single owner of enrichment execution.

Consolidation (owner-ordered 2026-08-11). Prior state: five overlapping
executors (reconciler tick execution, V1 auto-repair tick, per-doc
two-phase tasks, ad-hoc drivers, manual cycles) shared lanes and leases
and starved each other; the reconciler additionally gated execution on a
run ledger that the two-phase era never updated, so automatic ticks
skipped work as "all certified" while ~100k extraction jobs sat queued.

This loop is the one executor. Everything else plans; this runs.

Design:
- Iterates ACTIVE corpora (archived excluded) in a stable order.
- Owner rule enforced here, not by env discipline: a corpus with pending
  inline batch work (queued/staged/running/failed_recoverable) gets NO
  enrichment — fast lane first, dead last, always.
- Direct lane calls only: ingestion_service.run_extraction_jobs (claims
  carry the 7200s lease so big-document executions finish and flip) and
  run_graph_promotion_jobs. No reconciler, no ledger consultation.
- One heartbeat document (enrichment_executor/_id=primary) with counts —
  surfaced by /api/health/fleet — and one log line per non-empty cycle.
- Backoff when idle; exits never (lifespan cancels it on shutdown).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

INLINE_PENDING_STATUSES = ("queued", "staged", "running", "failed_recoverable")


def executor_enabled() -> bool:
    raw = os.environ.get("ENRICHMENT_EXECUTOR_ENABLED", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    # Default: run wherever ingest runners run (the workers).
    return os.environ.get("INGEST_RUNNERS_ENABLED", "").strip().lower() == "true"


async def _heartbeat(db: Any, payload: dict[str, Any]) -> None:
    try:
        await db["enrichment_executor"].update_one(
            {"_id": "primary"},
            {"$set": {**payload, "at": datetime.utcnow()}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001 — heartbeat must never kill the loop
        pass


async def run_enrichment_executor(db: Any, ingestion_service: Any) -> None:
    batch_limit = int(os.environ.get("ENRICHMENT_EXECUTOR_BATCH", "200") or 200)
    idle_sleep = float(os.environ.get("ENRICHMENT_EXECUTOR_IDLE_SECONDS", "120") or 120)
    cycle = 0
    logger.info("enrichment executor: single-owner loop starting (batch=%d)", batch_limit)
    while True:
        cycle += 1
        total_succeeded = 0
        try:
            corpora = await db["corpora"].find(
                {"status": {"$ne": "archived"}},
                {"corpus_id": 1, "user_id": 1},
            ).sort("corpus_id", 1).to_list(length=200)
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrichment executor: corpus scan failed: %s", exc)
            await asyncio.sleep(idle_sleep)
            continue
        for corpus in corpora:
            cid = str(corpus.get("corpus_id") or "")
            uid = str(corpus.get("user_id") or "") or None
            if not cid:
                continue
            try:
                pending_inline = await db["ingest_batch_items"].count_documents(
                    {"corpus_id": cid, "status": {"$in": list(INLINE_PENDING_STATUSES)}}
                )
                if pending_inline:
                    continue  # fast lane first — the owner rule, enforced here
                queued = await db["extraction_jobs"].count_documents(
                    {"corpus_id": cid, "status": "queued"}
                )
                if queued:
                    result = await ingestion_service.run_extraction_jobs(
                        corpus_id=cid, user_id=uid, limit=batch_limit
                    )
                    succeeded = int(
                        ((result or {}).get("counts") or {}).get("succeeded", 0) or 0
                    )
                    total_succeeded += succeeded
                    if succeeded or (result or {}).get("status") not in ("complete",):
                        logger.info(
                            "enrichment executor: corpus=%s status=%s claimed=%s succeeded=%d",
                            cid[:8], (result or {}).get("status"),
                            (result or {}).get("claimed"), succeeded,
                        )
                try:
                    await ingestion_service.run_graph_promotion_jobs(
                        corpus_id=cid, user_id=uid
                    )
                except Exception:  # noqa: BLE001 — promotion lane is best-effort per cycle
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "enrichment executor: corpus=%s cycle error: %s", cid[:8], exc
                )
        remaining = 0
        try:
            remaining = await db["extraction_jobs"].count_documents(
                {"status": {"$in": ["queued", "running"]}}
            )
        except Exception:  # noqa: BLE001
            pass
        await _heartbeat(db, {
            "cycle": cycle,
            "last_cycle_succeeded": total_succeeded,
            "remaining_jobs": remaining,
        })
        if total_succeeded:
            logger.info(
                "enrichment executor: cycle=%d succeeded=%d remaining=%d",
                cycle, total_succeeded, remaining,
            )
            continue  # more work likely claimable immediately
        await asyncio.sleep(idle_sleep)
