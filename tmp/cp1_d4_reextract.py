#!/usr/bin/env python3
"""Backup-first CP1-D4 extraction rerun for one explicitly supplied corpus."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient

from config import get_settings
from services.ingestion.extraction_jobs import (
    plan_extraction_jobs,
    run_extraction_jobs,
)
from services.ingestion.section_classifier import ChunkKind, should_skip_ghost_b
from services.settings import settings_service


BACKUP_ROOT = Path("/data/ingest-files/backups/rebatch-smoke-v2-cp1-d4-reextract-20260714")
REQUIRED_CAPTURES = {"winter 1911", "2018 drought summer"}


def safe_counts(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field) or "<missing>") for row in rows).items()))


async def load_state(db: Any, corpus_id: str) -> dict[str, Any]:
    corpus = await db.corpora.find_one(
        {"corpus_id": corpus_id}, {"_id": 0, "corpus_id": 1, "name": 1}
    )
    if not corpus:
        raise RuntimeError("corpus not found")
    now = datetime.utcnow()
    active_items = await db.ingest_batch_items.count_documents(
        {
            "corpus_id": corpus_id,
            "status": "running",
            "lease_until": {"$gt": now},
        }
    )
    chunks = await db.chunks.find(
        {"corpus_id": corpus_id},
        {"_id": 0, "chunk_id": 1, "text": 1, "chunk_kind": 1},
    ).to_list(length=None)
    ghost_rows = await db.ghost_b_extractions.find(
        {"corpus_id": corpus_id}
    ).to_list(length=None)
    job_rows = await db.extraction_jobs.find(
        {"corpus_id": corpus_id}
    ).to_list(length=None)
    return {
        "corpus": corpus,
        "active_items": active_items,
        "chunks": chunks,
        "ghost_rows": ghost_rows,
        "job_rows": job_rows,
    }


def receipt(state: dict[str, Any], corpus_id: str) -> dict[str, Any]:
    eligible_ids = {
        str(row.get("chunk_id") or "")
        for row in state["chunks"]
        if row.get("chunk_id")
        and str(row.get("text") or "").strip()
        and not should_skip_ghost_b(
            str(row.get("chunk_kind") or ChunkKind.BODY)
        )
    }
    return {
        "corpus_id": corpus_id,
        "corpus_name": state["corpus"].get("name"),
        "active_ingest_items": state["active_items"],
        "chunks": len(state["chunks"]),
        "unique_chunk_ids": len(
            {str(row.get("chunk_id") or "") for row in state["chunks"]}
        ),
        "eligible_chunks": len(eligible_ids),
        "ineligible_chunks": len(state["chunks"]) - len(eligible_ids),
        "ghost_rows": len(state["ghost_rows"]),
        "ghost_statuses": safe_counts(state["ghost_rows"], "status"),
        "ghost_providers": safe_counts(state["ghost_rows"], "provider"),
        "extraction_jobs": len(state["job_rows"]),
        "job_statuses": safe_counts(state["job_rows"], "status"),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    with path.open("wb") as handle:
        for row in rows:
            line = (json_util.dumps(row, sort_keys=True) + "\n").encode("utf-8")
            handle.write(line)
            digest.update(line)
            count += 1
        handle.flush()
    return count, digest.hexdigest()


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = mongo.get_default_database()
        except Exception:
            db = mongo[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        runpod_config, _legacy_key = await settings_service.get_system_runpod_flash()
        runpod_accounts = await settings_service.get_system_runpod_flash_accounts()
        print(
            json.dumps(
                {
                    "operation": "resolved_runpod_settings",
                    "enabled": runpod_config.enabled,
                    "accounts": [
                        {
                            "name": account.name,
                            "endpoint_id": account.endpoint_id,
                            "enabled": account.enabled,
                            "key_present": bool(key),
                        }
                        for account, key in runpod_accounts
                    ],
                },
                sort_keys=True,
            )
        )
        before = await load_state(db, args.corpus_id)
        print(json.dumps({"operation": "preflight", **receipt(before, args.corpus_id)}, sort_keys=True))
        chunk_count = len(before["chunks"])
        eligible_ids = {
            str(row.get("chunk_id") or "")
            for row in before["chunks"]
            if row.get("chunk_id")
            and str(row.get("text") or "").strip()
            and not should_skip_ghost_b(
                str(row.get("chunk_kind") or ChunkKind.BODY)
            )
        }
        ineligible_ids = {
            str(row.get("chunk_id") or "")
            for row in before["chunks"]
            if str(row.get("chunk_id") or "") not in eligible_ids
        }
        before_statuses = Counter(
            str(row.get("status") or "") for row in before["ghost_rows"]
        )
        expected_succeeded = len(eligible_ids)
        expected_skipped = len(ineligible_ids)
        if before["active_items"]:
            raise RuntimeError("corpus has active ingest-owned items")
        if chunk_count < 1 or expected_succeeded + expected_skipped != chunk_count:
            raise RuntimeError("live eligibility census is inconsistent")
        if args.operation in {"preflight", "verify"}:
            if len(before["ghost_rows"]) != chunk_count:
                raise RuntimeError("preflight requires one existing extraction row per chunk")
            if before_statuses != Counter(
                {"ok": expected_succeeded, "skipped": expected_skipped}
            ):
                raise RuntimeError("preflight extraction statuses differ from live eligibility")
            if args.operation == "verify":
                capture_texts = {
                    str(item.get("text") or "")
                    for row in before["ghost_rows"]
                    for item in (row.get("temporal_captures") or [])
                    if isinstance(item, dict)
                }
                capture_receipt = {
                    value: value in capture_texts
                    for value in sorted(REQUIRED_CAPTURES)
                }
                print(
                    json.dumps(
                        {
                            "operation": "verify_temporal_captures",
                            "required": capture_receipt,
                        },
                        sort_keys=True,
                    )
                )
                if not REQUIRED_CAPTURES.issubset(capture_texts):
                    raise RuntimeError("required temporal captures absent")
            return 0

        if args.operation == "apply":
            if len(before["ghost_rows"]) != chunk_count:
                raise RuntimeError("apply requires one existing extraction row per chunk")
            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup_receipts: dict[str, Any] = {}
            for label, rows in (
                ("ghost_b_extractions", before["ghost_rows"]),
                ("extraction_jobs", before["job_rows"]),
            ):
                path = BACKUP_ROOT / f"{label}_{args.corpus_id}_{stamp}.jsonl"
                count, sha256 = write_jsonl(path, rows)
                if count != len(rows) or path.stat().st_size < 1:
                    raise RuntimeError(f"{label} backup verification failed")
                backup_receipts[label] = {
                    "path": str(path),
                    "rows": count,
                    "sha256": sha256,
                }
            print(json.dumps({"operation": "backup", "backups": backup_receipts}, sort_keys=True))

            deleted = await db.ghost_b_extractions.delete_many(
                {"corpus_id": args.corpus_id}
            )
            if deleted.deleted_count != chunk_count:
                raise RuntimeError(
                    f"expected to delete {chunk_count} extraction rows, deleted {deleted.deleted_count}"
                )
            print(json.dumps({"operation": "clear_old_rows", "deleted": deleted.deleted_count}, sort_keys=True))
            expected_plan_queued = chunk_count
            expected_claimed = chunk_count
            expected_run_skipped = expected_skipped
        else:
            backup_paths = sorted(
                BACKUP_ROOT.glob(
                    f"ghost_b_extractions_{args.corpus_id}_*.jsonl"
                )
            )
            if not backup_paths:
                raise RuntimeError("resume requires the original Ghost B backup")
            backup_path = backup_paths[-1]
            backup_bytes = backup_path.read_bytes()
            backup_rows = len(backup_bytes.splitlines())
            backup_sha = hashlib.sha256(backup_bytes).hexdigest()
            if backup_rows != chunk_count:
                raise RuntimeError("resume backup row count differs from live chunks")
            current_ghost_ids = {
                str(row.get("chunk_id") or "") for row in before["ghost_rows"]
            }
            if current_ghost_ids != ineligible_ids or before_statuses != Counter(
                {"skipped": expected_skipped}
            ):
                raise RuntimeError("resume state is not exactly the failed-run skip residue")
            job_statuses = Counter(
                str(row.get("status") or "") for row in before["job_rows"]
            )
            if job_statuses != Counter({"failed": chunk_count}):
                raise RuntimeError("resume requires exactly one failed job per live chunk")
            print(
                json.dumps(
                    {
                        "operation": "resume_backup_verify",
                        "path": str(backup_path),
                        "rows": backup_rows,
                        "sha256": backup_sha,
                    },
                    sort_keys=True,
                )
            )
            expected_plan_queued = expected_succeeded
            expected_claimed = expected_succeeded
            expected_run_skipped = 0
            if args.operation == "resume-check":
                return 0

        plan = await plan_extraction_jobs(
            db,
            corpus_id=args.corpus_id,
            apply=True,
            limit=500,
            include_succeeded=True,
        )
        plan_safe = {
            "operation": "plan",
            "planned": plan.get("planned"),
            "counts": plan.get("counts"),
            "source_counts": plan.get("source_counts"),
            "apply": plan.get("apply"),
        }
        print(json.dumps(plan_safe, sort_keys=True))
        if int(plan.get("planned") or 0) != chunk_count:
            raise RuntimeError("planner did not materialize one job per live chunk")
        if int((plan.get("counts") or {}).get("queued") or 0) != expected_plan_queued:
            raise RuntimeError("planner queued count differs from eligible resume state")
        if int((plan.get("counts") or {}).get("skipped") or 0) != (
            chunk_count - expected_plan_queued
        ):
            raise RuntimeError("planner skipped count differs from live ineligible chunks")

        qdrant = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
        )
        try:
            result = await run_extraction_jobs(
                db,
                qdrant_client=qdrant,
                corpus_id=args.corpus_id,
                limit=500,
            )
        finally:
            await qdrant.close()
        result_safe = {
            "operation": "run",
            "status": result.get("status"),
            "requested": result.get("requested"),
            "claimed": result.get("claimed"),
            "counts": result.get("counts"),
            "active_ingest_docs_excluded": result.get("active_ingest_docs_excluded"),
            "document_concurrency": result.get("document_concurrency"),
            "docs": result.get("docs"),
        }
        print(json.dumps(result_safe, sort_keys=True, default=str))
        if int(result.get("claimed") or 0) != expected_claimed:
            raise RuntimeError("runner did not claim every runnable planned chunk")
        result_counts = result.get("counts") or {}
        if int(result_counts.get("succeeded") or 0) != expected_succeeded:
            raise RuntimeError("runner succeeded count differs from eligible preflight")
        if int(result_counts.get("skipped") or 0) != expected_run_skipped:
            raise RuntimeError("runner skipped count differs from ineligible preflight")
        unexpected_failures = sum(
            int(result_counts.get(status) or 0)
            for status in (
                "failed",
                "provider_failed",
                "validation_failed",
                "blocked_provider_contract",
            )
        )
        if unexpected_failures:
            raise RuntimeError("runner emitted failed or blocked jobs")

        after = await load_state(db, args.corpus_id)
        capture_rows = await db.ghost_b_extractions.find(
            {"corpus_id": args.corpus_id},
            {"_id": 0, "temporal_captures.text": 1},
        ).to_list(length=None)
        capture_texts = {
            str(item.get("text") or "")
            for row in capture_rows
            for item in (row.get("temporal_captures") or [])
            if isinstance(item, dict)
        }
        post = receipt(after, args.corpus_id)
        post.update(
            {
                "operation": "postflight",
                "required_temporal_captures": {
                    value: value in capture_texts for value in sorted(REQUIRED_CAPTURES)
                },
            }
        )
        print(json.dumps(post, sort_keys=True))
        if len(after["ghost_rows"]) != chunk_count:
            raise RuntimeError("postflight extraction row coverage mismatch")
        after_statuses = Counter(
            str(row.get("status") or "") for row in after["ghost_rows"]
        )
        if after_statuses != before_statuses:
            raise RuntimeError("postflight ok/skipped coverage differs from preflight")
        if not REQUIRED_CAPTURES.issubset(capture_texts):
            raise RuntimeError("required temporal captures absent after re-extraction")
        return 0
    finally:
        mongo.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=("preflight", "verify", "apply", "resume", "resume-check"),
    )
    parser.add_argument("--corpus-id", required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
