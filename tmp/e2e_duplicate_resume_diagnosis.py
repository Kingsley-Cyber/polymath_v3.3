"""Read-only classification of the incomplete exact-duplicate resume defect."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion_service import exact_source_duplicate_query


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")


async def main() -> None:
    state = json.loads(STATE.read_text())
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        item = await db["ingest_batch_items"].find_one(
            {
                "batch_id": batch_id,
                "corpus_id": corpus_id,
                "ordinal": 3,
                "status": "skipped",
            },
            {"_id": 0, "doc_id": 1, "filename": 1, "status": 1, "error": 1},
        )
        if not item or not item.get("doc_id"):
            raise RuntimeError("skipped item identity is absent")
        document = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": item["doc_id"]},
            {
                "_id": 0,
                "doc_id": 1,
                "status": 1,
                "ingest_stage": 1,
                "source_identity": 1,
                "write_state": 1,
                "ghost_b_metrics": 1,
            },
        )
        if not document:
            raise RuntimeError("skipped item document is absent")
        source_identity = document.get("source_identity") or {}
        query = exact_source_duplicate_query(
            corpus_id=corpus_id,
            source_identity=source_identity,
        )
        if not query:
            raise RuntimeError("strong exact-source query is absent")
        matches = await db["documents"].find(
            query,
            {
                "_id": 0,
                "doc_id": 1,
                "ingest_stage": 1,
                "write_state.qdrant_written": 1,
                "write_state.neo4j_written": 1,
                "write_state.verified": 1,
            },
        ).to_list(length=10)
        ws = document.get("write_state") or {}
        metrics = document.get("ghost_b_metrics") or {}
        counts = {
            "parents": await db["parent_chunks"].count_documents(
                {"corpus_id": corpus_id, "doc_id": item["doc_id"]}
            ),
            "children": await db["chunks"].count_documents(
                {"corpus_id": corpus_id, "doc_id": item["doc_id"]}
            ),
            "extractions": await db["ghost_b_extractions"].count_documents(
                {"corpus_id": corpus_id, "doc_id": item["doc_id"]}
            ),
        }
        result = {
            "schema_version": "e2e_incomplete_duplicate_diagnosis.v1",
            "item_status": item.get("status"),
            "item_error_class": (
                "exact_source_duplicate_skipped"
                if "Exact source duplicate skipped" in str(item.get("error") or "")
                else "unexpected"
            ),
            "document_status": document.get("status"),
            "document_ingest_stage": document.get("ingest_stage"),
            "write_state": {
                key: ws.get(key)
                for key in (
                    "mongo_written",
                    "qdrant_written",
                    "summaries_indexed",
                    "neo4j_written",
                    "verified",
                )
            },
            "durable_counts": counts,
            "ghost_b": {
                key: metrics.get(key)
                for key in (
                    "requested_chunks",
                    "extracted_chunks",
                    "failed_chunks",
                    "request_batches",
                )
            },
            "exact_query_match_count": len(matches),
            "exact_query_matches_same_doc_only": len(matches) == 1
            and matches[0].get("doc_id") == item.get("doc_id"),
            "classification": (
                "queryable_projection_incomplete_resume_blocked_by_pre_worker_skip"
            ),
            "raw_text_emitted": 0,
            "source_identity_values_emitted": 0,
            "secret_values_emitted": 0,
        }
        if result["exact_query_match_count"] != 1:
            raise RuntimeError(f"exact-source match closure drifted: {result}")
        if ws.get("verified") is True:
            raise RuntimeError("diagnosed document is already verified")
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        client.close()


asyncio.run(main())
