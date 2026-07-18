"""Read-only, secret-safe owner and summary-route preflight for the E2E corpus."""

from __future__ import annotations

import asyncio
import json

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.summary_provider_pool import resolve_summary_provider_pool
from services.settings import settings_service


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        settings_service.attach(database)
        users = await database["users"].find(
            {},
            {"_id": 1, "username": 1},
        ).to_list(length=2)
        if len(users) != 1:
            raise RuntimeError("exactly one API owner must be discoverable")
        user = users[0]
        if not ObjectId.is_valid(str(user.get("_id") or "")):
            raise RuntimeError("API owner identity is invalid")
        runtime = await settings_service.get_runtime_ingestion_settings(str(user["_id"]))
        summaries = []
        for model in runtime.summary.summary_models or []:
            row = model.model_dump() if hasattr(model, "model_dump") else dict(model)
            summaries.append(
                {
                    "capabilities": row.get("capabilities") or [],
                    "enabled": row.get("enabled", True),
                    "has_api_key": bool(row.get("api_key")),
                    "max_concurrent": row.get("max_concurrent"),
                    "model": row.get("model"),
                    "profile_id": row.get("profile_id"),
                    "provider_preset": row.get("provider_preset"),
                    "runtime": row.get("runtime"),
                }
            )
        if not summaries or not all(row["has_api_key"] for row in summaries):
            raise RuntimeError("certified API summary route is unavailable")
        _, resolution = await resolve_summary_provider_pool(
            configured_refs=runtime.summary.summary_models,
            runtime_refs=runtime.summary.summary_models,
            user_id=str(user["_id"]),
            db=database,
        )
        if not resolution.get("flash_primary") or not resolution.get(
            "flash_key_available"
        ):
            raise RuntimeError("DeepSeek Flash summary primary is unavailable")
        print(
            json.dumps(
                {
                    "owner_username": user["username"],
                    "summary_models": summaries,
                    "resolved_pool": resolution,
                    "max_summary_tokens": runtime.summary.max_summary_tokens,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
