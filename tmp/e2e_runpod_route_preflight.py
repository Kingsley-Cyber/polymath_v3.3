"""Read-only, secret-safe RunPod request-capacity preflight."""

from __future__ import annotations

import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.settings import settings_service


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        settings_service.attach(database)
        config, _ = await settings_service.get_system_runpod_flash()
        accounts = await settings_service.get_system_runpod_flash_accounts()
        primary = [
            account
            for account, key in accounts
            if account.enabled and account.name == "primary" and key
        ]
        if len(primary) != 1:
            raise RuntimeError("primary RunPod account did not resolve exactly once")
        account = primary[0]
        print(
            json.dumps(
                {
                    "account_max_workers": account.max_workers,
                    "account_request_concurrency": account.request_concurrency,
                    "global_request_concurrency": config.request_concurrency,
                    "request_batch_size": min(64, config.request_batch_size),
                    "timeout_seconds": config.timeout_seconds,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
