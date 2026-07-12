"""Exclude zero-chunk cover/navigation shells from corpus readiness.

The migration is intentionally explicit and bounded: operators pass exact
document IDs, the source is reparsed with the normal adapter, and a document is
changed only when it has zero Mongo chunks/parents and no visible semantic
content after deterministic markup normalization.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.docling_adapter import (
    has_retrievable_content,
    parse_document,
    retrievable_content_text,
)

TERMINAL_REASON = (
    "Source contains no retrievable text after deterministic markup "
    "normalization (for example, only cover images or empty EPUB anchors)."
)


async def _source_path(db: Any, *, corpus_id: str, doc_id: str) -> str:
    row = await db["source_parse_jobs"].find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"_id": 0, "source_path": 1},
        sort=[("updated_at", -1)],
    )
    return str((row or {}).get("source_path") or "")


async def inspect_document(db: Any, *, corpus_id: str, doc_id: str) -> dict[str, Any]:
    doc = await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"_id": 0, "doc_id": 1, "filename": 1, "ingest_stage": 1},
    )
    if not doc:
        return {"doc_id": doc_id, "status": "not_found", "eligible": False}
    child_count, parent_count = await asyncio.gather(
        db["chunks"].count_documents({"corpus_id": corpus_id, "doc_id": doc_id}),
        db["parent_chunks"].count_documents(
            {"corpus_id": corpus_id, "doc_id": doc_id}
        ),
    )
    if child_count or parent_count:
        return {
            "doc_id": doc_id,
            "status": "has_retrieval_artifacts",
            "eligible": False,
            "child_count": int(child_count),
            "parent_count": int(parent_count),
        }
    source_path = await _source_path(db, corpus_id=corpus_id, doc_id=doc_id)
    path = Path(source_path)
    if not source_path or not path.is_file():
        return {
            "doc_id": doc_id,
            "status": "source_missing",
            "eligible": False,
            "source_path": source_path,
        }
    data = path.read_bytes()
    mime, _ = mimetypes.guess_type(path.name)
    parsed = await parse_document(
        data,
        filename=str(doc.get("filename") or path.name),
        mime=mime or "application/octet-stream",
        do_ocr=False,
    )
    visible = retrievable_content_text(parsed)
    return {
        "doc_id": doc_id,
        "status": "nonsemantic_shell" if not has_retrievable_content(parsed) else "visible_content",
        "eligible": not has_retrievable_content(parsed),
        "source_path": source_path,
        "source_bytes": len(data),
        "visible_chars": len(visible),
        "child_count": 0,
        "parent_count": 0,
    }


async def apply_exclusion(db: Any, *, corpus_id: str, doc_id: str) -> dict[str, int]:
    now = datetime.utcnow()
    document = await db["documents"].update_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "ingest_stage": "skipped_nonsemantic",
                "queryable": False,
                "skipped_reason": TERMINAL_REASON,
                "excluded_from_readiness": True,
                "enrichment_pending_reason": None,
                "enrichment_status": {"summary": "excluded", "graph": "excluded"},
                "updated_at": now,
            },
            "$unset": {"error": ""},
            "$addToSet": {"write_state.warnings": TERMINAL_REASON},
        },
    )
    source = await db["source_parse_jobs"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "status": "skipped",
                "reason": "nonsemantic_source",
                "updated_at": now,
                "completed_at": now,
            },
            "$unset": {"lease_until": "", "runner": "", "started_at": ""},
        },
    )
    pipeline = await db["document_pipeline_jobs"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "status": "skipped",
                "reason": "nonsemantic_source",
                "updated_at": now,
                "completed_at": now,
            },
            "$unset": {"lease_until": "", "runner": "", "started_at": ""},
        },
    )
    summary = await db["summary_jobs"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "status": "superseded",
                "reason": "document_excluded_from_readiness",
                "updated_at": now,
                "artifact_reconciled_at": now,
            },
            "$unset": {"lease_until": "", "runner": "", "started_at": ""},
        },
    )
    graph = await db["graph_promotion_jobs"].update_many(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "status": "skipped",
                "reason": "nonsemantic_source",
                "updated_at": now,
                "completed_at": now,
            },
            "$unset": {"lease_until": "", "runner": "", "started_at": ""},
        },
    )
    return {
        "documents": int(document.modified_count or 0),
        "source_parse_jobs": int(source.modified_count or 0),
        "document_pipeline_jobs": int(pipeline.modified_count or 0),
        "summary_jobs": int(summary.modified_count or 0),
        "graph_promotion_jobs": int(graph.modified_count or 0),
    }


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client.get_default_database()
    try:
        for doc_id in list(dict.fromkeys(args.doc_id)):
            result = await inspect_document(
                db,
                corpus_id=args.corpus_id,
                doc_id=doc_id,
            )
            if args.apply and result.get("eligible"):
                result["modified"] = await apply_exclusion(
                    db,
                    corpus_id=args.corpus_id,
                    doc_id=doc_id,
                )
                result["status"] = "excluded"
            print(result)
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--doc-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
