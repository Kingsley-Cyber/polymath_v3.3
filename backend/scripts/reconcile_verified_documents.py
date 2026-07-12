"""Reverify completed document artifacts without re-embedding or model calls."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient

from config import get_settings
from services.ingestion.section_classifier import parent_summary_required_clause
from services.ingestion.verify import _expected_summary_count, verify_ingest


async def _artifact_state(db: Any, *, corpus_id: str, doc_id: str) -> dict[str, Any]:
    parent_clause = parent_summary_required_clause()
    required, summarized, tree = await asyncio.gather(
        db["parent_chunks"].count_documents(
            {"corpus_id": corpus_id, "doc_id": doc_id, "$and": [parent_clause]}
        ),
        db["parent_chunks"].count_documents(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "$and": [
                    parent_clause,
                    {"summary": {"$exists": True, "$nin": [None, ""]}},
                ],
            }
        ),
        db["summary_tree"].count_documents(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "node_type": "document",
                "summary": {"$exists": True, "$nin": [None, ""]},
            }
        ),
    )
    return {
        "required_parent_count": int(required),
        "summarized_parent_count": int(summarized),
        "document_tree_done": bool(tree),
    }


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo.get_default_database()
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL)
    neo4j = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        corpus = await db["corpora"].find_one(
            {"corpus_id": args.corpus_id},
            {"_id": 0, "default_ingestion_config": 1},
        )
        if not corpus:
            raise SystemExit("corpus not found")
        cfg = (corpus.get("default_ingestion_config") or {})
        target_collections = list(
            cfg.get("target_qdrant_collections") or ["naive", "hrag", "graph"]
        )
        use_neo4j = bool(cfg.get("use_neo4j", True))
        for doc_id in list(dict.fromkeys(args.doc_id)):
            doc = await db["documents"].find_one(
                {"corpus_id": args.corpus_id, "doc_id": doc_id},
                {"_id": 0, "doc_id": 1, "write_state": 1, "doc_profile.summary": 1},
            )
            if not doc:
                print({"doc_id": doc_id, "status": "not_found"})
                continue
            ok, errors = await verify_ingest(
                db=db,
                qdrant=qdrant,
                neo4j_driver=neo4j if use_neo4j else None,
                doc_id=doc_id,
                corpus_id=args.corpus_id,
                target_qdrant_collections=target_collections,
                use_neo4j=use_neo4j,
            )
            state = await _artifact_state(
                db,
                corpus_id=args.corpus_id,
                doc_id=doc_id,
            )
            state["profile_done"] = bool(
                str(((doc.get("doc_profile") or {}).get("summary") or "")).strip()
            )
            state["graph_done"] = bool(
                ((doc.get("write_state") or {}).get("neo4j_written") is True)
                or not use_neo4j
            )
            summary_points = await _expected_summary_count(
                db,
                doc_id=doc_id,
                corpus_id=args.corpus_id,
            )
            complete = bool(
                ok
                and state["summarized_parent_count"] >= state["required_parent_count"]
                and state["profile_done"]
                and state["document_tree_done"]
                and state["graph_done"]
            )
            result = {
                "doc_id": doc_id,
                "verified": ok,
                "errors": errors,
                "summary_points": summary_points,
                "fully_enriched": complete,
                **state,
            }
            if args.apply and not ok:
                now = datetime.utcnow()
                failed_update = await db["documents"].update_one(
                    {"corpus_id": args.corpus_id, "doc_id": doc_id},
                    {
                        "$set": {
                            "write_state.verified": False,
                            "write_state.verify_errors": list(errors),
                            "verification_reconciled_at": now,
                            "updated_at": now,
                        }
                    },
                )
                result["modified"] = {
                    "document": int(failed_update.modified_count or 0),
                    "document_pipeline_jobs": 0,
                }
            elif args.apply and ok:
                now = datetime.utcnow()
                stage = "fully_enriched" if complete else "queryable"
                await db["documents"].update_one(
                    {"corpus_id": args.corpus_id, "doc_id": doc_id},
                    {
                        "$set": {
                            "ingest_stage": stage,
                            "queryable": True,
                            "write_state.verified": True,
                            "write_state.verify_errors": [],
                            "write_state.summary_points": int(summary_points),
                            "write_state.summaries_indexed": True,
                            "enrichment_status": {
                                "summary": "complete" if complete else "pending",
                                "graph": "complete" if state["graph_done"] else "pending",
                            },
                            "enrichment_lanes": {
                                "summary": "complete" if complete else "pending",
                                "graph": "complete" if state["graph_done"] else "pending",
                            },
                            "enrichment_pending_reason": None if complete else "Artifact reconciliation pending.",
                            "verification_reconciled_at": now,
                            "updated_at": now,
                        },
                        "$unset": {"error": ""},
                    },
                )
                jobs = await db["document_pipeline_jobs"].update_many(
                    {
                        "corpus_id": args.corpus_id,
                        "doc_id": doc_id,
                        "status": {
                            "$in": [
                                "queued",
                                "running",
                                "failed",
                                "dead_letter",
                                "blocked_missing_chunks",
                                "blocked_mongo_state",
                            ]
                        },
                    },
                    {
                        "$set": {
                            "status": "superseded",
                            "reason": "artifact_already_satisfied",
                            "artifact_reconciled_at": now,
                            "updated_at": now,
                        },
                        "$unset": {"lease_until": "", "runner": "", "started_at": ""},
                    },
                )
                result["modified"] = {
                    "document": 1,
                    "document_pipeline_jobs": int(jobs.modified_count or 0),
                }
            print(result)
    finally:
        await qdrant.close()
        await neo4j.close()
        mongo.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--doc-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
