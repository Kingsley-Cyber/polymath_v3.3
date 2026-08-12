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

# RunPod-style GPU autoscale (owner-ordered 2026-08-11): while queued
# extraction work exists for GPU-routed corpora, keep the vLLM engine up
# and hammer it; when the tail drains, offload VRAM after a few idle
# cycles. New ingest (or Hermes via MCP) resurrects it at min notice.
# Inert until the engine is battery-qualified — never touches an engine
# still under test.
_GPU_IDLE_CYCLES = 0


def _probe_health(url: str) -> bool:
    if not url:
        return False
    import urllib.request as _rq

    try:
        with _rq.urlopen(url, timeout=4) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


def _lane_post(lane: dict[str, Any], path_key: str, default_path: str) -> str:
    import urllib.request as _rq

    url = str(lane.get("url") or "").rstrip("/")
    path = str(lane.get(path_key) or default_path)
    key = os.environ.get(str(lane.get("api_key_env") or "RTX_LANE_MANAGER_API_KEY"), "")
    if not url or not key:
        return "skipped_no_lane_credentials"
    try:
        req = _rq.Request(url + path, method="POST", headers={"X-Api-Key": key})
        with _rq.urlopen(req, timeout=20) as resp:
            return f"{path}:{resp.status}"
    except Exception as exc:  # noqa: BLE001
        return f"{path}:error:{str(exc)[:120]}"


async def _autoscale_gpu(db: Any, remaining_jobs: int) -> dict[str, Any]:
    global _GPU_IDLE_CYCLES
    try:
        route = await db["extraction_engine_routing"].find_one(
            {"_id": "primary"}, {"auto_gpu_engine": 1}
        ) or {}
        gpu = dict(route.get("auto_gpu_engine") or {})
        lane = dict(gpu.get("lane_manager") or {})
        if not gpu.get("enabled") or not lane.get("url"):
            return {"state": "inert"}
        if not gpu.get("qualified"):
            return {"state": "awaiting_qualification"}
        gpu_corpora = await db["corpora"].count_documents({
            "status": {"$ne": "archived"},
            "default_ingestion_config.extraction_engine": {
                "$in": ["auto", "ghost_b_llm"]
            },
        })
        want_gpu = bool(remaining_jobs and gpu_corpora)
        healthy = await asyncio.to_thread(
            _probe_health, str(gpu.get("health_url") or "")
        )
        action = ""
        if want_gpu and not healthy and gpu.get("autostart", True):
            _GPU_IDLE_CYCLES = 0
            action = await asyncio.to_thread(_lane_post, lane, "up_path", "/up")
        elif not want_gpu and healthy and gpu.get("auto_offload", True):
            _GPU_IDLE_CYCLES += 1
            if _GPU_IDLE_CYCLES >= int(gpu.get("offload_after_idle_cycles", 3) or 3):
                action = await asyncio.to_thread(_lane_post, lane, "down_path", "/down")
                _GPU_IDLE_CYCLES = 0
        else:
            if want_gpu:
                _GPU_IDLE_CYCLES = 0
        return {
            "state": "healthy" if healthy else "down",
            "want_gpu": want_gpu,
            "gpu_corpora": gpu_corpora,
            "idle_cycles": _GPU_IDLE_CYCLES,
            "action": action,
        }
    except Exception as exc:  # noqa: BLE001 — autoscale must never kill the loop
        return {"state": "error", "error": str(exc)[:200]}


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
    # Boot sweep (postmortem 2026-08-11): a deploy recreate kills runners
    # mid-batch and their corpus-lane leases idle the GPU until TTL expiry
    # (~30 min measured). Live holders heartbeat every few minutes, so any
    # lease with a 10-minute-stale heartbeat belongs to a dead process.
    try:
        from datetime import timedelta

        stale = datetime.utcnow() - timedelta(minutes=10)
        swept = await db["ingest_lane_leases"].delete_many(
            {"heartbeat_at": {"$lt": stale}}
        )
        if swept.deleted_count:
            logger.info(
                "enrichment executor: swept %d stale lane leases at boot",
                swept.deleted_count,
            )
    except Exception:  # noqa: BLE001 — sweep is best-effort
        pass
    while True:
        cycle += 1
        total_succeeded = 0
        # Per-cycle stale-lease sweep (boot-only proved insufficient: a
        # runner that dies AFTER startup leaves lanes wedged until TTL).
        try:
            from datetime import timedelta as _td

            await db["ingest_lane_leases"].delete_many(
                {"heartbeat_at": {"$lt": datetime.utcnow() - _td(minutes=10)}}
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            corpora = await db["corpora"].find(
                {"status": {"$ne": "archived"}},
                {"corpus_id": 1, "user_id": 1},
            ).sort("corpus_id", 1).to_list(length=200)
            # Lane-collision fix (owner speed ruling 2026-08-12): every
            # executor previously walked corpora in the SAME sorted order,
            # so N workers piled onto corpus[0] — one won its corpus-lane
            # mutex and the rest logged lease_busy, leaving the GPU fed by
            # a single lane. Rotate the walk by a per-process offset so
            # concurrent workers land on DIFFERENT corpora and the engine
            # sees N lanes of in-flight work instead of one.
            if corpora:
                import socket

                offset = (abs(hash(socket.gethostname())) + cycle) % len(corpora)
                corpora = corpora[offset:] + corpora[:offset]
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
                # Refill proactively (2026-08-12): the encoder engine consumes
                # ~53 chunks/s, so waiting for the queue to hit ZERO before
                # planning starves the GPU between cycles. Keep the pipe full.
                plan_threshold = int(os.environ.get("ENRICHMENT_PLAN_THRESHOLD", "5000") or 5000)
                if queued < plan_threshold:
                    # Gap-awareness (battery forensics 2026-08-11): two-phase
                    # defers enrichment, but the single-doc/MCP ingest path
                    # never plans jobs — docs sat extraction-less until a
                    # human called the planners. Planning is idempotent
                    # (deterministic job ids), so plan whenever idle.
                    try:
                        from services.ingestion.extraction_jobs import (
                            plan_extraction_jobs,
                        )
                        from services.ingestion.graph_promotion_jobs import (
                            plan_graph_promotion_jobs,
                        )

                        plan_limit = int(os.environ.get(
                            "ENRICHMENT_PLAN_LIMIT", "10000") or 10000)
                        await plan_extraction_jobs(
                            db, corpus_id=cid, user_id=uid, apply=True,
                            limit=plan_limit,
                        )
                        await plan_graph_promotion_jobs(db, corpus_id=cid, apply=True)
                        queued = await db["extraction_jobs"].count_documents(
                            {"corpus_id": cid, "status": "queued"}
                        )
                    except Exception:  # noqa: BLE001 — planning is best-effort per cycle
                        pass
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
        gpu_autoscale = await _autoscale_gpu(db, remaining)
        if gpu_autoscale.get("action"):
            logger.info("enrichment executor: gpu autoscale %s", gpu_autoscale)
        await _heartbeat(db, {
            "cycle": cycle,
            "last_cycle_succeeded": total_succeeded,
            "remaining_jobs": remaining,
            "gpu_autoscale": gpu_autoscale,
        })
        if total_succeeded:
            logger.info(
                "enrichment executor: cycle=%d succeeded=%d remaining=%d",
                cycle, total_succeeded, remaining,
            )
            continue  # more work likely claimable immediately
        await asyncio.sleep(idle_sleep)
