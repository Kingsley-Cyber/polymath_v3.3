"""Backfill missing graph-default payload keys on Qdrant graph points.

New writes include these keys directly. Older active points may lack them, which
forces query code to branch on absent payload shape. This script patches only
missing or null keys and never overwrites existing promoted graph values.

Dry-run is the default. Pass ``--apply`` to write payload updates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_settings
from qdrant_client import AsyncQdrantClient

from services.conversation import conversation_service
from services.storage.qdrant_writer import _col_for_corpus

logger = logging.getLogger(__name__)


GRAPH_DEFAULTS: dict[str, Any] = {
    "concepts": [],
    "entity_ids": [],
    "relation_predicates": [],
    "relation_families": [],
    "fact_types": [],
    "related_entities": [],
    "has_relations": False,
    "extract_schema_version": "",
    "promote_version": "",
}


async def _load_corpus_ids(db: Any, requested: list[str] | None) -> list[str]:
    if requested:
        return requested
    rows = await db["corpora"].find(
        {"$or": [{"status": {"$exists": False}}, {"status": "active"}]},
        {"_id": 0, "corpus_id": 1},
    ).to_list(length=None)
    return [str(row.get("corpus_id")) for row in rows if row.get("corpus_id")]


def _missing_graph_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in GRAPH_DEFAULTS.items()
        if key not in payload or payload.get(key) is None
    }


def _patch_key(patch: dict[str, Any]) -> str:
    return json.dumps(patch, sort_keys=True, separators=(",", ":"))


async def _backfill_collection(
    qdrant: AsyncQdrantClient,
    *,
    collection_name: str,
    apply: bool,
    page_size: int,
) -> dict[str, int]:
    stats = {
        "points": 0,
        "patched_points": 0,
        "set_payload_calls": 0,
    }
    offset = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=collection_name,
            limit=page_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        grouped: dict[str, list[Any]] = defaultdict(list)
        patches: dict[str, dict[str, Any]] = {}
        for point in points:
            stats["points"] += 1
            patch = _missing_graph_defaults(point.payload or {})
            if not patch:
                continue
            stats["patched_points"] += 1
            key = _patch_key(patch)
            grouped[key].append(point.id)
            patches[key] = patch

        if apply:
            for key, point_ids in grouped.items():
                await qdrant.set_payload(
                    collection_name=collection_name,
                    points=point_ids,
                    payload=patches[key],
                )
                stats["set_payload_calls"] += 1
        elif grouped:
            stats["set_payload_calls"] += len(grouped)

        if offset is None:
            break
    return stats


async def run(
    *,
    corpus_ids: list[str] | None,
    apply: bool,
    page_size: int,
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
            collection_name = _col_for_corpus(corpus_id, "graph")
            try:
                result["corpora"][corpus_id] = await _backfill_collection(
                    qdrant,
                    collection_name=collection_name,
                    apply=apply,
                    page_size=page_size,
                )
            except Exception as exc:
                result["corpora"][corpus_id] = {"error": str(exc)}
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
        help="Corpus id to backfill. Repeat for multiple. Default: all active corpora.",
    )
    parser.add_argument("--page-size", type=int, default=512)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write updates. Omit for dry-run.",
    )
    return parser.parse_args()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    result = await run(
        corpus_ids=args.corpus_ids,
        apply=args.apply,
        page_size=args.page_size,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
