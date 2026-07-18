#!/usr/bin/env python3
"""CAS-protected reversible RunPod client-concurrency scaling."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from models.schemas import RunpodFlashExtractionSettings


BASELINE_PATH = Path(
    "/data/ingest-files/runpod-job-journals/e2e-burst-settings-baseline.json"
)
PRIOR_GLOBAL_CONCURRENCY = 8
PRIOR_ACCOUNT_MAX = 8
PRIOR_ACCOUNT_CONCURRENCY = 8
BURST_GLOBAL_CONCURRENCY = 20
BURST_ACCOUNT_MAX = 10
BURST_ACCOUNT_CONCURRENCY = 10


def _account_projection(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": row.get("name"),
            "endpoint_id": row.get("endpoint_id"),
            "embed_endpoint_id": row.get("embed_endpoint_id"),
            "enabled": row.get("enabled"),
            "max_workers": row.get("max_workers"),
            "request_concurrency": row.get("request_concurrency"),
        }
        for row in raw.get("accounts") or []
    ]


def _safe(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": raw.get("enabled"),
        "request_batch_size": raw.get("request_batch_size"),
        "request_concurrency": raw.get("request_concurrency"),
        "timeout_seconds": raw.get("timeout_seconds"),
        "accounts": _account_projection(raw),
    }


def _validate_prestate(raw: dict[str, Any]) -> None:
    config = RunpodFlashExtractionSettings(**raw)
    if not config.enabled:
        raise RuntimeError("RunPod Flash is disabled")
    if config.request_concurrency != PRIOR_GLOBAL_CONCURRENCY:
        raise RuntimeError("RunPod global request concurrency prestate drifted")
    if {row.name for row in config.accounts} != {"primary", "secondary"}:
        raise RuntimeError("RunPod account set drifted")
    for account in config.accounts:
        if account.max_workers != PRIOR_ACCOUNT_MAX:
            raise RuntimeError(f"account={account.name} max_workers prestate drifted")
        if account.request_concurrency != PRIOR_ACCOUNT_CONCURRENCY:
            raise RuntimeError(
                f"account={account.name} request_concurrency prestate drifted"
            )


def _burst(raw: dict[str, Any]) -> dict[str, Any]:
    desired = copy.deepcopy(raw)
    desired["request_concurrency"] = BURST_GLOBAL_CONCURRENCY
    for account in desired.get("accounts") or []:
        account["max_workers"] = BURST_ACCOUNT_MAX
        account["request_concurrency"] = BURST_ACCOUNT_CONCURRENCY
    RunpodFlashExtractionSettings(**desired)
    return desired


def _semantic_burst_guard(doc_id: Any, expected: dict[str, Any]) -> dict[str, Any]:
    """CAS the burst state without relying on BSON subdocument key order.

    A settings-attach read/write can preserve identical Python semantics while
    changing embedded-document field order. Mongo's equality match treats that
    order as significant, so the whole-subdocument predicate is not a stable
    compare-and-swap boundary. The caller first proves full semantic equality;
    this predicate then guards every changed scalar plus the immutable routing
    identities immediately at the write boundary.
    """

    accounts = expected.get("accounts") or []
    return {
        "_id": doc_id,
        "ingestion.runpod_flash.enabled": expected.get("enabled"),
        "ingestion.runpod_flash.request_batch_size": expected.get(
            "request_batch_size"
        ),
        "ingestion.runpod_flash.request_concurrency": expected.get(
            "request_concurrency"
        ),
        "ingestion.runpod_flash.timeout_seconds": expected.get("timeout_seconds"),
        "ingestion.runpod_flash.accounts": {
            "$size": len(accounts),
            "$all": [
                {
                    "$elemMatch": {
                        "name": account.get("name"),
                        "endpoint_id": account.get("endpoint_id"),
                        "embed_endpoint_id": account.get("embed_endpoint_id"),
                        "enabled": account.get("enabled"),
                        "max_workers": account.get("max_workers"),
                        "request_concurrency": account.get("request_concurrency"),
                    }
                }
                for account in accounts
            ],
        },
    }


def _write_baseline(doc_id: Any, raw: dict[str, Any]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "polymath.runpod_e2e_burst_settings_baseline.v1",
        "settings_id": str(doc_id),
        "runpod_flash": raw,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with BASELINE_PATH.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


async def _document(database) -> dict[str, Any]:
    rows = await database["settings"].find(
        {
            "ingestion.runpod_flash": {"$exists": True},
            "api_keys.runpod_accounts.primary": {"$exists": True},
            "api_keys.runpod_accounts.secondary": {"$exists": True},
        },
        {"user_id": 1, "ingestion.runpod_flash": 1},
    ).to_list(length=3)
    if len(rows) != 1:
        raise RuntimeError(
            f"RunPod settings owner must resolve exactly once; observed={len(rows)}"
        )
    return rows[0]


async def run(action: str) -> dict[str, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            database = client.get_default_database()
        except Exception:  # noqa: BLE001
            database = client[settings.MONGODB_DATABASE]
        doc = await _document(database)
        raw = copy.deepcopy(doc["ingestion"]["runpod_flash"])
        if action == "preflight":
            _validate_prestate(raw)
            result = {"current": _safe(raw), "baseline_exists": BASELINE_PATH.exists()}
        elif action == "scale-up":
            _validate_prestate(raw)
            _write_baseline(doc["_id"], raw)
            desired = _burst(raw)
            update = await database["settings"].update_one(
                {"_id": doc["_id"], "ingestion.runpod_flash": raw},
                {"$set": {"ingestion.runpod_flash": desired}},
            )
            if update.matched_count != 1 or update.modified_count != 1:
                raise RuntimeError("RunPod settings scale CAS failed")
            verified = copy.deepcopy(
                (await _document(database))["ingestion"]["runpod_flash"]
            )
            if verified != desired:
                raise RuntimeError("RunPod burst settings verification drifted")
            result = {
                "before": _safe(raw),
                "after": _safe(verified),
                "baseline_path": str(BASELINE_PATH),
            }
        else:
            if not BASELINE_PATH.is_file():
                raise RuntimeError("RunPod settings baseline is missing")
            baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            original = baseline.get("runpod_flash") or {}
            _validate_prestate(original)
            expected_burst = _burst(original)
            if raw != expected_burst:
                raise RuntimeError("RunPod settings restore prestate drifted")
            update = await database["settings"].update_one(
                _semantic_burst_guard(doc["_id"], expected_burst),
                {"$set": {"ingestion.runpod_flash": original}},
            )
            if update.matched_count != 1 or update.modified_count != 1:
                raise RuntimeError("RunPod settings restore CAS failed")
            verified = copy.deepcopy(
                (await _document(database))["ingestion"]["runpod_flash"]
            )
            if verified != original:
                raise RuntimeError("RunPod settings restore verification drifted")
            result = {
                "before": _safe(raw),
                "after": _safe(verified),
                "baseline_path": str(BASELINE_PATH),
            }
        return {
            "action": action,
            "result": result,
            "secret_values_emitted": 0,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action", choices=("preflight", "scale-up", "restore"), required=True
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.action)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
