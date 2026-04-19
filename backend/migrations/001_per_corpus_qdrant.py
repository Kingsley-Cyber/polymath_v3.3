"""
Migration 001 — global Qdrant collections → per-corpus Qdrant collections.

Big-bang cutover. The new code already targets `corpus_{cid8}_{kind}` everywhere;
this script moves existing point data from the legacy globals into the new
per-corpus families and then drops the globals.

Source collections (legacy globals — names from settings.QDRANT_*):
    polymath_naive, polymath_hrag, polymath_graph, polymath_schemas

Target collections (per-corpus, resolved via _col_for_corpus):
    corpus_{cid8}_{naive|hrag|graph|schemas}

Idempotency: point IDs are deterministic MD5-derived UUIDs (see
qdrant_writer._child_point_id / _summary_point_id / _schema_point_id), so a
re-run upserts the same IDs and the source filter-delete becomes a no-op.

Operational runbook:

    docker compose exec backend python -m migrations.001_per_corpus_qdrant --dry-run
    docker compose exec backend python -m migrations.001_per_corpus_qdrant --execute
    docker compose restart backend

A summary JSON is emitted to stderr for audit. On any per-corpus failure,
the migration HALTS — the legacy globals are NOT dropped, so the system can
keep running on the old code (or this script can be re-run after the issue
is fixed; deterministic IDs make retries safe).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from config import get_settings
from services.storage.qdrant_writer import (
    _col,
    _col_for_corpus,
    ensure_collections_for_corpus,
)

logger = logging.getLogger("migration.001_per_corpus_qdrant")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

BATCH = 256


async def _scroll_all(
    client: AsyncQdrantClient,
    collection: str,
    corpus_id: str,
) -> list[PointStruct]:
    """Scroll every point belonging to `corpus_id` from the given collection.
    Returns PointStructs (id, vector, payload) ready for re-upsert into the
    per-corpus collection.
    """
    all_points: list[PointStruct] = []
    offset: Any = None
    flt = Filter(must=[FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id))])
    while True:
        records, offset = await client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for r in records:
            all_points.append(
                PointStruct(id=r.id, vector=r.vector, payload=r.payload or {})
            )
        if offset is None:
            break
    return all_points


async def _delete_by_ids(
    client: AsyncQdrantClient,
    collection: str,
    ids: list[str | int],
) -> None:
    """Delete points by ID in batches."""
    if not ids:
        return
    for i in range(0, len(ids), BATCH):
        await client.delete(collection_name=collection, points_selector=ids[i : i + BATCH])


async def _migrate_corpus(
    client: AsyncQdrantClient,
    corpus_id: str,
    dim: int,
    dry_run: bool,
) -> dict[str, dict[str, int]]:
    """Migrate one corpus across all 4 kinds. Returns per-kind counters."""
    result: dict[str, dict[str, int]] = {}

    if not dry_run:
        await ensure_collections_for_corpus(client, corpus_id, dim=dim)

    for kind in ("naive", "hrag", "graph", "schemas"):
        src = _col(kind)
        if not src or not await client.collection_exists(src):
            result[kind] = {"copied": 0, "deleted": 0, "dest_count": 0}
            continue

        points = await _scroll_all(client, src, corpus_id)
        copied = len(points)

        if not dry_run and points:
            dest = _col_for_corpus(corpus_id, kind)
            for i in range(0, len(points), BATCH):
                await client.upsert(collection_name=dest, points=points[i : i + BATCH])
            ids = [p.id for p in points]
            await _delete_by_ids(client, src, ids)

        dest_count = 0
        if not dry_run:
            try:
                info = await client.get_collection(_col_for_corpus(corpus_id, kind))
                dest_count = int(getattr(info, "points_count", 0) or 0)
            except Exception:
                dest_count = 0

        result[kind] = {
            "copied": copied,
            "deleted": copied if not dry_run else 0,
            "dest_count": dest_count,
        }
    return result


async def _drop_globals(client: AsyncQdrantClient) -> dict[str, bool]:
    """Drop the 4 legacy global collections. Run only after every corpus
    migrated successfully and source counts are 0.
    """
    out: dict[str, bool] = {}
    for kind in ("naive", "hrag", "graph", "schemas"):
        name = _col(kind)
        if not name:
            out[kind] = False
            continue
        try:
            if await client.collection_exists(name):
                await client.delete_collection(collection_name=name)
                logger.info("Dropped legacy global collection: %s", name)
                out[name] = True
            else:
                out[name] = False
        except Exception as exc:
            logger.error("Failed to drop %s: %s", name, exc)
            out[name] = False
    return out


async def _verify_globals_empty(client: AsyncQdrantClient) -> dict[str, int]:
    """Final safety check before dropping — every legacy global should be
    empty (all corpus filters drained). Returns name → remaining count.
    """
    counts: dict[str, int] = {}
    for kind in ("naive", "hrag", "graph", "schemas"):
        name = _col(kind)
        if not name or not await client.collection_exists(name):
            counts[name or kind] = 0
            continue
        try:
            info = await client.get_collection(name)
            counts[name] = int(getattr(info, "points_count", 0) or 0)
        except Exception:
            counts[name] = -1
    return counts


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan source collections and print plan, but do not write or delete.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually copy + delete + drop globals after successful migration.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        parser.error("must pass --dry-run or --execute")
    if args.dry_run and args.execute:
        parser.error("--dry-run and --execute are mutually exclusive")

    settings = get_settings()
    dim = settings.EMBEDDING_DIMENSION

    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL)

    logger.info("Migration 001 starting (dry_run=%s)", args.dry_run)

    corpora = await db["corpora"].find({}, {"corpus_id": 1, "name": 1}).to_list(length=None)
    logger.info("Found %d corpora to migrate", len(corpora))

    summary: dict[str, Any] = {
        "dry_run": args.dry_run,
        "corpora": [],
        "totals": {"copied": 0, "deleted": 0},
        "globals_after": {},
        "globals_dropped": {},
        "halted": False,
    }

    try:
        for corpus in corpora:
            cid = corpus["corpus_id"]
            name = corpus.get("name", "?")
            try:
                per_kind = await _migrate_corpus(qdrant, cid, dim=dim, dry_run=args.dry_run)
            except Exception as exc:
                logger.error("Migration FAILED for corpus %s (%s): %s", cid, name, exc)
                summary["halted"] = True
                summary["error"] = f"corpus {cid}: {exc}"
                break

            entry = {"corpus_id": cid, "name": name, "kinds": per_kind}
            summary["corpora"].append(entry)
            for k in per_kind.values():
                summary["totals"]["copied"] += k["copied"]
                summary["totals"]["deleted"] += k["deleted"]
            logger.info(
                "Corpus %s (%s) — %s",
                cid[:8],
                name,
                ", ".join(f"{k}: copied={v['copied']} dest={v['dest_count']}" for k, v in per_kind.items()),
            )

        if not summary["halted"]:
            summary["globals_after"] = await _verify_globals_empty(qdrant)
            if args.execute:
                non_empty = {n: c for n, c in summary["globals_after"].items() if c > 0}
                if non_empty:
                    logger.warning(
                        "Refusing to drop globals — non-zero remainder: %s", non_empty
                    )
                    summary["globals_dropped"] = {}
                else:
                    summary["globals_dropped"] = await _drop_globals(qdrant)
    finally:
        await qdrant.close()
        mongo.close()

    print("\n=== MIGRATION SUMMARY ===", file=sys.stderr)
    print(json.dumps(summary, indent=2, default=str), file=sys.stderr)
    return 0 if not summary.get("halted") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
