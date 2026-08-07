#!/usr/bin/env python3
"""Mark a corpus as the permanent integration fixture (owner directive 2026-08-03).

The P5 book-ingestion canary keeps exactly ONE retained 4-document corpus as
the permanent integration fixture:

    fixture:
      purpose: permanent_integration_fixture
      production_visible: false
      excluded_from_user_search: true

The `excluded_from_user_search` flag is consumed by
IngestionService.list_corpora, which hides such corpora from user-facing
corpus lists. Direct access by corpus_id (get_corpus, retrieval, eval
scripts, this program's re-ingest probes) is unaffected.

WHAT THIS TOUCHES
    Sets/replaces the `fixture` subdocument on one corpus document in Mongo.
    Property update only — no documents, chunks, or artifacts are touched.
    Idempotent: rerunning rewrites the same block with a fresh marked_at.

SAFETY
    Dry-run by default: prints the current and intended fixture block.
    --apply executes the update.

ENV
    MONGODB_URI or MONGO_USER/MONGO_PASSWORD (host runs resolve
    'mongodb:' hostnames to 127.0.0.1 automatically).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]


def _load_env_file() -> dict:
    env_path = REPO / ".env"
    out: dict = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
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


FIXTURE_BLOCK = {
    "purpose": "permanent_integration_fixture",
    "production_visible": False,
    "excluded_from_user_search": True,
    "program": "book_ingestion_p5_canary",
    "fixture_files": 4,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus_id", help="corpus to mark")
    ap.add_argument("--apply", action="store_true", help="execute the update")
    args = ap.parse_args()

    env = {**_load_env_file(), **os.environ}
    client, db = _mongo(env)
    coll = db["corpora"]
    doc = coll.find_one({"corpus_id": args.corpus_id})
    if not doc:
        print(f"corpus {args.corpus_id} not found", file=sys.stderr)
        return 1

    block = dict(FIXTURE_BLOCK)
    block["marked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    print(f"corpus: {doc.get('name')!r} doc_count={doc.get('doc_count')}")
    print(f"current fixture block: {doc.get('fixture')}")
    print(f"intended fixture block: {block}")
    if not args.apply:
        print("dry-run: no changes written (pass --apply to write)")
        client.close()
        return 0

    result = coll.update_one(
        {"corpus_id": args.corpus_id},
        {"$set": {"fixture": block}},
    )
    print(
        f"applied: matched={result.matched_count} modified={result.modified_count}"
    )
    after = coll.find_one({"corpus_id": args.corpus_id}, {"fixture": 1})
    print(f"persisted fixture block: {after.get('fixture')}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
