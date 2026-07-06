#!/usr/bin/env python3
"""Backfill Mongo relation_support_records from staged Ghost B extractions.

Neo4j RELATES_TO edges are the traversal projection. This script restores the
canonical per-chunk support rows in Mongo from already-staged extraction JSON, so
corpus deletion and audits can deactivate support records without re-extracting.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.graph.neo4j_writer import _relation_support_records, entity_id_from_name
from services.storage.mongo_writer import replace_relation_support_for_document
from services.storage.record_status import with_active_records


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


async def _chunk_parent_map(db: Any, *, corpus_id: str, doc_id: str) -> dict[str, str]:
    rows = await db["chunks"].find(
        with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
        {"_id": 0, "chunk_id": 1, "parent_id": 1},
    ).to_list(length=None)
    return {
        str(row.get("chunk_id") or ""): str(row.get("parent_id") or "")
        for row in rows
        if row.get("chunk_id")
    }


def _relation_rows_from_extraction(row: dict[str, Any]) -> list[dict[str, Any]]:
    chunk_id = str(row.get("chunk_id") or "")
    doc_id = str(row.get("doc_id") or "")
    schema_version = str(row.get("schema_version") or "polymath.extract.v1")
    out: list[dict[str, Any]] = []
    for relation in row.get("relations") or []:
        subject = str(relation.get("subject") or "").strip()
        obj = str(relation.get("object") or "").strip()
        predicate = str(relation.get("predicate") or "").strip()
        if not subject or not obj or not predicate:
            continue
        out.append({
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "subject_id": entity_id_from_name(subject),
            "object_id": entity_id_from_name(obj),
            "predicate": predicate,
            "evidence_phrase": relation.get("evidence_phrase") or "",
            "confidence": float(relation.get("confidence") or 0.0),
            "relation_family": relation.get("relation_family") or "",
            "source_predicate": relation.get("source_predicate") or predicate,
            "relation_cue": relation.get("relation_cue") or "",
            "validation_status": relation.get("validation_status") or "",
            "schema_version": schema_version,
        })
    return out


def _candidate_docs_pipeline(*, batch_size: int, corpus_id: str | None = None) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"status": "ok"}
    if corpus_id:
        match["corpus_id"] = corpus_id
    return [
        {"$match": match},
        {"$group": {"_id": {"corpus_id": "$corpus_id", "doc_id": "$doc_id"}}},
        {
            "$lookup": {
                "from": "documents",
                "let": {"corpus_id": "$_id.corpus_id", "doc_id": "$_id.doc_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$corpus_id", "$$corpus_id"]},
                                    {"$eq": ["$doc_id", "$$doc_id"]},
                                ]
                            },
                            "relation_support_backfilled_at": {"$exists": False},
                            "write_state.neo4j_written": True,
                            "$or": [
                                {"status": {"$exists": False}},
                                {"status": "active"},
                            ],
                        }
                    },
                    {"$project": {"_id": 0, "doc_id": 1, "corpus_id": 1}},
                ],
                "as": "doc",
            }
        },
        {"$unwind": "$doc"},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$limit": batch_size},
    ]


async def _ensure_relation_support_indexes(db: Any) -> None:
    await db["relation_support_records"].create_index(
        "support_id",
        unique=True,
        name="relation_support_support_id_unique",
    )
    await db["relation_support_records"].create_index(
        [("corpus_id", 1), ("doc_id", 1), ("status", 1)],
        name="relation_support_doc_status",
    )
    await db["relation_support_records"].create_index(
        [("corpus_id", 1), ("status", 1)],
        name="relation_support_corpus_status",
    )
    await db["relation_support_records"].create_index(
        [("edge_key", 1), ("status", 1), ("corpus_id", 1)],
        name="relation_support_edge_status",
    )
    await db["relation_support_records"].create_index("chunk_id")


async def _backfill_doc(db: Any, doc: dict[str, Any], *, dry_run: bool) -> tuple[int, int]:
    corpus_id = str(doc.get("corpus_id") or "")
    doc_id = str(doc.get("doc_id") or "")
    if not corpus_id or not doc_id:
        return 0, 0

    extraction_rows = await db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
        {
            "_id": 0,
            "schema_version": 1,
            "chunk_id": 1,
            "doc_id": 1,
            "relations": 1,
        },
    ).to_list(length=None)
    relation_rows: list[dict[str, Any]] = []
    for extraction in extraction_rows:
        relation_rows.extend(_relation_rows_from_extraction(extraction))

    chunk_parent_ids = await _chunk_parent_map(db, corpus_id=corpus_id, doc_id=doc_id)
    support_records = _relation_support_records(
        relation_rows=relation_rows,
        corpus_id=corpus_id,
        doc_id=doc_id,
        chunk_parent_ids=chunk_parent_ids,
    )
    if not dry_run:
        await replace_relation_support_for_document(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            records=support_records,
        )
        await db["documents"].update_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "$set": {
                    "relation_support_backfilled_at": datetime.utcnow(),
                    "relation_support_backfilled_count": len(support_records),
                }
            },
        )
    return len(extraction_rows), len(support_records)


async def backfill(
    *,
    mongo_uri: str,
    database: str | None,
    corpus_id: str | None,
    batch_size: int,
    max_batches: int,
    dry_run: bool,
) -> None:
    client = AsyncIOMotorClient(mongo_uri)
    db = client[database] if database else client.get_default_database()
    total_docs = 0
    total_extractions = 0
    total_support = 0
    try:
        await _ensure_relation_support_indexes(db)
        for batch_idx in range(max_batches):
            docs = await db["ghost_b_extractions"].aggregate(
                _candidate_docs_pipeline(batch_size=batch_size, corpus_id=corpus_id),
                allowDiskUse=True,
            ).to_list(length=batch_size)
            if not docs:
                break
            batch_extractions = 0
            batch_support = 0
            for doc in docs:
                extraction_count, support_count = await _backfill_doc(
                    db,
                    doc,
                    dry_run=dry_run,
                )
                batch_extractions += extraction_count
                batch_support += support_count
            total_docs += len(docs)
            total_extractions += batch_extractions
            total_support += batch_support
            print(
                "batch=%d docs=%d extractions=%d support_records=%d total_docs=%d total_support=%d dry_run=%s"
                % (
                    batch_idx + 1,
                    len(docs),
                    batch_extractions,
                    batch_support,
                    total_docs,
                    total_support,
                    dry_run,
                )
            )
            if dry_run:
                break
    finally:
        client.close()
    print({
        "docs": total_docs,
        "extractions": total_extractions,
        "support_records": total_support,
        "dry_run": dry_run,
    })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-uri", default=_env("MONGODB_URI"))
    parser.add_argument("--database", default=_env("MONGODB_DATABASE"))
    parser.add_argument("--corpus-id", default="")
    parser.add_argument("--batch-size", type=int, default=int(_env("SUPPORT_BACKFILL_BATCH_SIZE", "25")))
    parser.add_argument("--max-batches", type=int, default=int(_env("SUPPORT_BACKFILL_MAX_BATCHES", "1000")))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.mongo_uri:
        raise SystemExit("MONGODB_URI is required")
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.max_batches <= 0:
        raise SystemExit("--max-batches must be positive")
    asyncio.run(
        backfill(
            mongo_uri=args.mongo_uri,
            database=args.database or None,
            corpus_id=args.corpus_id or None,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
