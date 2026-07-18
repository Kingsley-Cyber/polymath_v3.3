"""Fail-closed ingest-complete census for the frozen 15-document E2E."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient, models as qmodels

from config import get_settings
from scripts.semantic_gateway_ugo_canary import _canonical_store_census
from services.ingestion.summary_cost_control import summary_cost_snapshot
from services.storage.qdrant_writer import _col_for_corpus


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")
SELECTION_SHA = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
RUNPOD_RATE = Decimal("0.00031")
RUNPOD_OVERHEAD = Decimal("1.5")


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True, default=str).encode() + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def _qdrant_counts(
    qdrant: AsyncQdrantClient, corpus_id: str
) -> dict[str, int]:
    result: dict[str, int] = {}
    for kind in ("naive", "hrag", "graph", "schemas"):
        collection = _col_for_corpus(corpus_id, kind)
        if not await qdrant.collection_exists(collection):
            raise RuntimeError(f"missing Qdrant collection: {collection}")
        count = await qdrant.count(collection_name=collection, exact=True)
        result[collection] = int(count.count)
    summary_collection = "polymath_doc_summaries"
    summary_count = await qdrant.count(
        collection_name=summary_collection,
        count_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="corpus_id", match=qmodels.MatchValue(value=corpus_id)
                )
            ]
        ),
        exact=True,
    )
    result[summary_collection] = int(summary_count.count)
    return result


async def _neo4j_counts(settings: Any, corpus_id: str) -> dict[str, Any]:
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        async with driver.session() as session:
            label_rows = await (
                await session.run(
                    "MATCH (n) WHERE n.corpus_id = $corpus_id "
                    "UNWIND labels(n) AS label "
                    "RETURN label, count(*) AS count ORDER BY label",
                    corpus_id=corpus_id,
                )
            ).data()
            relationship_rows = await (
                await session.run(
                    "MATCH (a)-[r]->(b) "
                    "WHERE r.corpus_id = $corpus_id "
                    "OR a.corpus_id = $corpus_id "
                    "OR b.corpus_id = $corpus_id "
                    "RETURN type(r) AS type, count(*) AS count ORDER BY type",
                    corpus_id=corpus_id,
                )
            ).data()
            return {
                "nodes_by_label": {
                    str(row["label"]): int(row["count"]) for row in label_rows
                },
                "relationships_touching_corpus_by_type": {
                    str(row["type"]): int(row["count"])
                    for row in relationship_rows
                },
            }
    finally:
        await driver.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selection_bytes = args.selection.read_bytes()
    if hashlib.sha256(selection_bytes).hexdigest() != SELECTION_SHA:
        raise RuntimeError("selection hash drifted")
    selection = json.loads(selection_bytes)
    expected_names = {str(row["filename"]) for row in selection["selected"]}
    if len(expected_names) != 15:
        raise RuntimeError("selection does not contain exactly 15 filenames")

    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL)
    try:
        database = client[settings.MONGODB_DATABASE]
        corpus = await database["corpora"].find_one(
            {"corpus_id": corpus_id},
            {"_id": 0, "name": 1, "doc_count": 1, "ready_doc_count": 1, "chunk_count": 1},
        )
        if not corpus or corpus.get("name") != "runpod_e2e_15doc_20260715":
            raise RuntimeError("fresh E2E corpus identity drifted")
        batch = await database["ingest_batches"].find_one(
            {"batch_id": batch_id, "corpus_id": corpus_id}, {"_id": 0}
        )
        if not batch:
            raise RuntimeError("E2E batch is absent")
        items = await database["ingest_batch_items"].find(
            {"batch_id": batch_id, "corpus_id": corpus_id},
            {"_id": 0, "filename": 1, "status": 1, "ordinal": 1, "doc_id": 1},
        ).sort("ordinal", 1).to_list(length=20)
        item_statuses = Counter(str(row.get("status") or "") for row in items)
        documents = await database["documents"].find(
            {"corpus_id": corpus_id},
            {
                "_id": 0,
                "doc_id": 1,
                "filename": 1,
                "original_filename": 1,
                "status": 1,
                "write_state": 1,
                "ghost_b_metrics": 1,
            },
        ).to_list(length=20)
        observed_names = {
            str(row.get("original_filename") or row.get("filename") or "")
            for row in documents
        }
        verified = [
            row
            for row in documents
            if (row.get("write_state") or {}).get("verified") is True
        ]
        parent_filter = {"corpus_id": corpus_id}
        parent_count = await database["parent_chunks"].count_documents(parent_filter)
        summary_count = await database["parent_chunks"].count_documents(
            {**parent_filter, "summary": {"$exists": True, "$nin": [None, ""]}}
        )
        retrieval_text_count = await database["parent_chunks"].count_documents(
            {
                **parent_filter,
                "retrieval_text": {"$exists": True, "$nin": [None, ""]},
            }
        )
        extraction_count = await database["ghost_b_extractions"].count_documents(
            {"corpus_id": corpus_id}
        )
        failed_extractions = await database["ghost_b_extractions"].count_documents(
            {"corpus_id": corpus_id, "status": {"$ne": "ok"}}
        )
        summary = await summary_cost_snapshot(database, batch_id)
        corpus_hash = hashlib.sha256(corpus_id.encode()).hexdigest()
        journal_path = JOURNAL_ROOT / f"corpus-{corpus_hash}.jsonl"
        journal_rows = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        submitted = [row for row in journal_rows if row.get("event") == "submitted"]
        terminal = [row for row in journal_rows if row.get("event") == "terminal"]
        submitted_ids = {str(row.get("job_id") or "") for row in submitted}
        terminal_ids = {str(row.get("job_id") or "") for row in terminal}
        durable_job_ids: set[str] = set()
        durable_document_jobs: dict[str, int] = {}
        durable_document_failures: list[str] = []
        for document in documents:
            filename = str(
                document.get("original_filename")
                or document.get("filename")
                or document.get("doc_id")
                or ""
            )
            metrics = document.get("ghost_b_metrics") or {}
            remote_jobs = metrics.get("remote_jobs") or []
            job_ids = [str(row.get("job_id") or "") for row in remote_jobs]
            valid = (
                bool(job_ids)
                and "" not in job_ids
                and len(job_ids) == len(set(job_ids))
                and int(metrics.get("request_batches") or 0) == len(job_ids)
                and int(metrics.get("failed_chunks") or 0) == 0
                and set(job_ids).issubset(terminal_ids)
                and not durable_job_ids.intersection(job_ids)
            )
            if not valid:
                durable_document_failures.append(filename)
                continue
            durable_job_ids.update(job_ids)
            durable_document_jobs[filename] = len(job_ids)
        worker_seconds = sum(
            Decimal(str(row.get("execution_time_ms") or 0)) for row in terminal
        ) / Decimal("1000")
        runpod_cost = worker_seconds * RUNPOD_RATE * RUNPOD_OVERHEAD
        summary_cost = Decimal(str(summary.get("ceiling_basis_usd") or "0"))
        canonical = await _canonical_store_census(db=database, settings=settings)
        result = {
            "schema_version": "runpod_e2e_ingest_complete_census.v1",
            "corpus_id": corpus_id,
            "batch_id": batch_id,
            "selection_sha256": SELECTION_SHA,
            "corpus": corpus,
            "batch_status": batch.get("status"),
            "batch_counts": batch.get("counts"),
            "item_status_counts": dict(sorted(item_statuses.items())),
            "documents": {
                "count": len(documents),
                "verified": len(verified),
                "selected_filename_set_match": observed_names == expected_names,
                "unverified_filenames": sorted(
                    str(row.get("original_filename") or row.get("filename") or "")
                    for row in documents
                    if (row.get("write_state") or {}).get("verified") is not True
                ),
            },
            "mongo": {
                "parent_chunks": parent_count,
                "parents_with_summary": summary_count,
                "parents_with_retrieval_text": retrieval_text_count,
                "chunks": await database["chunks"].count_documents({"corpus_id": corpus_id}),
                "ghost_b_extractions": extraction_count,
                "non_success_ghost_b_extractions": failed_extractions,
            },
            "qdrant": await _qdrant_counts(qdrant, corpus_id),
            "neo4j": await _neo4j_counts(settings, corpus_id),
            "canonical_census_scope": canonical.get("census_scope_version"),
            "runpod": {
                "journal_preflights": sum(
                    1 for row in journal_rows if row.get("event") == "journal_preflight"
                ),
                "submitted": len(submitted),
                "terminal": len(terminal),
                "unique_submitted": len(submitted_ids),
                "unique_terminal": len(terminal_ids),
                "durable_document_job_counts": dict(
                    sorted(durable_document_jobs.items())
                ),
                "durable_document_failures": sorted(durable_document_failures),
                "durable_job_ids": len(durable_job_ids),
                "durable_ids_equal_journal": durable_job_ids == submitted_ids,
                "all_completed": all(
                    str(row.get("status") or "") == "COMPLETED" for row in terminal
                ),
                "worker_seconds": str(worker_seconds),
                "conservative_cost_usd": str(runpod_cost),
            },
            "summary_cost": summary,
            "combined_cost": {
                "summary_ceiling_basis_usd": str(summary_cost),
                "runpod_conservative_cost_usd": str(runpod_cost),
                "total_usd": str(summary_cost + runpod_cost),
                "authority_usd": "35.00",
            },
        }
        _atomic_write(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))

        failures = []
        if batch.get("status") != "done" or item_statuses != {"done": 15}:
            failures.append("batch/item closure")
        if len(documents) != 15 or len(verified) != 15 or observed_names != expected_names:
            failures.append("document closure")
        if failed_extractions != 0:
            failures.append("extraction closure")
        if (
            len(submitted) != len(terminal)
            or len(submitted_ids) != len(submitted)
            or len(terminal_ids) != len(terminal)
            or submitted_ids != terminal_ids
            or not result["runpod"]["all_completed"]
            or result["runpod"]["journal_preflights"] < 15
            or durable_document_failures
            or durable_job_ids != submitted_ids
        ):
            failures.append("RunPod journal closure")
        if summary.get("calls_refused") != 0 or summary.get("outstanding_reserved_usd") != "0.000000000":
            failures.append("summary ledger closure")
        if summary_cost >= Decimal("30") or runpod_cost >= Decimal("5") or summary_cost + runpod_cost >= Decimal("35"):
            failures.append("cost authority")
        if (
            result["canonical_census_scope"]
            != "canonical_store_census.scope.v2"
        ):
            failures.append("canonical census scope")
        if failures:
            raise RuntimeError("; ".join(failures))
    finally:
        await qdrant.close()
        client.close()


asyncio.run(main())
