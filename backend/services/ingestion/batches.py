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
_UTC_EPOCH = datetime(1970, 1, 1)

BATCHES = "ingest_batches"
ITEMS = "ingest_batch_items"
SOURCE_LOCAL_FOLDER = "local_folder"
SOURCE_BROWSER_UPLOAD = "browser_upload"
RUNNABLE_SOURCES = [SOURCE_LOCAL_FOLDER, SOURCE_BROWSER_UPLOAD]

ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_DONE = "done"
ITEM_FAILED = "failed"
ITEM_FAILED_RECOVERABLE = "failed_recoverable"
ITEM_SKIPPED = "skipped"
ITEM_STAGED = "staged"  # §13-S: durable through the batch's target_stage

# ── §13-S stage ladder — monotonic per-item progress, distinct from `phase`
# (current activity). Rank persisted so lease queries can compare cheaply.
STAGE_LADDER = [
    "registered", "parsed", "chunked", "extracted",
    "indexed", "summarized", "promoted", "queryable",
]
STAGE_RANK = {s: i for i, s in enumerate(STAGE_LADDER)}
# Worker phase ENTRY events certify the PREVIOUS stage completed.
_PHASE_TO_STAGE = {
    "reading": "registered",
    "chunking": "parsed",
    "summaries": "chunked",
    "ghosts": "chunked",
    "mongo": "extracted",
    "staged_extracted": "extracted",
    "embedding": "extracted",
    "qdrant": "extracted",
    "staged_indexed": "indexed",
    "neo4j": "indexed",
    "verifying": "indexed",
    "awaiting_summary": "indexed",
    "complete": "queryable",
}

# ── §13-S named profiles (deterministic one-knob presets) ────────────────────
# mac_safe: staged passes, tiny concurrency, Mac sidecar extraction, memory
# released per pass — small files must be feasible on the M1 Studio ALONE.
# rtx_assisted: the elastic-car topology; single full pass.
INGEST_PROFILES: dict[str, dict] = {
    "mac_safe": {
        "concurrency": 2,
        "pass_plan": ["extracted", "indexed", None],  # None = run to completion
        "extraction_endpoint_urls": [
            os.environ.get(
                "MAC_SIDECAR_URL", "http://host.docker.internal:8084"
            ).rstrip("/")
        ],
    },
    "rtx_assisted": {
        "concurrency": None,  # honor batch/env
        "pass_plan": [None],
        "extraction_endpoint_urls": None,  # settings/global fleet
    },
}


def _normalize_profile(profile: str | None) -> str | None:
    value = str(profile or "").strip().lower()
    if not value:
        return None
    if value not in INGEST_PROFILES:
        raise ValueError(
            "Unknown ingest profile "
            f"{profile!r}; expected one of {', '.join(sorted(INGEST_PROFILES))}"
        )
    return value


def _profile_endpoint_urls(batch: dict[str, Any]) -> list[str] | None:
    prof = INGEST_PROFILES.get(
        str((batch.get("options") or {}).get("profile") or "").strip().lower()
    )
    return (prof or {}).get("extraction_endpoint_urls")


async def _advance_item_stage(
    db: AsyncIOMotorDatabase, item_id: str, stage: str
) -> None:
    rank = STAGE_RANK.get(stage)
    if rank is None:
        return
    await db[ITEMS].update_one(
        {
            "item_id": item_id,
            "$or": [
                {"stage_rank": {"$exists": False}},
                {"stage_rank": {"$lt": rank}},
            ],
        },
        {"$set": {"stage": stage, "stage_rank": rank}},
    )

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
    # Aligned with docling_adapter capability (2026-07-03) — the adapter has
    # dedicated lanes for all of these; rejecting them at upload was the only
    # thing stopping them (POLYMATH_ARCHITECTURE §3.S2 "all reasonable files").
    ".vtt", ".srt",                                  # subtitle transcripts (router 4)
    ".csv", ".tsv", ".xlsx", ".xlsm", ".log",        # tabular + logs
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",  # code lane (AST)
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".lua", ".luau", ".sh", ".sql", ".yaml", ".yml", ".json", ".toml",
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


def _mtime_ms(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return int((value - _UTC_EPOCH).total_seconds() * 1000)
        return int(value.timestamp() * 1000)
    return int(float(value) * 1000)


def _file_identity(*, root: Path, path: Path, size_bytes: int, mtime: Any) -> tuple[str, int, int]:
    try:
        rel_path = str(path.relative_to(root))
    except ValueError:
        rel_path = path.name
    return (rel_path, int(size_bytes), _mtime_ms(mtime))


def _item_identity(item: dict[str, Any]) -> tuple[str, int, int] | None:
    rel_path = str(item.get("relative_path") or "")
    if not rel_path:
        return None
    mtime = item.get("mtime")
    if mtime is None:
        return None
    try:
        mtime_ms = _mtime_ms(mtime)
    except (TypeError, ValueError):
        return None
    return (rel_path, int(item.get("size_bytes") or 0), mtime_ms)


def _infer_item_stage(item: dict[str, Any]) -> str | None:
    """Best-effort ladder position for old and new batch rows.

    New rows persist stage/stage_rank. Live batches created before §13-S only
    have status/phase, so the UI otherwise undercounts progress after a deploy.
    """
    explicit = str(item.get("stage") or "").strip()
    if explicit in STAGE_RANK:
        return explicit

    status = str(item.get("status") or "").strip()
    phase = str(item.get("phase") or "").strip()
    if status == ITEM_SKIPPED:
        return None
    if status == ITEM_DONE:
        if phase == "awaiting_summary":
            return "indexed"
        return _PHASE_TO_STAGE.get(phase) or "queryable"
    if phase in _PHASE_TO_STAGE:
        return _PHASE_TO_STAGE[phase]
    if status in {ITEM_QUEUED, ITEM_RUNNING, ITEM_FAILED_RECOVERABLE, ITEM_FAILED}:
        return "registered"
    return None


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
        # AppleDouble resource forks ("._x.md") and other dotfiles satisfy the
        # extension filter but are never documents — exFAT drives grow them on
        # any Finder copy.
        if path.name.startswith("."):
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
    source: str = SOURCE_LOCAL_FOLDER,
    relative_path: str | None = None,
    filename: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    stat = path.stat()
    if relative_path is None:
        try:
            relative_path = str(path.relative_to(root))
        except ValueError:
            relative_path = path.name
    filename = filename or path.name
    source_path = source_path if source_path is not None else str(path)
    item_id = item_id or str(uuid.uuid4())
    return {
        "item_id": item_id,
        "batch_id": batch_id,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "source": source,
        "source_path": source_path,
        "stored_path": str(stored_path) if stored_path is not None else None,
        "relative_path": relative_path,
        "filename": filename,
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
    profile: str | None = None,
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
    normalized_profile = _normalize_profile(profile)
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
        "max_files": max_files,
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
            "profile": normalized_profile,
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


async def create_upload_batch(
    *,
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    user_id: str,
    files: list[dict[str, Any]],
    max_total_bytes: int | None = None,
    use_neo4j: bool | None = None,
    chunk_summarization: bool | None = None,
    model: str = "",
    concurrency: int | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Create a durable one-off browser-upload batch from already-read bytes."""
    if not files:
        raise ValueError("No files uploaded")
    allowed_exts = _normalize_extensions(None)
    cleaned: list[dict[str, Any]] = []
    total_source_bytes = 0
    for idx, file in enumerate(files):
        filename = str(file.get("filename") or f"upload-{idx + 1}").strip()
        data = bytes(file.get("data") or b"")
        ext = Path(filename).suffix.lower()
        if ext not in allowed_exts:
            raise ValueError(f"Unsupported file extension for {filename}: {ext or '(none)'}")
        if not data:
            raise ValueError(f"Uploaded file is empty: {filename}")
        cleaned.append(
            {
                "filename": filename,
                "content_type": file.get("content_type"),
                "data": data,
            }
        )
        total_source_bytes += len(data)

    now = _now()
    batch_id = str(uuid.uuid4())
    settings = get_settings()
    normalized_profile = _normalize_profile(profile)
    storage_root = Path(settings.INGEST_FILE_STORAGE_DIR).expanduser().resolve()
    storage_limit = int(max_total_bytes or settings.INGEST_FILE_STORAGE_MAX_BYTES)
    _ensure_storage_quota(
        storage_root=storage_root,
        incoming_bytes=total_source_bytes,
        max_total_bytes=storage_limit,
    )
    worker_count = max(
        1,
        min(
            int(concurrency or 1),
            int(settings.INGEST_MAX_ACTIVE_JOBS),
        ),
    )
    batch_storage_dir = storage_root / batch_id
    batch_storage_dir.mkdir(parents=True, exist_ok=False)
    try:
        for file in cleaned:
            item_id = str(uuid.uuid4())
            stored_path = _storage_path_for_item(
                storage_root,
                batch_id,
                item_id,
                file["filename"],
            )
            stored_path.write_bytes(file["data"])
            file["item_id"] = item_id
            file["stored_path"] = stored_path
    except Exception:
        shutil.rmtree(batch_storage_dir, ignore_errors=True)
        raise

    batch_doc = {
        "batch_id": batch_id,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "source": SOURCE_BROWSER_UPLOAD,
        "root_path": None,
        "recursive": False,
        "extensions": sorted({Path(file["filename"]).suffix.lower() for file in cleaned}),
        "max_files": len(cleaned),
        "store_files": True,
        "total_source_bytes": total_source_bytes,
        "stored_bytes": total_source_bytes,
        "storage_limit_bytes": storage_limit,
        "status": BATCH_QUEUED,
        "total": len(cleaned),
        "counts": {
            ITEM_QUEUED: len(cleaned),
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
            "profile": normalized_profile,
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
                root=batch_storage_dir,
                path=file["stored_path"],
                ordinal=idx,
                item_id=file["item_id"],
                stored_path=file["stored_path"],
                source=SOURCE_BROWSER_UPLOAD,
                relative_path=file["filename"],
                filename=file["filename"],
                source_path=file["filename"],
            )
            for idx, file in enumerate(cleaned)
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


async def append_new_files_to_batch(
    *,
    db: AsyncIOMotorDatabase,
    batch_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Rescan a local-folder batch root and append unseen files as queued."""
    batch = await db[BATCHES].find_one({"batch_id": batch_id, "user_id": user_id})
    if not batch:
        raise ValueError("Batch not found")
    if batch.get("source") != "local_folder":
        raise ValueError("Only local_folder batches can be rescanned")
    root_path = str(batch.get("root_path") or "")
    if not root_path:
        raise ValueError("Batch has no root_path to rescan")

    root, files = discover_local_files(
        root_path,
        recursive=bool(batch.get("recursive", True)),
        extensions=batch.get("extensions") or None,
    )
    existing_items = await db[ITEMS].find(
        {"batch_id": batch_id, "user_id": user_id},
        {"_id": 0, "relative_path": 1, "size_bytes": 1, "mtime": 1},
    ).to_list(length=None)
    existing_identities = {
        identity
        for identity in (_item_identity(item) for item in existing_items)
        if identity is not None
    }

    new_files: list[Path] = []
    for path in files:
        stat = path.stat()
        identity = _file_identity(
            root=root,
            path=path,
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
        )
        if identity not in existing_identities:
            new_files.append(path)

    if not new_files:
        refreshed = await refresh_batch_counts(db, batch_id, user_id=user_id)
        return {**refreshed, "appended_items": 0, "discovered_files": len(files)}

    max_rows = await db[ITEMS].find(
        {"batch_id": batch_id, "user_id": user_id},
        {"_id": 0, "ordinal": 1},
    ).sort("ordinal", -1).limit(1).to_list(length=1)
    next_ordinal = int(max_rows[0].get("ordinal", -1)) + 1 if max_rows else 0
    incoming_bytes = _sum_file_sizes(new_files)

    settings = get_settings()
    storage_root = Path(settings.INGEST_FILE_STORAGE_DIR).expanduser().resolve()
    storage_limit = int(
        batch.get("storage_limit_bytes")
        or settings.INGEST_FILE_STORAGE_MAX_BYTES
    )
    store_files = bool(batch.get("store_files"))
    stored_paths: dict[Path, Path] = {}
    item_ids: dict[Path, str] = {}
    copied_paths: list[Path] = []
    if store_files:
        _ensure_storage_quota(
            storage_root=storage_root,
            incoming_bytes=incoming_bytes,
            max_total_bytes=storage_limit,
        )
        batch_storage_dir = storage_root / batch_id
        batch_storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            for path in new_files:
                item_id = str(uuid.uuid4())
                item_ids[path] = item_id
                stored_path = _storage_path_for_item(
                    storage_root,
                    batch_id,
                    item_id,
                    path.name,
                )
                shutil.copy2(path, stored_path)
                stored_paths[path] = stored_path
                copied_paths.append(stored_path)
        except Exception:
            for copied_path in copied_paths:
                copied_path.unlink(missing_ok=True)
            raise

    await db[ITEMS].insert_many(
        [
            _file_item_doc(
                batch_id=batch_id,
                corpus_id=str(batch["corpus_id"]),
                user_id=user_id,
                root=root,
                path=path,
                ordinal=next_ordinal + idx,
                item_id=item_ids.get(path),
                stored_path=stored_paths.get(path),
            )
            for idx, path in enumerate(new_files)
        ],
        ordered=False,
    )
    await db[BATCHES].update_one(
        {"batch_id": batch_id, "user_id": user_id},
        {
            "$inc": {
                "total_source_bytes": incoming_bytes,
                "stored_bytes": incoming_bytes if store_files else 0,
            },
            "$set": {"updated_at": _now()},
        },
    )
    refreshed = await refresh_batch_counts(db, batch_id, user_id=user_id)
    return {
        **refreshed,
        "appended_items": len(new_files),
        "discovered_files": len(files),
    }


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
            ITEM_STAGED,
        )
    }
    total = sum(counts.values())
    unfinished = (
        counts[ITEM_QUEUED]
        + counts[ITEM_RUNNING]
        + counts[ITEM_FAILED_RECOVERABLE]
        # §13-S: staged = mid-pass, never terminal.
        + counts[ITEM_STAGED]
    )
    if total == 0:
        status = BATCH_FAILED
    elif unfinished > 0:
        status = BATCH_RUNNING if counts[ITEM_RUNNING] else BATCH_QUEUED
    elif counts[ITEM_FAILED] > 0:
        status = BATCH_PARTIAL if counts[ITEM_DONE] or counts[ITEM_SKIPPED] else BATCH_FAILED
    else:
        status = BATCH_DONE

    # Owner metric (2026-07-05): progress in FILES and MB, with an explicit
    # "extracted" milestone — the extraction lane's own deliverable — distinct
    # from fully indexed. An item counts as extracted once its phase moves
    # PAST ghosts (embedding/writes/complete). Phases are enumerated on the
    # PRE side so any future post-extraction phase counts correctly by
    # default; "summaries" is pre because summary generation precedes ghosts
    # in the current pipeline order.
    pre_extraction_phases = [
        "queued",
        "reading",
        "starting_worker",
        "parse",
        "retrieval_setup",
        "chunking",
        "summaries",
        "summary",
        "summary_tree",
        "ghosts",
        "paused_cost_brake",
        "failed",
        "stale",
    ]
    extracted_cond = {
        "$or": [
            {"$eq": ["$status", ITEM_DONE]},
            {
                "$and": [
                    {"$eq": ["$status", ITEM_RUNNING]},
                    {
                        "$not": [
                            {
                                "$in": [
                                    {"$ifNull": ["$phase", "queued"]},
                                    pre_extraction_phases,
                                ]
                            }
                        ]
                    },
                ]
            },
        ]
    }
    size_of = {"$ifNull": ["$size_bytes", 0]}
    rows = await db[ITEMS].aggregate(
        [
            {"$match": {"batch_id": batch_id}},
            {
                "$group": {
                    "_id": None,
                    "total_bytes": {"$sum": size_of},
                    "done_bytes": {
                        "$sum": {
                            "$cond": [{"$eq": ["$status", ITEM_DONE]}, size_of, 0]
                        }
                    },
                    "extracted_files": {"$sum": {"$cond": [extracted_cond, 1, 0]}},
                    "extracted_bytes": {
                        "$sum": {"$cond": [extracted_cond, size_of, 0]}
                    },
                }
            },
        ]
    ).to_list(1)
    sizes = rows[0] if rows else {}
    _mb = lambda b: round(float(b or 0) / 1048576, 1)  # noqa: E731
    ladder_items = await db[ITEMS].find(
        {"batch_id": batch_id},
        {"_id": 0, "stage": 1, "status": 1, "phase": 1},
    ).to_list(length=None)
    _by_stage: dict[str, int] = {}
    for item in ladder_items:
        stage = _infer_item_stage(item)
        if stage:
            _by_stage[stage] = _by_stage.get(stage, 0) + 1
    # Cumulative ladder: a file AT rung k has passed every rung below it.
    ladder = {}
    _cum = 0
    for s in reversed(STAGE_LADDER):
        _cum += _by_stage.get(s, 0)
        ladder[s] = _cum

    progress = {
        "files_done": counts[ITEM_DONE],
        "files_total": total,
        "files_extracted": int(sizes.get("extracted_files") or 0),
        "mb_done": _mb(sizes.get("done_bytes")),
        "mb_extracted": _mb(sizes.get("extracted_bytes")),
        "mb_total": _mb(sizes.get("total_bytes")),
        # §13-S honest ladder ("500 chunked / 320 extracted / 60 queryable").
        "ladder": ladder,
    }

    update: dict[str, Any] = {
        "counts": counts,
        "total": total,
        "progress": progress,
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


async def recover_local_batch_runners(
    *,
    db: AsyncIOMotorDatabase,
    ingestion_service: Any,
    user_id: str | None = None,
    max_batches: int = 100,
) -> dict[str, Any]:
    """Rehydrate durable local-folder batches after a backend restart.

    The batch manifest is durable in Mongo, but the asyncio runner is process
    local. On startup, any item still marked ``running`` is necessarily
    orphaned in the single-backend deployment and must be made resumable before
    the next runner leases work.
    """
    now = _now()
    running_filter: dict[str, Any] = {
        "source": {"$in": RUNNABLE_SOURCES},
        "status": ITEM_RUNNING,
    }
    if user_id is not None:
        running_filter["user_id"] = user_id
    orphaned = await db[ITEMS].find(
        running_filter,
        {"batch_id": 1, "_id": 0},
    ).to_list(length=None)
    res = await db[ITEMS].update_many(
        running_filter,
        {
            "$set": {
                "status": ITEM_FAILED_RECOVERABLE,
                "phase": "stale",
                "failure_stage": "backend_restarted",
                "error": "Backend restarted while this item was running; item can be resumed.",
                "lease_owner": None,
                "lease_until": None,
                "last_heartbeat_at": now,
                "updated_at": now,
            }
        },
    )

    work_filter: dict[str, Any] = {
        "source": {"$in": RUNNABLE_SOURCES},
        "status": {"$in": [ITEM_QUEUED, ITEM_FAILED_RECOVERABLE]},
    }
    if user_id is not None:
        work_filter["user_id"] = user_id
    pending = await db[ITEMS].find(
        work_filter,
        {"batch_id": 1, "_id": 0},
    ).to_list(length=None)

    batch_ids = sorted(
        {
            str(row.get("batch_id"))
            for row in [*orphaned, *pending]
            if row.get("batch_id")
        }
    )
    if not batch_ids:
        return {
            "reclaimed_items": int(res.modified_count),
            "candidate_batches": 0,
            "started_batches": 0,
        }

    for batch_id in batch_ids:
        await refresh_batch_counts(db, batch_id, user_id=user_id)

    batch_filter: dict[str, Any] = {
        "batch_id": {"$in": batch_ids},
        "source": {"$in": RUNNABLE_SOURCES},
    }
    if user_id is not None:
        batch_filter["user_id"] = user_id
    rows = await db[BATCHES].find(
        batch_filter,
        {"_id": 0, "batch_id": 1, "user_id": 1, "status": 1, "started_at": 1},
    ).limit(max(1, int(max_batches))).to_list(length=max(1, int(max_batches)))

    started = 0
    for batch in rows:
        if batch.get("status") == BATCH_QUEUED and not batch.get("started_at"):
            continue
        batch_user_id = str(batch.get("user_id") or user_id or "")
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id or not batch_user_id:
            continue
        if start_local_batch_runner(
            db=db,
            ingestion_service=ingestion_service,
            batch_id=batch_id,
            user_id=batch_user_id,
        ):
            started += 1

    return {
        "reclaimed_items": int(res.modified_count),
        "candidate_batches": len(rows),
        "started_batches": started,
    }


async def _lease_next_item(
    db: AsyncIOMotorDatabase,
    *,
    batch_id: str,
    owner: str,
    lease_seconds: int,
    target_rank: int | None = None,
) -> dict[str, Any] | None:
    now = _now()
    # Audit 2026-07-06 (critical): `attempts` was incremented but never READ —
    # a doc that deterministically kills its worker (OOM, pathological
    # chunking) retried forever (observed: 270+ attempts crash-looping a
    # batch all night). Items over the cap are excluded here and reaped to a
    # terminal failure below by the runner's sweep.
    max_attempts = max(
        1, int(getattr(get_settings(), "INGEST_MAX_ITEM_ATTEMPTS", 5))
    )
    return await db[ITEMS].find_one_and_update(
        {
            "batch_id": batch_id,
            "source": {"$in": RUNNABLE_SOURCES},
            "$and": [
                {"$or": [
                    {"status": {"$in": [ITEM_QUEUED, ITEM_FAILED_RECOVERABLE]}},
                    # §13-S: staged items re-lease when the batch target moved
                    # past their persisted rung (next pass).
                    {"status": ITEM_STAGED,
                     "stage_rank": {"$lt": target_rank}} if target_rank is not None
                    else {"status": ITEM_STAGED},
                ]},
                {"$or": [
                    {"attempts": {"$exists": False}},
                    {"attempts": {"$lt": max_attempts}},
                ]},
            ],
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


async def _reap_over_attempt_items(db: AsyncIOMotorDatabase, batch_id: str) -> int:
    """Terminal-fail items that exceeded the attempt cap (audit 2026-07-06,
    critical): attempts was incremented but never read, so deterministic
    killers retried forever (270+ attempts observed). Loud, named, manual
    re-queue only."""
    max_attempts = max(1, int(getattr(get_settings(), "INGEST_MAX_ITEM_ATTEMPTS", 5)))
    res = await db[ITEMS].update_many(
        {
            "batch_id": batch_id,
            "status": {"$in": [ITEM_QUEUED, ITEM_FAILED_RECOVERABLE]},
            "attempts": {"$gte": max_attempts},
        },
        {
            "$set": {
                "status": ITEM_FAILED,
                "phase": "failed",
                "failure_stage": "max_attempts",
                "error": (
                    f"exceeded INGEST_MAX_ITEM_ATTEMPTS={max_attempts} — "
                    "deterministic failure loop; inspect the doc, then "
                    "re-queue manually if appropriate"
                ),
                "updated_at": _now(),
            },
        },
    )
    if res.modified_count:
        logger.warning(
            "batch %s: reaped %d item(s) at attempt cap", batch_id[:8], res.modified_count
        )
    return res.modified_count


async def _lease_heartbeat(
    db: AsyncIOMotorDatabase, item_id: str, lease_seconds: int
) -> None:
    """Renew the lease while the item is actively processing (audit
    2026-07-06, critical): leases were NEVER renewed, so any doc that
    legitimately ran longer than the stale threshold was reclaimed WHILE
    STILL RUNNING — double processing and phantom casualties. Renews both
    fields the stale sweep checks."""
    interval = max(15, int(lease_seconds / 2))
    while True:
        await asyncio.sleep(interval)
        now = _now()
        await db[ITEMS].update_one(
            {"item_id": item_id, "status": ITEM_RUNNING},
            {
                "$set": {
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "last_heartbeat_at": now,
                    "updated_at": now,
                }
            },
        )


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
            _ladder = _PHASE_TO_STAGE.get(phase)
            if _ladder:
                await _advance_item_stage(db, item_id, _ladder)
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
            target_stage=(batch.get("options") or {}).get("target_stage") or None,
            extraction_endpoint_urls=_profile_endpoint_urls(batch),
        )
        if result.status == "staged":
            # §13-S: durable through the pass target; next pass re-leases it.
            status, item_phase, failure_stage = ITEM_STAGED, "staged", None
        elif result.status == "done":
            status, item_phase, failure_stage = ITEM_DONE, "complete", None
        elif result.status == "awaiting_summary":
            status, item_phase, failure_stage = ITEM_DONE, "awaiting_summary", None
        elif result.status == "skipped_duplicate":
            # Deliberate skip (near-duplicate) — NOT a failure; don't retry.
            status, item_phase, failure_stage = ITEM_SKIPPED, "skipped", None
        else:
            status, item_phase, failure_stage = ITEM_FAILED, "failed", "worker_result_failed"
        await _set_item_phase(
            db,
            item_id,
            item_phase,
            status=status,
            doc_id=result.doc_id,
            error=result.error,
            failure_stage=failure_stage,
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


async def _preflight_summary_canary(db, batch: dict) -> str | None:
    """One real call through the batch's summary model path; None = pass.
    Validates CONTENT EXISTS and the §10.1 JSON parses to structure —
    exactly the two failure modes that burned real runs (empty thinking-mode
    responses; prose-only fallback swallowing structure)."""
    import httpx as _hx

    from services.ingestion.extraction_contract import provider_payload_extras
    from services.ingestion.summary_semantics import (
        SEMANTIC_SUMMARY_INSTRUCTION,
        parse_semantic_summary,
    )
    from services.secrets import decrypt as _decrypt_secret

    settings = get_settings()
    corpus = await db["corpora"].find_one({"corpus_id": batch.get("corpus_id")}) or {}
    cfg = corpus.get("default_ingestion_config") or {}
    opts = batch.get("options") or {}
    summary_enabled = opts.get("chunk_summarization")
    if summary_enabled is None:
        summary_enabled = cfg.get("chunk_summarization")
    if not bool(summary_enabled):
        return None
    pool = (cfg.get("summary_models") or cfg.get("summary_model_pool") or [])
    entry = dict(pool[0]) if pool else {"model": getattr(
        settings, "GHOST_A_DEFAULT_MODEL", "") or settings.DEFAULT_COMPLETION_MODEL}
    for field in ("api_key", "lifecycle_api_key"):
        secret = entry.get(field)
        if secret:
            plaintext = _decrypt_secret(secret)
            entry[field] = plaintext if plaintext is not None else secret
    model = str(entry.get("model") or "")
    if not model:
        return "no summary model configured (chip empty and no default)"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content":
            "Passage:\nCompounding: small consistent gains accumulate into "
            "large outcomes over time.\n\n" + SEMANTIC_SUMMARY_INSTRUCTION}],
        "temperature": 0,
        "max_tokens": 400,
    }
    if entry.get("base_url"):
        payload["api_base"] = entry["base_url"]
    if entry.get("api_key"):
        payload["api_key"] = entry["api_key"]
    payload.update(provider_payload_extras(entry.get("extra_params")))
    _m = model.lower()
    if "v4-flash" in _m or "v4-pro" in _m or "deepseek-v4" in _m:
        payload.setdefault("thinking", {"type": "disabled"})
    try:
        async with _hx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{settings.LITELLM_URL}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}"},
            )
            resp.raise_for_status()
            content = (resp.json().get("choices") or [{}])[0].get(
                "message", {}).get("content") or ""
    except Exception as exc:  # noqa: BLE001
        return f"summary model {model} unreachable/errored: {str(exc)[:180]}"
    if not content.strip():
        return (f"summary model {model} returned EMPTY content — thinking-mode "
                "model without thinking disabled, or wrong model for bounded output")
    parsed = parse_semantic_summary(content)
    if not parsed.get("summary"):
        return f"summary model {model} output unparseable: {content[:120]!r}"
    if not parsed.get("semantic_chunk_type"):
        logger.warning(
            "Preflight canary: %s returns prose but NOT §10.1 JSON structure "
            "— summaries will lack semantic fields (batch proceeds)", model,
        )
    logger.info("Preflight canary PASSED for %s (structure=%s)",
                model, bool(parsed.get("semantic_chunk_type")))
    return None


async def _batch_quality_report(db, batch: dict) -> dict[str, Any]:
    """Fallback ACCOUNTING (owner principle: graceful degradation without
    degradation accounting is slow-motion data loss). Aggregated per batch,
    persisted on the batch row, cheap indexed counts only."""
    corpus_id = batch.get("corpus_id")
    doc_ids = [
        d["doc_id"] async for d in db["documents"].find(
            {"corpus_id": corpus_id}, {"doc_id": 1})
    ]
    q = {"corpus_id": corpus_id}
    parents = await db["parent_chunks"].count_documents(q)
    summarized = await db["parent_chunks"].count_documents(
        {**q, "summary": {"$nin": [None, ""]}})
    structured = await db["parent_chunks"].count_documents(
        {**q, "semantic_chunk_type": {"$nin": [None, ""]}})
    children = await db["chunks"].count_documents(q)
    promoted = await db["chunks"].count_documents(
        {**q, "promote_version": {"$exists": True}})
    verified = await db["documents"].count_documents(
        {**q, "write_state.verified": True})
    report = {
        "docs": len(doc_ids),
        "docs_verified": verified,
        "parents": parents,
        "parents_summarized": summarized,
        "parents_structured": structured,
        "summary_fallback_rate": round(1 - (summarized / parents), 3) if parents else None,
        "structure_rate": round(structured / parents, 3) if parents else None,
        "children": children,
        "children_promoted": promoted,
    }
    if parents and summarized < parents * 0.5:
        report["alert"] = "over half of parents have NO summary — model path degraded"
    elif parents and structured < summarized * 0.5:
        report["alert"] = "summaries exist but structure rate <50% — JSON schema not honored"
    return report


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
    if batch.get("source") not in RUNNABLE_SOURCES:
        raise ValueError("Only durable ingest batches can be run by the backend")

    # Preflight canary (owner-agreed 2026-07-04): ONE real summary-shaped
    # call through the batch's actual model path BEFORE any book is spent.
    # 5 seconds to save 30 minutes — a misconfigured chip (deprecated model,
    # thinking-mode empties, bad key) burned two full runs before this
    # existed. Failure marks the batch failed with an actionable error.
    if bool(getattr(get_settings(), "INGEST_PREFLIGHT_CANARY", True)):
        canary_err = await _preflight_summary_canary(db, batch)
        if canary_err:
            await db[BATCHES].update_one(
                {"batch_id": batch_id},
                {"$set": {"status": "failed",
                          "error": f"Preflight canary failed: {canary_err}",
                          "updated_at": _now()}},
            )
            logger.error("Batch %s preflight canary FAILED: %s", batch_id[:8], canary_err)
            return await refresh_batch_counts(db, batch_id, user_id=user_id)

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
    # Ingest isolation (owner directive 2026-07-04): a GLOBAL in-flight
    # document cap across ALL batches. Each batch used to bring its own
    # worker pool, so 3 uploaded batches = 3+ docs ingesting concurrently
    # inside the API process — event loop saturated, healthcheck failed,
    # autoheal SIGTERMed the backend (RestartCount=37, Cloudflare 502s).
    # Batches now QUEUE behind this semaphore instead of stacking.
    sem = _global_doc_semaphore()
    lease_seconds = max(60, int(get_settings().INGEST_STALE_JOB_MINUTES * 60))
    concurrency = _runtime_batch_concurrency(batch, get_settings())
    await _reap_over_attempt_items(db, batch_id)

    # ── §13-S pass driver ────────────────────────────────────────────────────
    # A profile's pass_plan sweeps the whole batch stage-by-stage: each pass
    # sets options.target_stage, runs the worker pool until nothing is
    # leaseable, then advances. Durable artifacts + resume rehydration make
    # repeated passes cheap; memory is released between passes (mac_safe's
    # core property). Default plan = one full pass (today's behavior).
    _profile = INGEST_PROFILES.get(
        str((batch.get("options") or {}).get("profile") or "").strip().lower()
    )
    pass_plan = list((_profile or {}).get("pass_plan") or [None])
    if (_profile or {}).get("concurrency"):
        concurrency = min(concurrency, int(_profile["concurrency"])) or 1

    async def _worker(worker_idx: int, target_rank: int | None) -> None:
        owner = f"{owner_prefix}:{worker_idx}"
        while True:
            item = await _lease_next_item(
                db,
                batch_id=batch_id,
                owner=owner,
                lease_seconds=lease_seconds,
                target_rank=target_rank,
            )
            if not item:
                return
            async with sem:
                # Lease heartbeat covers the WHOLE hold — including the
                # admission wait inside _process_local_item — so a live doc
                # can never be reclaimed as stale mid-run.
                _hb = asyncio.create_task(
                    _lease_heartbeat(db, item["item_id"], lease_seconds)
                )
                try:
                    await _process_local_item(
                        db=db,
                        ingestion_service=ingestion_service,
                        batch=batch,
                        item=item,
                    )
                finally:
                    _hb.cancel()
            await refresh_batch_counts(db, batch_id, user_id=user_id)

    for _pass_target in pass_plan:
        _rank = STAGE_RANK.get(_pass_target) if _pass_target else None
        batch.setdefault("options", {})["target_stage"] = _pass_target
        await db[BATCHES].update_one(
            {"batch_id": batch_id},
            {"$set": {"options.target_stage": _pass_target, "updated_at": _now()}},
        )
        if len(pass_plan) > 1:
            logger.info(
                "batch %s pass -> %s (plan %s)",
                batch_id[:8], _pass_target or "full", pass_plan,
            )
        await asyncio.gather(*[_worker(idx, _rank) for idx in range(concurrency)])
    try:
        report = await _batch_quality_report(db, batch)
        await db[BATCHES].update_one(
            {"batch_id": batch_id},
            {"$set": {"report": report, "updated_at": _now()}},
        )
        logger.info("Batch %s quality report: %s", batch_id[:8], report)
    except Exception as exc:  # noqa: BLE001 — reporting never fails the batch
        logger.warning("Batch quality report failed: %s", exc)
    return await refresh_batch_counts(db, batch_id, user_id=user_id)


def _runtime_batch_concurrency(batch: dict[str, Any], settings: Any) -> int:
    """Resolve actual worker count for a durable batch.

    ``INGEST_GLOBAL_MAX_DOCS`` is a cap across batches, not a floor. Forcing
    every resumed batch up to that value can lease more large books than the
    API container can hold, which turns startup recovery into an OOM loop.
    """

    configured = int((batch.get("options") or {}).get("concurrency") or 0)
    requested = configured or int(getattr(settings, "INGEST_BATCH_WORKERS", 1))
    global_cap = max(1, int(getattr(settings, "INGEST_GLOBAL_MAX_DOCS", 1)))
    active_cap = max(1, int(getattr(settings, "INGEST_MAX_ACTIVE_JOBS", 1)))
    return max(1, min(requested, global_cap, active_cap))


_GLOBAL_DOC_SEM: "asyncio.Semaphore | None" = None
_GLOBAL_DOC_SEM_SIZE: int = 0


def _global_doc_semaphore() -> "asyncio.Semaphore":
    """Process-wide document-ingest semaphore, sized by
    INGEST_GLOBAL_MAX_DOCS (re-created if the knob changes)."""
    global _GLOBAL_DOC_SEM, _GLOBAL_DOC_SEM_SIZE
    size = max(1, int(getattr(get_settings(), "INGEST_GLOBAL_MAX_DOCS", 2)))
    if _GLOBAL_DOC_SEM is None or size != _GLOBAL_DOC_SEM_SIZE:
        _GLOBAL_DOC_SEM = asyncio.Semaphore(size)
        _GLOBAL_DOC_SEM_SIZE = size
    return _GLOBAL_DOC_SEM


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
