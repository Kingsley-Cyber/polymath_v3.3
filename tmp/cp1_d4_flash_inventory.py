#!/usr/bin/env python3
"""Read-only Flash application inventory for configured RunPod accounts."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.settings import settings_service


FLASH = Path("/tmp/cp1_d4_flash_venv/bin/flash")


async def resolved_accounts():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        return await settings_service.get_system_runpod_flash_accounts()
    finally:
        client.close()


def safe_environment(api_key: str) -> dict[str, str]:
    names = (
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "TMPDIR",
    )
    env = {name: os.environ[name] for name in names if os.environ.get(name)}
    env["RUNPOD_API_KEY"] = api_key
    return env


def main() -> int:
    accounts = asyncio.run(resolved_accounts())
    for account, api_key in accounts:
        print(f"ACCOUNT name={account.name} endpoint={account.endpoint_id}", flush=True)
        for label, command in (
            ("APP_LIST", [str(FLASH), "app", "list"]),
            (
                "APP_GET",
                [str(FLASH), "app", "get", "runpod_flash_extractor"],
            ),
            (
                "ENV_LIST",
                [
                    str(FLASH),
                    "env",
                    "list",
                    "--app",
                    "runpod_flash_extractor",
                ],
            ),
            (
                "ENV_GET",
                [
                    str(FLASH),
                    "env",
                    "get",
                    "production",
                    "--app",
                    "runpod_flash_extractor",
                ],
            ),
            (
                "ACTIVE_MANIFEST",
                [
                    "/tmp/cp1_d4_flash_venv/bin/python",
                    "/tmp/cp1_d4_flash_active_manifest.py",
                ],
            ),
        ):
            completed = subprocess.run(
                command,
                env=safe_environment(api_key),
                check=False,
            )
            print(f"{label}_EXIT={completed.returncode}", flush=True)
            if completed.returncode != 0:
                return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
