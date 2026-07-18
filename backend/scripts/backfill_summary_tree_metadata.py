#!/usr/bin/env python3
"""Backfill deterministic routing metadata onto existing summary-tree rows.

Dry-run is the default. Apply requires a durable pre-image backup directory,
an idle corpus, and optional exact row-count assertion. The migration reads
only existing tree topology and parent-summary carriers; it never parses a
source, embeds text, or calls a provider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
for candidate in (HERE.parent.parent, HERE.parent.parent / "backend"):
    if (candidate / "services" / "ingestion" / "summary_tree.py").exists():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break

from services.ingestion.summary_tree import (  # noqa: E402
    aggregate_tree_temporal,
    common_heading_path,
    summary_tree_retrieval_text,
)

BACKUP_VERSION = 1
ORIGIN = "summary_tree_metadata_backfill.v1"
METADATA_FIELDS = (
    "retrieval_text",
    "heading_path",
    "temporal_class",
    "time_expressions",
    "tree_metadata_provenance",
)
CORE_GUARD_FIELDS = (
    "summary",
    "section_range",
    "parent_ids",
    "child_node_ids",
    "schema_version",
)
ACTIVE_STATUSES = {"running", "leased", "processing"}


def _mongo():
    from motor.motor_asyncio import AsyncIOMotorClient

    uri = (
        os.environ.get("MONGODB_URI")
        or os.environ.get("MONGO_URL")
        or os.environ.get("MONGODB_URL")
    )
    if not uri:
        from config import get_settings

        uri = get_settings().MONGODB_URI
    client = AsyncIOMotorClient(uri)
    try:
        db = client.get_default_database()
    except Exception:
        from config import get_settings

        db = client[get_settings().MONGODB_DATABASE]
    return client, db


def _snapshot(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: {"present": field in row, "value": row.get(field)} for field in fields
    }


def _snapshot_after(
    pre_image: dict[str, Any], set_fields: dict[str, Any]
) -> dict[str, Any]:
    output = {
        field: {"present": state["present"], "value": state["value"]}
        for field, state in pre_image.items()
    }
    for field, value in set_fields.items():
        output[field] = {"present": True, "value": value}
    return output


def _snapshot_clauses(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    clauses = []
    for field, state in snapshot.items():
        if state["present"]:
            clauses.append({field: state["value"]})
        else:
            clauses.append({field: {"$exists": False}})
    return clauses


def _core_hash(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda value: str(value.get("node_id") or "")):
        core = {
            key: value
            for key, value in row.items()
            if key not in {"_id", *METADATA_FIELDS}
        }
        digest.update(
            (
                json.dumps(core, sort_keys=True, default=str, separators=(",", ":"))
                + "\n"
            ).encode()
        )
    return digest.hexdigest()


def build_plans(
    tree_rows: list[dict[str, Any]],
    parent_rows: list[dict[str, Any]],
    *,
    run_id: str,
    captured_at: str,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Build presence-aware plans from existing topology and parent carriers."""

    parents = {
        str(row.get("parent_id") or ""): row
        for row in parent_rows
        if str(row.get("parent_id") or "")
    }
    nodes = {
        str(row.get("node_id") or ""): row
        for row in tree_rows
        if str(row.get("node_id") or "")
    }
    memo: dict[str, list[str]] = {}

    def descendant_parent_ids(node_id: str, stack: tuple[str, ...] = ()) -> list[str]:
        if node_id in memo:
            return memo[node_id]
        if node_id in stack:
            raise ValueError(f"summary-tree cycle at {node_id}")
        row = nodes[node_id]
        direct = [str(value) for value in (row.get("parent_ids") or []) if str(value)]
        if direct:
            output = list(dict.fromkeys(direct))
        else:
            output = list(
                dict.fromkeys(
                    parent_id
                    for child_id in (row.get("child_node_ids") or [])
                    if str(child_id) in nodes
                    for parent_id in descendant_parent_ids(
                        str(child_id), (*stack, node_id)
                    )
                )
            )
        memo[node_id] = output
        return output

    plans: list[dict[str, Any]] = []
    for node_id in sorted(nodes):
        row = nodes[node_id]
        existing_provenance = row.get("tree_metadata_provenance") or {}
        if existing_provenance.get("origin") == ORIGIN and not force:
            continue
        source_parent_ids = descendant_parent_ids(node_id)
        source_parents = [
            parents[parent_id]
            for parent_id in source_parent_ids
            if parent_id in parents
        ]
        temporal_class, time_expressions = aggregate_tree_temporal(source_parents)
        heading_path = common_heading_path(
            source_parents,
            str(row.get("section_range") or ""),
        )
        set_fields = {
            "retrieval_text": summary_tree_retrieval_text(
                str(row.get("section_range") or ""),
                str(row.get("summary") or ""),
            ),
            "heading_path": heading_path,
            "temporal_class": temporal_class,
            "time_expressions": time_expressions,
            "tree_metadata_provenance": {
                "origin": ORIGIN,
                "run_id": run_id,
                "captured_at": captured_at,
                "source_parent_count": len(source_parents),
                "source_parent_ids_hash": hashlib.sha256(
                    "\n".join(source_parent_ids).encode()
                ).hexdigest(),
            },
        }
        pre_image = _snapshot(row, METADATA_FIELDS)
        post_image = _snapshot_after(pre_image, set_fields)
        core_guard = _snapshot(row, CORE_GUARD_FIELDS)
        plans.append(
            {
                "corpus_id": str(row.get("corpus_id") or ""),
                "doc_id": str(row.get("doc_id") or ""),
                "node_id": node_id,
                "node_type": str(row.get("node_type") or ""),
                "set_fields": set_fields,
                "pre_image": pre_image,
                "post_image": post_image,
                "core_guard": core_guard,
            }
        )
    return plans


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_backup(
    plans: list[dict[str, Any]],
    *,
    backup_dir: Path,
    corpus_id: str,
    run_id: str,
    captured_at: str,
) -> tuple[Path, str]:
    if not plans:
        raise ValueError("refusing to create an empty backup")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = backup_dir / (
        f"summary_tree_metadata_{corpus_id[:8]}_{stamp}_{run_id[:12]}.jsonl"
    )
    digest = hashlib.sha256()
    with path.open("x", encoding="utf-8") as handle:
        for plan in plans:
            row = {
                "_backup_kind": "summary_tree_metadata",
                "backup_version": BACKUP_VERSION,
                "run_id": run_id,
                "captured_at": captured_at,
                **plan,
            }
            encoded = (
                json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
                + "\n"
            ).encode()
            handle.write(encoded.decode())
            digest.update(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(backup_dir)
    return path, digest.hexdigest()


async def _active_work(db: Any, corpus_id: str) -> list[dict[str, Any]]:
    output = []
    for collection in (
        "ingest_batches",
        "document_pipeline_jobs",
        "extraction_jobs",
        "summary_jobs",
        "graph_promotion_jobs",
    ):
        count = await db[collection].count_documents(
            {
                "corpus_id": corpus_id,
                "status": {"$in": sorted(ACTIVE_STATUSES)},
            }
        )
        if count:
            output.append({"collection": collection, "count": count})
    return output


async def _coverage(db: Any, corpus_id: str) -> dict[str, Any]:
    base = {"corpus_id": corpus_id}
    output = {
        "rows": await db["summary_tree"].count_documents(base),
        "retrieval_text": await db["summary_tree"].count_documents(
            {**base, "retrieval_text": {"$type": "string", "$ne": ""}}
        ),
        "heading_path": await db["summary_tree"].count_documents(
            {**base, "heading_path.0": {"$exists": True}}
        ),
        "temporal_class": await db["summary_tree"].count_documents(
            {**base, "temporal_class": {"$type": "string", "$ne": ""}}
        ),
        "time_expressions_present": await db["summary_tree"].count_documents(
            {**base, "time_expressions": {"$exists": True}}
        ),
        "time_expressions_nonempty": await db["summary_tree"].count_documents(
            {**base, "time_expressions.0": {"$exists": True}}
        ),
        "provenance": await db["summary_tree"].count_documents(
            {
                **base,
                "tree_metadata_provenance.origin": ORIGIN,
            }
        ),
    }
    return output


async def run_corpus(
    db: Any,
    *,
    corpus_id: str,
    apply: bool,
    backup_dir: Path | None,
    expected_count: int | None,
    force: bool,
) -> dict[str, Any]:
    tree_rows = (
        await db["summary_tree"]
        .find({"corpus_id": corpus_id}, {"_id": 0})
        .sort("node_id", 1)
        .to_list(length=None)
    )
    if expected_count is not None and len(tree_rows) != expected_count:
        raise RuntimeError(
            f"row-count mismatch: expected {expected_count}, found {len(tree_rows)}"
        )
    doc_ids = sorted(
        {str(row.get("doc_id") or "") for row in tree_rows if row.get("doc_id")}
    )
    parent_rows = (
        await db["parent_chunks"]
        .find(
            {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}},
            {
                "_id": 0,
                "parent_id": 1,
                "heading_path": 1,
                "temporal_class": 1,
                "time_expressions": 1,
            },
        )
        .sort("parent_id", 1)
        .to_list(length=None)
    )
    active_work = await _active_work(db, corpus_id)
    if apply and active_work:
        raise RuntimeError(f"selected corpus has active durable work: {active_work}")

    run_id = uuid.uuid4().hex
    captured_at = datetime.now(timezone.utc).isoformat()
    plans = build_plans(
        tree_rows,
        parent_rows,
        run_id=run_id,
        captured_at=captured_at,
        force=force,
    )
    planned_coverage = {
        "retrieval_text": sum(
            bool(plan["set_fields"]["retrieval_text"]) for plan in plans
        ),
        "heading_path": sum(bool(plan["set_fields"]["heading_path"]) for plan in plans),
        "temporal_class": sum(
            bool(plan["set_fields"]["temporal_class"]) for plan in plans
        ),
        "time_expressions_present": len(plans),
        "time_expressions_nonempty": sum(
            bool(plan["set_fields"]["time_expressions"]) for plan in plans
        ),
        "zero_source_parent_nodes": sum(
            int(
                plan["set_fields"]["tree_metadata_provenance"]["source_parent_count"]
                == 0
            )
            for plan in plans
        ),
    }
    before = await _coverage(db, corpus_id)
    core_hash_before = _core_hash(tree_rows)
    backup_path = None
    backup_sha256 = None
    applied = modified = conflicts = noops = 0
    aborted = False
    if apply and plans:
        if backup_dir is None:
            raise ValueError("apply requires an explicit durable backup directory")
        backup_path, backup_sha256 = _write_backup(
            plans,
            backup_dir=backup_dir,
            corpus_id=corpus_id,
            run_id=run_id,
            captured_at=captured_at,
        )
        for plan in plans:
            query = {
                "$and": [
                    {"corpus_id": corpus_id},
                    {"node_id": plan["node_id"]},
                    *_snapshot_clauses(plan["core_guard"]),
                    *_snapshot_clauses(plan["pre_image"]),
                ]
            }
            result = await db["summary_tree"].update_one(
                query, {"$set": plan["set_fields"]}
            )
            if int(result.matched_count) != 1:
                conflicts += 1
                aborted = True
                break
            applied += 1
            if int(result.modified_count) != 1:
                noops += 1
                aborted = True
                break
            modified += 1

    after_rows = (
        await db["summary_tree"]
        .find({"corpus_id": corpus_id}, {"_id": 0})
        .sort("node_id", 1)
        .to_list(length=None)
    )
    core_hash_after = _core_hash(after_rows)
    if apply and core_hash_after != core_hash_before:
        raise RuntimeError("summary-tree core hash changed during metadata backfill")
    return {
        "mode": "apply" if apply else "dry-run",
        "corpus_id": corpus_id,
        "run_id": run_id,
        "planned": len(plans),
        "applied": applied,
        "modified": modified,
        "cas_conflicts": conflicts,
        "noops": noops,
        "aborted": aborted,
        "not_attempted": max(0, len(plans) - applied - conflicts),
        "active_work": active_work,
        "planned_coverage": planned_coverage,
        "coverage_before": before,
        "coverage_after": await _coverage(db, corpus_id) if apply else None,
        "core_hash_before": core_hash_before,
        "core_hash_after": core_hash_after,
        "core_hash_unchanged": core_hash_after == core_hash_before,
        "backup": str(backup_path) if backup_path else None,
        "backup_rows": len(plans) if backup_path else 0,
        "backup_sha256": backup_sha256,
    }


def _load_backup(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if (
                row.get("_backup_kind") != "summary_tree_metadata"
                or row.get("backup_version") != BACKUP_VERSION
            ):
                raise ValueError(f"unsupported backup row at line {line_no}")
            rows.append(row)
    if not rows:
        raise ValueError("refusing to restore an empty backup")
    return rows


async def restore_backup(db: Any, path: Path, *, apply: bool) -> dict[str, Any]:
    rows = _load_backup(path)
    restored = modified = conflicts = 0
    aborted = False
    if apply:
        for row in rows:
            set_fields = {
                field: state["value"]
                for field, state in row["pre_image"].items()
                if state["present"]
            }
            unset_fields = {
                field: ""
                for field, state in row["pre_image"].items()
                if not state["present"]
            }
            query = {
                "$and": [
                    {"corpus_id": row["corpus_id"]},
                    {"node_id": row["node_id"]},
                    *_snapshot_clauses(row["core_guard"]),
                    *_snapshot_clauses(row["post_image"]),
                ]
            }
            update: dict[str, Any] = {"$set": set_fields}
            if unset_fields:
                update["$unset"] = unset_fields
            result = await db["summary_tree"].update_one(query, update)
            if int(result.matched_count) != 1:
                conflicts += 1
                aborted = True
                break
            restored += 1
            modified += int(result.modified_count)
    return {
        "mode": "restore-apply" if apply else "restore-dry-run",
        "backup": str(path),
        "planned": len(rows),
        "restored": restored,
        "modified": modified,
        "cas_conflicts": conflicts,
        "aborted": aborted,
        "not_attempted": max(0, len(rows) - restored - conflicts),
    }


async def _amain(args: argparse.Namespace) -> int:
    client, db = _mongo()
    try:
        if args.restore_backup:
            report = await restore_backup(
                db, Path(args.restore_backup), apply=args.apply
            )
        elif args.verify:
            rows = (
                await db["summary_tree"]
                .find({"corpus_id": args.corpus_id}, {"_id": 0})
                .to_list(length=None)
            )
            report = {
                "corpus_id": args.corpus_id,
                "coverage": await _coverage(db, args.corpus_id),
                "core_hash": _core_hash(rows),
                "active_work": await _active_work(db, args.corpus_id),
            }
        else:
            report = await run_corpus(
                db,
                corpus_id=args.corpus_id,
                apply=args.apply,
                backup_dir=Path(args.backup_dir) if args.backup_dir else None,
                expected_count=args.expected_count,
                force=args.force,
            )
        print(json.dumps(report, indent=2, default=str))
        return 3 if report.get("aborted") else 0
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--backup-dir")
    parser.add_argument("--restore-backup")
    args = parser.parse_args()
    if args.apply and not args.restore_backup and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
