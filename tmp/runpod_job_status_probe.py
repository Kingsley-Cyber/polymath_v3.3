#!/usr/bin/env python3
"""Operational scratch: preserve one known RunPod job status without secrets."""

from __future__ import annotations

import asyncio
import json

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.settings import settings_service


ENDPOINT_ID = "zcdutqjzvlyz30"
JOB_ID = "e02cdc3a-4ce3-4005-bc71-7a26c3ee4a73-u2"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:  # noqa: BLE001
            db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        accounts = await settings_service.get_system_runpod_flash_accounts()
        primary = [row for row in accounts if row[0].name == "primary"]
        if len(primary) != 1:
            raise RuntimeError("primary account is not uniquely configured")
        _, key = primary[0]
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(
                f"https://api.runpod.ai/v2/{ENDPOINT_ID}/status/{JOB_ID}",
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
            body = response.json()
        print(
            json.dumps(
                {
                    "endpoint_id": ENDPOINT_ID,
                    "job": body,
                    "secret_values_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
