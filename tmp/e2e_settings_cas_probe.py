#!/usr/bin/env python3
"""Read-only diagnosis for the E2E burst-settings restoration CAS."""

from __future__ import annotations

import asyncio
import copy
import json

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from e2e_burst_settings_operator import BASELINE_PATH, _burst, _document, _safe


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            database = client.get_default_database()
        except Exception:  # noqa: BLE001
            database = client[settings.MONGODB_DATABASE]
        doc = await _document(database)
        raw = copy.deepcopy(doc["ingestion"]["runpod_flash"])
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        original = baseline.get("runpod_flash") or {}
        expected_burst = _burst(original)
        full_filter_count = await database["settings"].count_documents(
            {"_id": doc["_id"], "ingestion.runpod_flash": raw}
        )
        burst_filter_count = await database["settings"].count_documents(
            {"_id": doc["_id"], "ingestion.runpod_flash": expected_burst}
        )
        print(
            json.dumps(
                {
                    "raw_equals_expected_burst": raw == expected_burst,
                    "raw_equals_original": raw == original,
                    "full_filter_count": full_filter_count,
                    "burst_filter_count": burst_filter_count,
                    "safe_current": _safe(raw),
                    "safe_expected_burst": _safe(expected_burst),
                    "safe_original": _safe(original),
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
