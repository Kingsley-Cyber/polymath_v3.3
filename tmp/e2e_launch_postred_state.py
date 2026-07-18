"""Secret-free proof that the failed launch stopped before batch/provider work."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


CORPUS_NAME = "runpod_e2e_15doc_20260715"
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        corpora = await database["corpora"].find(
            {"name": CORPUS_NAME},
            {
                "_id": 0,
                "corpus_id": 1,
                "doc_count": 1,
                "ready_doc_count": 1,
                "chunk_count": 1,
                "default_ingestion_config.runpod_local_extraction_routes": 1,
            },
        ).to_list(length=2)
        if len(corpora) != 1:
            raise RuntimeError("fresh empty corpus did not resolve exactly once")
        corpus = corpora[0]
        corpus_id = str(corpus["corpus_id"])
        counts = {
            "documents": await database["documents"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "parent_chunks": await database["parent_chunks"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "chunks": await database["chunks"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "ingest_batches": await database["ingest_batches"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "ingest_batch_items": await database[
                "ingest_batch_items"
            ].count_documents({"corpus_id": corpus_id}),
            "summary_cost_runs": await database["summary_cost_runs"].count_documents(
                {"corpus_id": corpus_id}
            ),
            "summary_cost_claims": await database[
                "summary_cost_claims"
            ].count_documents({"corpus_id": corpus_id}),
        }
        if any(counts.values()):
            raise RuntimeError(f"failed launch wrote downstream rows: {counts}")
        corpus_hash = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
        journal = JOURNAL_ROOT / f"corpus-{corpus_hash}.jsonl"
        if journal.exists():
            raise RuntimeError("failed launch created a RunPod job journal")
        routes = (
            (corpus.get("default_ingestion_config") or {}).get(
                "runpod_local_extraction_routes"
            )
            or []
        )
        print(
            json.dumps(
                {
                    "corpus_id": corpus_id,
                    "corpus_surface_counts": {
                        "doc_count": int(corpus.get("doc_count") or 0),
                        "ready_doc_count": int(corpus.get("ready_doc_count") or 0),
                        "chunk_count": int(corpus.get("chunk_count") or 0),
                    },
                    "downstream_counts": counts,
                    "journal_absent": True,
                    "route_count": len(routes),
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
