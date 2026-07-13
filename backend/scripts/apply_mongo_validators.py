#!/usr/bin/env python3
"""Apply (or dry-run) the P0.8 warn-first Mongo JSON-schema validators.

Dry-run (default) is READ-ONLY: for each target collection it samples up
to 2000 documents and counts how many violate the proposed $jsonSchema,
evaluated inside an aggregation pipeline. Nothing is modified.

    python3 backend/scripts/apply_mongo_validators.py

Apply mode attaches the validators via collMod (create_collection
fallback), defaulting to validationAction "warn" / validationLevel
"moderate" so no existing writer can break:

    python3 backend/scripts/apply_mongo_validators.py --apply
    python3 backend/scripts/apply_mongo_validators.py --apply --action warn

Run from the deployment host (127.0.0.1 service ports, repo .env,
.tmp_pkgs pymongo — same conventions as capture_raptor_baseline.py).
POLYMATH_ENV_FILE / POLYMATH_PKGS_DIR override the .env / .tmp_pkgs
locations when running from a worktree that lacks them.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
SAMPLE_SIZE = 2000


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


def _load_validators():
    """Load schema_validators.py directly from file (no package side effects)."""
    path = REPO / "backend" / "services" / "storage" / "schema_validators.py"
    spec = importlib.util.spec_from_file_location("p0_8_schema_validators", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _AsyncDbAdapter:
    """Awaitable facade over a sync pymongo Database for apply_validators."""

    def __init__(self, db):
        self._db = db

    async def command(self, document):
        return self._db.command(document)

    async def create_collection(self, name, **kwargs):
        return self._db.create_collection(name, **kwargs)


def _dry_run(db, validators: dict) -> int:
    print(
        f"DRY RUN (read-only): violation counts over a {SAMPLE_SIZE}-doc "
        "$sample per collection"
    )
    report: dict = {}
    for collection, schema in validators.items():
        total = db[collection].estimated_document_count()
        sampled = min(SAMPLE_SIZE, total)
        pipeline = [
            {"$sample": {"size": SAMPLE_SIZE}},
            {"$match": {"$nor": [{"$jsonSchema": schema["$jsonSchema"]}]}},
            {"$count": "violations"},
        ]
        rows = list(db[collection].aggregate(pipeline))
        violations = rows[0]["violations"] if rows else 0
        report[collection] = {
            "estimated_total": total,
            "sampled": sampled,
            "violations_in_sample": violations,
        }
        print(
            f"  {collection}: total~{total} sampled={sampled} "
            f"violations={violations}"
        )
    print(json.dumps(report, indent=2))
    print("No changes were made. Re-run with --apply to attach validators.")
    return 0


def _apply(db, module, action: str) -> int:
    import asyncio

    adapter = _AsyncDbAdapter(db)
    results = asyncio.run(module.apply_validators(adapter, action=action))
    print(json.dumps(results, indent=2))
    failed = [c for c, r in results.items() if r.get("status") == "failed"]
    if failed:
        print(f"FAILED collections: {failed}", file=sys.stderr)
        return 1
    print(f"Validators attached with validationAction={action!r}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P0.8 Mongo JSON-schema validators (dry-run by default)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Attach the validators (default: read-only violation census).",
    )
    parser.add_argument(
        "--action",
        choices=["warn", "error"],
        default="warn",
        help="validationAction used with --apply (default: warn).",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(_pkgs_path()))
    module = _load_validators()
    env = _env()
    client, db = _mongo(env)
    try:
        if args.apply:
            return _apply(db, module, args.action)
        return _dry_run(db, module.VALIDATORS)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
