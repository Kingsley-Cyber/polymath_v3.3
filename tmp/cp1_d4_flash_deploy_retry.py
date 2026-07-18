#!/usr/bin/env python3
"""Sequential in-place deploy + behavioral canary for both RunPod accounts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import (
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    ExtractionTask,
    SchemaContext,
)
from services.runpod_flash_extraction import extract_entities
from services.settings import settings_service


EXPECTED_ENDPOINTS = {"t0nuyi6shc2t9a", "t5wjsqmvpjm0lm"}
FLASH_PYTHON = Path("/tmp/cp1_d4_flash_venv/bin/python")
DEPLOY_ONE = Path("/tmp/cp1_d4_flash_deploy_one.py")
PROJECT = Path(
    "/tmp/cp1_d4_account_deploys/account_0/runpod_flash_extractor"
)
ARTIFACT = PROJECT / ".flash" / "artifact.tar.gz"
SOURCE = Path("/tmp/cp1_d4_runpod_source/app.py")
CANARY_TEXT = (
    "The observatory reopened in autumn 1996. "
    "The 2003 coastal migration period required a revised supply schedule."
)
EXPECTED_CAPTURES = {"autumn 1996", "2003 coastal migration period"}


async def runtime():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        config, _legacy_key = await settings_service.get_system_runpod_flash()
        accounts = await settings_service.get_system_runpod_flash_accounts()
        return config, accounts
    finally:
        client.close()


def deploy_environment(api_key: str) -> dict[str, str]:
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


async def canary(config, account, api_key: str) -> dict:
    task = ExtractionTask(
        chunk_id=f"cp1-d4-{account.name}-canary",
        doc_id="cp1-d4-synthetic",
        corpus_id="cp1-d4-synthetic",
        text=CANARY_TEXT,
    )
    report = await extract_entities(
        [task],
        schema=SchemaContext(
            entity_schema=list(UNIVERSAL_ENTITY_SCHEMA),
            relation_schema=list(UNIVERSAL_RELATION_SCHEMA),
            strict="soft",
        ),
        runpod_config=config,
        accounts=[(account, api_key)],
        return_report=True,
    )
    if report.failures or len(report.results) != 1:
        raise RuntimeError(
            f"canary failed for {account.name}: results={len(report.results)} "
            f"failures={len(report.failures)}"
        )
    result = report.results[0]
    captures = [
        capture
        for capture in result.temporal_captures
        if isinstance(capture, dict)
    ]
    capture_texts = {str(capture.get("text") or "") for capture in captures}
    if not EXPECTED_CAPTURES.issubset(capture_texts):
        raise RuntimeError(
            f"canary capture mismatch for {account.name}: {sorted(capture_texts)}"
        )
    for capture in captures:
        start = int(capture.get("char_start") or 0)
        end = int(capture.get("char_end") or 0)
        if CANARY_TEXT[start:end] != capture.get("text"):
            raise RuntimeError(f"canary offset mismatch for {account.name}")
    return {
        "account": account.name,
        "endpoint_id": account.endpoint_id,
        "contract": result.temporal_capture_version,
        "capture_texts": sorted(capture_texts),
        "required_captures_present": True,
        "offsets_exact": True,
        "provider": result.provider,
        "model": result.model,
    }


async def main_async() -> int:
    config, accounts = await runtime()
    endpoint_ids = {account.endpoint_id for account, _key in accounts}
    if len(accounts) != 2 or endpoint_ids != EXPECTED_ENDPOINTS:
        raise RuntimeError(
            f"inventory mismatch: expected {sorted(EXPECTED_ENDPOINTS)}, "
            f"found {sorted(endpoint_ids)}"
        )
    if not all(path.is_file() for path in (FLASH_PYTHON, DEPLOY_ONE, ARTIFACT, SOURCE)):
        raise RuntimeError("two-stage deploy runtime, source, or artifact is absent")
    project_source = PROJECT / "app.py"
    source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    project_sha = hashlib.sha256(project_source.read_bytes()).hexdigest()
    if source_sha != project_sha:
        raise RuntimeError("built worker source differs from committed deploy source")
    print(f"WORKER_SOURCE_SHA256={source_sha}", flush=True)

    for account, api_key in accounts:
        print(
            f"INVENTORY_ENDPOINT_ID name={account.name} id={account.endpoint_id}",
            flush=True,
        )
        completed = subprocess.run(
            [
                str(FLASH_PYTHON),
                str(DEPLOY_ONE),
                "--project",
                str(PROJECT),
                "--artifact",
                str(ARTIFACT),
                "--endpoint-id",
                account.endpoint_id,
            ],
            cwd=PROJECT,
            env=deploy_environment(api_key),
            check=False,
        )
        print(
            f"IN_PLACE_DEPLOY_EXIT name={account.name} exit={completed.returncode}",
            flush=True,
        )
        if completed.returncode != 0:
            return completed.returncode
        canary_result = await canary(config, account, api_key)
        print(f"CANARY={json.dumps(canary_result, sort_keys=True)}", flush=True)
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
