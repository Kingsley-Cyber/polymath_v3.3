#!/usr/bin/env python3
"""Plan or run bounded Ghost B failed-chunk retries for a corpus.

This is the operator-safe wrapper for documents that already landed in Mongo,
Qdrant, and usually Neo4j, but still have ``ghost_b_failure_count > 0``. It
retries only the failed chunks and lets the existing graph backfill service
patch recovered results into Neo4j.

Defaults are conservative:
  - dry-run unless --apply is passed
  - bounded document count
  - largest failure counts first
  - refuses to run while durable ingest batches are active unless forced

The underlying service reads corpus/provider configuration and decrypts stored
keys internally. This script never prints or stores provider secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient

from config import get_settings
from services.ingestion.graph_backfill import backfill_failed_graph_chunks

log = logging.getLogger("polymath_failed_chunk_backfill")


DEFAULT_POLYMATH_V2_CORPUS_ID = "999b5934-272e-4f20-a538-b5d422249a05"
ACTIVE_BATCH_STATUSES = {"queued", "running"}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _mongo_db() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[settings.MONGODB_DATABASE]
    return client, db


def _qdrant_client() -> AsyncQdrantClient:
    settings = get_settings()
    return AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )


def _neo4j_driver() -> Any:
    settings = get_settings()
    if not settings.NEO4J_ENABLED:
        raise RuntimeError("NEO4J_ENABLED is false")
    if not settings.NEO4J_PASSWORD:
        raise RuntimeError("NEO4J_PASSWORD is not configured")
    return AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )


async def _active_batches(db: Any) -> list[dict[str, Any]]:
    return await db["ingest_batches"].find(
        {"status": {"$in": sorted(ACTIVE_BATCH_STATUSES)}},
        {
            "_id": 0,
            "batch_id": 1,
            "corpus_id": 1,
            "status": 1,
            "root_path": 1,
            "counts": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1).to_list(length=20)


async def _chunk_count(db: Any, *, corpus_id: str, doc_id: str) -> int:
    return await db["chunks"].count_documents(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )


async def _failure_row_count(db: Any, *, corpus_id: str, doc_id: str) -> int:
    if "ghost_b_extractions" not in await db.list_collection_names():
        return 0
    return await db["ghost_b_extractions"].count_documents(
        {"corpus_id": corpus_id, "doc_id": doc_id, "status": "error"}
    )


async def _candidate_rows(
    db: Any,
    *,
    corpus_id: str,
    limit: int,
    doc_ids: list[str] | None = None,
    max_failed_chunks: int | None = None,
    smallest_first: bool = False,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "ghost_b_failure_count": {"$gt": 0},
    }
    if doc_ids:
        query["doc_id"] = {"$in": doc_ids}

    rows = await db["documents"].find(
        query,
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "filename": 1,
            "ingest_stage": 1,
            "ghost_b_failure_count": 1,
            "ghost_b_staging_count": 1,
            "write_state.neo4j_written": 1,
            "write_state.verified": 1,
            "updated_at": 1,
        },
    ).to_list(length=None)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            continue
        failures = int(row.get("ghost_b_failure_count") or 0)
        if max_failed_chunks is not None and failures > max_failed_chunks:
            continue
        row["child_chunks"] = await _chunk_count(
            db, corpus_id=corpus_id, doc_id=doc_id
        )
        row["failure_rows"] = await _failure_row_count(
            db, corpus_id=corpus_id, doc_id=doc_id
        )
        row.pop("write_state", None)
        enriched.append(row)

    enriched.sort(
        key=lambda item: (
            int(item.get("ghost_b_failure_count") or 0),
            int(item.get("child_chunks") or 0),
            str(item.get("filename") or ""),
        ),
        reverse=not smallest_first,
    )
    return enriched[: max(1, limit)]


async def _write_run_record(
    db: Any,
    *,
    run_id: str,
    status: str,
    corpus_id: str,
    plan: list[dict[str, Any]],
    apply: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    now = datetime.utcnow()
    doc: dict[str, Any] = {
        "run_id": run_id,
        "kind": "failed_chunk_retry_scoped",
        "status": status,
        "corpus_id": corpus_id,
        "apply": apply,
        "counts": {
            "planned": len(plan),
            "queued": len(plan),
            "running": 0,
            "done": 0,
            "failed": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": sum(
                int(row.get("ghost_b_failure_count") or 0) for row in plan
            ),
        },
        "planned_docs": [
            {
                "doc_id": row.get("doc_id"),
                "filename": row.get("filename"),
                "child_chunks": row.get("child_chunks"),
                "failed_chunks": row.get("ghost_b_failure_count"),
                "staged_extractions": row.get("ghost_b_staging_count"),
                "failure_rows": row.get("failure_rows"),
            }
            for row in plan
        ],
        "created_at": now,
        "updated_at": now,
    }
    if extra:
        doc.update(extra)
    await db["ingest_repair_runs"].update_one(
        {"run_id": run_id},
        {"$set": doc},
        upsert=True,
    )


async def _update_run_progress(
    db: Any,
    *,
    run_id: str,
    counts: dict[str, int],
    current: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    status: str = "running",
) -> None:
    update: dict[str, Any] = {
        "status": status,
        "counts": counts,
        "updated_at": datetime.utcnow(),
    }
    if current is not None:
        update["current"] = current
    push: dict[str, Any] = {}
    if result is not None:
        push["results"] = result
    if failure is not None:
        push["failures"] = failure
    op: dict[str, Any] = {"$set": update}
    if push:
        op["$push"] = push
    await db["ingest_repair_runs"].update_one({"run_id": run_id}, op)


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    mongo_client, db = _mongo_db()
    qdrant = None
    neo4j = None
    run_id = (
        args.run_id
        or f"failed_chunk_retry_{datetime.utcnow():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    )
    try:
        active = await _active_batches(db)
        if active and args.apply and not args.force_active_ingest:
            print(json.dumps({
                "status": "refused_active_ingest",
                "reason": "active ingest batches exist; rerun with --force-active-ingest to override",
                "active_batches": active,
            }, default=_json_default, indent=2))
            return 3

        plan = await _candidate_rows(
            db,
            corpus_id=args.corpus_id,
            limit=args.limit,
            doc_ids=args.doc_id,
            max_failed_chunks=args.max_failed_chunks,
            smallest_first=args.smallest_first,
        )
        summary = {
            "run_id": run_id,
            "apply": args.apply,
            "corpus_id": args.corpus_id,
            "planned_docs": len(plan),
            "planned_failed_chunks": sum(
                int(row.get("ghost_b_failure_count") or 0) for row in plan
            ),
            "active_ingest_batches": len(active),
            "docs": plan,
        }
        print(json.dumps(summary, default=_json_default, indent=2))

        if not args.apply:
            await _write_run_record(
                db,
                run_id=run_id,
                status="dry_run",
                corpus_id=args.corpus_id,
                plan=plan,
                apply=False,
                extra={"active_batches_at_plan": active},
            )
            return 0

        if not plan:
            await _write_run_record(
                db,
                run_id=run_id,
                status="noop",
                corpus_id=args.corpus_id,
                plan=[],
                apply=True,
                extra={"active_batches_at_start": active},
            )
            return 0

        await _write_run_record(
            db,
            run_id=run_id,
            status="running",
            corpus_id=args.corpus_id,
            plan=plan,
            apply=True,
            extra={"active_batches_at_start": active},
        )
        qdrant = _qdrant_client()
        neo4j = _neo4j_driver()

        counts = {
            "planned": len(plan),
            "queued": len(plan),
            "running": 0,
            "done": 0,
            "failed": 0,
            "recovered_chunks": 0,
            "remaining_failed_chunks": sum(
                int(row.get("ghost_b_failure_count") or 0) for row in plan
            ),
        }
        for index, row in enumerate(plan, start=1):
            current = {
                "index": index,
                "doc_id": row["doc_id"],
                "filename": row.get("filename"),
                "failed_chunks": row.get("ghost_b_failure_count"),
                "child_chunks": row.get("child_chunks"),
            }
            counts["queued"] -= 1
            counts["running"] = 1
            await _update_run_progress(
                db, run_id=run_id, counts=counts, current=current
            )
            try:
                result = await backfill_failed_graph_chunks(
                    db=db,
                    qdrant_client=qdrant,
                    neo4j_driver=neo4j,
                    corpus_id=args.corpus_id,
                    doc_id=row["doc_id"],
                    user_id=str(row.get("user_id") or ""),
                )
                counts["running"] = 0
                counts["done"] += 1
                counts["recovered_chunks"] += int(result.get("recovered_chunks") or 0)
                counts["remaining_failed_chunks"] = max(
                    counts["remaining_failed_chunks"]
                    - int(row.get("ghost_b_failure_count") or 0)
                    + int(result.get("remaining_failed_chunks") or 0),
                    0,
                )
                await _update_run_progress(
                    db,
                    run_id=run_id,
                    counts=counts,
                    current=current,
                    result=result,
                )
                log.info("doc retry complete: %s %s", row.get("filename"), result)
            except Exception as exc:  # noqa: BLE001 - operator script records exact failure
                counts["running"] = 0
                counts["failed"] += 1
                failure = {
                    "doc_id": row.get("doc_id"),
                    "filename": row.get("filename"),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
                await _update_run_progress(
                    db,
                    run_id=run_id,
                    counts=counts,
                    current=current,
                    failure=failure,
                )
                log.exception("doc retry failed: %s", row.get("filename"))
                if not args.continue_on_error:
                    break

        final_status = "complete" if counts["failed"] == 0 else "partial"
        await _update_run_progress(
            db,
            run_id=run_id,
            counts=counts,
            current=None,
            status=final_status,
        )
        print(json.dumps({"run_id": run_id, "status": final_status, "counts": counts}, indent=2))
        return 0 if counts["failed"] == 0 else 1
    finally:
        if qdrant is not None:
            await qdrant.close()
        if neo4j is not None:
            await neo4j.close()
        mongo_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan/run scoped Ghost B failed-chunk retry for a corpus.",
    )
    parser.add_argument("--corpus-id", default=DEFAULT_POLYMATH_V2_CORPUS_ID)
    parser.add_argument("--limit", type=int, default=10, help="Maximum docs to plan/run.")
    parser.add_argument("--doc-id", action="append", help="Retry a specific doc_id. May be repeated.")
    parser.add_argument(
        "--max-failed-chunks",
        type=int,
        default=None,
        help="Skip docs with more failed chunks than this count.",
    )
    parser.add_argument(
        "--smallest-first",
        action="store_true",
        help="Process smallest failure counts first instead of largest first.",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--apply", action="store_true", help="Actually run the retry.")
    parser.add_argument(
        "--force-active-ingest",
        action="store_true",
        help="Allow --apply even while queued/running ingest batches exist.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep processing planned docs after a document-level failure.",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
