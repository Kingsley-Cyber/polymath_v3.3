#!/usr/bin/env python3
"""Plan or run bounded parent-summary backfill for an existing corpus.

This is the operator-safe wrapper around
``IngestionService.backfill_parent_summaries``. It exists for large legacy
corpora where "complete" can hide a mostly empty parent-summary retrieval tier.

Defaults are conservative:
  - dry-run unless --apply is passed
  - retrieval-required parent rows only, matching the production Ghost A contract
  - bounded parent count
  - refuses to run while durable ingest batches are active unless forced

The underlying service reads the corpus/runtime summary model pool and decrypts
stored keys internally. This script never prints or stores provider secrets.
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
from qdrant_client import AsyncQdrantClient

from config import get_settings
from services.ingestion.section_classifier import parent_summary_required_clause
from services.ingestion_service import IngestionService
from services.settings import settings_service

log = logging.getLogger("polymath_summary_backfill_scoped")


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


def _retrieval_parent_clause() -> dict[str, Any]:
    return parent_summary_required_clause()


def _missing_summary_clause() -> dict[str, Any]:
    return {
        "$or": [
            {"summary": {"$exists": False}},
            {"summary": None},
            {"summary": ""},
        ]
    }


def _summary_text_clause() -> dict[str, Any]:
    return {"summary": {"$exists": True, "$nin": [None, ""]}}


def _parent_query(corpus_id: str, *clauses: dict[str, Any]) -> dict[str, Any]:
    return {"corpus_id": corpus_id, "$and": [_retrieval_parent_clause(), *clauses]}


def _body_parent_query(corpus_id: str, *clauses: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {"corpus_id": corpus_id, "chunk_kind": "body"}
    if clauses:
        query["$and"] = list(clauses)
    return query


async def _summary_plan(db: Any, *, corpus_id: str, limit: int) -> dict[str, Any]:
    retrieval_parent_count = await db["parent_chunks"].count_documents(
        _parent_query(corpus_id)
    )
    body_parent_count = await db["parent_chunks"].count_documents(
        _body_parent_query(corpus_id)
    )
    with_summary_text = await db["parent_chunks"].count_documents(
        _parent_query(corpus_id, _summary_text_clause())
    )
    body_with_summary_text = await db["parent_chunks"].count_documents(
        _body_parent_query(corpus_id, _summary_text_clause())
    )
    missing_summary_text = await db["parent_chunks"].count_documents(
        _parent_query(corpus_id, _missing_summary_clause())
    )
    non_retrieval_missing = await db["parent_chunks"].count_documents(
        {
            "corpus_id": corpus_id,
            "$and": [
                _missing_summary_clause(),
                {"$nor": [_retrieval_parent_clause()]},
            ],
        }
    )
    planned_rows = await db["parent_chunks"].find(
        _parent_query(corpus_id, _missing_summary_clause()),
        {
            "_id": 0,
            "parent_id": 1,
            "doc_id": 1,
            "chunk_kind": 1,
            "source_tier": 1,
        },
    ).sort("parent_id", 1).limit(max(1, limit)).to_list(length=max(1, limit))
    top_docs = await db["parent_chunks"].aggregate(
        [
            {"$match": _parent_query(corpus_id, _missing_summary_clause())},
            {"$group": {"_id": "$doc_id", "missing": {"$sum": 1}}},
            {"$sort": {"missing": -1}},
            {"$limit": 10},
            {
                "$lookup": {
                    "from": "documents",
                    "localField": "_id",
                    "foreignField": "doc_id",
                    "as": "doc",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "doc_id": "$_id",
                    "missing": 1,
                    "filename": {"$arrayElemAt": ["$doc.filename", 0]},
                }
            },
        ]
    ).to_list(length=10)
    return {
        "retrieval_parent_count": retrieval_parent_count,
        "body_parent_count": body_parent_count,
        "with_summary_text": with_summary_text,
        "missing_summary_text": missing_summary_text,
        "body_with_summary_text": body_with_summary_text,
        "body_missing_summary_text": max(body_parent_count - body_with_summary_text, 0),
        "non_retrieval_missing_summary_text": non_retrieval_missing,
        # Deprecated compatibility alias for older operator notes/scripts. This
        # means non-retrieval, not literally non-body.
        "non_body_missing_summary_text": non_retrieval_missing,
        "coverage": round(
            with_summary_text / retrieval_parent_count if retrieval_parent_count else 1.0,
            4,
        ),
        "planned_limit": limit,
        "planned_parent_count": min(limit, missing_summary_text),
        "planned_parents_sample": planned_rows[:20],
        "top_missing_docs": top_docs,
    }


async def _write_run_record(
    db: Any,
    *,
    run_id: str,
    status: str,
    corpus_id: str,
    apply: bool,
    plan: dict[str, Any],
    result: dict[str, Any] | None = None,
    active_batches: list[dict[str, Any]] | None = None,
) -> None:
    now = datetime.utcnow()
    counts = {
        "planned": int(plan.get("planned_parent_count") or 0),
        "done": int((result or {}).get("generated") or 0),
        "indexed": int((result or {}).get("indexed") or 0),
        "failed": len((result or {}).get("generation_errors") or []),
    }
    doc: dict[str, Any] = {
        "run_id": run_id,
        "kind": "summary_backfill_scoped",
        "status": status,
        "corpus_id": corpus_id,
        "apply": apply,
        "counts": counts,
        "plan": plan,
        "updated_at": now,
    }
    if result is not None:
        doc["result"] = result
    if active_batches is not None:
        doc["active_batches"] = active_batches
    await db["ingest_repair_runs"].update_one(
        {"run_id": run_id},
        {"$setOnInsert": {"created_at": now}, "$set": doc},
        upsert=True,
    )


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    mongo_client, db = _mongo_db()
    qdrant: AsyncQdrantClient | None = None
    run_id = (
        args.run_id
        or f"summary_backfill_scoped_{datetime.utcnow():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    )
    try:
        active = await _active_batches(db)
        plan = await _summary_plan(db, corpus_id=args.corpus_id, limit=args.limit)
        payload = {
            "run_id": run_id,
            "apply": args.apply,
            "corpus_id": args.corpus_id,
            "active_ingest_batches": len(active),
            "plan": plan,
        }
        print(json.dumps(payload, default=_json_default, indent=2))

        if args.apply and active and not args.force_active_ingest:
            await _write_run_record(
                db,
                run_id=run_id,
                status="refused_active_ingest",
                corpus_id=args.corpus_id,
                apply=True,
                plan=plan,
                active_batches=active,
            )
            print(json.dumps({
                "status": "refused_active_ingest",
                "reason": "active ingest batches exist; rerun with --force-active-ingest to override",
                "active_batches": active,
            }, default=_json_default, indent=2))
            return 3

        if not args.apply:
            await _write_run_record(
                db,
                run_id=run_id,
                status="dry_run",
                corpus_id=args.corpus_id,
                apply=False,
                plan=plan,
                active_batches=active,
            )
            return 0

        if int(plan.get("planned_parent_count") or 0) <= 0:
            await _write_run_record(
                db,
                run_id=run_id,
                status="noop",
                corpus_id=args.corpus_id,
                apply=True,
                plan=plan,
                active_batches=active,
            )
            return 0

        settings = get_settings()
        qdrant = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
        )
        settings_service.attach(db)
        service = IngestionService()
        service._db = db  # operator script: avoid startup-wide readiness repair
        service._qdrant = qdrant
        service._settings = settings

        await _write_run_record(
            db,
            run_id=run_id,
            status="running",
            corpus_id=args.corpus_id,
            apply=True,
            plan=plan,
            active_batches=active,
        )
        result = await service.backfill_parent_summaries(
            args.corpus_id,
            generate=not args.index_only,
            index=not args.no_index,
            limit=args.limit if not args.index_only else None,
            batch=args.batch,
        )
        final_status = "complete"
        if result.get("generation_errors"):
            final_status = "partial"
        await _write_run_record(
            db,
            run_id=run_id,
            status=final_status,
            corpus_id=args.corpus_id,
            apply=True,
            plan=plan,
            result=result,
            active_batches=active,
        )
        print(json.dumps({"run_id": run_id, "status": final_status, "result": result}, default=_json_default, indent=2))
        return 0 if final_status == "complete" else 1
    finally:
        if qdrant is not None:
            await qdrant.close()
        mongo_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan/run scoped body-parent summary backfill.",
    )
    parser.add_argument("--corpus-id", default=DEFAULT_POLYMATH_V2_CORPUS_ID)
    parser.add_argument("--limit", type=int, default=500, help="Maximum body parents to generate.")
    parser.add_argument("--batch", type=int, default=32, help="Generation/index batch size.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--apply", action="store_true", help="Actually generate/index summaries.")
    parser.add_argument(
        "--force-active-ingest",
        action="store_true",
        help="Allow --apply even while queued/running ingest batches exist.",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Generate only; do not index generated summaries into Qdrant.",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Do not generate; reindex all existing body summary text.",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.batch < 1:
        parser.error("--batch must be >= 1")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
