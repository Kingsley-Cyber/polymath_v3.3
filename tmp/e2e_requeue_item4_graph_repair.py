"""Launch-state-bound isolated requeue after Neo4j batch-family repair."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings
from services.ingestion.summary_cost_control import summary_cost_snapshot


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


async def _single(session, query: str, **params) -> int:
    row = await (await session.run(query, **params)).single()
    return int(row["count"] or 0) if row else 0


async def main() -> None:
    state = json.loads(STATE.read_text())
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    neo4j = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = client[settings.MONGODB_DATABASE]
        scope = {"batch_id": batch_id, "corpus_id": corpus_id}
        statuses = ("queued", "running", "cancelled", "done", "failed", "skipped")
        before = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in statuses
        }
        expected = {
            "queued": 0,
            "running": 0,
            "cancelled": 11,
            "done": 3,
            "failed": 1,
            "skipped": 0,
        }
        if before != expected:
            raise RuntimeError(f"graph repair precondition drifted: {before}")
        item = await db["ingest_batch_items"].find_one(
            {**scope, "ordinal": 3, "status": "failed"},
            {"_id": 0, "doc_id": 1, "error": 1},
        )
        if not item or "MemoryPoolOutOfMemoryError" not in str(item.get("error") or ""):
            raise RuntimeError("sealed item-4 Neo4j memory failure is absent")
        doc_id = str(item.get("doc_id") or "")
        document = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "write_state": 1, "ghost_b_metrics": 1},
        )
        ws = (document or {}).get("write_state") or {}
        metrics = (document or {}).get("ghost_b_metrics") or {}
        parent_scope = {"corpus_id": corpus_id, "doc_id": doc_id}
        parents = await db["parent_chunks"].count_documents(parent_scope)
        summaries = await db["parent_chunks"].count_documents(
            {**parent_scope, "summary": {"$exists": True, "$nin": [None, ""]}}
        )
        retrieval_texts = await db["parent_chunks"].count_documents(
            {
                **parent_scope,
                "retrieval_text": {"$exists": True, "$nin": [None, ""]},
            }
        )
        staged = await db["ghost_b_extractions"].count_documents(parent_scope)
        if (
            parents != 174
            or summaries != 174
            or retrieval_texts != 174
            or staged != 1575
            or metrics.get("request_batches") != 50
            or ws.get("mongo_written") is not True
            or ws.get("qdrant_written") is not True
            or ws.get("summaries_indexed") is not True
            or ws.get("neo4j_written") is not False
            or ws.get("verified") is True
        ):
            raise RuntimeError("item-4 canonical graph-repair surface drifted")
        async with neo4j.session() as session:
            graph_counts = {
                "document_nodes": await _single(
                    session,
                    "MATCH (n:Document {doc_id:$doc_id, corpus_id:$corpus_id}) RETURN count(n) AS count",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                ),
                "chunk_nodes": await _single(
                    session,
                    "MATCH (n:Chunk {doc_id:$doc_id, corpus_id:$corpus_id}) RETURN count(n) AS count",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                ),
                "mention_edges": await _single(
                    session,
                    "MATCH (:Chunk {doc_id:$doc_id, corpus_id:$corpus_id})-[r:MENTIONS]->() RETURN count(r) AS count",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                ),
            }
        if any(graph_counts.values()):
            raise RuntimeError(f"item-4 graph retry surface is not clean: {graph_counts}")
        journal = _journal(corpus_id)
        if journal != {
            "submitted": 201,
            "terminal": 201,
            "closure_equal": True,
            "reused_terminal_output": 66,
        }:
            raise RuntimeError(f"provider closure drifted: {journal}")
        cost = await summary_cost_snapshot(db, batch_id)
        if cost.get("calls_refused") != 0 or cost.get("outstanding_reserved_usd") != "0.000000000":
            raise RuntimeError(f"summary ledger is not settled: {cost}")
        now = datetime.utcnow()
        changed = await db["ingest_batch_items"].update_one(
            {**scope, "ordinal": 3, "status": "failed"},
            {
                "$set": {"status": "queued", "phase": "queued", "updated_at": now},
                "$unset": {
                    "error": "",
                    "failure_stage": "",
                    "completed_at": "",
                    "lease_owner": "",
                    "lease_until": "",
                },
            },
        )
        if changed.modified_count != 1:
            raise RuntimeError(f"isolated graph repair requeued {changed.modified_count}")
        await db["ingest_batches"].update_one(
            scope,
            {
                "$set": {
                    "status": "queued",
                    "run_requested_at": now,
                    "updated_at": now,
                },
                "$unset": {"error": "", "completed_at": ""},
            },
        )
        after = {
            status: await db["ingest_batch_items"].count_documents(
                {**scope, "status": status}
            )
            for status in statuses
        }
        expected["queued"] = 1
        expected["failed"] = 0
        if after != expected:
            raise RuntimeError(f"graph repair postcondition drifted: {after}")
        print(
            json.dumps(
                {
                    "schema_version": "e2e_graph_repair_requeue.v1",
                    "corpus_id": corpus_id,
                    "batch_id": batch_id,
                    "ordinal": 3,
                    "before": before,
                    "after": after,
                    "canonical_parent_rows": parents,
                    "stored_extraction_rows": staged,
                    "clean_graph_surface": graph_counts,
                    "summary_ceiling_basis_usd": cost.get("ceiling_basis_usd"),
                    "journal": journal,
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await neo4j.close()
        client.close()


asyncio.run(main())
