#!/usr/bin/env python3
"""Read-only RunPod endpoint worker-quota census for configured accounts."""

from __future__ import annotations

import asyncio
import json

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.settings import settings_service


async def main() -> None:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = mongo.get_default_database()
        except Exception:
            db = mongo[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        accounts = await settings_service.get_system_runpod_flash_accounts()
        async with httpx.AsyncClient(
            base_url="https://rest.runpod.io/v1", timeout=30.0
        ) as client:
            for account, api_key in accounts:
                response = await client.get(
                    "/endpoints",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response.raise_for_status()
                endpoints = response.json()
                safe = [
                    {
                        "id": row.get("id"),
                        "name": row.get("name"),
                        "workersMax": int(row.get("workersMax") or 0),
                        "workersMin": int(row.get("workersMin") or 0),
                    }
                    for row in endpoints
                ]
                print(
                    json.dumps(
                        {
                            "account": account.name,
                            "configured_extraction_endpoint": account.endpoint_id,
                            "endpoint_count": len(safe),
                            "workersMax_total": sum(
                                row["workersMax"] for row in safe
                            ),
                            "endpoints": sorted(safe, key=lambda row: str(row["id"])),
                        },
                        sort_keys=True,
                    )
                )
    finally:
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
