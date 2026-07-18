"""Read-only classification of the Neo4j transaction-memory E2E RED."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient

from config import get_settings
from services.ingestion.summary_cost_control import summary_cost_snapshot
from services.ingestion.verify import _verify_qdrant_text_contract
from services.storage.qdrant_writer import _col_for_corpus


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")


def _journal(corpus_id: str) -> dict[str, int | bool]:
    digest = hashlib.sha256(corpus_id.encode()).hexdigest()
    rows = [
        json.loads(line)
        for line in (JOURNAL_ROOT / f"corpus-{digest}.jsonl").read_text().splitlines()
        if line.strip()
    ]
    submitted = {
        str(row.get("job_id") or "")
        for row in rows
        if row.get("event") == "submitted"
    }
    terminal = {
        str(row.get("job_id") or "")
        for row in rows
        if row.get("event") == "terminal"
    }
    return {
        "submitted": len(submitted),
        "terminal": len(terminal),
        "closure_equal": submitted == terminal,
        "reused_terminal_output": sum(
            row.get("event") == "reused_terminal_output" for row in rows
        ),
    }


async def _single_value(session, query: str, **params) -> int:
    result = await session.run(query, **params)
    row = await result.single()
    return int((row or {}).get("count") or 0)


async def main() -> None:
    state = json.loads(STATE.read_text())
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )
    neo4j = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = mongo[settings.MONGODB_DATABASE]
        scope = {"batch_id": batch_id, "corpus_id": corpus_id}
        item = await db["ingest_batch_items"].find_one(
            {**scope, "ordinal": 3},
            {"_id": 0, "status": 1, "phase": 1, "doc_id": 1, "error": 1},
        )
        if not item or item.get("status") != "failed" or not item.get("doc_id"):
            raise RuntimeError(f"fourth RED item state drifted: {item}")
        doc_id = str(item["doc_id"])
        document = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "ingest_stage": 1, "write_state": 1, "ghost_b_metrics": 1},
        )
        ws = (document or {}).get("write_state") or {}
        metrics = (document or {}).get("ghost_b_metrics") or {}
        parent_shape = {
            "parents": await db["parent_chunks"].count_documents(
                {"corpus_id": corpus_id, "doc_id": doc_id}
            ),
            "nonempty_summaries": await db["parent_chunks"].count_documents(
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "summary": {"$exists": True, "$nin": [None, ""]},
                }
            ),
            "nonempty_retrieval_texts": await db["parent_chunks"].count_documents(
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "retrieval_text": {"$exists": True, "$nin": [None, ""]},
                }
            ),
        }
        qdrant_contract = {}
        for kind in ("naive", "hrag"):
            qdrant_contract[kind] = await _verify_qdrant_text_contract(
                db=db,
                qdrant=qdrant,
                doc_id=doc_id,
                corpus_id=corpus_id,
                collection_name=_col_for_corpus(corpus_id, kind),
            )
        async with neo4j.session() as session:
            graph_shape = {
                "document_nodes": await _single_value(
                    session,
                    "MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id}) "
                    "RETURN count(d) AS count",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                ),
                "chunk_nodes": await _single_value(
                    session,
                    "MATCH (c:Chunk {doc_id: $doc_id, corpus_id: $corpus_id}) "
                    "RETURN count(c) AS count",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                ),
                "mention_edges": await _single_value(
                    session,
                    "MATCH (c:Chunk {doc_id: $doc_id, corpus_id: $corpus_id})"
                    "-[r:MENTIONS]->() RETURN count(r) AS count",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                ),
                "affected_entities_now": await _single_value(
                    session,
                    "MATCH (c:Chunk {doc_id: $doc_id, corpus_id: $corpus_id})"
                    "-[:MENTIONS]->(e:Entity) RETURN count(DISTINCT e) AS count",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                ),
            }
        cost = await summary_cost_snapshot(db, batch_id)
        counts = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in ("queued", "running", "cancelled", "done", "failed", "skipped")
        }
        result = {
            "schema_version": "e2e_neo4j_memory_red_diagnosis.v1",
            "item": {
                "status": item.get("status"),
                "phase": item.get("phase"),
                "error_class": (
                    "Neo.TransientError.General.MemoryPoolOutOfMemoryError"
                    if "MemoryPoolOutOfMemoryError" in str(item.get("error") or "")
                    else "unexpected"
                ),
            },
            "item_status_counts": counts,
            "document": {
                "ingest_stage": (document or {}).get("ingest_stage"),
                "write_state": {
                    key: ws.get(key)
                    for key in (
                        "mongo_written",
                        "qdrant_written",
                        "summaries_indexed",
                        "summary_points",
                        "neo4j_written",
                        "verified",
                    )
                },
                "stored_request_batches": metrics.get("request_batches"),
                "failed_chunks": metrics.get("failed_chunks"),
            },
            "canonical_parent_shape": parent_shape,
            "qdrant_text_contract_errors": qdrant_contract,
            "neo4j_document_shape_after_failed_cleanup": graph_shape,
            "code_batch_size_for_entity_aggregate_refresh": 1000,
            "last_successful_graph_entities_from_worker_receipt": 18143,
            "neo4j_transaction_total_max_mib_from_error": 716.8,
            "summary_cost": {
                key: cost.get(key)
                for key in (
                    "ceiling_basis_usd",
                    "remaining_authority_usd",
                    "calls_completed",
                    "calls_refused",
                    "outstanding_reserved_usd",
                )
            },
            "journal": _journal(corpus_id),
            "raw_text_emitted": 0,
            "secret_values_emitted": 0,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        await neo4j.close()
        await qdrant.close()
        mongo.close()


asyncio.run(main())
