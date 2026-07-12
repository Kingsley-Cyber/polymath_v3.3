#!/usr/bin/env python3
"""Backfill the corpus vocabulary bridge from durable extraction artifacts.

Dry-run is the default. ``--apply`` refreshes document source contributions,
materializes the Mongo corpus lexicon, then mirrors plain-language glosses into
the isolated Qdrant schemas collection. No document parsing or extraction is
performed and no provider credential is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient

from config import get_settings
from services.ingestion.corpus_lexicon import (
    LEXICON_COLLECTION,
    LEXICON_RUN_COLLECTION,
    LEXICON_SOURCE_COLLECTION,
    ensure_lexicon_indexes,
    finalize_corpus_lexicon_index,
    index_corpus_lexicon,
    index_corpus_lexicon_slice,
    materialize_corpus_lexicon,
    refresh_corpus_lexicon_glosses,
    refresh_document_lexicon_sources,
)
from services.storage.record_status import with_active_records

log = logging.getLogger("backfill_corpus_lexicon")
RUN_COLLECTION = LEXICON_RUN_COLLECTION
ACTIVE_BATCH_STATUSES = ("queued", "running")
RUNNING_JOB_COLLECTIONS = (
    "source_parse_jobs",
    "document_pipeline_jobs",
    "extraction_jobs",
    "summary_jobs",
    "graph_promotion_jobs",
)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def _active_work(db: Any, corpus_ids: list[str]) -> dict[str, int]:
    active: dict[str, int] = {}
    active["lexicon_backfill_runs"] = await db[RUN_COLLECTION].count_documents(
        {
            "corpus_ids": {"$in": corpus_ids},
            "status": "running",
        }
    )
    active["ingest_batches"] = await db["ingest_batches"].count_documents(
        {
            "corpus_id": {"$in": corpus_ids},
            "status": {"$in": list(ACTIVE_BATCH_STATUSES)},
        }
    )
    for collection_name in RUNNING_JOB_COLLECTIONS:
        active[collection_name] = await db[collection_name].count_documents(
            {
                "corpus_id": {"$in": corpus_ids},
                "status": "running",
            }
        )
    active["repair_runs"] = await db["ingest_repair_runs"].count_documents(
        {"corpus_id": {"$in": corpus_ids}, "status": "running"}
    )
    return {key: value for key, value in active.items() if value}


async def _plan(db: Any, corpus_id: str) -> dict[str, Any]:
    document_count = await db["documents"].count_documents(
        with_active_records({"corpus_id": corpus_id})
    )
    extraction_docs = len(
        await db["ghost_b_extractions"].distinct(
            "doc_id", {"corpus_id": corpus_id, "status": "ok"}
        )
    )
    source_entries = await db[LEXICON_SOURCE_COLLECTION].count_documents(
        {"corpus_id": corpus_id}
    )
    source_docs = len(
        await db[LEXICON_SOURCE_COLLECTION].distinct("doc_id", {"corpus_id": corpus_id})
    )
    lexicon_entries = await db[LEXICON_COLLECTION].count_documents(
        {"corpus_id": corpus_id}
    )
    processed_documents = await db["documents"].count_documents(
        {
            "$and": [
                *with_active_records({"corpus_id": corpus_id})["$and"],
                {
                    "lexicon_state": {
                        "$in": [
                            "lexicon_pending",
                            "lexicon_materialized",
                            "lexicon_ready",
                        ]
                    }
                },
            ]
        }
    )
    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {
            "_id": 0,
            "name": 1,
            "lexicon_state": 1,
            "lexicon_version": 1,
            "lexicon_entry_count": 1,
        },
    )
    return {
        "corpus_id": corpus_id,
        "corpus_name": (corpus or {}).get("name"),
        "documents": document_count,
        "documents_with_extractions": extraction_docs,
        "source_documents": source_docs,
        "processed_documents": processed_documents,
        "source_entries": source_entries,
        "lexicon_entries": lexicon_entries,
        "lexicon_state": (corpus or {}).get("lexicon_state"),
        "remaining_documents": max(document_count - processed_documents, 0),
    }


async def run(
    *,
    corpus_ids: list[str],
    apply: bool,
    doc_limit: int,
    resume_after_doc_id: str | None,
    skip_vector_index: bool,
    source_only: bool,
    materialize_only: bool,
    gloss_only: bool,
    index_only: bool,
    finalize_index: bool,
    index_limit: int,
    materialize_key_batch_size: int,
    resume_after_lexicon_id: str | None,
    force_active: bool,
    run_id: str,
    embed_batch_size: int,
) -> dict[str, Any]:
    settings = get_settings()
    mongo_client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = mongo_client.get_default_database()
    except Exception:
        db = mongo_client[settings.MONGODB_DATABASE]
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )
    now = datetime.now(timezone.utc)
    try:
        before = [await _plan(db, corpus_id) for corpus_id in corpus_ids]
        if not apply:
            return {
                "apply": False,
                "run_id": run_id,
                "corpora": before,
                "active_work": await _active_work(db, corpus_ids),
            }

        await ensure_lexicon_indexes(db)

        active = await _active_work(db, corpus_ids)
        if active and not force_active:
            raise RuntimeError(
                "selected corpora have active ingestion/repair work; rerun after it "
                f"settles or explicitly pass --force-active: {active}"
            )
        await db[RUN_COLLECTION].replace_one(
            {"run_id": run_id},
            {
                "run_id": run_id,
                "schema_version": "lexicon_backfill_run.v1",
                "corpus_ids": corpus_ids,
                "status": "running",
                "doc_limit": doc_limit,
                "resume_after_doc_id": resume_after_doc_id,
                "skip_vector_index": skip_vector_index,
                "source_only": source_only,
                "materialize_only": materialize_only,
                "gloss_only": gloss_only,
                "index_only": index_only,
                "finalize_index": finalize_index,
                "index_limit": index_limit,
                "materialize_key_batch_size": materialize_key_batch_size,
                "resume_after_lexicon_id": resume_after_lexicon_id,
                "started_at": now,
                "updated_at": now,
                "progress": {},
            },
            upsert=True,
        )
        results: list[dict[str, Any]] = []
        try:
            for corpus_id in corpus_ids:
                scanned = source_entries = 0
                last_doc_id = None
                if gloss_only:
                    result = await refresh_corpus_lexicon_glosses(
                        db,
                        corpus_id=corpus_id,
                    )
                    results.append(result)
                    await db[RUN_COLLECTION].update_one(
                        {"run_id": run_id},
                        {
                            "$set": {
                                f"progress.{corpus_id}": result,
                                "updated_at": datetime.now(timezone.utc),
                            }
                        },
                    )
                    continue
                if index_only:

                    async def persist_index_progress(
                        progress: dict[str, Any],
                        *,
                        selected_corpus_id: str = corpus_id,
                    ) -> None:
                        await db[RUN_COLLECTION].update_one(
                            {"run_id": run_id},
                            {
                                "$set": {
                                    f"progress.{selected_corpus_id}": {
                                        "corpus_id": selected_corpus_id,
                                        **progress,
                                    },
                                    "updated_at": datetime.now(timezone.utc),
                                }
                            },
                        )

                    result = await index_corpus_lexicon_slice(
                        db,
                        qdrant,
                        corpus_id=corpus_id,
                        resume_after_lexicon_id=resume_after_lexicon_id,
                        limit=index_limit,
                        batch_size=embed_batch_size,
                        progress_callback=persist_index_progress,
                    )
                    results.append(result)
                    await db[RUN_COLLECTION].update_one(
                        {"run_id": run_id},
                        {
                            "$set": {
                                f"progress.{corpus_id}": result,
                                "updated_at": datetime.now(timezone.utc),
                            }
                        },
                    )
                    continue
                if finalize_index:
                    result = await finalize_corpus_lexicon_index(
                        db,
                        qdrant,
                        corpus_id=corpus_id,
                    )
                    results.append(result)
                    await db[RUN_COLLECTION].update_one(
                        {"run_id": run_id},
                        {
                            "$set": {
                                f"progress.{corpus_id}": result,
                                "updated_at": datetime.now(timezone.utc),
                            }
                        },
                    )
                    continue
                if not materialize_only:
                    query: dict[str, Any] = with_active_records(
                        {"corpus_id": corpus_id}
                    )
                    if resume_after_doc_id:
                        query = {
                            "$and": [
                                query,
                                {"doc_id": {"$gt": resume_after_doc_id}},
                            ]
                        }
                    cursor = (
                        db["documents"]
                        .find(
                            query,
                            {"_id": 0, "doc_id": 1},
                        )
                        .sort("doc_id", 1)
                    )
                    if doc_limit > 0:
                        cursor = cursor.limit(doc_limit)
                    async for row in cursor:
                        doc_id = str(row.get("doc_id") or "")
                        if not doc_id:
                            continue
                        refreshed = await refresh_document_lexicon_sources(
                            db,
                            corpus_id=corpus_id,
                            doc_id=doc_id,
                        )
                        scanned += 1
                        source_entries += int(refreshed["source_entries"])
                        last_doc_id = doc_id
                        if scanned % 5 == 0:
                            await db[RUN_COLLECTION].update_one(
                                {"run_id": run_id},
                                {
                                    "$set": {
                                        f"progress.{corpus_id}": {
                                            "phase": "source_projection",
                                            "documents": scanned,
                                            "source_entries": source_entries,
                                            "last_doc_id": last_doc_id,
                                        },
                                        "updated_at": datetime.now(timezone.utc),
                                    }
                                },
                            )
                            log.info(
                                "corpus=%s documents=%d source_entries=%d last_doc=%s",
                                corpus_id[:8],
                                scanned,
                                source_entries,
                                doc_id[:12],
                            )
                if source_only:
                    result = {
                        "corpus_id": corpus_id,
                        "phase": "source_projection",
                        "documents_refreshed": scanned,
                        "source_entries_refreshed": source_entries,
                        "last_doc_id": last_doc_id,
                        "materialized_entries": None,
                        "indexed": {"indexed": 0, "skipped": True},
                        "next_action": "run another source slice or --materialize-only",
                    }
                    results.append(result)
                    await db[RUN_COLLECTION].update_one(
                        {"run_id": run_id},
                        {
                            "$set": {
                                f"progress.{corpus_id}": result,
                                "updated_at": datetime.now(timezone.utc),
                            }
                        },
                    )
                    continue
                async def persist_materialize_progress(
                    progress: dict[str, Any],
                    *,
                    selected_corpus_id: str = corpus_id,
                ) -> None:
                    await db[RUN_COLLECTION].update_one(
                        {"run_id": run_id},
                        {
                            "$set": {
                                f"progress.{selected_corpus_id}": {
                                    "corpus_id": selected_corpus_id,
                                    **progress,
                                },
                                "updated_at": datetime.now(timezone.utc),
                            }
                        },
                    )

                materialized = await materialize_corpus_lexicon(
                    db,
                    corpus_id=corpus_id,
                    materialization_id=run_id,
                    key_batch_size=materialize_key_batch_size,
                    progress_callback=persist_materialize_progress,
                )
                indexed: dict[str, Any] = {"indexed": 0, "skipped": True}
                if not skip_vector_index:
                    indexed = await index_corpus_lexicon(
                        db,
                        qdrant,
                        corpus_id=corpus_id,
                        entries=None,
                        batch_size=embed_batch_size,
                    )
                result = {
                    "corpus_id": corpus_id,
                    "documents_refreshed": scanned,
                    "source_entries_refreshed": source_entries,
                    "last_doc_id": last_doc_id,
                    "materialized_entries": materialized["lexicon_entries"],
                    "coverage": materialized["coverage"],
                    "indexed": indexed,
                }
                results.append(result)
                await db[RUN_COLLECTION].update_one(
                    {"run_id": run_id},
                    {
                        "$set": {
                            f"progress.{corpus_id}": result,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
        except BaseException as exc:
            interrupted = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            await db[RUN_COLLECTION].update_one(
                {"run_id": run_id},
                {
                    "$set": {
                        "status": "cancelled" if interrupted else "failed",
                        "completion_reason": (
                            "operator_interrupted"
                            if interrupted
                            else "backfill_exception"
                        ),
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                        "completed_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            raise

        after = [await _plan(db, corpus_id) for corpus_id in corpus_ids]
        await db[RUN_COLLECTION].update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": "complete",
                    "results": results,
                    "completed_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return {
            "apply": True,
            "run_id": run_id,
            "before": before,
            "results": results,
            "after": after,
        }
    finally:
        await qdrant.close()
        mongo_client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-id", action="append", dest="corpus_ids", required=True
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--doc-limit", type=int, default=0, help="0 means all active documents"
    )
    parser.add_argument("--resume-after-doc-id")
    parser.add_argument("--skip-vector-index", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--source-only",
        action="store_true",
        help=(
            "Refresh only document-scoped source projections. Use this for "
            "bounded slices, then run --materialize-only once coverage is complete."
        ),
    )
    mode.add_argument(
        "--materialize-only",
        action="store_true",
        help="Skip document scanning and reconcile/index the existing source projection.",
    )
    mode.add_argument(
        "--gloss-only",
        action="store_true",
        help=(
            "Stream existing materialized concepts and rebuild only contextual, "
            "embedding, utility, and retrieval gloss fields."
        ),
    )
    mode.add_argument(
        "--index-only",
        action="store_true",
        help=(
            "Index one bounded keyset slice from the materialized Mongo lexicon. "
            "Repeat with --resume-after-lexicon-id, then run --finalize-index."
        ),
    )
    mode.add_argument(
        "--finalize-index",
        action="store_true",
        help=(
            "Require exact Mongo/Qdrant eligible-ID parity, delete stale points, "
            "and only then publish lexicon readiness."
        ),
    )
    parser.add_argument(
        "--index-limit",
        type=int,
        default=10_000,
        help="Maximum entries in one --index-only keyset slice (1-50000).",
    )
    parser.add_argument(
        "--materialize-key-batch-size",
        type=int,
        default=2_000,
        help=(
            "Maximum canonical identity seeds per bounded materialization slice "
            "(1-5000). Alias-connected rows are always kept together."
        ),
    )
    parser.add_argument("--resume-after-lexicon-id")
    parser.add_argument("--force-active", action="store_true")
    parser.add_argument(
        "--embed-batch-size",
        type=int,
        default=512,
        help=(
            "Delta scan window (1-2048). The embedding client still enforces "
            "its provider-specific microbatch ceiling."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=f"corpus_lexicon_backfill_{uuid.uuid4().hex[:12]}",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Operator log verbosity; JSON results are always printed.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level)),
        format="%(levelname)s %(message)s",
    )
    result = await run(
        corpus_ids=list(dict.fromkeys(args.corpus_ids)),
        apply=bool(args.apply),
        doc_limit=max(0, int(args.doc_limit)),
        resume_after_doc_id=args.resume_after_doc_id,
        skip_vector_index=bool(args.skip_vector_index),
        source_only=bool(args.source_only),
        materialize_only=bool(args.materialize_only),
        gloss_only=bool(args.gloss_only),
        index_only=bool(args.index_only),
        finalize_index=bool(args.finalize_index),
        index_limit=max(1, min(int(args.index_limit), 50_000)),
        materialize_key_batch_size=max(
            1, min(int(args.materialize_key_batch_size), 5_000)
        ),
        resume_after_lexicon_id=args.resume_after_lexicon_id,
        force_active=bool(args.force_active),
        run_id=str(args.run_id),
        embed_batch_size=max(1, min(int(args.embed_batch_size), 2_048)),
    )
    print(json.dumps(result, default=_json_default, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
