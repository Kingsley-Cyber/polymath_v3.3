"""Backfill semantic facet metadata for already-ingested corpora.

New ingests write facet metadata during the worker pipeline. This script repairs
older documents/chunks and updates existing Qdrant point payloads without
re-embedding.

Dry-run is the default. Pass --apply to write updates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pymongo import UpdateOne

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_settings
from qdrant_client import AsyncQdrantClient
from services.conversation import conversation_service
from services.facets import (
    FACET_SCHEMA_VERSION,
    build_ingest_facet_profile,
    metadata_with_facets,
)
from services.storage.qdrant_writer import _col_for_corpus

logger = logging.getLogger(__name__)


async def _load_corpus_ids(db, requested: list[str] | None) -> list[str]:
    if requested:
        return requested
    rows = await db["corpora"].find({}, {"_id": 0, "corpus_id": 1}).to_list(length=None)
    return [str(row.get("corpus_id")) for row in rows if row.get("corpus_id")]


def _parent_obj(doc: dict[str, Any], parent: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        parent_id=str(parent.get("parent_id") or ""),
        doc_id=str(doc.get("doc_id") or ""),
        corpus_id=str(doc.get("corpus_id") or ""),
        text=str(parent.get("text") or ""),
        heading_path=parent.get("heading_path") or [],
        source_tier=parent.get("source_tier") or doc.get("source_tier") or "",
        metadata=parent.get("metadata") or {},
        children=[SimpleNamespace(chunk_id=cid) for cid in (parent.get("child_ids") or [])],
    )


def _child_obj(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_id=str(row.get("chunk_id") or ""),
        parent_id=str(row.get("parent_id") or ""),
        doc_id=str(row.get("doc_id") or ""),
        corpus_id=str(row.get("corpus_id") or ""),
        text=str(row.get("text") or ""),
        heading_path=row.get("heading_path") or [],
        source_tier=row.get("source_tier") or "",
        token_count=int(row.get("token_count") or 0),
        metadata=row.get("metadata") or {},
    )


def _compact_doc_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": profile.get("schema_version") or FACET_SCHEMA_VERSION,
        "doc_facets": profile.get("doc_facets") or [],
        "facet_ids": profile.get("facet_ids") or [],
        "facet_text": profile.get("facet_text") or "",
        "primary_facet_id": profile.get("primary_facet_id"),
        "source": profile.get("source") or "ingestion",
    }


def _carrier(facet_meta: dict[str, Any], doc_facet_ids: list[str]) -> dict[str, Any]:
    return {
        "facet_ids": facet_meta.get("facet_ids") or doc_facet_ids[:6],
        "facet_text": facet_meta.get("facet_text") or "",
        "content_facet_ids": facet_meta.get("content_facet_ids") or [],
        "content_facet_text": facet_meta.get("content_facet_text") or "",
        "content_facet_source": facet_meta.get("content_facet_source") or "",
        "content_facet_confidence": facet_meta.get("content_facet_confidence"),
        "doc_facet_ids": doc_facet_ids,
        "facet_schema_version": FACET_SCHEMA_VERSION,
    }


async def _backfill_mongo_for_doc(db, doc: dict[str, Any], *, apply: bool) -> tuple[dict[str, Any], int]:
    doc_id = str(doc.get("doc_id") or "")
    corpus_id = str(doc.get("corpus_id") or "")
    filename = str(doc.get("filename") or doc.get("title") or doc_id)
    parent_rows = [p for p in (doc.get("parent_chunks") or []) if isinstance(p, dict)]
    chunk_rows = await db["chunks"].find(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "_id": 0,
            "chunk_id": 1,
            "parent_id": 1,
            "doc_id": 1,
            "corpus_id": 1,
            "text": 1,
            "heading_path": 1,
            "source_tier": 1,
            "token_count": 1,
            "metadata": 1,
        },
    ).to_list(length=None)
    parents = [_parent_obj(doc, row) for row in parent_rows]
    children = [_child_obj(row) for row in chunk_rows]
    summaries = [
        SimpleNamespace(
            parent_id=str(row.get("parent_id") or ""),
            summary=str(row.get("summary") or ""),
        )
        for row in parent_rows
        if str(row.get("parent_id") or "") and str(row.get("summary") or "").strip()
    ]
    profile = build_ingest_facet_profile(
        filename=filename,
        doc_id=doc_id,
        corpus_id=corpus_id,
        schema_lens=doc.get("schema_lens"),
        parents=parents,
        children=children,
        summaries=summaries,
    )
    doc_facet_ids = profile.get("facet_ids") or []
    parent_facets = profile.get("parent_facets") or {}
    child_facets = profile.get("child_facets") or {}

    updated_parents: list[dict[str, Any]] = []
    for parent in parent_rows:
        parent_id = str(parent.get("parent_id") or "")
        facet_meta = parent_facets.get(parent_id, {})
        carrier = _carrier(facet_meta, doc_facet_ids)
        updated = dict(parent)
        updated["facet_ids"] = carrier["facet_ids"]
        updated["facet_text"] = carrier["facet_text"]
        updated["content_facet_ids"] = carrier["content_facet_ids"]
        updated["content_facet_text"] = carrier["content_facet_text"]
        updated["content_facet_source"] = carrier["content_facet_source"]
        updated["content_facet_confidence"] = carrier["content_facet_confidence"]
        updated["metadata"] = metadata_with_facets(updated.get("metadata"), carrier)
        updated_parents.append(updated)

    chunk_ops = []
    for chunk in chunk_rows:
        chunk_id = str(chunk.get("chunk_id") or "")
        facet_meta = child_facets.get(chunk_id, {})
        carrier = _carrier(facet_meta, doc_facet_ids)
        chunk_ops.append(
            UpdateOne(
                {"corpus_id": corpus_id, "chunk_id": chunk_id},
                {
                    "$set": {
                        "facet_ids": carrier["facet_ids"],
                        "facet_text": carrier["facet_text"],
                        "content_facet_ids": carrier["content_facet_ids"],
                        "content_facet_text": carrier["content_facet_text"],
                        "content_facet_source": carrier["content_facet_source"],
                        "content_facet_confidence": carrier["content_facet_confidence"],
                        "metadata": metadata_with_facets(chunk.get("metadata"), carrier),
                    }
                },
            )
        )

    if apply:
        await db["documents"].update_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "$set": {
                    "facet_profile": _compact_doc_profile(profile),
                    "parent_chunks": updated_parents,
                }
            },
        )
        if chunk_ops:
            await db["chunks"].bulk_write(chunk_ops, ordered=False)
    return profile, len(chunk_ops)


async def _backfill_qdrant_collection(
    qdrant: AsyncQdrantClient,
    *,
    collection_name: str,
    payload_by_chunk_id: dict[str, dict[str, Any]],
    apply: bool,
) -> dict[str, int]:
    stats = {"points": 0, "matched": 0, "updated": 0}
    offset = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=collection_name,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            stats["points"] += 1
            payload = point.payload or {}
            chunk_id = str(payload.get("chunk_id") or "")
            update_payload = payload_by_chunk_id.get(chunk_id)
            if not update_payload:
                continue
            stats["matched"] += 1
            stats["updated"] += 1
            if apply:
                await qdrant.set_payload(
                    collection_name=collection_name,
                    points=[point.id],
                    payload=update_payload,
                )
        if offset is None:
            break
    return stats


async def run(
    *,
    corpus_ids: list[str] | None,
    kinds: list[str],
    apply: bool,
) -> dict[str, Any]:
    settings = get_settings()
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )
    await conversation_service.connect()
    try:
        db = conversation_service._db
        if db is None:
            raise RuntimeError("Mongo connection was not initialized")
        resolved = await _load_corpus_ids(db, corpus_ids)
        result: dict[str, Any] = {"apply": apply, "corpora": {}}
        for corpus_id in resolved:
            docs = await db["documents"].find(
                {"corpus_id": corpus_id},
                {
                    "_id": 0,
                    "doc_id": 1,
                    "corpus_id": 1,
                    "filename": 1,
                    "title": 1,
                    "source_tier": 1,
                    "schema_lens": 1,
                    "parent_chunks": 1,
                },
            ).to_list(length=None)
            payload_by_chunk_id: dict[str, dict[str, Any]] = {}
            chunk_updates = 0
            for doc in docs:
                profile, updated_chunks = await _backfill_mongo_for_doc(db, doc, apply=apply)
                chunk_updates += updated_chunks
                doc_facet_ids = profile.get("facet_ids") or []
                for chunk_id, facet_meta in (profile.get("child_facets") or {}).items():
                    carrier = _carrier(facet_meta, doc_facet_ids)
                    payload_by_chunk_id[str(chunk_id)] = carrier
                for parent_id, facet_meta in (profile.get("parent_facets") or {}).items():
                    carrier = _carrier(facet_meta, doc_facet_ids)
                    payload_by_chunk_id[f"{parent_id}_summary"] = carrier
            corpus_stats: dict[str, Any] = {
                "documents": len(docs),
                "chunk_updates": chunk_updates,
                "qdrant_payloads": len(payload_by_chunk_id),
                "collections": {},
            }
            for kind in kinds:
                collection_name = _col_for_corpus(corpus_id, kind)
                try:
                    corpus_stats["collections"][kind] = await _backfill_qdrant_collection(
                        qdrant,
                        collection_name=collection_name,
                        payload_by_chunk_id=payload_by_chunk_id,
                        apply=apply,
                    )
                except Exception as exc:
                    corpus_stats["collections"][kind] = {"error": str(exc)}
            result["corpora"][corpus_id] = corpus_stats
        return result
    finally:
        await qdrant.close()
        await conversation_service.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-id",
        action="append",
        dest="corpus_ids",
        help="Corpus id to backfill. Repeat for multiple. Default: all corpora.",
    )
    parser.add_argument(
        "--kinds",
        default="naive,hrag,graph",
        help="Comma-separated Qdrant collection kinds to scan.",
    )
    parser.add_argument("--apply", action="store_true", help="Write updates.")
    return parser.parse_args()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    result = await run(corpus_ids=args.corpus_ids, kinds=kinds, apply=args.apply)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
