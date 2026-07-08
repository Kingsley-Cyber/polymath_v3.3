#!/usr/bin/env python3
"""Plan or run bounded Ghost B full-replay graph repair for a corpus.

This is the operator-safe wrapper for the "Qdrant written, Neo4j missing"
state: documents are already parsed/chunked/vectorized, but have no staged
Ghost B rows to flush. The only honest repair is a full Ghost B replay from
Mongo child chunks followed by a normal Neo4j graph write.

Defaults are intentionally conservative:
  - dry-run unless --apply is passed
  - smallest documents first
  - bounded doc count
  - refuses to run while durable ingest batches are active unless forced

Example:
  python backend/scripts/polymath_graph_replay_backlog.py \
    --corpus-id 999b5934-272e-4f20-a538-b5d422249a05 --limit 5

  docker exec -w /app polymath_v33-ingest-worker-1 python \
    scripts/polymath_graph_replay_backlog.py --corpus-id ... --limit 2 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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

log = logging.getLogger("polymath_graph_replay_backlog")


DEFAULT_POLYMATH_V2_CORPUS_ID = "999b5934-272e-4f20-a538-b5d422249a05"
ACTIVE_BATCH_STATUSES = {"queued", "running"}
GRAPH_VERIFY_PATTERN = "neo4j|HAS_CHUNK"


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
    rows = await db["ingest_batches"].find(
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
    return rows


async def _chunk_count(db: Any, *, corpus_id: str, doc_id: str) -> int:
    return await db["chunks"].count_documents(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )


async def _parent_count(db: Any, *, corpus_id: str, doc_id: str) -> int:
    return await db["parent_chunks"].count_documents(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )


async def _row_count(db: Any, collection: str, *, corpus_id: str, doc_id: str) -> int:
    if collection not in await db.list_collection_names():
        return 0
    return await db[collection].count_documents(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )


def _graph_gap_reason(row: dict[str, Any]) -> str | None:
    write_state = row.get("write_state") or {}
    if write_state.get("neo4j_written") is not True:
        return "neo4j_missing"
    if write_state.get("verified") is True:
        return None
    for raw in write_state.get("verify_errors") or []:
        text = str(raw).lower()
        if "neo4j" in text or "has_chunk" in text:
            return "neo4j_verify_mismatch"
    return None


async def _candidate_rows(
    db: Any,
    *,
    corpus_id: str,
    limit: int,
    max_chunks: int | None,
    largest_first: bool,
) -> list[dict[str, Any]]:
    cursor = db["documents"].find(
        {
            "corpus_id": corpus_id,
            "write_state.qdrant_written": True,
            "$or": [
                {"write_state.neo4j_written": {"$ne": True}},
                {
                    "write_state.verified": {"$ne": True},
                    "write_state.verify_errors": {
                        "$regex": GRAPH_VERIFY_PATTERN,
                        "$options": "i",
                    },
                },
            ],
        },
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "filename": 1,
            "user_id": 1,
            "ingest_stage": 1,
            "summary_count": 1,
            "ghost_b_failure_count": 1,
            "write_state": 1,
            "updated_at": 1,
        },
    )
    rows = await cursor.to_list(length=None)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            continue
        reason = _graph_gap_reason(row)
        if reason is None:
            continue
        row["graph_gap_reason"] = reason
        row.pop("write_state", None)
        child_chunks = await _chunk_count(db, corpus_id=corpus_id, doc_id=doc_id)
        if max_chunks is not None and child_chunks > max_chunks:
            continue
        row["child_chunks"] = child_chunks
        row["parents"] = await _parent_count(db, corpus_id=corpus_id, doc_id=doc_id)
        row["staged_extractions"] = await _row_count(
            db, "ghost_b_extractions", corpus_id=corpus_id, doc_id=doc_id
        )
        row["failure_rows"] = await _row_count(
            db, "ghost_b_failures", corpus_id=corpus_id, doc_id=doc_id
        )
        enriched.append(row)

    enriched.sort(
        key=lambda item: (
            int(item.get("child_chunks") or 0),
            str(item.get("filename") or ""),
        ),
        reverse=largest_first,
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
    doc = {
        "run_id": run_id,
        "kind": "neo4j_full_replay_scoped",
        "status": status,
        "corpus_id": corpus_id,
        "apply": apply,
        "counts": {
            "planned": len(plan),
            "queued": len(plan),
            "running": 0,
            "done": 0,
            "failed": 0,
            "noop": 0,
        },
        "planned_docs": [
            {
                "doc_id": row.get("doc_id"),
                "filename": row.get("filename"),
                "child_chunks": row.get("child_chunks"),
                "parents": row.get("parents"),
                "graph_gap_reason": row.get("graph_gap_reason"),
                "staged_extractions": row.get("staged_extractions"),
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
    run_id = args.run_id or f"graph_replay_scoped_{datetime.utcnow():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"

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
            max_chunks=args.max_chunks,
            largest_first=args.largest_first,
        )
        summary = {
            "run_id": run_id,
            "apply": args.apply,
            "corpus_id": args.corpus_id,
            "planned_docs": len(plan),
            "planned_child_chunks": sum(int(row.get("child_chunks") or 0) for row in plan),
            "planned_parents": sum(int(row.get("parents") or 0) for row in plan),
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
            "noop": 0,
        }
        for index, row in enumerate(plan, start=1):
            current = {
                "index": index,
                "doc_id": row["doc_id"],
                "filename": row.get("filename"),
                "child_chunks": row.get("child_chunks"),
                "parents": row.get("parents"),
            }
            counts["queued"] -= 1
            counts["running"] = 1
            await _update_run_progress(
                db,
                run_id=run_id,
                counts=counts,
                current=current,
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
                if result.get("status") == "noop":
                    counts["noop"] += 1
                else:
                    counts["done"] += 1
                await _update_run_progress(
                    db,
                    run_id=run_id,
                    counts=counts,
                    current=current,
                    result=result,
                )
                log.info("doc repaired: %s %s", row.get("filename"), result)
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
                log.exception("doc repair failed: %s", row.get("filename"))
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
        description="Plan/run scoped Neo4j full replay for qdrant-written docs missing graph promotion.",
    )
    parser.add_argument("--corpus-id", default=DEFAULT_POLYMATH_V2_CORPUS_ID)
    parser.add_argument("--limit", type=int, default=5, help="Maximum docs to plan/run.")
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Skip docs larger than this child-chunk count.",
    )
    parser.add_argument(
        "--largest-first",
        action="store_true",
        help="Process largest eligible docs first instead of smallest first.",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--apply", action="store_true", help="Actually run the repair.")
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
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
