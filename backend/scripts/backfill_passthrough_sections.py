#!/usr/bin/env python3
"""Backfill passthrough payloads for EXISTING one-child section rows (P0.2).

New trees already compute a singleton-section passthrough contract at index
time (services/ingestion/summary_tree.py, index_summary_tree_nodes): a section
whose child_node_ids is exactly one rollup gets

    passthrough_rollup_id    = the single child rollup's node_id
    passthrough_parent_ids   = list(child rollup's parent_ids)
    passthrough_lexicon_ids  = the section's lexicon_ids (for one child ==
                               the child rollup's support-ranked corpus_lexicon
                               join, retrieval_eligible != False, ranked by
                               (-support_count, lexicon_id), deduped, cap 96)

Those fields were only ever materialized on the ephemeral Qdrant entry dicts,
so EXISTING Mongo rows (and pre-fix Qdrant points) lack them — the durable
hierarchy is thinner than its projection (checklist Audit Delta 2). This
script mirrors that exact field set onto the Mongo `summary_tree` rows.
Eligibility mirrors the module exactly: the section AND its single child
rollup must both carry a non-empty summary (the module's entry filter),
otherwise the row is reported as skipped, never guessed.

DRY-RUN BY DEFAULT (read-only): per-corpus counts + derived sample. --apply
updates Mongo in 1000-op batches AFTER writing a JSONL backup (node_id +
prior passthrough fields) to docs/baselines/p0_2_backups/. Resumable: only
rows with passthrough_rollup_id missing/empty are targeted.

Qdrant points are NOT touched, deliberately: the only existing writer for
summary-tree points (qdrant_writer.upsert_summary_tree_entries) requires
embedding vectors, so there is no vector-free existing writer to call. The
sanctioned refresh for existing points remains
backend/scripts/backfill_summary_tree_index.py (container-run, re-embeds),
whose index path computes these same passthrough fields itself.

Host Mongo pattern mirrors backend/scripts/capture_raptor_baseline.py
(127.0.0.1 ports + repo .env + .tmp_pkgs). Run from the deployment host:

    python3.11 backend/scripts/backfill_passthrough_sections.py \
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
LEXICON_QUERY_LIMIT = 20_000  # mirrors summary_tree.index_summary_tree_nodes
LEXICON_CAP = 96  # mirrors lexicon_by_rollup[:96]

# Resumable target filter: one-child sections not yet backfilled.
TARGET_FILTER = {
    "node_type": "section",
    "child_node_ids": {"$size": 1},
    "$or": [
        {"passthrough_rollup_id": {"$exists": False}},
        {"passthrough_rollup_id": None},
        {"passthrough_rollup_id": ""},
    ],
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


def _lexicon_ids_by_rollup(
    db: Any, corpus_id: str, rollups: dict[str, dict]
) -> dict[str, list[str]]:
    """EXACT mirror of the lexicon join in index_summary_tree_nodes.

    corpus_lexicon rows (retrieval_eligible != False, source_parent_ids in the
    rollups' parents, limit 20k) -> per-parent (support, lexicon_id) pairs ->
    per-rollup dedup of ids ranked by (-support_count, lexicon_id), cap 96.
    """
    all_parent_ids = sorted(
        {
            str(parent_id)
            for row in rollups.values()
            for parent_id in (row.get("parent_ids") or [])
            if str(parent_id)
        }
    )
    all_parent_id_set = set(all_parent_ids)
    lexicon_rows = (
        list(
            db["corpus_lexicon"]
            .find(
                {
                    "corpus_id": corpus_id,
                    "retrieval_eligible": {"$ne": False},
                    "source_parent_ids": {"$in": all_parent_ids},
                },
                {
                    "_id": 0,
                    "lexicon_id": 1,
                    "source_parent_ids": 1,
                    "support_count": 1,
                },
            )
            .limit(LEXICON_QUERY_LIMIT)
        )
        if all_parent_ids
        else []
    )
    lexicon_by_parent: dict[str, list[tuple[int, str]]] = {}
    for lexicon_row in lexicon_rows:
        lexicon_id = str(lexicon_row.get("lexicon_id") or "")
        support = int(lexicon_row.get("support_count") or 0)
        if not lexicon_id:
            continue
        for parent_id in lexicon_row.get("source_parent_ids") or []:
            normalized_parent_id = str(parent_id or "")
            if normalized_parent_id in all_parent_id_set:
                lexicon_by_parent.setdefault(normalized_parent_id, []).append(
                    (support, lexicon_id)
                )
    out: dict[str, list[str]] = {}
    for node_id, row in rollups.items():
        ranked = [
            value
            for parent_id in (row.get("parent_ids") or [])
            for value in lexicon_by_parent.get(str(parent_id), [])
        ]
        out[node_id] = list(
            dict.fromkeys(
                lexicon_id
                for _support, lexicon_id in sorted(
                    ranked,
                    key=lambda item: (-item[0], item[1]),
                )
            )
        )[:LEXICON_CAP]
    return out


def _derive_for_doc(db: Any, corpus_id: str, doc_id: str) -> tuple[list[dict], dict[str, int]]:
    """Passthrough field sets for one document's target sections.

    Returns (updates, skips): updates = [{node_id, passthrough_rollup_id,
    passthrough_parent_ids, passthrough_lexicon_ids}], skips = reason counts.
    """
    skips = {"section_summary_empty": 0, "child_missing": 0, "child_not_rollup_or_empty_summary": 0}
    sections = list(
        db["summary_tree"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id, **TARGET_FILTER},
            {"_id": 0, "node_id": 1, "child_node_ids": 1, "summary": 1},
        )
    )
    child_ids = sorted(
        {
            str((s.get("child_node_ids") or [None])[0] or "")
            for s in sections
        }
        - {""}
    )
    children = {
        str(r.get("node_id") or ""): r
        for r in db["summary_tree"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id, "node_id": {"$in": child_ids}},
            {"_id": 0, "node_id": 1, "node_type": 1, "parent_ids": 1, "summary": 1},
        )
    } if child_ids else {}
    # Mirror index_summary_tree_nodes' entry filter: rollups usable for
    # passthrough must have a node_id and a non-empty summary.
    rollups = {
        node_id: row
        for node_id, row in children.items()
        if str(row.get("node_type") or "") == "rollup"
        and str(row.get("summary") or "").strip()
    }
    updates: list[dict] = []
    eligible: dict[str, str] = {}
    for section in sorted(sections, key=lambda s: str(s.get("node_id") or "")):
        node_id = str(section.get("node_id") or "")
        if not str(section.get("summary") or "").strip():
            skips["section_summary_empty"] += 1
            continue
        child_id = str((section.get("child_node_ids") or [None])[0] or "")
        if child_id not in children:
            skips["child_missing"] += 1
            continue
        if child_id not in rollups:
            skips["child_not_rollup_or_empty_summary"] += 1
            continue
        eligible[node_id] = child_id
    lexicon_by_rollup = (
        _lexicon_ids_by_rollup(
            db,
            corpus_id,
            {cid: rollups[cid] for cid in sorted(set(eligible.values()))},
        )
        if eligible
        else {}
    )
    for node_id, child_id in eligible.items():
        child = rollups[child_id]
        updates.append(
            {
                "node_id": node_id,
                "passthrough_rollup_id": child_id,
                "passthrough_parent_ids": list(child.get("parent_ids") or []),
                # one-child section lexicon_ids == its child rollup's list
                "passthrough_lexicon_ids": list(lexicon_by_rollup.get(child_id) or []),
            }
        )
    return updates, skips


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
    env = _env(deploy_root)
    client, db = _mongo(env)

    base: dict[str, Any] = dict(TARGET_FILTER)
    if args.corpus:
        base["corpus_id"] = args.corpus
    one_child_base: dict[str, Any] = {
        "node_type": "section",
        "child_node_ids": {"$size": 1},
    }
    if args.corpus:
        one_child_base["corpus_id"] = args.corpus

    def _per_corpus(match: dict) -> dict[str, int]:
        return {
            str(row["_id"]): int(row["n"])
            for row in db["summary_tree"].aggregate(
                [
                    {"$match": match},
                    {"$group": {"_id": "$corpus_id", "n": {"$sum": 1}}},
                    {"$sort": {"_id": 1}},
                ]
            )
        }

    one_child = _per_corpus(one_child_base)
    missing = _per_corpus(base)
    corpus_names = {
        str(c.get("corpus_id")): str(c.get("name") or "?")
        for c in db["corpora"].find({}, {"_id": 0, "corpus_id": 1, "name": 1})
    }
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN (read-only)'}")
    print(f"one-child sections total: {sum(one_child.values())}")
    print(f"one-child sections missing passthrough fields: {sum(missing.values())}")
    for cid in sorted(one_child):
        print(
            f"  corpus={cid} ({corpus_names.get(cid, '?')}): "
            f"one_child={one_child.get(cid, 0)} missing_passthrough={missing.get(cid, 0)}"
        )
    print(
        "summary_tree indexes:",
        json.dumps(sorted(db["summary_tree"].index_information()), default=str),
    )
    print(
        "NOTE: Qdrant untouched — the only existing summary-tree point writer "
        "(upsert_summary_tree_entries) requires embedding vectors; existing "
        "points get these fields via scripts/backfill_summary_tree_index.py "
        "(re-embeds), which computes the same passthrough contract."
    )
    if not sum(missing.values()):
        client.close()
        return 0

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
        skips_total: dict[str, int] = {}
        for pair in doc_pairs:
            if shown >= max(0, args.sample):
                break
            cid = str(pair["_id"].get("corpus_id"))
            did = str(pair["_id"].get("doc_id"))
            updates, skips = _derive_for_doc(db, cid, did)
            for reason, n in skips.items():
                skips_total[reason] = skips_total.get(reason, 0) + n
            for update in updates:
                if shown >= max(0, args.sample):
                    break
                print(
                    f"SAMPLE {update['node_id']} -> rollup={update['passthrough_rollup_id']} "
                    f"parent_ids={len(update['passthrough_parent_ids'])} "
                    f"lexicon_ids={len(update['passthrough_lexicon_ids'])}"
                )
                shown += 1
        print(f"[dry-run] sampled-doc skips: {json.dumps(skips_total, sort_keys=True)}")
        print(
            f"[dry-run] would backfill up to {sum(missing.values())} sections "
            f"across {len(doc_pairs)} documents; no writes performed"
        )
        client.close()
        return 0

    # ── APPLY: derive everything, back up FIRST, then batched updates ───────
    from pymongo import UpdateOne

    all_updates: list[dict] = []
    skips_total: dict[str, int] = {}
    prior: dict[str, Any] = {}
    for pair in doc_pairs:
        cid = str(pair["_id"].get("corpus_id"))
        did = str(pair["_id"].get("doc_id"))
        for row in db["summary_tree"].find(
            {"corpus_id": cid, "doc_id": did, **TARGET_FILTER},
            {
                "_id": 0,
                "node_id": 1,
                "passthrough_rollup_id": 1,
                "passthrough_parent_ids": 1,
                "passthrough_lexicon_ids": 1,
            },
        ):
            prior[str(row.get("node_id") or "")] = {
                "passthrough_rollup_id": row.get("passthrough_rollup_id"),
                "passthrough_parent_ids": row.get("passthrough_parent_ids"),
                "passthrough_lexicon_ids": row.get("passthrough_lexicon_ids"),
            }
        updates, skips = _derive_for_doc(db, cid, did)
        for reason, n in skips.items():
            skips_total[reason] = skips_total.get(reason, 0) + n
        all_updates.extend(updates)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = BACKUP_DIR / f"passthrough_sections_{stamp}.jsonl"
    with backup_path.open("w") as fh:
        for update in all_updates:
            fh.write(
                json.dumps(
                    {"node_id": update["node_id"], "prior": prior.get(update["node_id"])}
                )
                + "\n"
            )
        fh.flush()
    print(f"backup written FIRST: {backup_path} ({len(all_updates)} rows)")

    written = 0
    for start in range(0, len(all_updates), BATCH_OPS):
        batch = [
            UpdateOne(
                {"node_id": update["node_id"]},
                {
                    "$set": {
                        "passthrough_rollup_id": update["passthrough_rollup_id"],
                        "passthrough_parent_ids": update["passthrough_parent_ids"],
                        "passthrough_lexicon_ids": update["passthrough_lexicon_ids"],
                    }
                },
            )
            for update in all_updates[start : start + BATCH_OPS]
        ]
        result = db["summary_tree"].bulk_write(batch, ordered=False)
        written += result.modified_count
        print(f"  batch {start // BATCH_OPS + 1}: modified={result.modified_count}")
    print(
        f"APPLIED: modified={written} skips={json.dumps(skips_total, sort_keys=True)} "
        f"(re-run targets only still-missing rows)"
    )
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
