"""Durable ingestion batch helpers.

The single-file upload route is intentionally lightweight, but large folder
ingests need server-owned state before any expensive parsing/model work starts.
This module stores a manifest in Mongo and runs local-folder items through the
existing idempotent ingestion worker under a lease, so a process restart can
reconcile and resume unfinished files.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from config import get_settings
from models.schemas import IngestionConfig
from services.ingestion import admission

logger = logging.getLogger(__name__)

BATCHES = "ingest_batches"
ITEMS = "ingest_batch_items"

ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_DONE = "done"
ITEM_FAILED = "failed"
ITEM_FAILED_RECOVERABLE = "failed_recoverable"
ITEM_SKIPPED = "skipped"

BATCH_QUEUED = "queued"
BATCH_RUNNING = "running"
BATCH_DONE = "done"
BATCH_PARTIAL = "partial"
BATCH_FAILED = "failed"

DEFAULT_EXTENSIONS = {
    ".pdf",
    ".epub",
    ".doc",
    ".docx",
    ".rtf",
    ".odt",
    ".txt",
    ".text",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".xhtml",
}

_RUNNING_BATCHES: dict[str, asyncio.Task] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _normalize_extensions(extensions: list[str] | None) -> set[str]:
    raw = extensions if extensions is not None else sorted(DEFAULT_EXTENSIONS)
    normalized: set[str] = set()
    for ext in raw:
        value = str(ext or "").strip().lower()
        if not value:
            continue
        normalized.add(value if value.startswith(".") else f".{value}")
    return normalized


def _sum_file_sizes(files: list[Path]) -> int:
    return sum(path.stat().st_size for path in files)


def _directory_size_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _ensure_storage_quota(
    *,
    storage_root: Path,
    incoming_bytes: int,
    max_total_bytes: int,
) -> None:
    used = _directory_size_bytes(storage_root)
    if used + incoming_bytes > max_total_bytes:
        raise ValueError(
            "Durable ingest file storage quota exceeded: "
            f"{used + incoming_bytes} bytes requested, "
            f"{max_total_bytes} bytes available. "
            "Delete old stored batches or raise INGEST_FILE_STORAGE_MAX_BYTES."
        )


def _storage_path_for_item(storage_root: Path, batch_id: str, item_id: str, filename: str) -> Path:
    suffix = Path(filename).suffix
    return storage_root / batch_id / f"{item_id}{suffix}"


def discover_local_files(
    root_path: str,
    *,
    recursive: bool = True,
    extensions: list[str] | None = None,
    max_files: int | None = None,
) -> tuple[Path, list[Path]]:
    """Return stable, sorted files for a local backend-visible directory."""
    root = Path(root_path).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"Folder does not exist: {root_path}")
    if not root.is_dir():
        raise ValueError(f"Path is not a folder: {root_path}")

    allowed_exts = _normalize_extensions(extensions)
    candidates = root.rglob("*") if recursive else root.glob("*")
    files: list[Path] = []
    for path in sorted(candidates, key=lambda p: str(p).lower()):
        if not path.is_file():
            continue
        if allowed_exts and path.suffix.lower() not in allowed_exts:
            continue
        files.append(path)
        if max_files is not None and len(files) >= max_files:
            break
    return root, files


def _file_item_doc(
    *,
    batch_id: str,
    corpus_id: str,
    user_id: str,
    root: Path,
    path: Path,
    ordinal: int,
    item_id: str | None = None,
    stored_path: Path | None = None,
) -> dict[str, Any]:
    stat = path.stat()
    try:
        rel_path = str(path.relative_to(root))
    except ValueError:
        rel_path = path.name
    item_id = item_id or str(uuid.uuid4())
    return {
        "item_id": item_id,
        "batch_id": batch_id,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "source": "local_folder",
        "source_path": str(path),
        "stored_path": str(stored_path) if stored_path is not None else None,
        "relative_path": rel_path,
        "filename": path.name,
        "ordinal": ordinal,
        "size_bytes": int(stat.st_size),
        "stored_bytes": int(stat.st_size) if stored_path is not None else 0,
        "mtime": datetime.utcfromtimestamp(stat.st_mtime),
        "mime_type": mimetypes.guess_type(path.name)[0],
        "status": ITEM_QUEUED,
        "phase": "queued",
        "failure_stage": None,
        "attempts": 0,
        "doc_id": None,
        "error": None,
        "lease_owner": None,
        "lease_until": None,
        "last_heartbeat_at": None,
        "phase_started_at": None,
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": None,
        "completed_at": None,
    }


async def create_local_batch(
    *,
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    user_id: str,
    root_path: str,
    recursive: bool = True,
    extensions: list[str] | None = None,
    max_files: int | None = None,
    store_files: bool = False,
    max_total_bytes: int | None = None,
    use_neo4j: bool | None = None,
    chunk_summarization: bool | None = None,
    model: str = "",
    concurrency: int | None = None,
) -> dict[str, Any]:
    root, files = discover_local_files(
        root_path,
        recursive=recursive,
        extensions=extensions,
        max_files=max_files,
    )
    if not files:
        raise ValueError(f"No ingestable files found under {root}")

    now = _now()
    batch_id = str(uuid.uuid4())
    settings = get_settings()
    total_source_bytes = _sum_file_sizes(files)
    storage_root = Path(settings.INGEST_FILE_STORAGE_DIR).expanduser().resolve()
    storage_limit = int(max_total_bytes or settings.INGEST_FILE_STORAGE_MAX_BYTES)
    stored_paths: dict[Path, Path] = {}
    item_ids: dict[Path, str] = {}
    if store_files:
        _ensure_storage_quota(
            storage_root=storage_root,
            incoming_bytes=total_source_bytes,
            max_total_bytes=storage_limit,
        )
        batch_storage_dir = storage_root / batch_id
        batch_storage_dir.mkdir(parents=True, exist_ok=False)
        try:
            for path in files:
                item_id = str(uuid.uuid4())
                item_ids[path] = item_id
                stored_path = _storage_path_for_item(
                    storage_root,
                    batch_id,
                    item_id,
                    path.name,
                )
                stored_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, stored_path)
                stored_paths[path] = stored_path
        except Exception:
            shutil.rmtree(batch_storage_dir, ignore_errors=True)
            raise
    worker_count = max(
        1,
        min(
            int(concurrency or settings.INGEST_BATCH_WORKERS),
            int(settings.INGEST_MAX_ACTIVE_JOBS),
        ),
    )
    batch_doc = {
        "batch_id": batch_id,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "source": "local_folder",
        "root_path": str(root),
        "recursive": recursive,
        "extensions": sorted(_normalize_extensions(extensions)),
        "store_files": store_files,
        "total_source_bytes": total_source_bytes,
        "stored_bytes": total_source_bytes if store_files else 0,
        "storage_limit_bytes": storage_limit if store_files else None,
        "status": BATCH_QUEUED,
        "total": len(files),
        "counts": {
            ITEM_QUEUED: len(files),
            ITEM_RUNNING: 0,
            ITEM_DONE: 0,
            ITEM_FAILED: 0,
            ITEM_FAILED_RECOVERABLE: 0,
            ITEM_SKIPPED: 0,
        },
        "options": {
            "use_neo4j": use_neo4j,
            "chunk_summarization": chunk_summarization,
            "model": model or "",
            "concurrency": worker_count,
        },
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
    }
    await db[BATCHES].insert_one(batch_doc)
    await db[ITEMS].insert_many(
        [
            _file_item_doc(
                batch_id=batch_id,
                corpus_id=corpus_id,
                user_id=user_id,
                root=root,
                path=path,
                ordinal=idx,
                item_id=item_ids.get(path),
                stored_path=stored_paths.get(path),
            )
            for idx, path in enumerate(files)
        ],
        ordered=False,
    )
    return await refresh_batch_counts(db, batch_id, user_id=user_id)


async def get_batch(
    db: AsyncIOMotorDatabase,
    batch_id: str,
    *,
    user_id: str,
    include_items: bool = True,
    item_limit: int = 500,
) -> dict[str, Any] | None:
    batch = await db[BATCHES].find_one(
        {"batch_id": batch_id, "user_id": user_id},
        {"_id": 0},
    )
    if not batch:
        return None
    if include_items:
        batch["items"] = await db[ITEMS].find(
            {"batch_id": batch_id, "user_id": user_id},
            {"_id": 0},
        ).sort("ordinal", 1).limit(item_limit).to_list(length=item_limit)
    return batch


async def list_batches(
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    *,
    user_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent durable ingest batches for a corpus."""
    limit = max(1, min(int(limit), 100))
    cursor = db[BATCHES].find(
        {"corpus_id": corpus_id, "user_id": user_id},
        {"_id": 0},
    ).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def refresh_batch_counts(
    db: AsyncIOMotorDatabase,
    batch_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    filter_q: dict[str, Any] = {"batch_id": batch_id}
    if user_id is not None:
        filter_q["user_id"] = user_id
    batch = await db[BATCHES].find_one(filter_q)
    if not batch:
        raise ValueError("Batch not found")

    counts = {
        status: await db[ITEMS].count_documents({"batch_id": batch_id, "status": status})
        for status in (
            ITEM_QUEUED,
            ITEM_RUNNING,
            ITEM_DONE,
            ITEM_FAILED,
            ITEM_FAILED_RECOVERABLE,
            ITEM_SKIPPED,
        )
    }
    total = sum(counts.values())
    unfinished = counts[ITEM_QUEUED] + counts[ITEM_RUNNING] + counts[ITEM_FAILED_RECOVERABLE]
    if total == 0:
        status = BATCH_FAILED
    elif unfinished > 0:
        status = BATCH_RUNNING if counts[ITEM_RUNNING] else BATCH_QUEUED
    elif counts[ITEM_FAILED] > 0:
        status = BATCH_PARTIAL if counts[ITEM_DONE] or counts[ITEM_SKIPPED] else BATCH_FAILED
    else:
        status = BATCH_DONE

    update: dict[str, Any] = {
        "counts": counts,
        "total": total,
        "status": status,
        "updated_at": _now(),
    }
    if status in {BATCH_DONE, BATCH_PARTIAL, BATCH_FAILED}:
        update["completed_at"] = _now()
    else:
        update["completed_at"] = None
    await db[BATCHES].update_one({"batch_id": batch_id}, {"$set": update})
    refreshed = await db[BATCHES].find_one({"batch_id": batch_id}, {"_id": 0})
    return refreshed or {**batch, **update}


async def reconcile_stale_items(
    db: AsyncIOMotorDatabase,
    *,
    batch_id: str | None = None,
    user_id: str | None = None,
    stale_after_minutes: int | None = None,
) -> dict[str, Any]:
    cutoff = _now() - timedelta(
        minutes=int(stale_after_minutes or get_settings().INGEST_STALE_JOB_MINUTES)
    )
    filter_q: dict[str, Any] = {
        "status": ITEM_RUNNING,
        "$or": [
            {"lease_until": {"$lt": _now()}},
            {"updated_at": {"$lt": cutoff}},
        ],
    }
    if batch_id is not None:
        filter_q["batch_id"] = batch_id
    if user_id is not None:
        filter_q["user_id"] = user_id
    affected = await db[ITEMS].find(
        filter_q,
        {"batch_id": 1, "_id": 0},
    ).to_list(length=None)
    res = await db[ITEMS].update_many(
        filter_q,
        {
            "$set": {
                "status": ITEM_FAILED_RECOVERABLE,
                "phase": "stale",
                "failure_stage": "lease_expired",
                "error": "Worker lease expired before completion; item can be resumed.",
                "lease_owner": None,
                "lease_until": None,
                "last_heartbeat_at": _now(),
                "updated_at": _now(),
            }
        },
    )
    for affected_batch_id in sorted(
        {str(row.get("batch_id")) for row in affected if row.get("batch_id")}
    ):
        await refresh_batch_counts(db, affected_batch_id, user_id=user_id)
    return {"reconciled_items": int(res.modified_count)}


async def _lease_next_item(
    db: AsyncIOMotorDatabase,
    *,
    batch_id: str,
    owner: str,
    lease_seconds: int,
) -> dict[str, Any] | None:
    now = _now()
    return await db[ITEMS].find_one_and_update(
        {
            "batch_id": batch_id,
            "source": "local_folder",
            "status": {"$in": [ITEM_QUEUED, ITEM_FAILED_RECOVERABLE]},
        },
        {
            "$set": {
                "status": ITEM_RUNNING,
                "phase": "claimed",
                "failure_stage": None,
                "lease_owner": owner,
                "lease_until": now + timedelta(seconds=lease_seconds),
                "started_at": now,
                "phase_started_at": now,
                "last_heartbeat_at": now,
                "updated_at": now,
                "error": None,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("ordinal", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def _wait_for_ingest_slot() -> None:
    while not await admission.try_acquire_ingest_slot():
        await asyncio.sleep(1.0)


async def _build_item_config(
    *,
    ingestion_service: Any,
    corpus_id: str,
    options: dict[str, Any],
) -> IngestionConfig:
    corpus = await ingestion_service._get_corpus_raw(corpus_id)
    if not corpus:
        raise ValueError(f"Corpus not found: {corpus_id}")
    cfg_dict = dict(corpus.get("default_ingestion_config") or {})
    if options.get("use_neo4j") is not None:
        cfg_dict["use_neo4j"] = bool(options["use_neo4j"])
    if options.get("chunk_summarization") is not None:
        cfg_dict["chunk_summarization"] = bool(options["chunk_summarization"])
    return IngestionConfig(**cfg_dict)


async def _set_item_phase(
    db: AsyncIOMotorDatabase,
    item_id: str,
    phase: str,
    *,
    status: str | None = None,
    doc_id: str | None = None,
    error: str | None = None,
    failure_stage: str | None = None,
    completed: bool = False,
) -> None:
    now = _now()
    set_doc: dict[str, Any] = {
        "phase": phase,
        "phase_started_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
    }
    if status is not None:
        set_doc["status"] = status
    if doc_id is not None:
        set_doc["doc_id"] = doc_id
    if error is not None:
        set_doc["error"] = error[:1000]
    if failure_stage is not None:
        set_doc["failure_stage"] = failure_stage
    if completed:
        set_doc["completed_at"] = now
        set_doc["lease_owner"] = None
        set_doc["lease_until"] = None
    await db[ITEMS].update_one({"item_id": item_id}, {"$set": set_doc})


async def _process_local_item(
    *,
    db: AsyncIOMotorDatabase,
    ingestion_service: Any,
    batch: dict[str, Any],
    item: dict[str, Any],
) -> None:
    item_id = item["item_id"]
    stored_path_raw = item.get("stored_path")
    source_path_raw = item.get("source_path")
    path = Path(str(stored_path_raw or source_path_raw or ""))
    if not path.exists() or not path.is_file():
        label = "Stored source" if stored_path_raw else "Source file"
        await _set_item_phase(
            db,
            item_id,
            "failed",
            status=ITEM_FAILED,
            error=f"{label} is missing: {path}",
            failure_stage="source_missing",
            completed=True,
        )
        return

    slot_acquired = False
    try:
        await _wait_for_ingest_slot()
        slot_acquired = True
        await _set_item_phase(db, item_id, "reading", status=ITEM_RUNNING)
        data = path.read_bytes()
        await _set_item_phase(db, item_id, "starting_worker", status=ITEM_RUNNING)
        config = await _build_item_config(
            ingestion_service=ingestion_service,
            corpus_id=batch["corpus_id"],
            options=batch.get("options") or {},
        )

        async def _on_doc_id(doc_id: str) -> None:
            await _set_item_phase(
                db,
                item_id,
                "chunking",
                status=ITEM_RUNNING,
                doc_id=doc_id,
            )

        async def _on_phase(phase: str, details: dict[str, Any]) -> None:
            phase_doc_id = details.get("doc_id")
            phase_error = details.get("error")
            failure_stage = phase if phase.endswith("failed") else None
            await _set_item_phase(
                db,
                item_id,
                phase,
                status=ITEM_RUNNING,
                doc_id=str(phase_doc_id) if phase_doc_id else None,
                error=str(phase_error) if phase_error else None,
                failure_stage=failure_stage,
            )

        result = await ingestion_service.ingest(
            data=data,
            filename=str(item.get("filename") or path.name),
            corpus_id=batch["corpus_id"],
            user_id=batch["user_id"],
            ingestion_config=config,
            model=str((batch.get("options") or {}).get("model") or ""),
            ingest_overrides=None,
            on_doc_id=_on_doc_id,
            on_phase=_on_phase,
        )
        status = ITEM_DONE if result.status == "done" else ITEM_FAILED
        await _set_item_phase(
            db,
            item_id,
            "complete" if status == ITEM_DONE else "failed",
            status=status,
            doc_id=result.doc_id,
            error=result.error,
            failure_stage=None if status == ITEM_DONE else "worker_result_failed",
            completed=True,
        )
    except Exception as exc:
        logger.exception("Batch item ingest failed item=%s path=%s", item_id, path)
        await _set_item_phase(
            db,
            item_id,
            "failed",
            status=ITEM_FAILED,
            error=str(exc),
            failure_stage="worker_exception",
            completed=True,
        )
    finally:
        if slot_acquired:
            await admission.release_ingest_slot()


async def run_local_batch(
    *,
    db: AsyncIOMotorDatabase,
    ingestion_service: Any,
    batch_id: str,
    user_id: str,
) -> dict[str, Any]:
    batch = await db[BATCHES].find_one({"batch_id": batch_id, "user_id": user_id})
    if not batch:
        raise ValueError("Batch not found")
    if batch.get("source") != "local_folder":
        raise ValueError("Only local_folder batches can be run by the backend")

    await reconcile_stale_items(db, batch_id=batch_id, user_id=user_id)
    await db[BATCHES].update_one(
        {"batch_id": batch_id},
        {"$set": {
            "status": BATCH_RUNNING,
            "started_at": batch.get("started_at") or _now(),
            "updated_at": _now(),
        }},
    )
    batch = await db[BATCHES].find_one({"batch_id": batch_id, "user_id": user_id})
    if not batch:
        raise ValueError("Batch not found")

    owner_prefix = f"{os.getpid()}:{batch_id[:8]}"
    lease_seconds = max(60, int(get_settings().INGEST_STALE_JOB_MINUTES * 60))
    concurrency = max(1, int((batch.get("options") or {}).get("concurrency") or 1))

    async def _worker(worker_idx: int) -> None:
        owner = f"{owner_prefix}:{worker_idx}"
        while True:
            item = await _lease_next_item(
                db,
                batch_id=batch_id,
                owner=owner,
                lease_seconds=lease_seconds,
            )
            if not item:
                return
            await _process_local_item(
                db=db,
                ingestion_service=ingestion_service,
                batch=batch,
                item=item,
            )
            await refresh_batch_counts(db, batch_id, user_id=user_id)

    await asyncio.gather(*[_worker(idx) for idx in range(concurrency)])
    return await refresh_batch_counts(db, batch_id, user_id=user_id)


def start_local_batch_runner(
    *,
    db: AsyncIOMotorDatabase,
    ingestion_service: Any,
    batch_id: str,
    user_id: str,
) -> bool:
    existing = _RUNNING_BATCHES.get(batch_id)
    if existing and not existing.done():
        return False

    async def _run() -> None:
        try:
            await run_local_batch(
                db=db,
                ingestion_service=ingestion_service,
                batch_id=batch_id,
                user_id=user_id,
            )
        except Exception:
            logger.exception("Local ingest batch failed batch=%s", batch_id)
            await db[BATCHES].update_one(
                {"batch_id": batch_id},
                {"$set": {
                    "status": BATCH_FAILED,
                    "updated_at": _now(),
                    "completed_at": _now(),
                }},
            )
        finally:
            _RUNNING_BATCHES.pop(batch_id, None)

    task = asyncio.create_task(_run())
    _RUNNING_BATCHES[batch_id] = task
    return True
