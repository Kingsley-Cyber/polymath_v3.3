"""Backfill pre-embedded RAPTOR section/rollup routing points.

This migration reuses durable ``summary_tree`` and ``corpus_lexicon`` records.
It never parses sources, reruns extraction, or calls a summary model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient, models

from config import get_settings
from models.schemas import IngestionConfig
from services.ingestion.corpus_lexicon import ensure_lexicon_indexes
from services.ingestion.summary_tree import index_summary_tree_nodes
from services.storage.qdrant_writer import (
    SUMMARY_TREE_SCHEMA_KIND,
    _col_for_corpus,
    ensure_collections_for_corpus,
)

ACTIVE_STATUSES = {"running", "leased", "processing"}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def _active_work(db: Any, corpus_id: str) -> list[dict[str, Any]]:
    checks = {
        "ingest_batches": {
            "corpus_id": corpus_id,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
        "document_pipeline_jobs": {
            "corpus_id": corpus_id,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
        "extraction_jobs": {
            "corpus_id": corpus_id,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
        "summary_jobs": {
            "corpus_id": corpus_id,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
        "graph_promotion_jobs": {
            "corpus_id": corpus_id,
            "status": {"$in": list(ACTIVE_STATUSES)},
        },
    }
    output = []
    for collection, query in checks.items():
        count = await db[collection].count_documents(query)
        if count:
            output.append({"collection": collection, "count": count})
    return output


async def run(
    *,
    corpus_ids: list[str],
    doc_ids: list[str],
    apply: bool,
    doc_limit: int,
    resume_after_doc_id: str | None,
    force_active: bool,
    run_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = mongo.get_default_database()
    except Exception:
        db = mongo[settings.MONGODB_DATABASE]
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )
    started = datetime.now(timezone.utc)
    try:
        plans: list[dict[str, Any]] = []
        for corpus_id in corpus_ids:
            query: dict[str, Any] = {
                "corpus_id": corpus_id,
                "node_type": {"$in": ["section", "rollup"]},
                "summary": {"$type": "string", "$ne": ""},
            }
            if doc_ids:
                query["doc_id"] = {"$in": doc_ids}
            if resume_after_doc_id:
                query["doc_id"] = (
                    {
                        "$in": doc_ids,
                        "$gt": resume_after_doc_id,
                    }
                    if doc_ids
                    else {"$gt": resume_after_doc_id}
                )
            selected_doc_ids = sorted(
                await db["summary_tree"].distinct("doc_id", query)
            )
            if doc_limit > 0:
                selected_doc_ids = selected_doc_ids[:doc_limit]
            plans.append(
                {
                    "corpus_id": corpus_id,
                    "documents": len(selected_doc_ids),
                    "nodes": await db["summary_tree"].count_documents(query),
                    "active_work": await _active_work(db, corpus_id),
                    "doc_ids": selected_doc_ids,
                }
            )
        if not apply:
            return {
                "apply": False,
                "run_id": run_id,
                "plans": [
                    {key: value for key, value in plan.items() if key != "doc_ids"}
                    | {"doc_id_sample": list(plan["doc_ids"][:8])}
                    for plan in plans
                ],
            }
        active = [
            {"corpus_id": plan["corpus_id"], "work": plan["active_work"]}
            for plan in plans
            if plan["active_work"]
        ]
        if active and not force_active:
            raise RuntimeError(f"selected corpora have active durable work: {active}")

        await ensure_lexicon_indexes(db)
        await db["ingest_repair_runs"].replace_one(
            {"run_id": run_id},
            {
                "run_id": run_id,
                "kind": "summary_tree_vector_backfill",
                "status": "running",
                "corpus_ids": corpus_ids,
                "started_at": started,
                "updated_at": started,
                "progress": {},
            },
            upsert=True,
        )
        results: list[dict[str, Any]] = []
        try:
            for plan in plans:
                corpus_id = str(plan["corpus_id"])
                corpus = await db["corpora"].find_one(
                    {"corpus_id": corpus_id},
                    {
                        "_id": 0,
                        "name": 1,
                        "default_ingestion_config": 1,
                    },
                )
                if not corpus:
                    results.append({"corpus_id": corpus_id, "status": "not_found"})
                    continue
                cfg = IngestionConfig(**(corpus.get("default_ingestion_config") or {}))
                await ensure_collections_for_corpus(
                    qdrant,
                    corpus_id,
                    dim=int(cfg.embedding_dimension),
                    corpus_name=str(corpus.get("name") or ""),
                )
                indexed = eligible = failed = 0
                last_doc_id = None
                for doc_id in plan["doc_ids"]:
                    nodes = (
                        await db["summary_tree"]
                        .find(
                            {
                                "corpus_id": corpus_id,
                                "doc_id": doc_id,
                                "node_type": {"$in": ["section", "rollup"]},
                                "summary": {"$type": "string", "$ne": ""},
                            },
                            {"_id": 0},
                        )
                        .sort("node_id", 1)
                        .to_list(length=None)
                    )
                    try:
                        result = await index_summary_tree_nodes(
                            qdrant_client=qdrant,
                            db=db,
                            corpus_id=corpus_id,
                            nodes=nodes,
                            embedding_config=cfg,
                        )
                        indexed += int(result.get("indexed") or 0)
                        eligible += int(result.get("eligible") or 0)
                        await db["documents"].update_one(
                            {"corpus_id": corpus_id, "doc_id": doc_id},
                            {
                                "$set": {
                                    "summary_tree_index_state": (
                                        "summary_tree_index_ready"
                                        if int(result.get("indexed") or 0)
                                        == int(result.get("eligible") or 0)
                                        and int(result.get("eligible") or 0) > 0
                                        else "summary_tree_index_pending"
                                    ),
                                    "summary_tree_indexed_nodes": int(
                                        result.get("indexed") or 0
                                    ),
                                    "summary_tree_index_eligible_nodes": int(
                                        result.get("eligible") or 0
                                    ),
                                    "summary_tree_index_updated_at": datetime.now(
                                        timezone.utc
                                    ),
                                },
                                "$unset": {"summary_tree_index_error": ""},
                            },
                        )
                    except Exception as exc:
                        failed += 1
                        await db["documents"].update_one(
                            {"corpus_id": corpus_id, "doc_id": doc_id},
                            {
                                "$set": {
                                    "summary_tree_index_state": (
                                        "summary_tree_index_pending"
                                    ),
                                    "summary_tree_index_error": (
                                        f"{type(exc).__name__}: {exc}"[:500]
                                    ),
                                    "summary_tree_index_updated_at": datetime.now(
                                        timezone.utc
                                    ),
                                }
                            },
                        )
                    last_doc_id = str(doc_id)
                    await db["ingest_repair_runs"].update_one(
                        {"run_id": run_id},
                        {
                            "$set": {
                                f"progress.{corpus_id}": {
                                    "last_doc_id": last_doc_id,
                                    "indexed": indexed,
                                    "eligible": eligible,
                                    "failed_documents": failed,
                                },
                                "updated_at": datetime.now(timezone.utc),
                            }
                        },
                    )
                collection = _col_for_corpus(corpus_id, "schemas")
                qdrant_count = (
                    await qdrant.count(
                        collection_name=collection,
                        count_filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="corpus_id",
                                    match=models.MatchValue(value=corpus_id),
                                ),
                                models.FieldCondition(
                                    key="kind",
                                    match=models.MatchValue(
                                        value=SUMMARY_TREE_SCHEMA_KIND
                                    ),
                                ),
                            ]
                        ),
                        exact=True,
                    )
                ).count
                results.append(
                    {
                        "corpus_id": corpus_id,
                        "status": "complete" if failed == 0 else "partial",
                        "documents": len(plan["doc_ids"]),
                        "eligible": eligible,
                        "indexed": indexed,
                        "qdrant_count": int(qdrant_count),
                        "failed_documents": failed,
                        "last_doc_id": last_doc_id,
                    }
                )
        except Exception as exc:
            await db["ingest_repair_runs"].update_one(
                {"run_id": run_id},
                {
                    "$set": {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            raise
        status = (
            "complete"
            if all(row.get("status") == "complete" for row in results)
            else "partial"
        )
        completed = datetime.now(timezone.utc)
        await db["ingest_repair_runs"].update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": status,
                    "results": results,
                    "completed_at": completed,
                    "updated_at": completed,
                }
            },
        )
        return {
            "apply": True,
            "run_id": run_id,
            "status": status,
            "results": results,
        }
    finally:
        await qdrant.close()
        mongo.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-id", dest="corpus_ids", action="append", required=True
    )
    parser.add_argument(
        "--doc-id",
        dest="doc_ids",
        action="append",
        default=[],
        help="Retry only the selected document ID; may be repeated.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--doc-limit", type=int, default=0)
    parser.add_argument("--resume-after-doc-id")
    parser.add_argument("--force-active", action="store_true")
    parser.add_argument(
        "--run-id",
        default=f"summary_tree_vector_backfill_{uuid.uuid4().hex[:12]}",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    result = await run(
        corpus_ids=list(dict.fromkeys(args.corpus_ids)),
        doc_ids=list(dict.fromkeys(args.doc_ids)),
        apply=bool(args.apply),
        doc_limit=max(0, int(args.doc_limit)),
        resume_after_doc_id=args.resume_after_doc_id,
        force_active=bool(args.force_active),
        run_id=str(args.run_id),
    )
    print(json.dumps(result, default=_json_default, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
