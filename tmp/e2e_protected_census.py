"""Capture/compare the frozen existing-corpus store surface around the E2E."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings
from scripts.semantic_gateway_ugo_canary import _canonical_store_census


BASELINE = Path(
    "/data/ingest-files/runpod-job-journals/e2e-protected-existing-baseline.json"
)
SCHEMA = "e2e_protected_existing_corpora.v1"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _stable_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(_stable_bytes(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def _mongo_counts(database: Any, corpus_ids: list[str]) -> dict[str, dict[str, int]]:
    if not corpus_ids:
        return {}
    counts: dict[str, dict[str, int]] = {}
    for collection_name in sorted(await database.list_collection_names()):
        if collection_name.startswith("system."):
            continue
        pipeline = [
            {"$match": {"corpus_id": {"$in": corpus_ids}}},
            {"$group": {"_id": "$corpus_id", "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        try:
            rows = await database[collection_name].aggregate(
                pipeline, allowDiskUse=True
            ).to_list(length=len(corpus_ids) + 1)
        except Exception as exc:
            raise RuntimeError(
                f"Mongo protected census failed for {collection_name}: "
                f"{type(exc).__name__}"
            ) from exc
        grouped = {
            str(row["_id"]): int(row["count"])
            for row in rows
            if row.get("_id") in corpus_ids
        }
        if grouped:
            counts[collection_name] = grouped
    return counts


async def _neo4j_counts(settings: Any, corpus_ids: list[str]) -> dict[str, dict[str, int]]:
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    result: dict[str, dict[str, int]] = {}
    try:
        async with driver.session() as session:
            for corpus_id in corpus_ids:
                node = await (
                    await session.run(
                        "MATCH (n) WHERE n.corpus_id = $corpus_id "
                        "RETURN count(n) AS count",
                        corpus_id=corpus_id,
                    )
                ).single()
                relationship = await (
                    await session.run(
                        "MATCH (a)-[r]->(b) "
                        "WHERE r.corpus_id = $corpus_id "
                        "OR a.corpus_id = $corpus_id "
                        "OR b.corpus_id = $corpus_id "
                        "RETURN count(r) AS count",
                        corpus_id=corpus_id,
                    )
                ).single()
                node_count = int(node["count"] if node else 0)
                relationship_count = int(
                    relationship["count"] if relationship else 0
                )
                if node_count or relationship_count:
                    result[corpus_id] = {
                        "nodes": node_count,
                        "relationships_touching_corpus": relationship_count,
                    }
    finally:
        await driver.close()
    return result


async def _capture(*, frozen_ids: list[str] | None = None) -> dict[str, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        if frozen_ids is None:
            rows = await database["corpora"].find(
                {}, {"_id": 0, "corpus_id": 1}
            ).to_list(length=None)
            corpus_ids = sorted(
                {
                    str(row.get("corpus_id") or "")
                    for row in rows
                    if row.get("corpus_id")
                }
            )
        else:
            corpus_ids = sorted(set(frozen_ids))
        prefixes = [corpus_id[:8] for corpus_id in corpus_ids]
        if len(prefixes) != len(set(prefixes)):
            raise RuntimeError("existing corpus Qdrant prefixes are not unique")
        canonical = await _canonical_store_census(db=database, settings=settings)
        qdrant = canonical.get("qdrant_collection_points") or {}
        protected_qdrant = {
            corpus_id: {
                name: int(count)
                for name, count in sorted(qdrant.items())
                if name.startswith(f"corpus_{corpus_id[:8]}_")
            }
            for corpus_id in corpus_ids
        }
        protected_qdrant = {
            corpus_id: rows
            for corpus_id, rows in protected_qdrant.items()
            if rows
        }
        return {
            "schema_version": SCHEMA,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "frozen_existing_corpus_ids": corpus_ids,
            "canonical_store_census": canonical,
            "mongo_counts_by_collection_and_corpus": await _mongo_counts(
                database, corpus_ids
            ),
            "qdrant_counts_by_corpus": protected_qdrant,
            "neo4j_counts_by_corpus": await _neo4j_counts(settings, corpus_ids),
        }
    finally:
        client.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("baseline", "compare"))
    args = parser.parse_args()
    if args.action == "baseline":
        if BASELINE.exists():
            raise RuntimeError(f"refusing to overwrite existing baseline: {BASELINE}")
        snapshot = await _capture()
        snapshot["snapshot_sha256"] = hashlib.sha256(
            _stable_bytes(
                {
                    key: value
                    for key, value in snapshot.items()
                    if key not in {"captured_at_utc", "snapshot_sha256"}
                }
            )
        ).hexdigest()
        _atomic_write(BASELINE, snapshot)
        print(
            json.dumps(
                {
                    "action": "baseline",
                    "baseline_path": str(BASELINE),
                    "canonical_scope": snapshot["canonical_store_census"][
                        "census_scope_version"
                    ],
                    "existing_corpus_count": len(
                        snapshot["frozen_existing_corpus_ids"]
                    ),
                    "mongo_collection_count": len(
                        snapshot["mongo_counts_by_collection_and_corpus"]
                    ),
                    "neo4j_corpus_count": len(snapshot["neo4j_counts_by_corpus"]),
                    "qdrant_corpus_count": len(snapshot["qdrant_counts_by_corpus"]),
                    "snapshot_sha256": snapshot["snapshot_sha256"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if not BASELINE.is_file():
        raise RuntimeError(f"baseline is absent: {BASELINE}")
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = await _capture(frozen_ids=baseline["frozen_existing_corpus_ids"])
    fields = (
        "mongo_counts_by_collection_and_corpus",
        "qdrant_counts_by_corpus",
        "neo4j_counts_by_corpus",
    )
    changed = [field for field in fields if baseline.get(field) != current.get(field)]
    result = {
        "action": "compare",
        "canonical_scope": current["canonical_store_census"][
            "census_scope_version"
        ],
        "changed_protected_fields": changed,
        "existing_corpus_count": len(baseline["frozen_existing_corpus_ids"]),
        "protected_existing_corpora_unchanged": not changed,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if changed:
        raise RuntimeError("protected existing-corpus census changed")


if __name__ == "__main__":
    asyncio.run(main())
