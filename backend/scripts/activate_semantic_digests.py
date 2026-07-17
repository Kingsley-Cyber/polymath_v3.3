#!/usr/bin/env python3
"""Governed census, enqueue, drain, and reconcile for digest Tier-0 points.

The default ``census`` mode is read-only. Every mutation mode requires an
explicit corpus scope, a matching shared-eval lock owner, a dry-run candidate
count, the frozen confirmation phrase, and a durable count-only receipt path.
No source rows or legacy Qdrant points are modified.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient

from config import get_settings
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from services.semantic_activation import (
    MANIFEST_COLLECTION,
    OUTBOX_COLLECTION,
    ProjectionActivationRepository,
    SemanticDigestProjectionWorker,
    discover_digest_projection_selection,
    enqueue_digest_projections,
    ensure_activation_contracts,
)
from services.settings import settings_service

LOCK_PATH = Path("/tmp/polymath-eval.lock")
CONFIRMATION = "semantic-digest-tier0-v1"
RECEIPT_SCHEMA_VERSION = "semantic_digest_activation_receipt.v1"


class ActivationCommandError(RuntimeError):
    """A CLI authority, preflight, or durable-state invariant failed."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("census", "enqueue", "drain", "reconcile"),
        nargs="?",
        default="census",
    )
    parser.add_argument(
        "--corpus-id",
        action="append",
        dest="corpus_ids",
        required=True,
        help="Exact corpus_id; repeat for an explicitly governed scope.",
    )
    parser.add_argument("--lock-owner")
    parser.add_argument("--expected-candidate-count", type=int)
    parser.add_argument("--confirm-write")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--batch-limit", type=int, default=32)
    parser.add_argument("--max-batches", type=int, default=100)
    return parser


def _require_write_authority(args: argparse.Namespace, candidate_count: int) -> None:
    if args.confirm_write != CONFIRMATION:
        raise ActivationCommandError(
            f"write mode requires --confirm-write {CONFIRMATION!r}"
        )
    if args.expected_candidate_count is None:
        raise ActivationCommandError(
            "write mode requires --expected-candidate-count from a prior census"
        )
    if int(args.expected_candidate_count) != int(candidate_count):
        raise ActivationCommandError(
            "candidate census drifted: "
            f"expected {args.expected_candidate_count}, found {candidate_count}"
        )
    if not args.lock_owner:
        raise ActivationCommandError("write mode requires --lock-owner")
    if not LOCK_PATH.exists():
        raise ActivationCommandError(f"shared lock is missing: {LOCK_PATH}")
    actual_owner = LOCK_PATH.read_text(encoding="utf-8").strip()
    if actual_owner != args.lock_owner:
        raise ActivationCommandError(
            f"shared lock owner mismatch: expected {args.lock_owner!r}"
        )
    if args.receipt is None:
        raise ActivationCommandError("write mode requires --receipt")


async def _database() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI, tz_aware=True)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[settings.MONGODB_DATABASE]
    settings_service.attach(db)
    return client, db


def _qdrant() -> AsyncQdrantClient:
    settings = get_settings()
    return AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
        prefer_grpc=settings.QDRANT_PREFER_GRPC,
        grpc_port=settings.QDRANT_GRPC_PORT,
    )


async def _index_preflight(db: Any) -> dict[str, Any]:
    await db.command({"ping": 1})
    details: dict[str, Any] = {}
    for name in (MANIFEST_COLLECTION, OUTBOX_COLLECTION):
        try:
            indexes = await db[name].list_indexes().to_list(length=None)
        except Exception:
            indexes = []
        details[name] = {
            "index_count": len(indexes),
            "v2_partial_unique_present": any(
                bool(row.get("unique"))
                and row.get("partialFilterExpression", {}).get("schema_version")
                in {"projection_manifest.v2", "projection_outbox.v2"}
                for row in indexes
            ),
        }
    return details


async def _census(
    db: Any,
    qdrant: AsyncQdrantClient,
    *,
    corpus_ids: list[str],
) -> dict[str, Any]:
    selection = await discover_digest_projection_selection(db, corpus_ids=corpus_ids)
    candidates = list(selection.candidates)
    manifest_ids = sorted({row.manifest.manifest_id for row in candidates})
    outbox_ids = [row.entry.outbox_id for row in candidates]
    point_ids = [row.entry.point_id for row in candidates]
    manifest_count = await db[MANIFEST_COLLECTION].count_documents(
        {
            "schema_version": "projection_manifest.v2",
            "manifest_id": {"$in": manifest_ids},
        }
    )
    outbox_rows = (
        await db[OUTBOX_COLLECTION]
        .find(
            {
                "schema_version": "projection_outbox.v2",
                "outbox_id": {"$in": outbox_ids},
            },
            {"_id": 0, "state": 1, "point_id": 1, "projected_payload_hash": 1},
        )
        .to_list(length=None)
    )
    outbox_states = Counter(str(row.get("state") or "unknown") for row in outbox_rows)

    qdrant_rows = []
    collection_name = (
        candidates[0].manifest.target.collection_name if candidates else ""
    )
    if point_ids:
        for start in range(0, len(point_ids), 256):
            qdrant_rows.extend(
                await qdrant.retrieve(
                    collection_name=collection_name,
                    ids=point_ids[start : start + 256],
                    with_payload=True,
                    with_vectors=False,
                )
            )
    expected_hashes = {
        row.entry.point_id: row.entry.projected_payload_hash for row in candidates
    }
    matching_payloads = 0
    for point in qdrant_rows:
        payload = dict(point.payload or {})
        stored_hash = str(payload.pop("projected_payload_hash", ""))
        expected_hash = expected_hashes.get(str(point.id))
        if (
            stored_hash == expected_hash
            and namespace_hash("body", payload) == expected_hash
        ):
            matching_payloads += 1
    return {
        "candidate_count": len(candidates),
        "legacy_provenance_closure_count": sum(
            row.provenance_closure["mode"] == "legacy_missing_job_prompt_version_labels"
            for row in candidates
        ),
        "legacy_provenance_closures": [
            row.provenance_closure
            for row in candidates
            if row.provenance_closure["mode"]
            == "legacy_missing_job_prompt_version_labels"
        ],
        "excluded_count": len(selection.exclusions),
        "exclusions": [row.receipt() for row in selection.exclusions],
        "unique_manifest_count": len(manifest_ids),
        "stored_manifest_count": int(manifest_count),
        "stored_outbox_count": len(outbox_rows),
        "outbox_states": dict(sorted(outbox_states.items())),
        "qdrant_point_count": len(qdrant_rows),
        "qdrant_matching_payload_count": int(matching_payloads),
        "qdrant_missing_point_count": max(0, len(point_ids) - len(qdrant_rows)),
        "collection_name": collection_name,
        "index_preflight": await _index_preflight(db),
    }


async def _drain(
    db: Any,
    qdrant: AsyncQdrantClient,
    *,
    corpus_ids: list[str],
    owner: str,
    batch_limit: int,
    max_batches: int,
    reconciliation_only: bool,
) -> dict[str, int]:
    worker = SemanticDigestProjectionWorker(
        db,
        qdrant,
        owner=owner,
        corpus_ids=corpus_ids,
    )
    totals: Counter[str] = Counter()
    for _ in range(max(1, int(max_batches))):
        counts = await worker.drain_batch(
            limit=min(32, max(1, int(batch_limit))),
            reconciliation_only=reconciliation_only,
        )
        totals.update(counts)
        if counts["claimed"] == 0:
            break
    return dict(sorted(totals.items()))


def _write_receipt(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temp = resolved.with_suffix(resolved.suffix + ".partial")
    body = canonical_json_v1(receipt) + "\n"
    temp.write_text(body, encoding="utf-8")
    temp.replace(resolved)
    return {
        "receipt_path": str(resolved),
        "receipt_hash": namespace_hash("body", receipt),
        "receipt_bytes": len(body.encode("utf-8")),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    corpus_ids = sorted({str(value) for value in args.corpus_ids if str(value)})
    mongo, db = await _database()
    qdrant = _qdrant()
    try:
        before = await _census(db, qdrant, corpus_ids=corpus_ids)
        result: dict[str, Any] = {"mode": args.mode, "before": before}
        if args.mode != "census":
            _require_write_authority(args, before["candidate_count"])
            result["contract_preflight"] = await ensure_activation_contracts(db)
            if args.mode == "enqueue":
                enqueue = await enqueue_digest_projections(db, corpus_ids=corpus_ids)
                result["operation"] = {
                    "candidate_count": int(enqueue["candidate_count"]),
                    "excluded_count": int(enqueue["excluded_count"]),
                    "exclusions": list(enqueue["exclusions"]),
                    "legacy_provenance_closure_count": int(
                        enqueue["legacy_provenance_closure_count"]
                    ),
                    "legacy_provenance_closures": list(
                        enqueue["legacy_provenance_closures"]
                    ),
                    "manifest_count": len(enqueue["manifest_ids"]),
                    "outbox_count": len(enqueue["outbox_ids"]),
                    "point_count": len(enqueue["point_ids"]),
                }
            else:
                result["operation"] = await _drain(
                    db,
                    qdrant,
                    corpus_ids=corpus_ids,
                    owner=str(args.lock_owner),
                    batch_limit=args.batch_limit,
                    max_batches=args.max_batches,
                    reconciliation_only=args.mode == "reconcile",
                )
            result["after"] = await _census(db, qdrant, corpus_ids=corpus_ids)
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "corpus_ids": corpus_ids,
            **result,
        }
        receipt["receipt_id"] = namespace_hash("work", receipt)
        if args.receipt is not None:
            result["durable_receipt"] = _write_receipt(args.receipt, receipt)
        return result
    finally:
        await qdrant.close()
        mongo.close()


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(
            canonical_json_v1(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            )
        )
        return 1
    print(canonical_json_v1({"status": "ok", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
