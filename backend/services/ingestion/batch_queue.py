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
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

TERMINAL_BATCH_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_ITEM_STATUSES = {
    "graph_ready",
    "graph_partial",
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
RETRYABLE_ITEM_STATUSES = {"queued", "failed", "needs_backfill"}


IngestCallable = Callable[..., Awaitable[IngestJobResponse]]
WarmCallable = Callable[..., Awaitable[dict]]


def _now() -> datetime:
    return datetime.utcnow()


def _safe_filename(filename: str) -> str:
    name = Path(filename or "upload").name
    name = re.sub(r"[^a-zA-Z0-9._ -]+", "_", name).strip(" .")
    return name[:180] or "upload"


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

    if available_ram and available_ram < 4:
        max_active_docs = 1
    elif available_ram and available_ram < 12:
        max_active_docs = 2
    else:
        max_active_docs = min(4, max(1, cpu_count // 4))
    if gpu_devices:
        max_active_docs = min(max_active_docs, max(1, len(gpu_devices) + 1))
    recommended_parse = min(max_active_docs, 2)
    recommended_vector = max(1, min(max_active_docs, 2))
    recommended_graph = max(1, min(len(gpu_devices) or 1, max_active_docs))
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
        "max_active_docs": max_active_docs,
        "max_spooled_bytes": int(settings.INGEST_MAX_SPOOLED_BYTES),
    }


def item_status_from_write_state(write_state: Any) -> str:
    graph_status = getattr(write_state, "graph_status", None) or ""
    if graph_status == "graph_ready":
        return "graph_ready"
    if graph_status == "graph_partial":
        return "graph_partial"
    if graph_status in {"needs_backfill", "graph_retry_scheduled"}:
        return "needs_backfill"
    if getattr(write_state, "vector_ready", False) or getattr(write_state, "qdrant_written", False):
        return "vector_ready"
    return "failed"


class BatchIngestionManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._db: AsyncIOMotorDatabase | None = None
        self._ingest: IngestCallable | None = None
        self._warm_graph_cache: WarmCallable | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_items: set[str] = set()
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
        await self._db["ingestion_batch_items"].update_many(
            {"status": {"$in": list(RUNNING_ITEM_STATUSES)}},
            {
                "$set": {
                    "status": "queued",
                    "updated_at": _now(),
                    "resume_reason": "backend_restart",
                }
            },
        )
        await self._db["ingestion_batches"].update_many(
            {"status": {"$in": ["running", "queued"]}},
            {"$set": {"status": "queued", "updated_at": _now()}},
        )
        cursor = self._db["ingestion_batches"].find(
            {"status": {"$in": ["queued", "running"]}},
            {"batch_id": 1, "_id": 0},
        )
        async for row in cursor:
            self.ensure_running(str(row.get("batch_id")))

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

    async def create_batch(
        self,
        *,
        corpus_id: str,
        user_id: str,
        uploads: list[Any],
        ingestion_config: IngestionConfig,
        model: str = "",
        ingest_overrides: dict | None = None,
    ) -> dict[str, Any]:
        if self._db is None:
            raise RuntimeError("BatchIngestionManager is not attached")
        if not uploads:
            raise ValueError("No files supplied")
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
            "warnings": [],
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
        filename = _safe_filename(getattr(upload, "filename", None) or "upload")
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
            "mime": getattr(upload, "content_type", None) or "",
            "content_hash": content_hash,
            "spool_path": str(final_path.resolve()),
            "status": "queued",
            "attempts": 0,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
            "started_at": None,
            "finished_at": None,
        }

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
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id, "user_id": user_id},
            {"$set": {"status": "cancelled", "current_phase": "cancelled", "finished_at": _now(), "updated_at": _now()}},
        )
        await self._db["ingestion_batch_items"].update_many(
            {"batch_id": batch_id, "status": {"$in": ["queued", "failed"]}},
            {"$set": {"status": "cancelled", "updated_at": _now()}},
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
        try:
            while True:
                batch = await self._db["ingestion_batches"].find_one({"batch_id": batch_id})
                if not batch or batch.get("status") in TERMINAL_BATCH_STATUSES:
                    return
                if batch.get("status") == "paused":
                    await asyncio.sleep(float(self.settings.INGEST_BATCH_POLL_SECONDS))
                    continue
                profile = self.resource_profile()
                concurrency = max(1, int(profile.get("max_active_docs") or 1))
                queued = await self._db["ingestion_batch_items"].find(
                    {"batch_id": batch_id, "status": "queued"}
                ).sort([("size_bytes", 1), ("created_at", 1)]).to_list(length=concurrency)
                if not queued:
                    await self._finish_if_done(batch_id)
                    return
                sem = asyncio.Semaphore(concurrency)

                async def _run_item(item: dict[str, Any]) -> None:
                    async with sem:
                        await self._process_item(batch, item)

                await asyncio.gather(*[_run_item(item) for item in queued], return_exceptions=False)
                await self._refresh_batch_counts(batch_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("phase=batch_scheduler_failed batch=%s: %s", batch_id, exc)
            await self._db["ingestion_batches"].update_one(
                {"batch_id": batch_id},
                {
                    "$set": {"status": "failed", "current_phase": "failed", "updated_at": _now(), "error": str(exc)[:1000]},
                    "$addToSet": {"warnings": f"Batch scheduler failed: {str(exc)[:300]}"},
                },
            )

    async def _process_item(self, batch: dict[str, Any], item: dict[str, Any]) -> None:
        if self._db is None or self._ingest is None:
            return
        upload_id = str(item["upload_id"])
        self._active_items.add(upload_id)
        started = time.monotonic()
        path = Path(str(item["spool_path"]))
        try:
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
            if not path.exists():
                raise FileNotFoundError(f"Spooled file missing: {path}")
            data = await asyncio.to_thread(path.read_bytes)

            def _on_doc_id(doc_id: str) -> None:
                asyncio.create_task(
                    self._db["ingestion_batch_items"].update_one(
                        {"upload_id": upload_id},
                        {"$set": {"doc_id": doc_id, "status": "vectorizing", "updated_at": _now()}},
                    )
                )

            result = await self._ingest(
                data=data,
                filename=str(item.get("filename") or "upload"),
                corpus_id=str(batch["corpus_id"]),
                user_id=str(batch["user_id"]),
                ingestion_config=IngestionConfig(**(batch.get("ingestion_config") or {})),
                model=str(batch.get("model") or ""),
                ingest_overrides=batch.get("ingest_overrides") or None,
                on_doc_id=_on_doc_id,
            )
            ws = result.write_state
            status = item_status_from_write_state(ws)
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
        except Exception as exc:
            logger.exception("phase=batch_item_failed upload=%s batch=%s: %s", upload_id, batch["batch_id"], exc)
            await self._db["ingestion_batch_items"].update_one(
                {"upload_id": upload_id},
                {
                    "$set": {
                        "status": "failed",
                        "error": str(exc)[:1000],
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "finished_at": _now(),
                        "updated_at": _now(),
                    }
                },
            )
        finally:
            self._active_items.discard(upload_id)

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
        terminal = "failed" if failed and failed == int(batch.get("total_files") or 0) else "completed"
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id},
            {"$set": {"status": terminal, "current_phase": terminal, "finished_at": _now(), "updated_at": _now()}},
        )
        if self._warm_graph_cache is not None and terminal == "completed":
            try:
                await self._warm_graph_cache(corpus_id=batch["corpus_id"], user_id=batch["user_id"])
            except Exception as exc:
                logger.warning("Batch graph cache warm failed batch=%s: %s", batch_id, exc)

    async def _refresh_batch_counts(self, batch_id: str) -> None:
        if self._db is None:
            return
        counts = Counter()
        async for row in self._db["ingestion_batch_items"].aggregate(
            [
                {"$match": {"batch_id": batch_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        ):
            counts[str(row["_id"])] = int(row["count"])
        total = sum(counts.values())
        processing = sum(counts.get(status, 0) for status in RUNNING_ITEM_STATUSES)
        await self._db["ingestion_batches"].update_one(
            {"batch_id": batch_id},
            {
                "$set": {
                    "total_files": total,
                    "queued_count": counts.get("queued", 0),
                    "processing_count": processing,
                    "vector_ready_count": counts.get("vector_ready", 0),
                    "graph_ready_count": counts.get("graph_ready", 0),
                    "graph_partial_count": counts.get("graph_partial", 0),
                    "failed_count": counts.get("failed", 0),
                    "cancelled_count": counts.get("cancelled", 0),
                    "needs_backfill_count": counts.get("needs_backfill", 0),
                    "updated_at": _now(),
                }
            },
        )


batch_ingestion_manager = BatchIngestionManager()
