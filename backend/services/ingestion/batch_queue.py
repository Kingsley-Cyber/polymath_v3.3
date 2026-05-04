"""Durable, resource-aware batch ingestion queue.

This layer deliberately stays above the single-document worker. The worker
continues to own parse/chunk/vector/graph semantics; this module owns upload
spooling, durable batch state, process-local scheduling, restart recovery, and
hardware-aware admission. It is Mongo-backed so a backend restart can resume
queued work without asking the user to upload again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
import uuid
from collections import Counter
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from services.ingestion.cancellation import IngestCancelled
from services.ingestion.file_intake import normalize_upload_filename
from services.ingestion.vllm_idle_watcher import get_watcher as _get_vllm_watcher

logger = logging.getLogger(__name__)

TERMINAL_BATCH_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}


class DiskFloorExceeded(RuntimeError):
    """Raised when admission is refused because spool free disk is below
    INGEST_MIN_FREE_DISK_GB. Surfaces as 507 (Insufficient Storage) at the
    router so the user gets a clear "free up disk before retrying" signal
    rather than an opaque write failure deep in the worker.
    """

    def __init__(self, *, free_gb: float, required_gb: float, spool_dir: str) -> None:
        self.free_gb = free_gb
        self.required_gb = required_gb
        self.spool_dir = spool_dir
        super().__init__(
            f"Spool drive ({spool_dir}) has {free_gb:.2f} GB free but "
            f"INGEST_MIN_FREE_DISK_GB={required_gb:.2f} GB. "
            "Free up space (or migrate the volume root to a bigger drive) "
            "before queuing more files."
        )


def _spool_free_gb(spool_dir: str | os.PathLike[str]) -> float:
    """Best-effort free-space lookup. Returns 0.0 if the path doesn't exist
    yet — callers treat 0.0 as "starved" and refuse admission, so we never
    silently admit work onto a non-existent spool."""
    try:
        Path(spool_dir).mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(spool_dir).free / (1024**3)
    except Exception as exc:
        logger.warning("Failed to probe spool free disk for %s: %s", spool_dir, exc)
        return 0.0


_FAILURE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("token_budget", "context length"),
    ("token_budget", "maximum context"),
    ("token_budget", "input_tokens"),
    ("ghost_a_outage", "Ghost A produced 0/"),
    ("lane_disabled", "all summary lanes were disabled"),
    ("lane_disabled", "all extraction lanes were disabled"),
    ("mongo_bson_overflow", "BSONObjectTooLarge"),
    ("mongo_bson_overflow", "exceeds 16MB"),
    ("disk_full", "No space left on device"),
    ("disk_full", "ENOSPC"),
    ("vram_starved", "vram_floor_exceeded"),
    ("provider_timeout", "ReadTimeout"),
    ("provider_timeout", "ConnectTimeout"),
    ("provider_unreachable", "Connection refused"),
)


def _classify_failure(exc: Exception) -> str:
    """Bucket failures by recurrence-relevant error kind so the circuit
    breaker can detect "every doc is failing with the same config error"
    and pause the batch.

    Conservative: returns "unknown" when no pattern matches. Better to
    let the circuit breaker stay quiet than to misfire on a one-off."""
    text = str(exc) or exc.__class__.__name__
    text_lower = text.lower()
    for kind, hint in _FAILURE_PATTERNS:
        if hint.lower() in text_lower:
            return kind
    return "unknown"


async def _embedder_free_vram_mb(health_url: str) -> int | None:
    """Probe the embedder /health for current GPU free VRAM.

    Returns None on probe failure — callers treat None as "fail open" so
    the scheduler keeps moving when the probe itself is broken. The point
    of this probe is to back off when VRAM is GENUINELY tight, not to
    block the queue every time the embedder restarts.
    """
    try:
        import httpx  # local import keeps batch_queue importable in non-runtime tests
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(health_url)
            resp.raise_for_status()
            payload = resp.json() or {}
            free_mb = payload.get("gpu_free_mb")
            return int(free_mb) if free_mb is not None else None
    except Exception as exc:
        logger.debug("Embedder health probe failed (%s): %s", health_url, exc)
        return None

TERMINAL_ITEM_STATUSES = {
    "graph_ready",
    "graph_partial",
    "graph_failed_token_budget",
    "needs_backfill",
    "vector_ready",
    "failed",
    "cancelled",
}
RUNNING_ITEM_STATUSES = {
    "parsing",
    "chunked",
    "vectorizing",
    "graph_pending",
    "graph_extracting",
}
NON_TERMINAL_ITEM_STATUSES = RUNNING_ITEM_STATUSES | {"queued"}
RETRYABLE_ITEM_STATUSES = {"queued", "failed", "needs_backfill"}


IngestCallable = Callable[..., Awaitable[IngestJobResponse]]
WarmCallable = Callable[..., Awaitable[dict]]


def _now() -> datetime:
    return datetime.utcnow()


def _ws_get(write_state: Any, key: str, default: Any = None) -> Any:
    if isinstance(write_state, dict):
        return write_state.get(key, default)
    return getattr(write_state, key, default)


def _safe_filename(filename: str, max_len: int = 180) -> str:
    name = Path(filename or "upload").name
    name = re.sub(r"[^a-zA-Z0-9._ -]+", "_", name).strip(" .")
    if not name:
        return "upload"
    if len(name) <= max_len:
        return name
    suffix = Path(name).suffix
    if suffix and len(suffix) < max_len:
        stem_len = max_len - len(suffix)
        stem = name[: -len(suffix)].rstrip(" .")[:stem_len].rstrip(" .")
        return f"{stem or 'upload'}{suffix}"
    return name[:max_len].rstrip(" .") or "upload"


def _resume_partition_by_corpus(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Keep only the newest queued/running batch per corpus on backend restart."""
    ordered = sorted(
        rows,
        key=lambda row: (
            row.get("created_at") or datetime.min,
            row.get("updated_at") or datetime.min,
            str(row.get("batch_id") or ""),
        ),
        reverse=True,
    )
    seen: set[str] = set()
    resume: list[str] = []
    superseded: list[str] = []
    for row in ordered:
        batch_id = str(row.get("batch_id") or "")
        if not batch_id:
            continue
        key = str(row.get("corpus_id") or batch_id)
        if key in seen:
            superseded.append(batch_id)
            continue
        seen.add(key)
        resume.append(batch_id)
    return resume, superseded


def _sys_memory() -> tuple[float, float]:
    """Return total/available RAM in GB without requiring psutil."""
    try:
        import psutil  # type: ignore

        mem = psutil.virtual_memory()
        return round(mem.total / (1024**3), 2), round(mem.available / (1024**3), 2)
    except Exception:
        pass
    if hasattr(os, "sysconf"):
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            total = (page * pages) / (1024**3)
            # Linux exposes available memory in /proc/meminfo. If it is not
            # available, use total as a conservative-ish fallback for local dev.
            available = total
            try:
                with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("MemAvailable:"):
                            available = int(line.split()[1]) * 1024 / (1024**3)
                            break
            except Exception:
                pass
            return round(total, 2), round(available, 2)
        except Exception:
            pass
    return 0.0, 0.0


def detect_resource_profile(spool_dir: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    root = Path(spool_dir or settings.INGEST_SPOOL_DIR)
    root.mkdir(parents=True, exist_ok=True)
    total_ram, available_ram = _sys_memory()
    cpu_count = os.cpu_count() or 1
    disk = shutil.disk_usage(root)
    gpu_devices: list[dict[str, Any]] = []
    cuda_available = False
    try:
        import torch  # type: ignore

        cuda_available = bool(torch.cuda.is_available())
        if cuda_available:
            for idx in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(idx)
                allocated = torch.cuda.memory_allocated(idx) / (1024**3)
                reserved = torch.cuda.memory_reserved(idx) / (1024**3)
                gpu_devices.append(
                    {
                        "index": idx,
                        "device": f"cuda:{idx}",
                        "name": torch.cuda.get_device_name(idx),
                        "vram_total_gb": round(props.total_memory / (1024**3), 2),
                        "memory_allocated_gb": round(allocated, 2),
                        "memory_reserved_gb": round(reserved, 2),
                    }
                )
    except Exception as exc:
        logger.debug("CUDA resource detection unavailable: %s", exc)

    # Doc-worker auto-sizing. The legacy caps (min(4, …) on pre-vector,
    # min(…, 4) on graph headroom, min(…, 2) on recommended_parse / vector)
    # were tuned for a 24 GB consumer card. On an RTX Pro 6000 Blackwell
    # (97 GB) or H100, those caps leave ~75% of vllm idle during graph
    # extraction. Two new env knobs let high-VRAM operators raise the
    # ceilings without abandoning the safety floor for low-RAM laptops.
    pre_vector_cap = max(
        1, int(getattr(settings, "INGEST_PRE_VECTOR_DOC_CAP", 4))
    )
    graph_cap = max(
        1, int(getattr(settings, "INGEST_GRAPH_DOC_CAP", 4))
    )
    if available_ram and available_ram < 4:
        pre_vector_doc_slots = 1
    elif available_ram and available_ram < 12:
        pre_vector_doc_slots = 2
    else:
        pre_vector_doc_slots = min(pre_vector_cap, max(1, cpu_count // 4))
    if gpu_devices:
        # Was: cap at gpu_count + 1 (forces 2 on a single-GPU box). Now: only
        # cap below the GPU count when VRAM is genuinely tight; otherwise
        # respect pre_vector_cap.
        gpu_floor = max(1, len(gpu_devices) + 1)
        if pre_vector_cap > gpu_floor:
            pre_vector_doc_slots = min(pre_vector_doc_slots, pre_vector_cap)
        else:
            pre_vector_doc_slots = min(pre_vector_doc_slots, gpu_floor)

    # Deep ingestion keeps vector RAG usable before graph completion. Give the
    # batch scheduler graph headroom so graph_extracting items do not consume
    # every document worker and starve queued docs from reaching Qdrant.
    graph_doc_headroom = 0
    if not (available_ram and available_ram < 4):
        graph_doc_headroom = max(1, min(int(settings.INGEST_MAX_GRAPH_MODEL_PHASE_DOCS), graph_cap))
    max_active_docs = pre_vector_doc_slots + graph_doc_headroom
    if settings.INGEST_BATCH_MAX_ACTIVE_DOCS:
        max_active_docs = max(1, int(settings.INGEST_BATCH_MAX_ACTIVE_DOCS))
    recommended_parse = min(max_active_docs, pre_vector_cap)
    recommended_vector = max(1, min(pre_vector_doc_slots, pre_vector_cap))
    recommended_graph = max(
        1,
        min(
            int(settings.INGEST_MAX_GRAPH_MODEL_PHASE_DOCS),
            max(1, graph_doc_headroom or len(gpu_devices) or 1),
        ),
    )
    worker_batches: dict[str, int] = {}
    for gpu in gpu_devices:
        label = str(gpu.get("name") or "").lower()
        if "3090" in label:
            worker_batches[gpu["device"]] = 16
        elif "4070" in label:
            worker_batches[gpu["device"]] = 8
        else:
            vram = float(gpu.get("vram_total_gb") or 0)
            worker_batches[gpu["device"]] = 12 if vram >= 16 else 6

    return {
        "cpu_count": cpu_count,
        "ram_total_gb": total_ram,
        "ram_available_gb": available_ram,
        "disk_free_gb": round(disk.free / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "spool_dir": str(root),
        "cuda_available": cuda_available,
        "gpu_count": len(gpu_devices),
        "gpu_devices": gpu_devices,
        "recommended_parse_concurrency": recommended_parse,
        "recommended_vector_concurrency": recommended_vector,
        "recommended_graph_concurrency": recommended_graph,
        "recommended_local_worker_batch_sizes": worker_batches,
        "pre_vector_doc_slots": pre_vector_doc_slots,
        "graph_doc_headroom": graph_doc_headroom,
        "max_active_docs": max_active_docs,
        "max_spooled_bytes": int(settings.INGEST_MAX_SPOOLED_BYTES),
    }


def item_status_from_write_state(write_state: Any) -> str:
    graph_status = _ws_get(write_state, "graph_status") or ""
    if graph_status == "graph_ready":
        return "graph_ready"
    if graph_status == "graph_partial":
        return "graph_partial"
    if graph_status == "graph_failed_token_budget":
        return "graph_failed_token_budget"
    if graph_status in {"needs_backfill", "graph_retry_scheduled"}:
        return "needs_backfill"
    if graph_status == "graph_extracting":
        return "graph_extracting"
    if _ws_get(write_state, "vector_ready", False) or _ws_get(write_state, "qdrant_written", False):
        return "vector_ready"
    return "failed"


def _write_state_vector_ready(write_state: Any) -> bool:
    return bool(
        _ws_get(write_state, "vector_ready", False)
        or _ws_get(write_state, "qdrant_written", False)
    )


def _write_state_graph_ready(write_state: Any) -> bool:
    graph_status = str(_ws_get(write_state, "graph_status") or "")
    if graph_status == "graph_ready":
        return True
    if graph_status in {
        "graph_partial",
        "needs_backfill",
        "graph_retry_scheduled",
        "graph_failed_token_budget",
        "graph_extracting",
    }:
        return False
    return bool(_ws_get(write_state, "neo4j_written", False))


def _live_running_status_from_write_state(write_state: Any) -> str | None:
    graph_status = str(_ws_get(write_state, "graph_status") or "")
    if graph_status == "graph_extracting":
        return "graph_extracting"
    return None


def _vector_ready_query(batch_id: str) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "$or": [
            {"write_state.vector_ready": True},
            {"write_state.qdrant_written": True},
            {"status": "vector_ready"},
            {"status": "graph_ready"},
            {"status": "graph_partial"},
            {"status": "needs_backfill"},
            {"status": "graph_failed_token_budget"},
        ],
    }


def _terminal_batch_status(
    *,
    total_files: int,
    failed: int,
    needs_backfill: int,
    graph_partial: int,
    graph_failed: int = 0,
    cancelled: int = 0,
) -> str:
    if cancelled and cancelled == total_files:
        return "cancelled"
    if failed and failed == total_files:
        return "failed"
    if failed or cancelled or needs_backfill or graph_partial or graph_failed:
        return "completed_with_errors"
    return "completed"


def _batch_count_fields(
    counts: Counter, *, vector_ready: int, graph_ready: int | None = None
) -> dict[str, int]:
    total = sum(counts.values())
    processing = sum(counts.get(status, 0) for status in RUNNING_ITEM_STATUSES)
    return {
        "total_files": total,
        "queued_count": counts.get("queued", 0),
        "processing_count": processing,
        "vector_ready_count": vector_ready,
        "graph_ready_count": counts.get("graph_ready", 0) if graph_ready is None else graph_ready,
        "graph_partial_count": counts.get("graph_partial", 0),
        "failed_count": counts.get("failed", 0),
        "cancelled_count": counts.get("cancelled", 0),
        "needs_backfill_count": counts.get("needs_backfill", 0),
        "graph_failed_count": counts.get("graph_failed_token_budget", 0),
    }


class BatchIngestionManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._db: AsyncIOMotorDatabase | None = None
        self._ingest: IngestCallable | None = None
        self._warm_graph_cache: WarmCallable | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_items: set[str] = set()
        self._cancelled_upload_ids: set[str] = set()
        self._lock = asyncio.Lock()

    def attach(
        self,
        *,
        db: AsyncIOMotorDatabase,
        ingest_callable: IngestCallable,
        warm_graph_cache_callable: WarmCallable | None = None,
    ) -> None:
        self._db = db
        self._ingest = ingest_callable
        self._warm_graph_cache = warm_graph_cache_callable

    async def start_resume(self) -> None:
        if self._db is None:
            return
        rows = await self._db["ingestion_batches"].find(
            {"status": {"$in": ["queued", "running"]}},
            {"batch_id": 1, "corpus_id": 1, "created_at": 1, "updated_at": 1, "_id": 0},
        ).to_list(length=None)
        resume_ids, superseded_ids = _resume_partition_by_corpus(rows)
        if superseded_ids:
            reason = "superseded_by_newer_batch_after_backend_restart"
            await self._cancel_items(
                {
                    "batch_id": {"$in": superseded_ids},
                    "status": {"$in": list(NON_TERMINAL_ITEM_STATUSES)},
                },
                reason=reason,
                cleanup_spool=True,
            )
            await self._db["ingestion_batches"].update_many(
                {"batch_id": {"$in": superseded_ids}, "status": {"$in": ["queued", "running"]}},
                {
                    "$set": {
                        "status": "cancelled",
                        "current_phase": "cancelled",
                        "updated_at": _now(),
                        "finished_at": _now(),
                    },
                    "$addToSet": {"warnings": reason},
                },
            )
        if resume_ids:
            await self._db["ingestion_batch_items"].update_many(
                {
                    "batch_id": {"$in": resume_ids},
                    "status": {"$in": list(RUNNING_ITEM_STATUSES)},
                },
                {
                    "$set": {
                        "status": "queued",
                        "updated_at": _now(),
                        "resume_reason": "backend_restart",
                    }
                },
            )
            await self._db["ingestion_batches"].update_many(
                {"batch_id": {"$in": resume_ids}, "status": {"$in": ["running", "queued"]}},
                {"$set": {"status": "queued", "updated_at": _now()}},
            )
        for batch_id in resume_ids:
            self.ensure_running(batch_id)

    async def disconnect(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    @property
    def active_doc_count(self) -> int:
        return len(self._active_items)

    def resource_profile(self) -> dict[str, Any]:
        profile = detect_resource_profile(self.settings.INGEST_SPOOL_DIR)
        profile["active_batch_workers"] = len(self._tasks)
        profile["active_doc_jobs"] = len(self._active_items)
        return profile

    async def queue_metrics(self) -> dict[str, Any]:
        if self._db is None:
            return {}
        item_counts = Counter()
        async for row in self._db["ingestion_batch_items"].aggregate(
            [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        ):
            item_counts[str(row["_id"])] = int(row["count"])
        spooled = await self._db["ingestion_batch_items"].aggregate(
            [
                {"$match": {"status": {"$nin": list(TERMINAL_ITEM_STATUSES - {"failed"})}}},
                {"$group": {"_id": None, "bytes": {"$sum": "$size_bytes"}}},
            ]
        ).to_list(length=1)
        return {
            "parse_queue_depth": item_counts.get("queued", 0),
            "vector_queue_depth": item_counts.get("chunked", 0) + item_counts.get("vectorizing", 0),
            "graph_queue_depth": item_counts.get("graph_pending", 0) + item_counts.get("graph_extracting", 0),
            "active_parse_jobs": item_counts.get("parsing", 0),
            "active_vector_jobs": item_counts.get("vectorizing", 0),
            "active_graph_jobs": item_counts.get("graph_extracting", 0),
            "vector_ready_docs": item_counts.get("vector_ready", 0),
            "graph_ready_docs": item_counts.get("graph_ready", 0),
            "graph_partial_docs": item_counts.get("graph_partial", 0),
            "failed_docs": item_counts.get("failed", 0),
            "spooled_bytes_current": int(spooled[0]["bytes"]) if spooled else 0,
        }

    def ensure_running(self, batch_id: str) -> None:
        if not batch_id or batch_id in self._tasks:
            return
        task = asyncio.create_task(self._run_batch(batch_id))
        self._tasks[batch_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(batch_id, None))

    async def _cancel_items(
        self,
        query: dict[str, Any],
        *,
        reason: str,
        cleanup_spool: bool = False,
    ) -> dict[str, Any]:
        if self._db is None:
            return {"cancelled_items": 0, "batch_ids": []}
        rows = await self._db["ingestion_batch_items"].find(
            query,
            {"_id": 0, "upload_id": 1, "batch_id": 1, "spool_path": 1},
        ).to_list(length=None)
        if not rows:
            return {"cancelled_items": 0, "batch_ids": []}
        upload_ids = [str(row.get("upload_id")) for row in rows if row.get("upload_id")]
        batch_ids = sorted({str(row.get("batch_id")) for row in rows if row.get("batch_id")})
        self._cancelled_upload_ids.update(upload_ids)
        now = _now()
        await self._db["ingestion_batch_items"].update_many(
            {"upload_id": {"$in": upload_ids}},
            {
                "$set": {
                    "status": "cancelled",
                    "current_phase": "cancelled",
                    "cancel_reason": reason,
                    "error": reason,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
        )
        if cleanup_spool:
            for row in rows:
                path = row.get("spool_path")
                if path:
                    await asyncio.to_thread(self._safe_cleanup_spool, Path(str(path)))
        for batch_id in batch_ids:
            await self._refresh_batch_counts(batch_id)
            await self._finish_if_done(batch_id)
        return {"cancelled_items": len(upload_ids), "batch_ids": batch_ids}

    async def cancel_document_work(
        self,
        *,
        corpus_id: str,
        doc_id: str | None = None,
        content_hash: str | None = None,
        user_id: str | None = None,
        reason: str = "document_deleted",
    ) -> dict[str, Any]:
        selectors: list[dict[str, Any]] = []
        if doc_id:
            selectors.append({"doc_id": doc_id})
        if content_hash:
            selectors.append({"content_hash": content_hash})
        if not selectors:
            return {"cancelled_items": 0, "batch_ids": []}
        query: dict[str, Any] = {
            "corpus_id": corpus_id,
            "status": {"$in": list(NON_TERMINAL_ITEM_STATUSES)},
        }
        if user_id:
            query["user_id"] = user_id
        if len(selectors) == 1:
            query.update(selectors[0])
        else:
            query["$or"] = selectors
        return await self._cancel_items(query, reason=reason, cleanup_spool=True)

    async def cancel_corpus_work(
        self,
        *,
        corpus_id: str,
        user_id: str | None = None,
        reason: str = "corpus_deleted",
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "corpus_id": corpus_id,
            "status": {"$in": list(NON_TERMINAL_ITEM_STATUSES)},
        }
        if user_id:
            query["user_id"] = user_id
        return await self._cancel_items(query, reason=reason, cleanup_spool=True)

    async def create_batch(
        self,
        *,
        corpus_id: str,
        user_id: str,
        uploads: list[Any],
        ingestion_config: IngestionConfig,
        model: str = "",
        ingest_overrides: dict | None = None,
        warnings: list[str] | None = None,
        preflight: dict | None = None,
    ) -> dict[str, Any]:
        if self._db is None:
            raise RuntimeError("BatchIngestionManager is not attached")
        if not uploads:
            raise ValueError("No files supplied")
        # Wake vllm containers if the idle watcher had stopped them. Blocks
        # until /health is healthy; keeps batch admission from racing into
        # a half-loaded model. No-op when the watcher is disabled.
        watcher = _get_vllm_watcher()
        if watcher is not None:
            try:
                await watcher.ensure_started()
            except Exception as exc:
                logger.warning(
                    "create_batch: vllm idle watcher could not start "
                    "containers (%s) — proceeding; the next ingest may "
                    "fail health checks until the operator brings them up.",
                    exc,
                )
        # Refuse to admit a new batch when the spool drive is below the disk
        # floor — protects against queue-builds-up scenarios where ENOSPC
        # would otherwise corrupt mid-flight writes (mongo journals, qdrant
        # write-ahead logs, neo4j store) several minutes into ingestion.
        free_gb = _spool_free_gb(self.settings.INGEST_SPOOL_DIR)
        if free_gb < float(self.settings.INGEST_MIN_FREE_DISK_GB):
            raise DiskFloorExceeded(
                free_gb=free_gb,
                required_gb=float(self.settings.INGEST_MIN_FREE_DISK_GB),
                spool_dir=str(self.settings.INGEST_SPOOL_DIR),
            )
        profile = self.resource_profile()
        batch_id = str(uuid.uuid4())
        now = _now()
        batch_dir = Path(self.settings.INGEST_SPOOL_DIR).resolve() / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_doc = {
            "batch_id": batch_id,
            "corpus_id": corpus_id,
            "user_id": user_id,
            "status": "queued",
            "total_files": len(uploads),
            "queued_count": 0,
            "processing_count": 0,
            "vector_ready_count": 0,
            "graph_ready_count": 0,
            "graph_partial_count": 0,
            "failed_count": 0,
            "cancelled_count": 0,
            "estimated_chunks": 0,
            "estimated_runtime": None,
            "current_phase": "spooling",
            "warnings": list(warnings or []),
            "preflight": preflight or {},
            "ingestion_config": ingestion_config.model_dump(),
            "ingest_overrides": ingest_overrides or {},
            "model": model or "",
            "resource_profile_at_enqueue": profile,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
        }
        await self._db["ingestion_batches"].insert_one(batch_doc)
        total_bytes = 0
        item_docs: list[dict[str, Any]] = []
        try:
            for upload in uploads:
                item = await self._spool_upload(
                    upload=upload,
                    batch_id=batch_id,
                    batch_dir=batch_dir,
                    corpus_id=corpus_id,
                    user_id=user_id,
                )
                if total_bytes + int(item["size_bytes"]) > int(self.settings.INGEST_MAX_SPOOLED_BYTES):
                    await asyncio.to_thread(
                        self._safe_cleanup_spool,
                        Path(str(item["spool_path"])),
                    )
                    raise ValueError(
                        "Batch upload exceeds configured spool limit "
                        f"({self.settings.INGEST_MAX_SPOOLED_BYTES} bytes)."
                    )
                total_bytes += int(item["size_bytes"])
                item_docs.append(item)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, batch_dir, True)
            await self._db["ingestion_batches"].update_one(
                {"batch_id": batch_id},
                {"$set": {"status": "failed", "current_phase": "spool_failed", "updated_at": _now()}},
            )
            raise
        content_hashes = sorted({str(item.get("content_hash")) for item in item_docs if item.get("content_hash")})
        if content_hashes:
            result = await self._cancel_items(
                {
                    "corpus_id": corpus_id,
                    "user_id": user_id,
                    "content_hash": {"$in": content_hashes},
                    "status": {"$in": list(NON_TERMINAL_ITEM_STATUSES)},
                    "batch_id": {"$ne": batch_id},
                },
                reason="superseded_by_new_upload_of_same_content",
                cleanup_spool=True,
            )
            if result.get("cancelled_items"):
                await self._db["ingestion_batches"].update_one(
                    {"batch_id": batch_id},
                    {
                        "$addToSet": {
                            "warnings": (
                                "Cancelled older queued/running ingest work for "
                                "duplicate content in this corpus."
                            )
                        }
                    },
                )
        if item_docs:
            await self._db["ingestion_batch_items"].insert_many(item_docs, ordered=False)
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id},
            {
                "$set": {
                    "status": "queued",
                    "queued_count": len(item_docs),
                    "batch_total_bytes": total_bytes,
                    "current_phase": "queued",
                    "updated_at": _now(),
                }
            },
        )
        await self._refresh_batch_counts(batch_id)
        self.ensure_running(batch_id)
        return await self.get_batch(batch_id, user_id=user_id) or {"batch_id": batch_id}

    async def _spool_upload(
        self,
        *,
        upload: Any,
        batch_id: str,
        batch_dir: Path,
        corpus_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        mime = getattr(upload, "content_type", None) or ""
        intake = normalize_upload_filename(getattr(upload, "filename", None) or "upload", mime)
        filename = _safe_filename(intake.filename)
        upload_id = str(uuid.uuid4())
        tmp_path = batch_dir / f"{upload_id}.tmp"
        hasher = hashlib.sha256()
        size = 0
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                hasher.update(chunk)
                fh.write(chunk)
        if size <= 0:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise ValueError(f"Uploaded file is empty: {filename}")
        content_hash = hasher.hexdigest()
        final_path = batch_dir / f"{content_hash[:16]}_{upload_id}_{filename}"
        tmp_path.replace(final_path)
        return {
            "upload_id": upload_id,
            "batch_id": batch_id,
            "doc_id": None,
            "filename": filename,
            "corpus_id": corpus_id,
            "user_id": user_id,
            "size_bytes": size,
            "mime": intake.mime or mime,
            "content_hash": content_hash,
            "spool_path": str(final_path.resolve()),
            "status": "queued",
            "intake_normalized": intake.normalized,
            "intake_warning": intake.warning,
            "attempts": 0,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
            "started_at": None,
            "finished_at": None,
        }

    async def get_batch_summary(
        self, batch_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Aggregate counts + dominant error_kind buckets for a batch.

        Cheap: never returns the full items list (which can be 500 rows
        with embedded warnings). Frontend uses this for the end-of-batch
        summary card. The /batches/{id} endpoint is for the live
        per-item view.
        """
        if self._db is None:
            return None
        query: dict[str, Any] = {"batch_id": batch_id}
        if user_id:
            query["user_id"] = user_id
        batch = await self._db["ingestion_batches"].find_one(
            query,
            {
                "_id": 0,
                "batch_id": 1,
                "status": 1,
                "current_phase": 1,
                "total_files": 1,
                "queued_count": 1,
                "processing_count": 1,
                "vector_ready_count": 1,
                "graph_ready_count": 1,
                "graph_partial_count": 1,
                "graph_failed_count": 1,
                "needs_backfill_count": 1,
                "failed_count": 1,
                "cancelled_count": 1,
                "warnings": 1,
                "paused_reason": 1,
                "created_at": 1,
                "started_at": 1,
                "finished_at": 1,
            },
        )
        if not batch:
            return None
        # error_kind histogram from the items collection — cheap aggregate.
        error_pipeline = [
            {"$match": {"batch_id": batch_id, "status": "failed"}},
            {"$group": {
                "_id": {"$ifNull": ["$error_kind", "unknown"]},
                "count": {"$sum": 1},
                "sample_filename": {"$first": "$filename"},
                "sample_error": {"$first": "$error"},
            }},
            {"$sort": {"count": -1}},
            {"$limit": 8},
        ]
        error_buckets = []
        async for row in self._db["ingestion_batch_items"].aggregate(error_pipeline):
            error_buckets.append({
                "error_kind": row["_id"],
                "count": row["count"],
                "sample_filename": row.get("sample_filename"),
                "sample_error": (row.get("sample_error") or "")[:200],
            })
        batch["error_buckets"] = error_buckets
        # Convenience: total successful = vector_ready + graph_ready + graph_partial
        # so the UI doesn't have to add three fields to render "X of Y succeeded".
        batch["successful_count"] = (
            int(batch.get("vector_ready_count") or 0)
            + int(batch.get("graph_ready_count") or 0)
            + int(batch.get("graph_partial_count") or 0)
        )
        return batch

    async def get_batch(self, batch_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        if self._db is None:
            return None
        query = {"batch_id": batch_id}
        if user_id:
            query["user_id"] = user_id
        batch = await self._db["ingestion_batches"].find_one(
            query,
            {
                "_id": 0,
                # These can contain provider wiring and ephemeral request
                # secrets. Workers read the raw batch doc internally; public
                # status responses only need counts, resource profile, and
                # item state.
                "ingestion_config": 0,
                "ingest_overrides": 0,
            },
        )
        if not batch:
            return None
        items = await self._db["ingestion_batch_items"].find(
            {"batch_id": batch_id},
            {"_id": 0, "spool_path": 0},
        ).sort([("status", 1), ("size_bytes", 1)]).to_list(length=1000)
        batch["items"] = items
        batch["resource_profile"] = self.resource_profile()
        batch["queue_metrics"] = await self.queue_metrics()
        return batch

    async def pause(self, batch_id: str, *, user_id: str) -> dict[str, Any]:
        await self._require_batch(batch_id, user_id)
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id, "user_id": user_id},
            {"$set": {"status": "paused", "current_phase": "paused", "updated_at": _now()}},
        )
        return await self.get_batch(batch_id, user_id=user_id) or {"batch_id": batch_id}

    async def resume(self, batch_id: str, *, user_id: str) -> dict[str, Any]:
        await self._require_batch(batch_id, user_id)
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id, "user_id": user_id},
            {"$set": {"status": "queued", "current_phase": "queued", "updated_at": _now()}},
        )
        self.ensure_running(batch_id)
        return await self.get_batch(batch_id, user_id=user_id) or {"batch_id": batch_id}

    async def cancel(self, batch_id: str, *, user_id: str) -> dict[str, Any]:
        await self._require_batch(batch_id, user_id)
        await self._cancel_items(
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "status": {"$in": list(NON_TERMINAL_ITEM_STATUSES | {"failed"})},
            },
            reason="batch_cancelled_by_user",
            cleanup_spool=True,
        )
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id, "user_id": user_id},
            {"$set": {"status": "cancelled", "current_phase": "cancelled", "finished_at": _now(), "updated_at": _now()}},
        )
        await self._refresh_batch_counts(batch_id)
        return await self.get_batch(batch_id, user_id=user_id) or {"batch_id": batch_id}

    async def retry_failed(self, batch_id: str, *, user_id: str) -> dict[str, Any]:
        await self._require_batch(batch_id, user_id)
        await self._db["ingestion_batch_items"].update_many(
            {"batch_id": batch_id, "status": {"$in": ["failed"]}},
            {"$set": {"status": "queued", "error": None, "updated_at": _now()}},
        )
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id, "user_id": user_id},
            {"$set": {"status": "queued", "current_phase": "queued", "updated_at": _now()}},
        )
        await self._refresh_batch_counts(batch_id)
        self.ensure_running(batch_id)
        return await self.get_batch(batch_id, user_id=user_id) or {"batch_id": batch_id}

    async def _require_batch(self, batch_id: str, user_id: str) -> dict[str, Any]:
        if self._db is None:
            raise RuntimeError("BatchIngestionManager is not attached")
        batch = await self._db["ingestion_batches"].find_one({"batch_id": batch_id, "user_id": user_id})
        if not batch:
            raise KeyError(batch_id)
        return batch

    async def _run_batch(self, batch_id: str) -> None:
        if self._db is None or self._ingest is None:
            return
        logger.info("phase=batch_scheduler_start batch=%s", batch_id)
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id},
            {"$set": {"status": "running", "started_at": _now(), "current_phase": "running", "updated_at": _now()}},
        )
        workers: list[asyncio.Task] = []
        try:
            profile = self.resource_profile()
            concurrency = max(1, int(profile.get("max_active_docs") or 1))
            workers = [
                asyncio.create_task(self._run_batch_worker(batch_id, worker_idx))
                for worker_idx in range(concurrency)
            ]
            await asyncio.gather(*workers, return_exceptions=False)
            await self._finish_if_done(batch_id)
        except asyncio.CancelledError:
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            raise
        except Exception as exc:
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            logger.exception("phase=batch_scheduler_failed batch=%s: %s", batch_id, exc)
            await self._db["ingestion_batches"].update_one(
                {"batch_id": batch_id},
                {
                    "$set": {"status": "failed", "current_phase": "failed", "updated_at": _now(), "error": str(exc)[:1000]},
                    "$addToSet": {"warnings": f"Batch scheduler failed: {str(exc)[:300]}"},
                },
            )

    async def _run_batch_worker(self, batch_id: str, worker_idx: int) -> None:
        if self._db is None:
            return
        poll_seconds = float(self.settings.INGEST_BATCH_POLL_SECONDS)
        floor_gb = float(self.settings.INGEST_MIN_FREE_DISK_GB)
        vram_floor_mb = int(self.settings.INGEST_MIN_FREE_VRAM_MB)
        embedder_health_url = str(self.settings.EMBEDDER_HEALTH_URL)
        last_floor_warn_at = 0.0
        last_vram_warn_at = 0.0
        while True:
            batch = await self._db["ingestion_batches"].find_one({"batch_id": batch_id})
            if not batch or batch.get("status") in TERMINAL_BATCH_STATUSES:
                return
            if batch.get("status") == "paused":
                await asyncio.sleep(poll_seconds)
                continue

            # VRAM backpressure: refuse to claim new items when the embedder
            # is under its VRAM floor. Falls open if the probe fails (we'd
            # rather keep ingesting than stall on a broken health check).
            # Closes GOTCHAS §65.1 — without this the embedder, vllm-summary,
            # and vllm-extract on the same GPU can OOM-kill each other.
            if vram_floor_mb > 0:
                free_vram = await _embedder_free_vram_mb(embedder_health_url)
                if free_vram is not None and free_vram < vram_floor_mb:
                    now_t = time.monotonic()
                    if now_t - last_vram_warn_at > 60:
                        logger.warning(
                            "phase=batch_worker_vram_floor batch=%s worker=%d "
                            "free_mb=%d floor_mb=%d — pausing claims",
                            batch_id, worker_idx, free_vram, vram_floor_mb,
                        )
                        last_vram_warn_at = now_t
                    await self._db["ingestion_batches"].update_one(
                        {"batch_id": batch_id},
                        {
                            "$addToSet": {
                                "warnings": (
                                    f"Embedder VRAM floor reached: {free_vram} MB "
                                    f"free < {vram_floor_mb} MB required. New "
                                    "items paused until in-flight work drains."
                                )
                            }
                        },
                    )
                    await asyncio.sleep(max(poll_seconds, 5.0))
                    continue

            # Defense in depth: even when admission gated against the floor at
            # batch creation, a long-running ingest can drive disk down (qdrant
            # WAL, mongo journals, intermediate parses). When we drop under
            # the floor, hold off on claiming new items so the in-flight ones
            # can drain instead of all racing into ENOSPC together.
            free_gb = _spool_free_gb(self.settings.INGEST_SPOOL_DIR)
            if free_gb < floor_gb:
                now_t = time.monotonic()
                if now_t - last_floor_warn_at > 60:
                    logger.warning(
                        "phase=batch_worker_disk_floor batch=%s worker=%d "
                        "free_gb=%.2f floor_gb=%.2f — pausing claims",
                        batch_id, worker_idx, free_gb, floor_gb,
                    )
                    last_floor_warn_at = now_t
                await self._db["ingestion_batches"].update_one(
                    {"batch_id": batch_id},
                    {
                        "$addToSet": {
                            "warnings": (
                                f"Disk floor reached: {free_gb:.2f} GB free < "
                                f"{floor_gb:.2f} GB required. New items paused "
                                "until disk is reclaimed."
                            )
                        }
                    },
                )
                await asyncio.sleep(max(poll_seconds, 5.0))
                continue

            item = await self._claim_next_item(batch_id)
            if item is None:
                await self._finish_if_done(batch_id)
                return

            logger.debug(
                "phase=batch_worker_claim batch=%s worker=%d upload=%s",
                batch_id,
                worker_idx,
                item.get("upload_id"),
            )
            await self._refresh_batch_counts(batch_id)
            await self._process_item(batch, item, already_claimed=True)
            await self._refresh_batch_counts(batch_id)

    async def _claim_next_item(self, batch_id: str) -> dict[str, Any] | None:
        if self._db is None:
            return None
        return await self._db["ingestion_batch_items"].find_one_and_update(
            {"batch_id": batch_id, "status": "queued"},
            {
                "$set": {
                    "status": "parsing",
                    "started_at": _now(),
                    "updated_at": _now(),
                },
                "$inc": {"attempts": 1},
            },
            sort=[("size_bytes", 1), ("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    async def _process_item(
        self,
        batch: dict[str, Any],
        item: dict[str, Any],
        *,
        already_claimed: bool = False,
    ) -> None:
        if self._db is None or self._ingest is None:
            return
        upload_id = str(item["upload_id"])
        self._active_items.add(upload_id)
        started = time.monotonic()
        path = Path(str(item["spool_path"]))
        progress_task: asyncio.Task | None = None
        try:
            if not already_claimed:
                claimed = await self._db["ingestion_batch_items"].update_one(
                    {"upload_id": upload_id, "status": "queued"},
                    {
                        "$set": {
                            "status": "parsing",
                            "started_at": item.get("started_at") or _now(),
                            "updated_at": _now(),
                        },
                        "$inc": {"attempts": 1},
                    },
                )
                if claimed.modified_count != 1:
                    return
                await self._refresh_batch_counts(str(batch["batch_id"]))
            if not path.exists():
                raise FileNotFoundError(f"Spooled file missing: {path}")
            data = await asyncio.to_thread(path.read_bytes)

            async def _cancel_check() -> bool:
                if upload_id in self._cancelled_upload_ids:
                    return True
                row = await self._db["ingestion_batch_items"].find_one(
                    {"upload_id": upload_id},
                    {"_id": 0, "status": 1},
                )
                return str((row or {}).get("status") or "") == "cancelled"

            async def _mark_vectorizing(doc_id: str) -> None:
                nonlocal progress_task
                await self._db["ingestion_batch_items"].update_one(
                    {
                        "upload_id": upload_id,
                        "status": {"$nin": list(TERMINAL_ITEM_STATUSES)},
                    },
                    {
                        "$set": {
                            "doc_id": doc_id,
                            "status": "vectorizing",
                            "current_phase": "vectorizing",
                            "updated_at": _now(),
                        }
                    },
                )
                await self._refresh_batch_counts(str(batch["batch_id"]))
                if progress_task is None or progress_task.done():
                    progress_task = asyncio.create_task(
                        self._track_item_progress(
                            batch_id=str(batch["batch_id"]),
                            upload_id=upload_id,
                            doc_id=doc_id,
                            corpus_id=str(batch["corpus_id"]),
                        )
                    )

            def _on_doc_id(doc_id: str) -> None:
                asyncio.create_task(_mark_vectorizing(doc_id))

            result = await self._ingest(
                data=data,
                filename=str(item.get("filename") or "upload"),
                corpus_id=str(batch["corpus_id"]),
                user_id=str(batch["user_id"]),
                ingestion_config=IngestionConfig(**(batch.get("ingestion_config") or {})),
                model=str(batch.get("model") or ""),
                ingest_overrides=batch.get("ingest_overrides") or None,
                source_mime=str(item.get("mime") or ""),
                cancel_check=_cancel_check,
                on_doc_id=_on_doc_id,
            )
            ws = result.write_state
            status = item_status_from_write_state(ws)
            latest = await self._db["ingestion_batch_items"].find_one(
                {"upload_id": upload_id},
                {"_id": 0, "status": 1},
            )
            if str((latest or {}).get("status") or "") == "cancelled":
                if getattr(ws, "mongo_written", False):
                    await asyncio.to_thread(self._safe_cleanup_spool, path)
                return
            await self._db["ingestion_batch_items"].update_one(
                {"upload_id": upload_id},
                {
                    "$set": {
                        "status": status,
                        "doc_id": result.doc_id,
                        "chunk_count": result.chunk_count,
                        "parent_count": result.parent_count,
                        "source_tier": result.source_tier,
                        "write_state": ws.model_dump(),
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "finished_at": _now(),
                        "updated_at": _now(),
                    }
                },
            )
            if getattr(ws, "mongo_written", False):
                await asyncio.to_thread(self._safe_cleanup_spool, path)
        except IngestCancelled as exc:
            await self._db["ingestion_batch_items"].update_one(
                {"upload_id": upload_id},
                {
                    "$set": {
                        "status": "cancelled",
                        "current_phase": "cancelled",
                        "cancel_reason": str(exc)[:1000],
                        "error": str(exc)[:1000],
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "finished_at": _now(),
                        "updated_at": _now(),
                    }
                },
            )
            # The mid-pipeline cancel handler is the only safe place to release
            # this item's spool file. cancel() at the batch level also cleans
            # spools but only for items it sweeps — an item that was already
            # claimed and running races into THIS branch instead of the batch
            # cancel path. Without this cleanup, every cancelled in-flight
            # item leaks a spool file (50MB+ for big PDFs).
            with suppress(Exception):
                await asyncio.to_thread(self._safe_cleanup_spool, path)
        except Exception as exc:
            logger.exception("phase=batch_item_failed upload=%s batch=%s: %s", upload_id, batch["batch_id"], exc)
            error_kind = _classify_failure(exc)
            await self._db["ingestion_batch_items"].update_one(
                {"upload_id": upload_id},
                {
                    "$set": {
                        "status": "failed",
                        "error": str(exc)[:1000],
                        "error_kind": error_kind,
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "finished_at": _now(),
                        "updated_at": _now(),
                    }
                },
            )
            await self._maybe_trip_circuit_breaker(
                batch_id=str(batch["batch_id"]),
                latest_error_kind=error_kind,
            )
        finally:
            if progress_task is not None:
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task
            self._active_items.discard(upload_id)
            await self._refresh_batch_counts(str(batch["batch_id"]))

    async def _track_item_progress(
        self,
        *,
        batch_id: str,
        upload_id: str,
        doc_id: str,
        corpus_id: str,
    ) -> None:
        poll_seconds = max(1.0, float(self.settings.INGEST_BATCH_POLL_SECONDS))
        while True:
            await asyncio.sleep(poll_seconds)
            await self._sync_item_progress_from_document(
                batch_id=batch_id,
                upload_id=upload_id,
                doc_id=doc_id,
                corpus_id=corpus_id,
            )

    async def _sync_item_progress_from_document(
        self,
        *,
        batch_id: str,
        upload_id: str,
        doc_id: str,
        corpus_id: str,
    ) -> None:
        if self._db is None:
            return
        doc = await self._db["documents"].find_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "_id": 0,
                "write_state": 1,
                "chunk_count": 1,
                "parent_count": 1,
                "decision_trace.child_count": 1,
                "decision_trace.parent_count": 1,
            },
        )
        if not doc:
            return
        item = await self._db["ingestion_batch_items"].find_one(
            {"batch_id": batch_id, "upload_id": upload_id},
            {"_id": 0, "status": 1},
        )
        item_status = str((item or {}).get("status") or "")
        write_state = doc.get("write_state") or {}
        status = _live_running_status_from_write_state(write_state)
        current_phase = status
        if current_phase is None and _write_state_vector_ready(write_state):
            current_phase = "vector_ready"

        update_doc: dict[str, Any] = {
            "write_state": write_state,
            "updated_at": _now(),
        }
        if current_phase:
            update_doc["current_phase"] = current_phase
        if status:
            update_doc["status"] = status
        elif item_status in {"parsing", "chunked"}:
            update_doc["status"] = "vectorizing"
        chunk_count = doc.get("chunk_count")
        if chunk_count is None:
            chunk_count = (doc.get("decision_trace") or {}).get("child_count")
        parent_count = doc.get("parent_count")
        if parent_count is None:
            parent_count = (doc.get("decision_trace") or {}).get("parent_count")
        if chunk_count is not None:
            update_doc["chunk_count"] = int(chunk_count or 0)
        if parent_count is not None:
            update_doc["parent_count"] = int(parent_count or 0)

        await self._db["ingestion_batch_items"].update_one(
            {
                "batch_id": batch_id,
                "upload_id": upload_id,
                "status": {"$nin": list(TERMINAL_ITEM_STATUSES)},
            },
            {"$set": update_doc},
        )
        await self._refresh_batch_counts(batch_id)

    def _safe_cleanup_spool(self, path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.warning("Could not remove spool file %s: %s", path, exc)

    async def _finish_if_done(self, batch_id: str) -> None:
        if self._db is None:
            return
        remaining = await self._db["ingestion_batch_items"].count_documents(
            {"batch_id": batch_id, "status": {"$nin": list(TERMINAL_ITEM_STATUSES)}}
        )
        await self._refresh_batch_counts(batch_id)
        if remaining:
            return
        batch = await self._db["ingestion_batches"].find_one({"batch_id": batch_id})
        if not batch:
            return
        failed = await self._db["ingestion_batch_items"].count_documents({"batch_id": batch_id, "status": "failed"})
        needs_backfill = await self._db["ingestion_batch_items"].count_documents({"batch_id": batch_id, "status": "needs_backfill"})
        graph_partial = await self._db["ingestion_batch_items"].count_documents({"batch_id": batch_id, "status": "graph_partial"})
        graph_failed = await self._db["ingestion_batch_items"].count_documents({"batch_id": batch_id, "status": "graph_failed_token_budget"})
        cancelled = await self._db["ingestion_batch_items"].count_documents({"batch_id": batch_id, "status": "cancelled"})
        total_files = int(batch.get("total_files") or 0)
        terminal = _terminal_batch_status(
            total_files=total_files,
            failed=failed,
            cancelled=cancelled,
            needs_backfill=needs_backfill,
            graph_partial=graph_partial,
            graph_failed=graph_failed,
        )
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id},
            {"$set": {"status": terminal, "current_phase": terminal, "finished_at": _now(), "updated_at": _now()}},
        )
        if self._warm_graph_cache is not None and terminal in {"completed", "completed_with_errors"}:
            try:
                await self._warm_graph_cache(corpus_id=batch["corpus_id"], user_id=batch["user_id"])
            except Exception as exc:
                logger.warning("Batch graph cache warm failed batch=%s: %s", batch_id, exc)

    async def _maybe_trip_circuit_breaker(
        self, *, batch_id: str, latest_error_kind: str
    ) -> None:
        """Pause the batch when the last N items all failed with the same
        error_kind. Prevents a misconfigured corpus / failed lane from
        burning through 500 docs while the operator is afk.

        Operator unpauses by calling POST /api/ingestion/batches/{id}/resume
        after fixing the underlying config. The circuit breaker only
        TRIPS once per batch — once paused, stays paused until manual
        resume — so we don't flap.
        """
        if self._db is None:
            return
        if latest_error_kind in {"unknown"}:
            return
        threshold = int(self.settings.INGEST_CIRCUIT_BREAKER_CONSECUTIVE_FAILS)
        if threshold < 2:
            return
        recent = await (
            self._db["ingestion_batch_items"]
            .find(
                {"batch_id": batch_id, "status": {"$in": ["failed", "graph_ready", "vector_ready", "graph_partial"]}},
                {"_id": 0, "status": 1, "error_kind": 1, "finished_at": 1},
            )
            .sort("finished_at", -1)
            .limit(threshold)
            .to_list(length=threshold)
        )
        if len(recent) < threshold:
            return
        # Only failures count — a vector_ready item interleaved with failures
        # resets the streak. The check below requires every item in the last
        # `threshold` window to be a failure of the same kind.
        if not all(
            (r.get("status") == "failed" and r.get("error_kind") == latest_error_kind)
            for r in recent
        ):
            return
        # Already paused? don't double-trip.
        batch = await self._db["ingestion_batches"].find_one(
            {"batch_id": batch_id, "status": {"$ne": "paused"}}
        )
        if not batch:
            return
        logger.error(
            "phase=batch_circuit_breaker_tripped batch=%s error_kind=%s "
            "threshold=%d — pausing batch",
            batch_id, latest_error_kind, threshold,
        )
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id},
            {
                "$set": {
                    "status": "paused",
                    "paused_at": _now(),
                    "paused_reason": f"circuit_breaker:{latest_error_kind}",
                    "current_phase": "paused_by_circuit_breaker",
                    "updated_at": _now(),
                },
                "$addToSet": {
                    "warnings": (
                        f"Circuit breaker: {threshold} consecutive items failed "
                        f"with error_kind={latest_error_kind}. Batch paused. "
                        "Fix the underlying configuration (model lane, disk, "
                        "VRAM, BSON limit, etc.) and POST /resume to continue."
                    )
                },
            },
        )

    async def _refresh_batch_counts(self, batch_id: str) -> None:
        if self._db is None:
            return
        counts = Counter()
        items = await self._db["ingestion_batch_items"].find(
            {"batch_id": batch_id},
            {
                "_id": 0,
                "status": 1,
                "doc_id": 1,
                "corpus_id": 1,
                "write_state": 1,
            },
        ).to_list(length=None)
        for item in items:
            counts[str(item.get("status") or "unknown")] += 1

        doc_ids = sorted({str(item.get("doc_id")) for item in items if item.get("doc_id")})
        corpus_id = next((str(item.get("corpus_id")) for item in items if item.get("corpus_id")), None)
        docs_by_id: dict[str, dict[str, Any]] = {}
        if doc_ids and corpus_id:
            cursor = self._db["documents"].find(
                {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}},
                {"_id": 0, "doc_id": 1, "write_state": 1},
            )
            async for doc in cursor:
                docs_by_id[str(doc.get("doc_id"))] = doc

        vector_ready = 0
        graph_ready = 0
        vector_statuses = {
            "vector_ready",
            "graph_extracting",
            "graph_ready",
            "graph_partial",
            "needs_backfill",
            "graph_failed_token_budget",
        }
        for item in items:
            status = str(item.get("status") or "")
            item_ws = item.get("write_state") or {}
            doc_ws = (docs_by_id.get(str(item.get("doc_id"))) or {}).get("write_state") or {}
            if (
                status in vector_statuses
                or _write_state_vector_ready(item_ws)
                or _write_state_vector_ready(doc_ws)
            ):
                vector_ready += 1
            if status == "graph_ready" or _write_state_graph_ready(item_ws) or _write_state_graph_ready(doc_ws):
                graph_ready += 1

        fields = _batch_count_fields(
            counts,
            vector_ready=vector_ready,
            graph_ready=graph_ready,
        )
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id},
            {
                "$set": {
                    **fields,
                    "updated_at": _now(),
                }
            },
        )


batch_ingestion_manager = BatchIngestionManager()
