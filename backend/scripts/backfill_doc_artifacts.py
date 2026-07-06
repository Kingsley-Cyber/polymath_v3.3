"""Backfill passive doc_profile.doc_artifact records for existing documents.

The artifact is a synthesis-only source-role label. This script does not embed
artifact text, does not touch Qdrant, and does not alter retrieval state.

Dry-run is the default. Pass ``--apply`` to write updates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.conversation import conversation_service
from services.ingestion.doc_artifact import build_doc_artifact
from services.storage.record_status import with_active_records

logger = logging.getLogger(__name__)


async def _ghost_entities(db: Any, *, corpus_id: str, doc_id: str, limit: int) -> list[Any]:
    rows = await db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
        {"_id": 0, "entities": 1},
    ).limit(limit).to_list(length=limit)
    return [entity for row in rows for entity in (row.get("entities") or [])]


async def _chunk_kind_stats(db: Any, *, corpus_id: str, doc_id: str) -> dict[str, int]:
    pipeline = [
        {"$match": with_active_records({"corpus_id": corpus_id, "doc_id": doc_id})},
        {"$group": {"_id": {"$ifNull": ["$chunk_kind", "body"]}, "count": {"$sum": 1}}},
    ]
    rows = await db["parent_chunks"].aggregate(pipeline).to_list(length=None)
    return {str(row.get("_id") or "body"): int(row.get("count") or 0) for row in rows}


async def _compile_for_doc(
    db: Any,
    doc: dict[str, Any],
    *,
    corpus_descriptions: dict[str, str],
    ghost_limit: int,
) -> dict[str, Any] | None:
    corpus_id = str(doc.get("corpus_id") or "")
    doc_id = str(doc.get("doc_id") or "")
    profile = doc.get("doc_profile") or {}
    if not corpus_id or not doc_id or not profile:
        return None
    existing_artifact = profile.get("doc_artifact") or {}
    return build_doc_artifact(
        doc_profile=profile,
        facet_profile=doc.get("facet_profile") or {},
        source_meta={
            "title": doc.get("title"),
            "filename": doc.get("filename"),
            "source_type": doc.get("source_type"),
            "source_path": doc.get("source_path"),
        },
        ghost_b_entities=await _ghost_entities(
            db,
            corpus_id=corpus_id,
            doc_id=doc_id,
            limit=ghost_limit,
        ),
        chunk_kind_stats=await _chunk_kind_stats(db, corpus_id=corpus_id, doc_id=doc_id),
        owner_fields=existing_artifact,
        corpus_description=corpus_descriptions.get(corpus_id),
    )


async def run(
    *,
    corpus_ids: list[str] | None,
    apply: bool,
    limit: int,
    batch_size: int,
    ghost_limit: int,
) -> dict[str, Any]:
    await conversation_service.connect()
    try:
        db = conversation_service._db
        if db is None:
            raise RuntimeError("Mongo connection was not initialized")

        corpus_query: dict[str, Any] = {}
        if corpus_ids:
            corpus_query["corpus_id"] = {"$in": corpus_ids}
        corpora = await db["corpora"].find(
            with_active_records(corpus_query),
            {"_id": 0, "corpus_id": 1, "description": 1},
        ).to_list(length=None)
        descriptions = {
            str(row.get("corpus_id") or ""): str(row.get("description") or "")
            for row in corpora
            if row.get("corpus_id")
        }

        query: dict[str, Any] = {"doc_profile.summary": {"$exists": True}}
        if corpus_ids:
            query["corpus_id"] = {"$in": corpus_ids}

        scanned = written = skipped = 0
        cursor = db["documents"].find(
            with_active_records(query),
            {
                "_id": 0,
                "doc_id": 1,
                "corpus_id": 1,
                "filename": 1,
                "title": 1,
                "source_type": 1,
                "source_path": 1,
                "facet_profile": 1,
                "doc_profile": 1,
            },
        ).limit(limit if limit > 0 else 0)

        async for doc in cursor:
            scanned += 1
            artifact = await _compile_for_doc(
                db,
                doc,
                corpus_descriptions=descriptions,
                ghost_limit=ghost_limit,
            )
            if not artifact:
                skipped += 1
                continue
            if apply:
                await db["documents"].update_one(
                    {"corpus_id": doc["corpus_id"], "doc_id": doc["doc_id"]},
                    {
                        "$set": {
                            "doc_profile.doc_artifact": artifact,
                            "doc_profile.doc_artifact_backfilled_at": datetime.utcnow(),
                        }
                    },
                )
            written += 1
            if batch_size > 0 and scanned % batch_size == 0:
                logger.info("doc_artifact_backfill scanned=%d written=%d skipped=%d", scanned, written, skipped)

        return {"apply": apply, "scanned": scanned, "written": written, "skipped": skipped}
    finally:
        await conversation_service.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-id", action="append", dest="corpus_ids")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max docs. 0 means all.")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--ghost-limit", type=int, default=300)
    return parser.parse_args()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    result = await run(
        corpus_ids=args.corpus_ids,
        apply=args.apply,
        limit=args.limit,
        batch_size=args.batch_size,
        ghost_limit=args.ghost_limit,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
