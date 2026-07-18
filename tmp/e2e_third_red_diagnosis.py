"""Read-only classification of the item-4 summary verification RED."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from config import get_settings
from services.storage.qdrant_writer import _col_for_corpus


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")


async def _summary_payloads(
    qdrant: AsyncQdrantClient,
    *,
    collection: str,
    corpus_id: str,
    doc_id: str,
) -> list[dict]:
    payloads = []
    offset = None
    scroll_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="corpus_id", match=qmodels.MatchValue(value=corpus_id)
            ),
            qmodels.FieldCondition(
                key="doc_id", match=qmodels.MatchValue(value=doc_id)
            ),
            qmodels.FieldCondition(
                key="chunk_type", match=qmodels.MatchValue(value="summary")
            ),
        ]
    )
    while True:
        hits, offset = await qdrant.scroll(
            collection_name=collection,
            scroll_filter=scroll_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        payloads.extend(hit.payload or {} for hit in hits)
        if offset is None:
            return payloads


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
    try:
        db = mongo[settings.MONGODB_DATABASE]
        item = await db["ingest_batch_items"].find_one(
            {"batch_id": batch_id, "corpus_id": corpus_id, "ordinal": 3},
            {"_id": 0, "status": 1, "phase": 1, "doc_id": 1, "error": 1},
        )
        if not item or item.get("status") != "failed" or not item.get("doc_id"):
            raise RuntimeError(f"item-4 RED state drifted: {item}")
        doc_id = str(item["doc_id"])
        document = await db["documents"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "ingest_stage": 1, "write_state": 1, "ghost_b_metrics": 1},
        )
        parents = await db["parent_chunks"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "_id": 0,
                "parent_id": 1,
                "summary": 1,
                "retrieval_text": 1,
                "summary_id": 1,
            },
        ).to_list(length=None)
        parent_ids = {str(row.get("parent_id") or "") for row in parents}
        expected_summary_ids = {f"{parent_id}_summary" for parent_id in parent_ids}
        mongo_shape = {
            "parents": len(parents),
            "unique_parent_ids": len(parent_ids),
            "nonempty_summary": sum(bool(str(row.get("summary") or "").strip()) for row in parents),
            "nonempty_retrieval_text": sum(
                bool(str(row.get("retrieval_text") or "").strip()) for row in parents
            ),
            "summary_id_matches_parent_suffix": sum(
                str(row.get("summary_id") or "") == f"{row.get('parent_id')}_summary"
                for row in parents
            ),
        }
        collections = {}
        for kind in ("naive", "hrag"):
            collection = _col_for_corpus(corpus_id, kind)
            payloads = await _summary_payloads(
                qdrant,
                collection=collection,
                corpus_id=corpus_id,
                doc_id=doc_id,
            )
            chunk_ids = [str(row.get("chunk_id") or "") for row in payloads]
            payload_parent_ids = [str(row.get("parent_id") or "") for row in payloads]
            chunk_set = set(chunk_ids)
            collections[kind] = {
                "summary_payloads": len(payloads),
                "unique_chunk_ids": len(chunk_set),
                "matches_current_expected_id": sum(
                    chunk_id in expected_summary_ids for chunk_id in chunk_ids
                ),
                "orphan_to_current_expected_id": sum(
                    chunk_id not in expected_summary_ids for chunk_id in chunk_ids
                ),
                "current_expected_ids_absent": len(expected_summary_ids - chunk_set),
                "parent_id_matches_current": sum(
                    parent_id in parent_ids for parent_id in payload_parent_ids
                ),
                "chunk_id_matches_own_parent_suffix": sum(
                    chunk_id == f"{parent_id}_summary"
                    for chunk_id, parent_id in zip(chunk_ids, payload_parent_ids)
                ),
                "nonempty_chunk_text": sum(
                    bool(str(row.get("chunk_text") or row.get("text") or "").strip())
                    for row in payloads
                ),
                "payload_key_shape": sorted(
                    set().union(*(set(row) for row in payloads))
                ) if payloads else [],
            }
        ws = (document or {}).get("write_state") or {}
        metrics = (document or {}).get("ghost_b_metrics") or {}
        result = {
            "schema_version": "e2e_summary_verify_red_diagnosis.v1",
            "item": {
                "status": item.get("status"),
                "phase": item.get("phase"),
                "error": item.get("error"),
            },
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
            "mongo_summary_shape": mongo_shape,
            "qdrant_summary_shape": collections,
            "journal": _journal(corpus_id),
            "raw_text_emitted": 0,
            "secret_values_emitted": 0,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        await qdrant.close()
        mongo.close()


asyncio.run(main())
