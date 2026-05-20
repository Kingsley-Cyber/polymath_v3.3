"""Backfill Qdrant text payloads from Mongo canonical text.

This repairs old points that stored ``chunk_text[:512]`` without re-embedding
or changing point IDs. It updates payload fields only:

  chunk_text, text_len, text_hash, is_truncated

Dry-run is the default. Pass ``--apply`` to write the payload updates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_settings
from qdrant_client import AsyncQdrantClient

from services.conversation import conversation_service
from services.storage.qdrant_writer import _col_for_corpus, payload_text_contract

logger = logging.getLogger(__name__)


async def _load_corpus_ids(db, requested: list[str] | None) -> list[str]:
    if requested:
        return requested
    rows = await db["corpora"].find({}, {"_id": 0, "corpus_id": 1}).to_list(length=None)
    return [str(row.get("corpus_id")) for row in rows if row.get("corpus_id")]


async def _load_expected_texts(db, corpus_id: str) -> dict[str, str]:
    expected: dict[str, str] = {}
    cursor = db["chunks"].find(
        {"corpus_id": corpus_id},
        {"_id": 0, "chunk_id": 1, "text": 1},
    )
    async for row in cursor:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id:
            expected[chunk_id] = str(row.get("text") or "")

    docs = db["documents"].find(
        {"corpus_id": corpus_id},
        {"_id": 0, "parent_chunks.parent_id": 1, "parent_chunks.summary": 1},
    )
    async for doc in docs:
        for parent in doc.get("parent_chunks") or []:
            parent_id = str(parent.get("parent_id") or "")
            summary = str(parent.get("summary") or "")
            if parent_id and summary:
                expected[f"{parent_id}_summary"] = summary
    return expected


def _needs_update(payload: dict[str, Any], expected_text: str) -> bool:
    contract = payload_text_contract(expected_text)
    try:
        text_len = int(payload.get("text_len"))
    except Exception:
        text_len = -1
    return (
        str(payload.get("chunk_text") or payload.get("text") or "") != expected_text
        or text_len != contract["text_len"]
        or payload.get("text_hash") != contract["text_hash"]
        or payload.get("is_truncated") is not False
    )


async def _backfill_collection(
    qdrant: AsyncQdrantClient,
    *,
    collection_name: str,
    expected: dict[str, str],
    apply: bool,
) -> dict[str, int]:
    stats = {
        "points": 0,
        "matched": 0,
        "updated": 0,
        "missing_mongo": 0,
        "already_ok": 0,
    }
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
            expected_text = expected.get(chunk_id)
            if expected_text is None:
                stats["missing_mongo"] += 1
                continue
            stats["matched"] += 1
            if not _needs_update(payload, expected_text):
                stats["already_ok"] += 1
                continue
            stats["updated"] += 1
            if apply:
                await qdrant.set_payload(
                    collection_name=collection_name,
                    points=[point.id],
                    payload={
                        "chunk_text": expected_text,
                        **payload_text_contract(expected_text),
                    },
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
        resolved_corpus_ids = await _load_corpus_ids(db, corpus_ids)
        result: dict[str, Any] = {"apply": apply, "corpora": {}}
        for corpus_id in resolved_corpus_ids:
            expected = await _load_expected_texts(db, corpus_id)
            corpus_stats: dict[str, Any] = {
                "expected_texts": len(expected),
                "collections": {},
            }
            for kind in kinds:
                collection_name = _col_for_corpus(corpus_id, kind)
                try:
                    corpus_stats["collections"][kind] = await _backfill_collection(
                        qdrant,
                        collection_name=collection_name,
                        expected=expected,
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write updates. Omit for dry-run.",
    )
    return parser.parse_args()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    result = await run(corpus_ids=args.corpus_ids, kinds=kinds, apply=args.apply)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
