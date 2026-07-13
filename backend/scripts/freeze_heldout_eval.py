#!/usr/bin/env python3
"""Validate and freeze the held-out evaluation suite (checklist P1.1).

1. Structural validation: every line parses; required fields present; shapes
   from the allowed set; answerable/negative-control consistency.
2. Ground-truth validation: every expected_doc_id exists in Mongo `documents`
   for one of the question's corpora (catches authoring typos).
3. Freeze: writes backend/evals/heldout_hashes.json with the sha256 of each
   normalized question (and follow-up history turns), activating the
   contamination firewall consulted by services/eval_firewall.py.

Exit non-zero on any validation failure. Read-only apart from the hash file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
QUESTIONS = BACKEND / "evals" / "heldout_questions.jsonl"
HASHES = BACKEND / "evals" / "heldout_hashes.json"

sys.path.insert(0, str(BACKEND))
from services.eval_firewall import heldout_query_hash  # noqa: E402

SHAPES = {
    "direct",
    "naive",
    "single_fact",
    "broad",
    "list",
    "procedural",
    "comparison",
    "followup",
    "negative_control",
    "cross_domain",
    "cross_corpus",
    "cross_corpus_irrelevant",
}


def _mongo():
    sys.path.insert(0, str(REPO / ".tmp_pkgs"))
    from pymongo import MongoClient

    env: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return client, client[env.get("MONGODB_DATABASE", "polymath")]


def main() -> int:
    errors: list[str] = []
    rows: list[dict] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(QUESTIONS.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {lineno}: invalid JSON ({exc})")
            continue
        qid = str(row.get("id") or "")
        if not qid or qid in seen_ids:
            errors.append(f"line {lineno}: missing/duplicate id {qid!r}")
        seen_ids.add(qid)
        if row.get("shape") not in SHAPES:
            errors.append(f"{qid}: unknown shape {row.get('shape')!r}")
        if not str(row.get("question") or "").strip():
            errors.append(f"{qid}: empty question")
        if not row.get("corpora"):
            errors.append(f"{qid}: no corpora")
        answerable = bool(row.get("answerable"))
        if row.get("shape") == "negative_control" and answerable:
            errors.append(f"{qid}: negative_control must be answerable=false")
        if answerable and not row.get("expected_doc_ids"):
            errors.append(f"{qid}: answerable question without expected_doc_ids")
        if not answerable and row.get("expected_doc_ids"):
            errors.append(f"{qid}: unanswerable question lists expected docs")
        rows.append(row)

    client, db = _mongo()
    corpus_by_name = {
        row["name"]: row["corpus_id"]
        for row in db.corpora.find({}, {"name": 1, "corpus_id": 1})
    }
    for row in rows:
        qid = row.get("id")
        cids = []
        for name in row.get("corpora") or []:
            if name not in corpus_by_name:
                errors.append(f"{qid}: unknown corpus {name!r}")
            else:
                cids.append(corpus_by_name[name])
        for doc_id in row.get("expected_doc_ids") or []:
            found = db.documents.count_documents(
                {"doc_id": doc_id, "corpus_id": {"$in": cids}}, limit=1
            )
            if not found:
                errors.append(f"{qid}: expected_doc_id not found in scope: {doc_id}")
    client.close()

    if errors:
        print(f"VALIDATION FAILED ({len(errors)} errors):")
        for err in errors:
            print(" -", err)
        return 1

    hashes: set[str] = set()
    for row in rows:
        hashes.add(heldout_query_hash(row["question"]))
        for turn in row.get("history") or []:
            hashes.add(heldout_query_hash(turn))
    HASHES.write_text(
        json.dumps(
            {
                "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "question_count": len(rows),
                "hash_count": len(hashes),
                "hashes": sorted(hashes),
            },
            indent=2,
        )
        + "\n"
    )
    shapes = {}
    for row in rows:
        shapes[row["shape"]] = shapes.get(row["shape"], 0) + 1
    print(f"VALIDATED {len(rows)} questions; froze {len(hashes)} hashes -> {HASHES}")
    print("shapes:", dict(sorted(shapes.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
