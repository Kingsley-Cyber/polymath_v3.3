#!/usr/bin/env python3
"""Normalize extraction provenance in ghost_b_extractions (P0.8).

Dry-run (default) is READ-ONLY: prints row counts grouped by
schema_version plus the extractor-missing count.

    python3 backend/scripts/normalize_extraction_provenance.py

Apply mode backfills, in 1000-op bulk batches:

  - schema_version = "polymath.extract.v1"  ONLY where missing or None
  - extractor      = "legacy_unknown"       ONLY where missing or None
  - provenance_normalized_at = <run timestamp> on every modified row

Before any write it saves a JSONL backup (modified _id + prior field
values, absence preserved by key omission) to docs/baselines/p0_8_backups/.

    python3 backend/scripts/normalize_extraction_provenance.py --apply

Run from the deployment host (127.0.0.1 service ports, repo .env,
.tmp_pkgs pymongo — same conventions as capture_raptor_baseline.py).
POLYMATH_ENV_FILE / POLYMATH_PKGS_DIR override the .env / .tmp_pkgs
locations when running from a worktree that lacks them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
COLLECTION = "ghost_b_extractions"
DEFAULT_SCHEMA_VERSION = "polymath.extract.v1"
DEFAULT_EXTRACTOR = "legacy_unknown"
BATCH_SIZE = 1000
BACKUP_DIR = REPO / "docs" / "baselines" / "p0_8_backups"

_MISSING_OR_NULL_QUERY = {
    "$or": [
        {"schema_version": {"$exists": False}},
        {"schema_version": None},
        {"extractor": {"$exists": False}},
        {"extractor": None},
    ]
}


def _env_path() -> Path:
    override = os.environ.get("POLYMATH_ENV_FILE")
    return Path(override) if override else REPO / ".env"


def _pkgs_path() -> Path:
    override = os.environ.get("POLYMATH_PKGS_DIR")
    return Path(override) if override else REPO / ".tmp_pkgs"


def _env() -> dict:
    out: dict = {}
    for line in _env_path().read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _mongo(env: dict):
    from pymongo import MongoClient

    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    db = client[env.get("MONGODB_DATABASE", "polymath")]
    db.command("ping")
    return client, db


def _census(coll) -> dict:
    by_version = {
        str(row["_id"]): row["n"]
        for row in coll.aggregate(
            [
                {"$group": {"_id": "$schema_version", "n": {"$sum": 1}}},
                {"$sort": {"n": -1}},
            ]
        )
    }
    extractor_missing = coll.count_documents(
        {"$or": [{"extractor": {"$exists": False}}, {"extractor": None}]}
    )
    schema_version_missing = coll.count_documents(
        {"$or": [{"schema_version": {"$exists": False}}, {"schema_version": None}]}
    )
    return {
        "total": coll.estimated_document_count(),
        "by_schema_version": by_version,
        "schema_version_missing_or_null": schema_version_missing,
        "extractor_missing_or_null": extractor_missing,
        "rows_needing_normalization": coll.count_documents(_MISSING_OR_NULL_QUERY),
    }


def _dry_run(coll) -> int:
    print(f"DRY RUN (read-only) census of {COLLECTION}:")
    print(json.dumps(_census(coll), indent=2, default=str))
    print("No changes were made. Re-run with --apply to normalize.")
    return 0


def _backup(coll) -> tuple[Path, int]:
    """Write a JSONL backup of every row that would be modified. Runs FIRST."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    path = BACKUP_DIR / f"ghost_b_provenance_{stamp}.jsonl"
    n = 0
    with path.open("w") as fh:
        cursor = coll.find(
            _MISSING_OR_NULL_QUERY,
            {"_id": 1, "schema_version": 1, "extractor": 1},
        )
        for row in cursor:
            record: dict = {"_id": str(row["_id"]), "prior": {}}
            # Preserve missing-vs-null distinction by key omission.
            if "schema_version" in row:
                record["prior"]["schema_version"] = row["schema_version"]
            if "extractor" in row:
                record["prior"]["extractor"] = row["extractor"]
            fh.write(json.dumps(record, default=str) + "\n")
            n += 1
    print(f"BACKUP written: {path} ({n} rows)")
    return path, n


def _apply(coll) -> int:
    from pymongo import UpdateOne

    print("BEFORE:")
    print(json.dumps(_census(coll), indent=2, default=str))

    _backup(coll)

    normalized_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ops: list = []
    modified = 0
    cursor = coll.find(
        _MISSING_OR_NULL_QUERY,
        {"_id": 1, "schema_version": 1, "extractor": 1},
    )
    for row in cursor:
        updates: dict = {}
        if row.get("schema_version") is None:
            updates["schema_version"] = DEFAULT_SCHEMA_VERSION
        if row.get("extractor") is None:
            updates["extractor"] = DEFAULT_EXTRACTOR
        if not updates:
            continue
        updates["provenance_normalized_at"] = normalized_at
        ops.append(UpdateOne({"_id": row["_id"]}, {"$set": updates}))
        if len(ops) >= BATCH_SIZE:
            result = coll.bulk_write(ops, ordered=False)
            modified += result.modified_count
            ops = []
    if ops:
        result = coll.bulk_write(ops, ordered=False)
        modified += result.modified_count

    print(f"MODIFIED {modified} rows (bulk batches of {BATCH_SIZE}).")
    print("AFTER:")
    print(json.dumps(_census(coll), indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize ghost_b_extractions provenance (dry-run by default)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Backfill schema_version/extractor where missing or None "
            "(default: read-only census). Writes a JSONL backup first."
        ),
    )
    args = parser.parse_args()

    sys.path.insert(0, str(_pkgs_path()))
    env = _env()
    client, db = _mongo(env)
    try:
        coll = db[COLLECTION]
        if args.apply:
            return _apply(coll)
        return _dry_run(coll)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
