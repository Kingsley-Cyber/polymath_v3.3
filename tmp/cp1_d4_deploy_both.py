#!/usr/bin/env python3
"""Deploy the committed RunPod Flash worker to both configured accounts.

Credentials are resolved inside the backend process from encrypted Settings,
passed only in-memory to the Flash child process, and never printed or written.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.settings import settings_service


EXPECTED_ENDPOINTS = {"t0nuyi6shc2t9a", "t5wjsqmvpjm0lm"}
FLASH = Path("/tmp/cp1_d4_flash_venv/bin/flash")
SOURCE = Path("/tmp/cp1_d4_runpod_source/app.py")
DEPLOY_ROOT = Path("/tmp/cp1_d4_account_deploys")


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


def child_environment(api_key: str) -> dict[str, str]:
    safe_names = (
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "TMPDIR",
    )
    env = {name: os.environ[name] for name in safe_names if os.environ.get(name)}
    env.update(
        {
            "RUNPOD_API_KEY": api_key,
            "RUNPOD_FLASH_MIN_WORKERS": "0",
            "RUNPOD_FLASH_MAX_WORKERS": "8",
            "RUNPOD_FLASH_WORKER_CONCURRENCY": "1",
            "RUNPOD_FLASH_IDLE_TIMEOUT": "60",
            "RUNPOD_FLASH_SCALER_VALUE": "1",
            "RUNPOD_FLASH_EXECUTION_TIMEOUT_MS": "1800000",
        }
    )
    return env


def main() -> int:
    accounts = asyncio.run(resolved_accounts())
    endpoint_ids = {account.endpoint_id for account, _key in accounts}
    if len(accounts) != 2 or endpoint_ids != EXPECTED_ENDPOINTS:
        raise RuntimeError(
            f"expected two configured extraction endpoints {sorted(EXPECTED_ENDPOINTS)}, "
            f"found {sorted(endpoint_ids)}"
        )
    if not FLASH.is_file() or not SOURCE.is_file():
        raise RuntimeError("deploy runtime or source is absent")

    DEPLOY_ROOT.mkdir(parents=True, exist_ok=True)
    for index, (account, api_key) in enumerate(accounts):
        if not api_key:
            raise RuntimeError(f"configured account {account.name!r} has no resolved key")
        project = DEPLOY_ROOT / f"account_{index}" / "runpod_flash_extractor"
        if project.parent.exists():
            shutil.rmtree(project.parent)
        project.mkdir(parents=True)
        shutil.copy2(SOURCE, project / "app.py")
        print(f"DEPLOY_START name={account.name} endpoint={account.endpoint_id}", flush=True)
        completed = subprocess.run(
            [
                str(FLASH),
                "deploy",
                "--app",
                "runpod_flash_extractor",
                "--env",
                "production",
                "--python-version",
                "3.12",
            ],
            cwd=project,
            env=child_environment(api_key),
            check=False,
        )
        print(
            f"DEPLOY_END name={account.name} endpoint={account.endpoint_id} "
            f"exit={completed.returncode}",
            flush=True,
        )
        if completed.returncode != 0:
            return completed.returncode
    print(f"DEPLOYED_ENDPOINTS={sorted(endpoint_ids)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
