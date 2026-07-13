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
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument, UpdateOne

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
    "registered",
    "parsed",
    "chunked",
    # Legacy pre-index extraction checkpoint. New graph-specific progress uses
    # graph_extracted below so "queryable" is not hidden behind Ghost B.
    "extracted",
    "indexed",
    "queryable",
    "summary_pending",
    "summarized",
    "summary_complete",
    "graph_pending",
    "graph_extracted",
    "promoted",
    "graph_promoted",
    "fully_enriched",
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
    "staged_indexed": "queryable",
    "staged_queryable": "queryable",
    "neo4j": "graph_extracted",
    "verifying": "graph_promoted",
    "awaiting_summary": "summary_pending",
    "complete": "fully_enriched",
    "fully_enriched": "fully_enriched",
    "queryable_with_pending_summary": "summary_pending",
    "queryable_with_pending_graph": "graph_pending",
    "queryable_with_pending_summary_and_graph": "graph_pending",
}

PENDING_ENRICHMENT_PHASES = [
    "awaiting_summary",
    "queryable",
    "queryable_with_pending_summary",
    "queryable_with_pending_graph",
    "queryable_with_pending_summary_and_graph",
]

# ── §13-S named profiles (deterministic one-knob presets) ────────────────────
# mac_safe is the global local/Mac rule:
#   - one active document owns the heavy phase budget;
#   - staged sweeps release memory between queryability and enrichment;
#   - local Mac sidecars are preferred over remote/cloud pools.
# First pass is queryable-first: Mongo chunks + dense/sparse vectors land before
# Ghost B/Neo4j, so local extraction can never gate initial retrieval.
# rtx_assisted: the elastic-car topology; single full pass.
# runpod_burst: autoscaling extraction with summaries allowed to overlap; unlike
# rtx_assisted it does not intentionally defer Ghost A, so a new cloud ingest
# can reach strict enrichment in one durable pass when both providers are live.
INGEST_PROFILES: dict[str, dict] = {
    "mac_queryable_first": {
        "concurrency": 1,
        "pass_plan": ["queryable", None],  # None = run enrichment to completion
        "extraction_endpoint_urls": [
            os.environ.get(
                "MAC_SIDECAR_URL", "http://host.docker.internal:8084"
            ).rstrip("/")
        ],
    },
    "mac_safe": {
        "concurrency": 1,
        "pass_plan": ["queryable", None],  # legacy name, same safe contract
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
        # RTX/cloud extraction is the expensive/offloaded lane. Do not let a
        # slow or exhausted summary provider keep the RTX idle; summaries are
        # filled by the summary backfill lane after graph extraction lands.
        "defer_summaries": True,
    },
    "runpod_burst": {
        "concurrency": None,
        "pass_plan": [None],
        "extraction_endpoint_urls": None,
        "defer_summaries": False,
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


def _profile_defaults(profile: str | None) -> dict[str, Any]:
    value = _normalize_profile(profile)
    return dict(INGEST_PROFILES.get(value or "", {}))


def _batch_defer_summaries(batch: dict[str, Any]) -> bool:
    options = batch.get("options") or {}
    if "defer_summaries" in options:
        return bool(options.get("defer_summaries"))
    profile_defaults = _profile_defaults(options.get("profile"))
    return bool(profile_defaults.get("defer_summaries"))


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


async def _ensure_storage_quota_durable(
    *,
    db: AsyncIOMotorDatabase,
    storage_root: Path,
    incoming_bytes: int,
    max_total_bytes: int,
) -> None:
    """Check the durable byte ledger without walking every stored file.

    Every successful stored batch already records ``stored_bytes``. Use that
    indexed Mongo projection on the request path and retain the filesystem walk
    only as a compatibility fallback for older/fake databases.
    """

    try:
        rows = await db[BATCHES].aggregate(
            [
                {"$match": {"stored_bytes": {"$gt": 0}}},
                {"$group": {"_id": None, "used": {"$sum": "$stored_bytes"}}},
            ]
        ).to_list(length=1)
        used = int(rows[0].get("used") or 0) if rows else 0
    except Exception:
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


def _safe_upload_filename(filename: str, *, fallback: str) -> str:
    """Return a human-readable leaf name without permitting path traversal."""
    leaf = Path(str(filename or "").replace("\\", "/")).name.strip()
    return leaf or fallback


def _unique_drop_off_path(directory: Path, filename: str, *, reserved: set[str]) -> Path:
    """Preserve the upload name, adding a deterministic suffix for duplicates."""
    candidate = filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    sequence = 2
    while candidate.casefold() in reserved or (directory / candidate).exists():
        candidate = f"{stem} ({sequence}){suffix}"
        sequence += 1
    reserved.add(candidate.casefold())
    return directory / candidate


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
    status = str(item.get("status") or "").strip()
    phase = str(item.get("phase") or "").strip()
    if status == ITEM_SKIPPED:
        return None
    if status == ITEM_DONE:
        if phase == "awaiting_summary":
            return "summary_pending"
        return _PHASE_TO_STAGE.get(phase) or "fully_enriched"

    explicit = str(item.get("stage") or "").strip()
    if explicit in STAGE_RANK:
        return explicit

    if phase in _PHASE_TO_STAGE:
        return _PHASE_TO_STAGE[phase]
    if status in {ITEM_QUEUED, ITEM_RUNNING, ITEM_FAILED_RECOVERABLE, ITEM_FAILED}:
        return "registered"
    return None


def _enrichment_completion_state(
    *,
    doc: dict[str, Any],
    summary_required: bool,
    graph_required: bool,
    required_parent_count: int,
    summarized_parent_count: int,
    document_tree_done: bool,
) -> dict[str, Any]:
    """Derive parallel enrichment-lane truth from durable artifacts."""

    write_state = doc.get("write_state") or {}
    profile = doc.get("doc_profile") or {}
    queryable = bool(
        write_state.get("mongo_written") is True
        and write_state.get("qdrant_written") is True
    )
    document_profile_done = bool(str(profile.get("summary") or "").strip())
    summary_complete = bool(
        not summary_required
        or (
            summarized_parent_count >= required_parent_count
            and document_profile_done
            and document_tree_done
        )
    )
    graph_complete = bool(
        not graph_required or write_state.get("neo4j_written") is True
    )
    verified = write_state.get("verified") is True
    return {
        "queryable": queryable,
        "summary": "complete" if summary_complete else "pending",
        "graph": "complete" if graph_complete else "pending",
        "verified": verified,
        "fully_enriched": bool(
            queryable and summary_complete and graph_complete and verified
        ),
    }


async def reconcile_batch_enrichment_truth(
    db: AsyncIOMotorDatabase,
    *,
    batch_id: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Promote stale batch/document phases from completed durable artifacts.

    Summary and graph lanes can finish after the source batch has become
    queryable. Their workers write the authoritative artifacts, but older code
    never projected that truth back onto the originating batch rows. This
    reconciliation is deterministic and idempotent; it never invokes a model.
    """

    batch_filter: dict[str, Any] = {"batch_id": batch_id}
    if user_id is not None:
        batch_filter["user_id"] = user_id
    batch = await db[BATCHES].find_one(batch_filter)
    if not batch:
        return {"status": "not_found", "promoted": 0}

    item_query: dict[str, Any] = {
        "batch_id": batch_id,
        "status": ITEM_DONE,
        "doc_id": {"$exists": True, "$nin": [None, ""]},
        "$or": [
            {"phase": {"$in": PENDING_ENRICHMENT_PHASES}},
            {"stage": {"$in": ["summary_pending", "graph_pending", "graph_promoted"]}},
        ],
    }
    if user_id is not None:
        item_query["user_id"] = user_id
    items = await db[ITEMS].find(
        item_query,
        {"_id": 0, "item_id": 1, "doc_id": 1},
    ).to_list(length=None)
    if not items:
        return {"status": "noop", "promoted": 0, "examined": 0}

    corpus_id = str(batch.get("corpus_id") or "")
    doc_ids = sorted(
        {str(item.get("doc_id") or "") for item in items if item.get("doc_id")}
    )
    docs = await db["documents"].find(
        {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}},
        {
            "_id": 0,
            "doc_id": 1,
            "ingest_stage": 1,
            "write_state": 1,
            "doc_profile.summary": 1,
        },
    ).to_list(length=None)
    docs_by_id = {str(doc.get("doc_id") or ""): doc for doc in docs}

    options = batch.get("options") or {}
    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "default_ingestion_config": 1},
    )
    defaults = (corpus or {}).get("default_ingestion_config") or {}
    summary_required = bool(
        options.get("chunk_summarization", defaults.get("chunk_summarization", False))
    )
    graph_required = bool(options.get("use_neo4j", defaults.get("use_neo4j", True)))

    required_counts = {doc_id: 0 for doc_id in doc_ids}
    summarized_counts = {doc_id: 0 for doc_id in doc_ids}
    if summary_required:
        from services.ingestion.section_classifier import parent_summary_required_clause
        from services.storage.record_status import with_active_records

        parent_query = with_active_records(
            {
                "corpus_id": corpus_id,
                "doc_id": {"$in": doc_ids},
                "$and": [parent_summary_required_clause()],
            }
        )
        parent_rows = await db["parent_chunks"].find(
            parent_query,
            {"_id": 0, "doc_id": 1, "summary": 1},
        ).to_list(length=None)
        for parent in parent_rows:
            doc_id = str(parent.get("doc_id") or "")
            required_counts[doc_id] = required_counts.get(doc_id, 0) + 1
            if str(parent.get("summary") or "").strip():
                summarized_counts[doc_id] = summarized_counts.get(doc_id, 0) + 1

    tree_rows = await db["summary_tree"].find(
        {
            "corpus_id": corpus_id,
            "doc_id": {"$in": doc_ids},
            "node_type": "document",
            "summary": {"$exists": True, "$nin": [None, ""]},
        },
        {"_id": 0, "doc_id": 1},
    ).to_list(length=None)
    tree_doc_ids = {
        str(row.get("doc_id") or "")
        for row in tree_rows
    }

    item_ops: list[UpdateOne] = []
    doc_ops: list[UpdateOne] = []
    promoted_doc_ids: set[str] = set()
    for item in items:
        doc_id = str(item.get("doc_id") or "")
        doc = docs_by_id.get(doc_id)
        if not doc:
            continue
        state = _enrichment_completion_state(
            doc=doc,
            summary_required=summary_required,
            graph_required=graph_required,
            required_parent_count=required_counts.get(doc_id, 0),
            summarized_parent_count=summarized_counts.get(doc_id, 0),
            document_tree_done=doc_id in tree_doc_ids,
        )
        item_set: dict[str, Any] = {
            "enrichment_lanes": {
                "summary": state["summary"],
                "graph": state["graph"],
            },
            "enrichment_status": {
                "summary": state["summary"],
                "graph": state["graph"],
            },
            "enrichment_reconciled_at": _now(),
            "updated_at": _now(),
        }
        item_update: dict[str, Any] = {"$set": item_set}
        if state["fully_enriched"]:
            item_set.update(
                {
                    "phase": "fully_enriched",
                    "stage": "fully_enriched",
                    "stage_rank": STAGE_RANK["fully_enriched"],
                }
            )
            item_update["$unset"] = {"error": "", "failure_stage": ""}
            promoted_doc_ids.add(doc_id)
        item_ops.append(
            UpdateOne({"item_id": item.get("item_id")}, item_update, upsert=False)
        )
        if state["fully_enriched"]:
            doc_ops.append(
                UpdateOne(
                    {"corpus_id": corpus_id, "doc_id": doc_id},
                    {
                        "$set": {
                            "ingest_stage": "fully_enriched",
                            "enrichment_lanes": {
                                "summary": "complete",
                                "graph": "complete",
                            },
                            "enrichment_status": {
                                "summary": "complete",
                                "graph": "complete",
                            },
                            "enrichment_reconciled_at": _now(),
                            "updated_at": _now(),
                        },
                        "$unset": {
                            "enrichment_pending_reason": "",
                            "summary_pending_reason": "",
                        },
                    },
                    upsert=False,
                )
            )

    if item_ops:
        await db[ITEMS].bulk_write(item_ops, ordered=False)
    if doc_ops:
        await db["documents"].bulk_write(doc_ops, ordered=False)

    remaining = await db[ITEMS].count_documents(
        {
            "batch_id": batch_id,
            "status": ITEM_DONE,
            "phase": {"$in": PENDING_ENRICHMENT_PHASES},
        }
    )
    if remaining == 0 and batch.get("summary_backfill_run_id"):
        now = _now()
        run_id = str(batch.get("summary_backfill_run_id"))
        await db["ingest_repair_runs"].update_one(
            {"run_id": run_id, "status": {"$in": ["queued", "running"]}},
            {
                "$set": {
                    "status": "complete",
                    "completion_reason": "durable_artifacts_reconciled",
                    "completed_at": now,
                    "updated_at": now,
                }
            },
        )
        await db[BATCHES].update_one(
            {"batch_id": batch_id},
            {
                "$set": {
                    "summary_backfill_status": "complete",
                    "summary_backfill_completed_at": now,
                    "updated_at": now,
                },
                "$unset": {"summary_pending_reason": ""},
            },
        )

    return {
        "status": "complete",
        "examined": len(items),
        "promoted": len(promoted_doc_ids),
        "remaining": remaining,
    }


async def reconcile_pending_batch_enrichment_truth(
    db: AsyncIOMotorDatabase,
    *,
    corpus_id: str,
    doc_ids: list[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Reconcile batches selected from pending durable items, not recency.

    Summary and graph lanes can finish long after their source batch. Selecting
    the newest batches starves older completed artifacts and leaves UI stages
    permanently stale. Pending batch items are the authoritative work index.
    """

    limit = max(1, min(int(limit or 500), 5000))
    item_query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "status": ITEM_DONE,
        "doc_id": {"$exists": True, "$nin": [None, ""]},
        "$or": [
            {"phase": {"$in": PENDING_ENRICHMENT_PHASES}},
            {"stage": {"$in": ["summary_pending", "graph_pending", "graph_promoted"]}},
        ],
    }
    normalized_doc_ids = sorted(
        {str(doc_id).strip() for doc_id in (doc_ids or []) if str(doc_id).strip()}
    )
    if normalized_doc_ids:
        item_query["doc_id"] = {"$in": normalized_doc_ids}

    item_rows = await db[ITEMS].find(
        item_query,
        {"_id": 0, "batch_id": 1},
    ).limit(limit).to_list(length=limit)
    batch_ids = sorted(
        {
            str(row.get("batch_id") or "")
            for row in item_rows
            if row.get("batch_id")
        }
    )
    if not batch_ids:
        return {
            "status": "noop",
            "batches": 0,
            "examined": 0,
            "promoted": 0,
            "remaining": 0,
        }

    batch_rows = await db[BATCHES].find(
        {"corpus_id": corpus_id, "batch_id": {"$in": batch_ids}},
        {"_id": 0, "batch_id": 1, "user_id": 1},
    ).to_list(length=len(batch_ids))
    examined = 0
    promoted = 0
    remaining = 0
    reconciled_batches = 0
    for batch in batch_rows:
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id:
            continue
        result = await reconcile_batch_enrichment_truth(
            db,
            batch_id=batch_id,
            user_id=str(batch.get("user_id") or "") or None,
        )
        if result.get("status") == "not_found":
            continue
        reconciled_batches += 1
        examined += int(result.get("examined") or 0)
        promoted += int(result.get("promoted") or 0)
        remaining += int(result.get("remaining") or 0)

    return {
        "status": "complete",
        "batches": reconciled_batches,
        "examined": examined,
        "promoted": promoted,
        "remaining": remaining,
    }


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
        # AppleDouble resource forks ("._x.md") and other dotfiles satisfy the
        # extension filter but are never documents — exFAT drives grow them on
        # any Finder copy. Check before stat: Docker can raise EPERM when
        # statting macOS metadata files on mounted external volumes.
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            relative_parts = (path.name,)
        if any(part.startswith(".") for part in relative_parts):
            continue
        try:
            if not path.is_file():
                continue
        except OSError as exc:
            logger.debug("Skipping unreadable ingest candidate %s: %s", path, exc)
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
    drop_off_path: Path | None = None,
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
        "drop_off_path": str(drop_off_path) if drop_off_path is not None else None,
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
        await _ensure_storage_quota_durable(
            db=db,
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
            "defer_summaries": bool(
                _profile_defaults(normalized_profile).get("defer_summaries")
            ),
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
        filename = _safe_upload_filename(
            str(file.get("filename") or ""),
            fallback=f"upload-{idx + 1}",
        )
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
    storage_root = Path(settings.INGEST_DROP_OFF_DIR).expanduser().resolve()
    storage_limit = int(max_total_bytes or settings.INGEST_FILE_STORAGE_MAX_BYTES)
    await _ensure_storage_quota_durable(
        db=db,
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
    batch_storage_dir = storage_root / corpus_id / "uploads" / batch_id
    batch_storage_dir.mkdir(parents=True, exist_ok=False)
    try:
        reserved_names: set[str] = set()
        for file in cleaned:
            item_id = str(uuid.uuid4())
            stored_path = _unique_drop_off_path(
                batch_storage_dir,
                file["filename"],
                reserved=reserved_names,
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
        "root_path": str(batch_storage_dir),
        "drop_off_dir": str(batch_storage_dir),
        "drop_off_relative_dir": str(Path(corpus_id) / "uploads" / batch_id),
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
            "defer_summaries": bool(
                _profile_defaults(normalized_profile).get("defer_summaries")
            ),
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
                drop_off_path=file["stored_path"],
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
    reconciliation = await reconcile_batch_enrichment_truth(
        db,
        batch_id=batch_id,
        user_id=user_id,
    )
    if int(reconciliation.get("promoted") or 0) > 0:
        await refresh_batch_counts(db, batch_id, user_id=user_id)
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
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Return recent durable ingest batches for a corpus.

    The default is the operator-facing "current" view: queued/running and
    successfully completed batches. Cancelled/failed terminal rows are history,
    not live truth, and showing them as the primary corpus card caused stale
    progress displays after repair/cancel workflows. Callers that need audit
    history can opt in with include_archived=True.
    """
    limit = max(1, min(int(limit), 100))
    query: dict[str, Any] = {"corpus_id": corpus_id, "user_id": user_id}
    if not include_archived:
        query["$or"] = [
            {"status": {"$in": [BATCH_QUEUED, BATCH_RUNNING, BATCH_DONE, BATCH_PARTIAL]}},
            {"status": {"$exists": False}},
        ]
    cursor = db[BATCHES].find(
        query,
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
        await _ensure_storage_quota_durable(
            db=db,
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
                    {"$eq": ["$status", ITEM_STAGED]},
                    {
                        "$gte": [
                            {"$ifNull": ["$stage_rank", -1]},
                            STAGE_RANK["extracted"],
                        ]
                    },
                ]
            },
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
    queryable_cond = {
        "$or": [
            {"$eq": ["$status", ITEM_DONE]},
            {
                "$and": [
                    {"$eq": ["$status", ITEM_STAGED]},
                    {
                        "$gte": [
                            {"$ifNull": ["$stage_rank", -1]},
                            STAGE_RANK["queryable"],
                        ]
                    },
                ]
            },
        ]
    }
    graph_extracted_cond = {
        "$or": [
            {
                "$and": [
                    {"$eq": ["$status", ITEM_DONE]},
                    {
                        "$not": [
                            {
                                "$in": [
                                    {"$ifNull": ["$phase", ""]},
                                    [
                                        "queryable_with_pending_graph",
                                        "queryable_with_pending_summary_and_graph",
                                    ],
                                ]
                            }
                        ]
                    },
                ]
            },
            {
                "$and": [
                    {"$eq": ["$status", ITEM_STAGED]},
                    {
                        "$gte": [
                            {"$ifNull": ["$stage_rank", -1]},
                            STAGE_RANK["graph_extracted"],
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
                    "queryable_files": {"$sum": {"$cond": [queryable_cond, 1, 0]}},
                    "queryable_bytes": {
                        "$sum": {"$cond": [queryable_cond, size_of, 0]}
                    },
                    "graph_extracted_files": {
                        "$sum": {"$cond": [graph_extracted_cond, 1, 0]}
                    },
                    "graph_extracted_bytes": {
                        "$sum": {"$cond": [graph_extracted_cond, size_of, 0]}
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
        "files_queryable": int(sizes.get("queryable_files") or 0),
        "files_graph_extracted": int(sizes.get("graph_extracted_files") or 0),
        "mb_done": _mb(sizes.get("done_bytes")),
        "mb_extracted": _mb(sizes.get("extracted_bytes")),
        "mb_queryable": _mb(sizes.get("queryable_bytes")),
        "mb_graph_extracted": _mb(sizes.get("graph_extracted_bytes")),
        "mb_total": _mb(sizes.get("total_bytes")),
        # §13-S honest ladder ("500 chunked / 320 queryable / 60 graph promoted").
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


async def requeue_failed_items_for_resume(
    db: AsyncIOMotorDatabase,
    *,
    batch_id: str,
    user_id: str,
) -> dict[str, int]:
    """Requeue bounded worker failures after an explicit operator Resume.

    Missing sources and exhausted-attempt items stay terminal. Automatic
    recovery never calls this path, so deterministic failures cannot loop
    unless an operator explicitly retries after changing parser/provider
    configuration.
    """

    now = _now()
    max_attempts = max(1, int(get_settings().INGEST_MAX_ITEM_ATTEMPTS))
    result = await db[ITEMS].update_many(
        {
            "batch_id": batch_id,
            "user_id": user_id,
            "status": ITEM_FAILED,
            "failure_stage": {"$in": ["worker_exception", "worker_result_failed"]},
            "attempts": {"$lt": max_attempts},
        },
        {
            "$set": {
                "status": ITEM_FAILED_RECOVERABLE,
                "phase": "retry_requested",
                "lease_owner": None,
                "lease_until": None,
                "completed_at": None,
                "updated_at": now,
            },
            "$unset": {"error": ""},
        },
    )
    await refresh_batch_counts(db, batch_id, user_id=user_id)
    return {"requeued_items": int(result.modified_count)}


async def recover_local_batch_runners(
    *,
    db: AsyncIOMotorDatabase,
    ingestion_service: Any,
    user_id: str | None = None,
    max_batches: int = 100,
    reclaim_active_running: bool = False,
) -> dict[str, Any]:
    """Rehydrate durable local-folder batches after a backend restart.

    The batch manifest is durable in Mongo, but the asyncio runner is process
    local. On startup, any item still marked ``running`` is orphaned in this
    process and must be made resumable before the next runner leases work.
    During periodic polling, only expired leases are reclaimed; otherwise the
    poller can mark its own live Ghost B item stale while the heartbeat is
    still extending the lease.
    """
    now = _now()
    running_filter: dict[str, Any] = {
        "source": {"$in": RUNNABLE_SOURCES},
        "status": ITEM_RUNNING,
    }
    if not reclaim_active_running:
        running_filter["$or"] = [
            {"lease_until": {"$exists": False}},
            {"lease_until": None},
            {"lease_until": {"$lt": now}},
        ]
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
        "status": {"$in": [ITEM_QUEUED, ITEM_FAILED_RECOVERABLE, ITEM_STAGED]},
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
        {
            "_id": 0,
            "batch_id": 1,
            "user_id": 1,
            "status": 1,
            "started_at": 1,
            "run_requested_at": 1,
        },
    ).limit(max(1, int(max_batches))).to_list(length=max(1, int(max_batches)))

    started = 0
    for batch in rows:
        if (
            batch.get("status") == BATCH_QUEUED
            and not batch.get("started_at")
            and not batch.get("run_requested_at")
        ):
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


async def _wait_for_ingest_slot(limit: int | None = None) -> None:
    while not await admission.try_acquire_ingest_slot(limit=limit):
        await asyncio.sleep(1.0)


def _is_transient_store_exception(exc: BaseException) -> bool:
    """Return True for retryable infrastructure/store transport failures."""

    message = str(exc).lower()
    deterministic_markers = (
        "chunker timeout",
        "tier_chunker",
        "chunk_failed",
        "pathological content",
    )
    if any(marker in message for marker in deterministic_markers):
        return False
    needles = (
        "defunct connection",
        "connection reset",
        "connection refused",
        "connection aborted",
        "server disconnected",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "readtimeout",
        "writetimeout",
        "service unavailable",
        "503",
        "504",
        "neo4j",
        "qdrant",
        "mongodb",
        "mongo",
    )
    return any(needle in message for needle in needles)


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


async def _batch_lifecycle_pool(
    *,
    ingestion_service: Any,
    corpus_id: str,
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve managed model entries once for a durable batch.

    The worker still uses the normal per-document routing code. This helper is
    only for remote runtime lifecycle control so a managed RTX/vLLM server stays
    hot across the batch instead of cooling down between documents.
    """

    try:
        config = await _build_item_config(
            ingestion_service=ingestion_service,
            corpus_id=corpus_id,
            options=options,
        )
    except Exception as exc:  # noqa: BLE001 - lifecycle must not block batch setup
        logger.warning("batch lifecycle pool resolution failed: %s", exc)
        return []

    refs: list[Any] = []
    for attr in ("summary_models", "extraction_models", "embedding_models"):
        value = getattr(config, attr, None)
        if value:
            refs.extend(list(value))
    if not refs:
        return []

    plaintext_pool = []
    decrypt_pool = getattr(ingestion_service, "_plaintext_model_pool", None)
    if callable(decrypt_pool):
        plaintext_pool = decrypt_pool(refs)
    else:
        plaintext_pool = [
            ref.model_dump() if hasattr(ref, "model_dump") else dict(ref)
            for ref in refs
        ]
    return [entry for entry in plaintext_pool if entry.get("lifecycle_base_url")]


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
    release_lease: bool = False,
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
    if completed or release_lease:
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
        await _wait_for_ingest_slot(_global_doc_limit_for_batch(batch, get_settings()))
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
            defer_summaries=_batch_defer_summaries(batch),
        )
        if result.status == "staged":
            # §13-S: durable through the pass target; next pass re-leases it.
            status, item_phase, failure_stage = ITEM_STAGED, "staged", None
        elif result.status == "done":
            status, item_phase, failure_stage = ITEM_DONE, "complete", None
        elif result.status == "awaiting_summary":
            status, item_phase, failure_stage = ITEM_DONE, "awaiting_summary", None
        elif str(result.status).startswith("queryable_with_pending_"):
            item_phase = str(result.status)
            status, failure_stage = ITEM_DONE, None
        elif result.status in {"skipped_duplicate", "skipped_nonsemantic"}:
            # Deliberate terminal exclusions are not failures and must never
            # enter the retry queue.
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
        recoverable = _is_transient_store_exception(exc)
        await _set_item_phase(
            db,
            item_id,
            "failed",
            status=ITEM_FAILED_RECOVERABLE if recoverable else ITEM_FAILED,
            error=str(exc),
            failure_stage=(
                "transient_store_exception" if recoverable else "worker_exception"
            ),
            completed=not recoverable,
            release_lease=recoverable,
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
    if _batch_defer_summaries(batch):
        return None
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
    parent_rows = await db["parent_chunks"].find(
        q,
        {
            "_id": 0,
            "chunk_kind": 1,
            "summary": 1,
            "semantic_chunk_type": 1,
        },
    ).to_list(length=None)
    parents = len(parent_rows)
    try:
        from services.ingestion.section_classifier import ChunkKind, should_skip_ghost_b

        def _summary_required(parent: dict[str, Any]) -> bool:
            return not should_skip_ghost_b(
                str(parent.get("chunk_kind") or ChunkKind.BODY)
            )
    except Exception:  # noqa: BLE001 - reporting must not fail a batch
        def _summary_required(parent: dict[str, Any]) -> bool:
            return True

    required_parents = [parent for parent in parent_rows if _summary_required(parent)]
    skipped_parents = parents - len(required_parents)
    summarized_all = sum(
        1 for parent in parent_rows if str(parent.get("summary") or "").strip()
    )
    structured_all = sum(
        1
        for parent in parent_rows
        if str(parent.get("semantic_chunk_type") or "").strip()
    )
    summarized_required = sum(
        1 for parent in required_parents if str(parent.get("summary") or "").strip()
    )
    structured_required = sum(
        1
        for parent in required_parents
        if str(parent.get("semantic_chunk_type") or "").strip()
    )
    children = await db["chunks"].count_documents(q)
    promoted = await db["chunks"].count_documents(
        {**q, "promote_version": {"$exists": True}})
    verified = await db["documents"].count_documents(
        {**q, "write_state.verified": True})
    graph_docs = await db["documents"].find(
        q,
        {
            "_id": 0,
            "ghost_b_metrics": 1,
            "write_state.warnings": 1,
        },
    ).to_list(length=None)
    graph_requested = 0
    graph_extracted = 0
    graph_failed = 0
    graph_relations = 0
    graph_related_to = 0
    graph_validation_rejections = 0
    graph_docs_requested = 0
    graph_docs_partial = 0
    graph_docs_dead = 0
    lane_call_counts: dict[str, int] = {}
    provider_call_counts: dict[str, int] = {}
    model_call_counts: dict[str, int] = {}

    def _add_counts(target: dict[str, int], source: dict | None) -> None:
        for key, value in (source or {}).items():
            try:
                target[str(key)] = target.get(str(key), 0) + int(value or 0)
            except (TypeError, ValueError):
                continue

    for doc in graph_docs:
        metrics = doc.get("ghost_b_metrics") or {}
        try:
            requested = int(metrics.get("requested_chunks") or 0)
            extracted = int(metrics.get("extracted_chunks") or 0)
            failed = int(metrics.get("failed_chunks") or 0)
        except (TypeError, ValueError):
            requested = extracted = failed = 0
        graph_requested += requested
        graph_extracted += extracted
        graph_failed += failed
        graph_relations += int(metrics.get("relation_count") or 0)
        graph_related_to += int(metrics.get("related_to_count") or 0)
        graph_validation_rejections += int(
            metrics.get("validation_rejection_count") or 0
        )
        _add_counts(lane_call_counts, metrics.get("lane_call_counts"))
        _add_counts(provider_call_counts, metrics.get("provider_call_counts"))
        _add_counts(model_call_counts, metrics.get("model_call_counts"))
        if requested > 0:
            graph_docs_requested += 1
            if extracted <= 0:
                graph_docs_dead += 1
            elif extracted < requested or failed > 0:
                graph_docs_partial += 1

    summary_required_count = len(required_parents)
    summary_missing_required = max(summary_required_count - summarized_required, 0)
    summary_coverage_rate = (
        round(summarized_required / summary_required_count, 3)
        if summary_required_count
        else None
    )
    structure_rate = (
        round(structured_required / summary_required_count, 3)
        if summary_required_count
        else None
    )
    report = {
        "docs": len(doc_ids),
        "docs_verified": verified,
        "parents": parents,
        "parents_summary_required": summary_required_count,
        "parents_summary_skipped": skipped_parents,
        # Backward-compatible total count, including intentionally skipped parents.
        "parents_summarized": summarized_all,
        "parents_summary_required_summarized": summarized_required,
        "parents_summary_missing_required": summary_missing_required,
        "parents_structured": structured_all,
        "parents_summary_required_structured": structured_required,
        "summary_coverage_rate": summary_coverage_rate,
        "summary_fallback_rate": (
            round(1 - summary_coverage_rate, 3)
            if summary_coverage_rate is not None
            else None
        ),
        "summary_raw_missing_rate": (
            round(1 - (summarized_all / parents), 3) if parents else None
        ),
        "structure_rate": structure_rate,
        "children": children,
        "children_promoted": promoted,
        "ghost_b_requested_chunks": graph_requested,
        "ghost_b_extracted_chunks": graph_extracted,
        "ghost_b_failed_chunks": graph_failed,
        "ghost_b_success_rate": round(graph_extracted / graph_requested, 4)
        if graph_requested
        else None,
        "ghost_b_docs_requested": graph_docs_requested,
        "ghost_b_docs_partial": graph_docs_partial,
        "ghost_b_docs_dead": graph_docs_dead,
        "ghost_b_related_to_ratio": round(graph_related_to / graph_relations, 4)
        if graph_relations
        else 0.0,
        "ghost_b_validation_rejection_count": graph_validation_rejections,
        "ghost_b_lane_call_counts": lane_call_counts,
        "ghost_b_provider_call_counts": provider_call_counts,
        "ghost_b_model_call_counts": model_call_counts,
    }
    alerts = []
    if summary_required_count and summarized_required < summary_required_count * 0.5:
        alerts.append(
            "over half of summarizable parents have NO summary — model path degraded"
        )
    if summarized_required and structured_required < summarized_required * 0.5:
        alerts.append("summaries exist but structure rate <50% — JSON schema not honored")
    if graph_docs_dead:
        alerts.append(
            f"{graph_docs_dead} document(s) are graph-dead — extraction produced zero chunk results"
        )
    if graph_docs_partial:
        alerts.append(
            f"{graph_docs_partial} document(s) have partial graph extraction — backfill recommended"
        )
    if alerts:
        report["alerts"] = alerts
        report["alert"] = alerts[0]
    return report


async def _run_deferred_summary_backfill(
    *,
    db: AsyncIOMotorDatabase,
    ingestion_service: Any,
    batch: dict[str, Any],
) -> dict[str, Any] | None:
    """Drain the summary lane for a deferred-summary batch.

    RTX/cloud-assisted ingestion intentionally makes documents queryable and
    graph-promoted before spending time on parent-summary vectors. This hook
    keeps that speed contract while preventing "done but summary-dead" batches:
    after the file work drains, run one bounded, doc-scoped summary backfill for
    this batch's completed documents. It is best-effort and fully recorded.
    """

    settings = get_settings()
    if not bool(getattr(settings, "INGEST_DEFERRED_SUMMARY_BACKFILL_ENABLED", True)):
        return None
    if not _batch_defer_summaries(batch):
        return None

    options = batch.get("options") or {}
    if options.get("chunk_summarization") is False:
        return None

    try:
        config = await _build_item_config(
            ingestion_service=ingestion_service,
            corpus_id=str(batch["corpus_id"]),
            options=options,
        )
    except Exception as exc:  # noqa: BLE001 - summary backfill must not fail ingest
        logger.warning("deferred summary backfill config resolution failed: %s", exc)
        return None
    if not bool(getattr(config, "chunk_summarization", False)):
        return None

    doc_ids = [
        str(row["doc_id"])
        async for row in db[ITEMS].find(
            {
                "batch_id": batch["batch_id"],
                "status": ITEM_DONE,
                "doc_id": {"$exists": True, "$nin": [None, ""]},
            },
            {"_id": 0, "doc_id": 1},
        )
    ]
    doc_ids = sorted(set(doc_ids))
    if not doc_ids:
        return None

    limit = max(
        0,
        int(getattr(settings, "INGEST_DEFERRED_SUMMARY_BACKFILL_LIMIT", 2000)),
    )
    parent_batch = max(
        1,
        min(
            128,
            int(getattr(settings, "INGEST_DEFERRED_SUMMARY_BACKFILL_BATCH", 32)),
        ),
    )
    run_id = f"summary_backfill_after_batch_{batch['batch_id'][:8]}_{uuid.uuid4().hex[:8]}"
    now = _now()
    run_doc = {
        "run_id": run_id,
        "kind": "summary_backfill_after_batch",
        "status": "running",
        "corpus_id": batch["corpus_id"],
        "batch_id": batch["batch_id"],
        "doc_scope_count": len(doc_ids),
        "limit": limit,
        "batch": parent_batch,
        "updated_at": now,
    }
    await db["ingest_repair_runs"].update_one(
        {"run_id": run_id},
        {"$setOnInsert": {"created_at": now}, "$set": run_doc},
        upsert=True,
    )
    await db[BATCHES].update_one(
        {"batch_id": batch["batch_id"]},
        {
            "$set": {
                "summary_backfill_status": "running",
                "summary_backfill_run_id": run_id,
                "summary_backfill_started_at": now,
                "updated_at": now,
            }
        },
    )
    logger.info(
        "batch %s deferred summary backfill start docs=%d limit=%d batch=%d",
        batch["batch_id"][:8],
        len(doc_ids),
        limit,
        parent_batch,
    )
    try:
        result = await ingestion_service.backfill_parent_summaries(
            str(batch["corpus_id"]),
            user_id=str(batch.get("user_id") or ""),
            generate=limit != 0,
            index=True,
            limit=limit if limit != 0 else None,
            batch=parent_batch,
            doc_ids=doc_ids,
            index_existing_doc_summaries=True,
        )
        document_job_limit = min(max(len(doc_ids), 1), 500)
        document_plan = await ingestion_service.plan_summary_jobs(
            corpus_id=str(batch["corpus_id"]),
            user_id=str(batch.get("user_id") or "") or None,
            apply=True,
            limit=document_job_limit,
            kinds=["document_summary"],
        )
        document_run: dict[str, Any] = {
            "status": "empty",
            "claimed": 0,
            "counts": {},
        }
        if int(document_plan.get("planned") or 0) > 0:
            document_run = await ingestion_service.run_summary_jobs(
                corpus_id=str(batch["corpus_id"]),
                user_id=str(batch.get("user_id") or "") or None,
                limit=document_job_limit,
                statuses=["queued"],
                kinds=["document_summary"],
            )
        result["document_summary_jobs"] = {
            "plan": {
                "status": document_plan.get("status"),
                "planned": int(document_plan.get("planned") or 0),
                "counts": document_plan.get("counts") or {},
            },
            "run": {
                "status": document_run.get("status"),
                "claimed": int(document_run.get("claimed") or 0),
                "counts": document_run.get("counts") or {},
                "batch_reconciliation": document_run.get("batch_reconciliation")
                or {},
            },
        }
        status = "complete"
        if result.get("generation_errors") or result.get("status") not in {
            "healthy",
            "empty",
        }:
            status = "partial"
        if document_run.get("status") not in {"complete", "empty"} or any(
            int((document_run.get("counts") or {}).get(key) or 0) > 0
            for key in ("failed", "blocked_no_parent_summaries", "blocked_parent_summaries_incomplete")
        ):
            status = "partial"
        finished_at = _now()
        await db["ingest_repair_runs"].update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": status,
                    "result": result,
                    "updated_at": finished_at,
                    "completed_at": finished_at,
                }
            },
        )
        await db[BATCHES].update_one(
            {"batch_id": batch["batch_id"]},
            {
                "$set": {
                    "summary_backfill_status": status,
                    "summary_backfill_result": result,
                    "summary_backfill_completed_at": finished_at,
                    "updated_at": finished_at,
                }
            },
        )
        logger.info(
            "batch %s deferred summary backfill %s: generated=%s indexed=%s status=%s",
            batch["batch_id"][:8],
            status,
            result.get("generated"),
            result.get("indexed"),
            result.get("status"),
        )
        await reconcile_batch_enrichment_truth(
            db,
            batch_id=str(batch["batch_id"]),
            user_id=str(batch.get("user_id") or "") or None,
        )
        return result
    except Exception as exc:  # noqa: BLE001 - queryable ingest remains valid
        finished_at = _now()
        message = str(exc)[:1000]
        await db["ingest_repair_runs"].update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": "failed",
                    "error": message,
                    "updated_at": finished_at,
                    "completed_at": finished_at,
                }
            },
        )
        await db[BATCHES].update_one(
            {"batch_id": batch["batch_id"]},
            {
                "$set": {
                    "summary_backfill_status": "failed",
                    "summary_backfill_error": message,
                    "updated_at": finished_at,
                },
                "$addToSet": {
                    "warnings": f"Deferred summary backfill failed: {message}"
                },
            },
        )
        logger.warning(
            "batch %s deferred summary backfill failed: %s",
            batch["batch_id"][:8],
            message,
        )
        return {"status": "failed", "error": message}


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
    # thinking-mode empties, bad key) burned two full runs before this existed.
    # In safe-summary mode, failure defers summaries for this batch instead of
    # stopping extraction; strict mode preserves the old fail-fast behavior.
    if bool(getattr(get_settings(), "INGEST_PREFLIGHT_CANARY", True)):
        canary_err = await _preflight_summary_canary(db, batch)
        if canary_err:
            if bool(getattr(get_settings(), "INGEST_SAFE_SUMMARY_FAILURES", True)):
                reason = f"Preflight summary canary failed; summaries deferred: {canary_err}"
                await db[BATCHES].update_one(
                    {"batch_id": batch_id},
                    {
                        "$set": {
                            "options.defer_summaries": True,
                            "options.summary_preflight_failed": True,
                            "options.summary_preflight_error": canary_err,
                            "summary_pending_reason": reason,
                            "updated_at": _now(),
                        },
                        "$addToSet": {"warnings": reason},
                        "$unset": {"error": ""},
                    },
                )
                logger.warning(
                    "Batch %s preflight canary deferred summaries: %s",
                    batch_id[:8],
                    canary_err,
                )
                batch = await db[BATCHES].find_one(
                    {"batch_id": batch_id, "user_id": user_id}
                ) or batch
            else:
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
    settings = get_settings()
    sem = await _global_doc_semaphore(_global_doc_limit_for_batch(batch, settings))
    lease_seconds = max(60, int(get_settings().INGEST_STALE_JOB_MINUTES * 60))
    concurrency = _runtime_batch_concurrency(batch, settings)
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
            await refresh_batch_counts(db, batch_id, user_id=user_id)
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

    lifecycle_pool = await _batch_lifecycle_pool(
        ingestion_service=ingestion_service,
        corpus_id=batch["corpus_id"],
        options=batch.get("options") or {},
    )
    lifecycle_hold_acquired = False
    lifecycle_hold_id = f"batch:{batch_id}"
    lifecycle_purpose = f"batch:{batch_id[:8]}"
    try:
        if lifecycle_pool:
            from services.ingestion.model_lifecycle import acquire_model_lifecycle_hold

            try:
                await acquire_model_lifecycle_hold(
                    lifecycle_pool,
                    purpose=lifecycle_purpose,
                    hold_id=lifecycle_hold_id,
                )
                lifecycle_hold_acquired = True
            except Exception as exc:  # noqa: BLE001 - cloud lanes are independent
                logger.warning(
                    "batch lifecycle warmup unavailable; continuing with "
                    "provider-level failover batch=%s failure_class=%s",
                    batch_id[:8],
                    type(exc).__name__,
                )

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
        refreshed = await refresh_batch_counts(db, batch_id, user_id=user_id)
        if refreshed.get("status") in {BATCH_DONE, BATCH_PARTIAL}:
            await _run_deferred_summary_backfill(
                db=db,
                ingestion_service=ingestion_service,
                batch=refreshed,
            )
            refreshed = await refresh_batch_counts(db, batch_id, user_id=user_id)
        return refreshed
    finally:
        if lifecycle_hold_acquired:
            from services.ingestion.model_lifecycle import release_model_lifecycle_hold

            await release_model_lifecycle_hold(
                lifecycle_pool,
                purpose=lifecycle_purpose,
                hold_id=lifecycle_hold_id,
            )


def _runtime_batch_concurrency(batch: dict[str, Any], settings: Any) -> int:
    """Resolve actual worker count for a durable batch.

    ``INGEST_GLOBAL_MAX_DOCS`` is a cap across batches, not a floor. Forcing
    every resumed batch up to that value can lease more large books than the
    API container can hold, which turns startup recovery into an OOM loop.
    """

    options = batch.get("options") or {}
    configured = int(options.get("concurrency") or 0)
    requested = configured or int(getattr(settings, "INGEST_BATCH_WORKERS", 1))
    if str(options.get("profile") or "").strip().lower() == "rtx_assisted":
        remote_cap = _rtx_assisted_doc_cap(settings)
        return max(1, min(configured or max(requested, remote_cap), remote_cap))
    global_cap = max(1, int(getattr(settings, "INGEST_GLOBAL_MAX_DOCS", 1)))
    active_cap = max(1, int(getattr(settings, "INGEST_MAX_ACTIVE_JOBS", 1)))
    return max(1, min(requested, global_cap, active_cap))


def _rtx_assisted_doc_cap(settings: Any) -> int:
    return max(
        1,
        int(getattr(settings, "EXTRACTION_MANAGED_VLLM_MAX_ACTIVE_DOCS", 2)),
    )


def _global_doc_limit_for_batch(batch: dict[str, Any], settings: Any) -> int:
    options = batch.get("options") or {}
    if str(options.get("profile") or "").strip().lower() == "rtx_assisted":
        return _rtx_assisted_doc_cap(settings)
    return max(1, int(getattr(settings, "INGEST_GLOBAL_MAX_DOCS", 1)))


class _AdjustableDocSemaphore:
    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._active = 0
        self._cond = asyncio.Condition()

    async def set_limit(self, limit: int) -> None:
        new_limit = max(1, int(limit))
        async with self._cond:
            widened = new_limit > self._limit
            self._limit = new_limit
            if widened:
                self._cond.notify_all()

    async def __aenter__(self):
        async with self._cond:
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1
        return self

    async def __aexit__(self, *exc):
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()
        return False


_GLOBAL_DOC_SEM: "_AdjustableDocSemaphore | None" = None
_GLOBAL_DOC_SEM_SIZE: int = 0


async def _global_doc_semaphore(limit: int | None = None) -> "_AdjustableDocSemaphore":
    """Process-wide document-ingest semaphore, sized by
    INGEST_GLOBAL_MAX_DOCS (re-created if the knob changes)."""
    global _GLOBAL_DOC_SEM, _GLOBAL_DOC_SEM_SIZE
    size = max(
        1,
        int(
            limit
            if limit is not None
            else getattr(get_settings(), "INGEST_GLOBAL_MAX_DOCS", 2)
        ),
    )
    if _GLOBAL_DOC_SEM is None:
        _GLOBAL_DOC_SEM = _AdjustableDocSemaphore(size)
        _GLOBAL_DOC_SEM_SIZE = size
    elif size != _GLOBAL_DOC_SEM_SIZE:
        await _GLOBAL_DOC_SEM.set_limit(size)
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
            batch = await db[BATCHES].find_one(
                {"batch_id": batch_id, "user_id": user_id},
                {"_id": 0, "corpus_id": 1},
            )
            corpus_id = str((batch or {}).get("corpus_id") or "")
            if not corpus_id:
                return
            from services.ingestion.job_leases import corpus_lane_lease

            owner = f"batch:{batch_id}"
            async with AsyncExitStack() as stack:
                for lane in (
                    "source_parse",
                    "document_pipeline",
                    "extraction",
                    "summary",
                    "graph_promotion",
                ):
                    lease = await stack.enter_async_context(
                        corpus_lane_lease(
                            db,
                            corpus_id=corpus_id,
                            lane=lane,
                            owner=owner,
                        )
                    )
                    if not lease:
                        logger.info(
                            "batch %s deferred because corpus lane %s is owned",
                            batch_id[:8],
                            lane,
                        )
                        return
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
