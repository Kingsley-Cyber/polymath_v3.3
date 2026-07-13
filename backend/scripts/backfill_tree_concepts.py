#!/usr/bin/env python3
"""Backfill deterministic `concepts` onto existing summary_tree rows (P0.2/P2.1).

Live gap (checklist Audit Delta 2): `summary_tree.concepts` is empty on every
existing row because construction never passed it. New trees now derive node
concepts deterministically in services/ingestion/summary_tree.py
(`derive_node_concepts` — union of member parents' key_terms + mechanisms +
concept_tags, snake_case, deduped, cap 16, frequency-desc then alpha; higher
nodes union their children's derived concepts). This script applies the SAME
helper to existing Mongo rows, bottom-up per document:

  rollup   <- parent_chunks rows referenced by its parent_ids
  section  <- its child rollups' (stored or freshly derived) concepts
  document <- its child sections' (stored or freshly derived) concepts

DRY-RUN BY DEFAULT (read-only): per-corpus concept-less node counts by type
plus a derived sample. --apply updates Mongo in 1000-op batches AFTER writing
a JSONL backup (node_id + prior concepts) to docs/baselines/p0_2_backups/.
Resumable: only rows whose concepts are missing/None/[] are ever targeted, so
re-runs skip everything already filled.

Qdrant is NOT touched, deliberately: the summary-tree point payload contract
(services/storage/qdrant_writer._summary_tree_payload) has no `concepts`
field, and the only existing writer (upsert_summary_tree_entries) requires
embedding vectors. The sanctioned Qdrant refresh remains
backend/scripts/backfill_summary_tree_index.py (container-run, re-embeds).

Host Mongo pattern mirrors backend/scripts/capture_raptor_baseline.py
(127.0.0.1 ports + repo .env + .tmp_pkgs). Run from the deployment host:

    python3.11 backend/scripts/backfill_tree_concepts.py \
        [--deploy-root /Users/king/polymath_v3.3] [--corpus <id>] [--sample 5]
    # writes: add --apply (backs up first)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
BACKUP_DIR = REPO / "docs" / "baselines" / "p0_2_backups"
BATCH_OPS = 1000

# Rows counted/targeted as "concept-less" — never overwrite non-empty concepts.
EMPTY_CONCEPTS_FILTER = {
    "$or": [
        {"concepts": {"$exists": False}},
        {"concepts": None},
        {"concepts": []},
    ]
}


def _env(deploy_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (deploy_root / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _mongo(env: dict[str, str]):
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


def _derive_for_doc(db: Any, derive, corpus_id: str, doc_id: str) -> dict[str, list[str]]:
    """Derived concepts for every concept-less node of one document, bottom-up.

    Uses the module's own derive_node_concepts so the backfill can never drift
    from the construction-time contract. Nodes that already carry concepts
    keep them and feed them upward unchanged.
    """
    nodes = list(
        db["summary_tree"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "_id": 0,
                "node_id": 1,
                "node_type": 1,
                "parent_ids": 1,
                "child_node_ids": 1,
                "concepts": 1,
            },
        )
    )
    by_id = {str(n.get("node_id") or ""): n for n in nodes if n.get("node_id")}
    rollups = [n for n in nodes if n.get("node_type") == "rollup"]
    all_parent_ids = sorted(
        {str(p) for n in rollups for p in (n.get("parent_ids") or []) if str(p)}
    )
    parent_rows = {
        str(r.get("parent_id") or ""): r
        for r in db["parent_chunks"].find(
            {"corpus_id": corpus_id, "parent_id": {"$in": all_parent_ids}},
            {"_id": 0, "parent_id": 1, "key_terms": 1, "mechanisms": 1, "concept_tags": 1},
        )
    } if all_parent_ids else {}

    concepts_of: dict[str, list[str]] = {}  # effective (stored or derived)
    derived: dict[str, list[str]] = {}  # only for concept-less target nodes

    def _effective(node: dict) -> list[str]:
        node_id = str(node.get("node_id") or "")
        if node_id in concepts_of:
            return concepts_of[node_id]
        stored = node.get("concepts")  # same emptiness test as the Mongo filter
        if stored:
            concepts_of[node_id] = [str(c) for c in stored]
            return concepts_of[node_id]
        if node.get("node_type") == "rollup":
            rows = [
                parent_rows[str(p)]
                for p in (node.get("parent_ids") or [])
                if str(p) in parent_rows
            ]
            value = derive(rows)
        else:  # section | document — union of children's derived concepts
            children = [
                by_id[str(c)] for c in (node.get("child_node_ids") or []) if str(c) in by_id
            ]
            value = derive([{"concept_tags": _effective(child)} for child in children])
        concepts_of[node_id] = value
        derived[node_id] = value
        return value

    for node_type in ("rollup", "section", "document"):
        for node in nodes:
            if node.get("node_type") == node_type and str(node.get("node_id") or ""):
                _effective(node)
    derived.pop("", None)
    return derived


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--deploy-root",
        default=str(REPO),
        help=".env + .tmp_pkgs location (deployment checkout); default: this repo",
    )
    ap.add_argument("--corpus", default=None, help="restrict to one corpus_id")
    ap.add_argument("--sample", type=int, default=5, help="derived sample size (dry-run)")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write Mongo updates (default: READ-ONLY dry run)",
    )
    args = ap.parse_args()

    deploy_root = Path(args.deploy_root).resolve()
    sys.path.insert(0, str(deploy_root / ".tmp_pkgs"))
    sys.path.insert(0, str(REPO / "backend"))
    from services.ingestion.summary_tree import derive_node_concepts  # noqa: E402

    env = _env(deploy_root)
    client, db = _mongo(env)

    base: dict[str, Any] = dict(EMPTY_CONCEPTS_FILTER)
    if args.corpus:
        base["corpus_id"] = args.corpus

    # ── per-corpus concept-less counts by node type (always printed) ────────
    counts = list(
        db["summary_tree"].aggregate(
            [
                {"$match": base},
                {
                    "$group": {
                        "_id": {"corpus_id": "$corpus_id", "node_type": "$node_type"},
                        "n": {"$sum": 1},
                    }
                },
                {"$sort": {"_id.corpus_id": 1, "_id.node_type": 1}},
            ]
        )
    )
    per_corpus: dict[str, dict[str, int]] = {}
    for row in counts:
        cid = str(row["_id"].get("corpus_id"))
        per_corpus.setdefault(cid, {})[str(row["_id"].get("node_type"))] = int(row["n"])
    total_targets = sum(n for types in per_corpus.values() for n in types.values())
    corpus_names = {
        str(c.get("corpus_id")): str(c.get("name") or "?")
        for c in db["corpora"].find({}, {"_id": 0, "corpus_id": 1, "name": 1})
    }
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN (read-only)'}")
    print(f"concept-less summary_tree rows: {total_targets}")
    for cid, types in sorted(per_corpus.items()):
        label = corpus_names.get(cid, "?")
        print(f"  corpus={cid} ({label}): {json.dumps(types, sort_keys=True)}")
    print(
        "summary_tree indexes:",
        json.dumps(sorted(db["summary_tree"].index_information()), default=str),
    )
    print(
        "NOTE: Qdrant untouched — tree point payloads have no `concepts` field "
        "(qdrant_writer._summary_tree_payload) and the only writer requires "
        "embedding vectors; refresh path = scripts/backfill_summary_tree_index.py."
    )
    if not total_targets:
        client.close()
        return 0

    # ── target docs, deterministic order ────────────────────────────────────
    doc_pairs = list(
        db["summary_tree"].aggregate(
            [
                {"$match": base},
                {"$group": {"_id": {"corpus_id": "$corpus_id", "doc_id": "$doc_id"}}},
                {"$sort": {"_id.corpus_id": 1, "_id.doc_id": 1}},
            ]
        )
    )

    if not args.apply:
        shown = 0
        for pair in doc_pairs:
            if shown >= max(0, args.sample):
                break
            cid = str(pair["_id"].get("corpus_id"))
            did = str(pair["_id"].get("doc_id"))
            derived = _derive_for_doc(db, derive_node_concepts, cid, did)
            for node_id in sorted(derived):
                if shown >= max(0, args.sample):
                    break
                print(f"SAMPLE {node_id} -> {derived[node_id]}")
                shown += 1
        print(f"[dry-run] would derive concepts for {total_targets} rows "
              f"across {len(doc_pairs)} documents; no writes performed")
        client.close()
        return 0

    # ── APPLY: derive everything, back up FIRST, then batched updates ───────
    from pymongo import UpdateOne

    updates: list[tuple[str, list[str]]] = []
    prior: dict[str, Any] = {}
    derived_empty = 0
    for pair in doc_pairs:
        cid = str(pair["_id"].get("corpus_id"))
        did = str(pair["_id"].get("doc_id"))
        for row in db["summary_tree"].find(
            {"corpus_id": cid, "doc_id": did, **EMPTY_CONCEPTS_FILTER},
            {"_id": 0, "node_id": 1, "concepts": 1},
        ):
            prior[str(row.get("node_id") or "")] = row.get("concepts")
        derived = _derive_for_doc(db, derive_node_concepts, cid, did)
        for node_id, value in sorted(derived.items()):
            if not value:
                derived_empty += 1
                continue
            updates.append((node_id, value))

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = BACKUP_DIR / f"tree_concepts_{stamp}.jsonl"
    with backup_path.open("w") as fh:
        for node_id, _value in updates:
            fh.write(
                json.dumps({"node_id": node_id, "prior_concepts": prior.get(node_id)})
                + "\n"
            )
        fh.flush()
    print(f"backup written FIRST: {backup_path} ({len(updates)} rows)")

    written = 0
    for start in range(0, len(updates), BATCH_OPS):
        batch = [
            UpdateOne({"node_id": node_id}, {"$set": {"concepts": value}})
            for node_id, value in updates[start : start + BATCH_OPS]
        ]
        result = db["summary_tree"].bulk_write(batch, ordered=False)
        written += result.modified_count
        print(f"  batch {start // BATCH_OPS + 1}: modified={result.modified_count}")
    print(f"APPLIED: modified={written} derived_empty_skipped={derived_empty} "
          f"(re-run stays no-op for filled rows)")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
