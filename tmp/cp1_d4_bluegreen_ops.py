#!/usr/bin/env python3
"""Receiptable CP1-D4 RunPod blue-green and in-place deployment operations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/tmp/cp1_d4_flash_venv/lib/python3.11/site-packages")
from runpod_flash.core.api.runpod import RunpodGraphQLClient

from config import get_settings
from services.ghost_b import (
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    ExtractionTask,
    SchemaContext,
)
from services.runpod_flash_extraction import extract_entities
from services.settings import settings_service


REST_BASE = "https://rest.runpod.io/v1"
FLASH = Path("/tmp/cp1_d4_flash_venv/bin/flash")
SOURCE = Path("/tmp/cp1_d4_runpod_source/app.py")
SECONDARY_PICKLE = Path("/tmp/cp1_d4_secondary_resources.pkl")
RESOURCE_NAME = "polymath-gliner-relex"
EMBED_RESOURCE_NAME = "polymath-embed-qwen3"
EXPECTED_OLD = {
    "primary": "t0nuyi6shc2t9a",
    "secondary": "t5wjsqmvpjm0lm",
}
CANARY_TEXT = (
    "The observatory reopened in autumn 1996. "
    "The 2003 coastal migration period required a revised supply schedule."
)
EXPECTED_CAPTURES = {"autumn 1996", "2003 coastal migration period"}


class Context:
    def __init__(self, account_name: str) -> None:
        self.account_name = account_name
        self.mongo: AsyncIOMotorClient | None = None
        self.db: Any = None
        self.config: Any = None
        self.account: Any = None
        self.api_key = ""

    async def open(self) -> "Context":
        settings = get_settings()
        self.mongo = AsyncIOMotorClient(settings.MONGODB_URI)
        try:
            self.db = self.mongo.get_default_database()
        except Exception:
            self.db = self.mongo[settings.MONGODB_DATABASE]
        settings_service.attach(self.db)
        self.config, _legacy = await settings_service.get_system_runpod_flash()
        accounts = await settings_service.get_system_runpod_flash_accounts()
        matches = [(account, key) for account, key in accounts if account.name == self.account_name]
        if len(matches) != 1:
            raise RuntimeError(f"expected one configured {self.account_name} account")
        self.account, self.api_key = matches[0]
        if not self.api_key:
            raise RuntimeError("resolved account key is empty")
        return self

    def close(self) -> None:
        if self.mongo is not None:
            self.mongo.close()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


def state_path(account_name: str) -> Path:
    return Path(f"/tmp/cp1_d4_bluegreen_{account_name}.json")


def load_state(account_name: str) -> dict[str, Any]:
    path = state_path(account_name)
    if not path.is_file():
        raise RuntimeError(f"blue-green state absent for {account_name}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("account") != account_name:
        raise RuntimeError("blue-green state account mismatch")
    return state


def write_state(account_name: str, state: dict[str, Any]) -> None:
    state_path(account_name).write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )


async def endpoint(client: httpx.AsyncClient, ctx: Context, endpoint_id: str) -> dict:
    response = await client.get(f"/endpoints/{endpoint_id}", headers=ctx.headers())
    response.raise_for_status()
    return response.json()


async def endpoint_or_none(
    client: httpx.AsyncClient, ctx: Context, endpoint_id: str
) -> dict | None:
    response = await client.get(f"/endpoints/{endpoint_id}", headers=ctx.headers())
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def endpoints(client: httpx.AsyncClient, ctx: Context) -> list[dict]:
    response = await client.get("/endpoints", headers=ctx.headers())
    response.raise_for_status()
    return list(response.json())


async def patch_workers(
    client: httpx.AsyncClient, ctx: Context, endpoint_id: str, workers_max: int
) -> dict:
    response = await client.patch(
        f"/endpoints/{endpoint_id}",
        headers=ctx.headers(),
        json={"workersMax": workers_max},
    )
    response.raise_for_status()
    row = response.json()
    verified = await endpoint(client, ctx, endpoint_id)
    if int(verified.get("workersMax") or -1) != workers_max:
        raise RuntimeError(
            f"workersMax patch mismatch for {endpoint_id}: {verified.get('workersMax')}"
        )
    print(
        json.dumps(
            {
                "operation": "patch_workers",
                "endpoint_id": endpoint_id,
                "response_id": row.get("id"),
                "workersMax": workers_max,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return verified


async def delete_endpoint(
    client: httpx.AsyncClient, ctx: Context, endpoint_id: str
) -> None:
    response = await client.delete(
        f"/endpoints/{endpoint_id}", headers=ctx.headers()
    )
    if response.status_code != 204:
        raise RuntimeError(
            f"delete {endpoint_id} returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    if await endpoint_or_none(client, ctx, endpoint_id) is not None:
        raise RuntimeError(f"deleted endpoint {endpoint_id} remains visible")
    print(
        json.dumps(
            {"operation": "delete_endpoint", "endpoint_id": endpoint_id, "http": 204},
            sort_keys=True,
        ),
        flush=True,
    )


def flash_environment(api_key: str, workers_max: int) -> dict[str, str]:
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
    env.update(
        {
            "RUNPOD_API_KEY": api_key,
            "RUNPOD_FLASH_MIN_WORKERS": "0",
            "RUNPOD_FLASH_MAX_WORKERS": str(workers_max),
            "RUNPOD_FLASH_WORKER_CONCURRENCY": "1",
            "RUNPOD_FLASH_IDLE_TIMEOUT": "60",
            "RUNPOD_FLASH_SCALER_VALUE": "1",
            "RUNPOD_FLASH_EXECUTION_TIMEOUT_MS": "1800000",
        }
    )
    return env


def prepare_project(account_name: str, lane: str, *, resource_pickle: Path | None = None) -> Path:
    project = Path(
        f"/tmp/cp1_d4_{lane}_{account_name}/runpod_flash_extractor"
    )
    if project.parent.exists():
        shutil.rmtree(project.parent)
    project.mkdir(parents=True)
    shutil.copy2(SOURCE, project / "app.py")
    if resource_pickle is not None:
        flash_dir = project / ".flash"
        flash_dir.mkdir()
        shutil.copy2(resource_pickle, flash_dir / "resources.pkl")
    return project


def flash_deploy(project: Path, ctx: Context, workers_max: int) -> int:
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
        env=flash_environment(ctx.api_key, workers_max),
        check=False,
    )
    print(f"FLASH_DEPLOY_EXIT={completed.returncode}", flush=True)
    return completed.returncode


def deployed_manifest_id(project: Path) -> str:
    manifest = json.loads(
        (project / ".flash" / "flash_manifest.json").read_text(encoding="utf-8")
    )
    return str(
        ((manifest.get("resources") or {}).get(RESOURCE_NAME) or {}).get(
            "endpoint_id"
        )
        or ""
    )


async def run_canary(ctx: Context, endpoint_id: str) -> dict[str, Any]:
    target = ctx.account.model_copy(
        update={"endpoint_id": endpoint_id, "max_workers": 7}
    )
    task = ExtractionTask(
        chunk_id=f"cp1-d4-{ctx.account_name}-canary",
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
        runpod_config=ctx.config,
        accounts=[(target, ctx.api_key)],
        return_report=True,
    )
    if report.failures or len(report.results) != 1:
        raise RuntimeError(
            f"canary results={len(report.results)} failures={len(report.failures)}"
        )
    result = report.results[0]
    captures = [
        row for row in result.temporal_captures if isinstance(row, dict)
    ]
    capture_texts = {str(row.get("text") or "") for row in captures}
    if not EXPECTED_CAPTURES.issubset(capture_texts):
        raise RuntimeError(f"required canary captures absent: {sorted(capture_texts)}")
    for row in captures:
        start = int(row.get("char_start") or 0)
        end = int(row.get("char_end") or 0)
        if CANARY_TEXT[start:end] != row.get("text"):
            raise RuntimeError("canary offset mismatch")
    safe = {
        "operation": "synthetic_canary",
        "account": ctx.account_name,
        "endpoint_id": endpoint_id,
        "capture_texts": sorted(capture_texts),
        "required_captures_present": True,
        "offsets_exact": True,
        "contract": result.temporal_capture_version,
        "provider": result.provider,
        "model": result.model,
    }
    print(json.dumps(safe, sort_keys=True), flush=True)
    return safe


async def configured_endpoint(ctx: Context) -> str:
    rows = await ctx.db.settings.find(
        {
            "ingestion.runpod_flash.accounts": {
                "$elemMatch": {"name": ctx.account_name}
            }
        },
        {"_id": 1, "ingestion.runpod_flash.accounts": 1},
    ).to_list(length=10)
    if len(rows) != 1:
        raise RuntimeError(f"expected one settings row for {ctx.account_name}")
    accounts = ((rows[0].get("ingestion") or {}).get("runpod_flash") or {}).get(
        "accounts"
    ) or []
    matches = [row for row in accounts if row.get("name") == ctx.account_name]
    if len(matches) != 1:
        raise RuntimeError("settings account row mismatch")
    return str(matches[0].get("endpoint_id") or "")


async def swap_settings(ctx: Context, old_id: str, new_id: str) -> None:
    row = await ctx.db.settings.find_one(
        {
            "ingestion.runpod_flash.accounts": {
                "$elemMatch": {"name": ctx.account_name, "endpoint_id": old_id}
            }
        },
        {"_id": 1},
    )
    if not row:
        raise RuntimeError("settings CAS precondition did not match")
    result = await ctx.db.settings.update_one(
        {"_id": row["_id"]},
        {"$set": {"ingestion.runpod_flash.accounts.$[acct].endpoint_id": new_id}},
        array_filters=[
            {"acct.name": ctx.account_name, "acct.endpoint_id": old_id}
        ],
    )
    if result.matched_count != 1 or result.modified_count != 1:
        raise RuntimeError(
            f"settings CAS matched={result.matched_count} modified={result.modified_count}"
        )
    settings_service._invalidate_cache()
    current = await configured_endpoint(ctx)
    if current != new_id:
        raise RuntimeError(f"settings verification expected {new_id}, found {current}")
    print(
        json.dumps(
            {
                "operation": "settings_cas_swap",
                "account": ctx.account_name,
                "old_endpoint_id": old_id,
                "new_endpoint_id": new_id,
                "matched": result.matched_count,
                "modified": result.modified_count,
            },
            sort_keys=True,
        ),
        flush=True,
    )


async def operation(args: argparse.Namespace) -> int:
    ctx = await Context(args.account).open()
    try:
        expected_old = EXPECTED_OLD[args.account]
        async with httpx.AsyncClient(base_url=REST_BASE, timeout=60.0) as client:
            if args.operation == "patch-old-to-one":
                if ctx.account.endpoint_id != expected_old:
                    raise RuntimeError(
                        f"configured old endpoint mismatch: {ctx.account.endpoint_id}"
                    )
                old = await endpoint(client, ctx, expected_old)
                if int(old.get("workersMax") or -1) != 8:
                    raise RuntimeError("old endpoint is not at workersMax=8")
                await patch_workers(client, ctx, expected_old, 1)
                return 0

            if args.operation == "deploy-fresh-seven":
                old = await endpoint(client, ctx, expected_old)
                if int(old.get("workersMax") or -1) != 1:
                    raise RuntimeError("old endpoint must be at workersMax=1")
                before = await endpoints(client, ctx)
                if sum(int(row.get("workersMax") or 0) for row in before) != 3:
                    raise RuntimeError("pre-deploy quota total is not 3")
                project = prepare_project(args.account, "bluegreen")
                source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
                print(f"WORKER_SOURCE_SHA256={source_sha}", flush=True)
                rc = flash_deploy(project, ctx, 7)
                if rc != 0:
                    await patch_workers(client, ctx, expected_old, 8)
                    raise RuntimeError("fresh Flash deployment failed; old endpoint restored")
                new_id = deployed_manifest_id(project)
                if not new_id or new_id == expected_old:
                    await patch_workers(client, ctx, expected_old, 8)
                    raise RuntimeError("fresh deploy did not return a distinct endpoint")
                new = await endpoint(client, ctx, new_id)
                if int(new.get("workersMax") or -1) != 7:
                    await delete_endpoint(client, ctx, new_id)
                    await patch_workers(client, ctx, expected_old, 8)
                    raise RuntimeError("fresh endpoint workersMax is not 7")
                after = await endpoints(client, ctx)
                total = sum(int(row.get("workersMax") or 0) for row in after)
                if total != 10:
                    await delete_endpoint(client, ctx, new_id)
                    await patch_workers(client, ctx, expected_old, 8)
                    raise RuntimeError(f"post-deploy quota total is {total}, not 10")
                write_state(
                    args.account,
                    {
                        "account": args.account,
                        "old_endpoint_id": expected_old,
                        "new_endpoint_id": new_id,
                        "canary_passed": False,
                        "settings_swapped": False,
                        "old_deleted": False,
                    },
                )
                print(
                    json.dumps(
                        {
                            "operation": "fresh_deploy",
                            "old_endpoint_id": expected_old,
                            "new_endpoint_id": new_id,
                            "new_workersMax": 7,
                            "quota_total": total,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

            if args.operation == "canary-new":
                state = load_state(args.account)
                new_id = str(state["new_endpoint_id"])
                new = await endpoint(client, ctx, new_id)
                if int(new.get("workersMax") or -1) != 7:
                    raise RuntimeError("new endpoint is not at workersMax=7")
                await run_canary(ctx, new_id)
                state["canary_passed"] = True
                write_state(args.account, state)
                return 0

            if args.operation == "settings-swap":
                state = load_state(args.account)
                if not state.get("canary_passed"):
                    raise RuntimeError("settings swap requires a passed canary")
                await swap_settings(
                    ctx,
                    str(state["old_endpoint_id"]),
                    str(state["new_endpoint_id"]),
                )
                state["settings_swapped"] = True
                write_state(args.account, state)
                return 0

            if args.operation == "delete-old":
                state = load_state(args.account)
                if not state.get("settings_swapped"):
                    raise RuntimeError("old delete requires settings swap")
                current = await configured_endpoint(ctx)
                if current != state["new_endpoint_id"]:
                    raise RuntimeError("settings no longer point to the new endpoint")
                await delete_endpoint(client, ctx, str(state["old_endpoint_id"]))
                state["old_deleted"] = True
                write_state(args.account, state)
                return 0

            if args.operation == "promote-new-to-eight":
                state = load_state(args.account)
                if not state.get("old_deleted"):
                    raise RuntimeError("promotion requires old endpoint deletion")
                new_id = str(state["new_endpoint_id"])
                await patch_workers(client, ctx, new_id, 8)
                rows = await endpoints(client, ctx)
                total = sum(int(row.get("workersMax") or 0) for row in rows)
                if total != 10:
                    raise RuntimeError(f"final quota total is {total}, not 10")
                print(
                    json.dumps(
                        {"operation": "quota_final", "workersMax_total": total},
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

            if args.operation == "secondary-inplace":
                if args.account != "secondary":
                    raise RuntimeError("secondary-inplace is secondary-only")
                if ctx.account.endpoint_id != expected_old:
                    raise RuntimeError("secondary configured endpoint drift")
                if not SECONDARY_PICKLE.is_file():
                    raise RuntimeError("secondary resources.pkl is absent")
                project = prepare_project(
                    args.account, "inplace", resource_pickle=SECONDARY_PICKLE
                )
                print(
                    f"SECONDARY_PICKLE_SHA256={hashlib.sha256(SECONDARY_PICKLE.read_bytes()).hexdigest()}",
                    flush=True,
                )
                rc = flash_deploy(project, ctx, 8)
                if rc != 0:
                    raise RuntimeError("secondary standard in-place deploy failed")
                deployed_id = deployed_manifest_id(project)
                if deployed_id != expected_old:
                    raise RuntimeError(
                        f"secondary in-place id mismatch: {deployed_id}"
                    )
                live = await endpoint(client, ctx, expected_old)
                if int(live.get("workersMax") or -1) != 8:
                    raise RuntimeError("secondary endpoint workersMax drifted")
                print(
                    json.dumps(
                        {
                            "operation": "secondary_inplace",
                            "inventory_endpoint_id": expected_old,
                            "deployed_endpoint_id": deployed_id,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

            if args.operation == "canary-configured":
                await run_canary(ctx, ctx.account.endpoint_id)
                return 0

            if args.operation == "census":
                rows = await endpoints(client, ctx)
                safe = [
                    {
                        "id": row.get("id"),
                        "name": row.get("name"),
                        "workersMax": int(row.get("workersMax") or 0),
                        "workersMin": int(row.get("workersMin") or 0),
                    }
                    for row in rows
                ]
                print(
                    json.dumps(
                        {
                            "account": args.account,
                            "configured_endpoint_id": ctx.account.endpoint_id,
                            "workersMax_total": sum(row["workersMax"] for row in safe),
                            "endpoints": sorted(safe, key=lambda row: str(row["id"])),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

            if args.operation == "inspect-new":
                state = load_state(args.account)
                new_id = str(state["new_endpoint_id"])
                response = await client.get(
                    f"/endpoints/{new_id}",
                    headers=ctx.headers(),
                    params={"includeTemplate": "true"},
                )
                response.raise_for_status()
                row = response.json()
                template = row.get("template") or {}
                template_id = str(row.get("templateId") or "")
                if template_id and not template:
                    template_response = await client.get(
                        f"/templates/{template_id}", headers=ctx.headers()
                    )
                    template_response.raise_for_status()
                    template = template_response.json()
                allowed_env = {
                    "_FLASH_SOURCE_FINGERPRINT",
                    "FLASH_APP",
                    "FLASH_ENV",
                    "FLASH_MODULE_PATH",
                    "FLASH_RESOURCE_NAME",
                }
                safe_env = {
                    str(item.get("key")): item.get("value")
                    for item in (template.get("env") or [])
                    if isinstance(item, dict) and item.get("key") in allowed_env
                }
                print(
                    json.dumps(
                        {
                            "operation": "inspect_new",
                            "endpoint_id": row.get("id"),
                            "activeBuildid": row.get("activeBuildid"),
                            "templateId": template_id,
                            "version": row.get("version"),
                            "workersMax": row.get("workersMax"),
                            "template_image": template.get("imageName"),
                            "safe_template_env": safe_env,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

            if args.operation == "bisect-b1":
                state = load_state(args.account)
                endpoint_ids = (
                    str(state["old_endpoint_id"]),
                    str(state["new_endpoint_id"]),
                )
                rows = []
                os.environ["RUNPOD_API_KEY"] = ctx.api_key
                async with RunpodGraphQLClient() as graphql:
                    for endpoint_id in endpoint_ids:
                        row = await endpoint(client, ctx, endpoint_id)
                        template_id = str(row.get("templateId") or "")
                        if not template_id:
                            raise RuntimeError(
                                f"endpoint {endpoint_id} has no templateId"
                            )
                        template = await graphql.get_template(template_id)
                        allowed_env = {
                            "_FLASH_SOURCE_FINGERPRINT",
                            "FLASH_APP",
                            "FLASH_ENV",
                            "FLASH_ENVIRONMENT_ID",
                            "FLASH_MODULE_PATH",
                            "FLASH_RESOURCE_NAME",
                        }
                        safe_env = {
                            str(item.get("key")): item.get("value")
                            for item in (template.get("env") or [])
                            if isinstance(item, dict)
                            and item.get("key") in allowed_env
                        }
                        rows.append(
                            {
                                "endpoint_id": row.get("id"),
                                "template_id": template_id,
                                "image_name": template.get("imageName"),
                                "container_disk_gb": template.get("containerDiskInGb"),
                                "workersMax": row.get("workersMax"),
                                "activeBuildid": row.get("activeBuildid"),
                                "docker_args": template.get("dockerArgs"),
                                "safe_env": safe_env,
                            }
                        )
                print(
                    json.dumps(
                        {"operation": "bisect_b1", "endpoints": rows},
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

            if args.operation == "bisect-b2":
                os.environ["RUNPOD_API_KEY"] = ctx.api_key
                async with RunpodGraphQLClient() as graphql:
                    app_data = await graphql.get_flash_app_by_name(
                        "runpod_flash_extractor"
                    )
                    environments = [
                        item
                        for item in (app_data.get("flashEnvironments") or [])
                        if item.get("name") == "production"
                    ]
                    if len(environments) != 1:
                        raise RuntimeError("expected one production environment")
                    environment = environments[0]
                    active_build_id = str(environment.get("activeBuildId") or "")
                    builds = [
                        item
                        for item in (app_data.get("flashBuilds") or [])
                        if str(item.get("id") or "") == active_build_id
                    ]
                    if len(builds) != 1:
                        raise RuntimeError("active build absent from app build list")
                    manifest = await graphql.get_flash_build(active_build_id)
                    unsupported_build_fields = {}
                    for field in ("state", "status", "imageName", "imageTag"):
                        try:
                            await graphql._execute_graphql(
                                f"""
                                query BuildField($input: String!) {{
                                  flashBuild(flashBuildId: $input) {{ id {field} }}
                                }}
                                """,
                                {"input": active_build_id},
                            )
                        except Exception as exc:
                            unsupported_build_fields[field] = (
                                f"{type(exc).__name__}: {str(exc)[:240]}"
                            )
                print(
                    json.dumps(
                        {
                            "operation": "bisect_b2",
                            "active_build_id": active_build_id,
                            "environment_state": environment.get("state"),
                            "active_build_list_record": builds[0],
                            "unsupported_build_fields": unsupported_build_fields,
                            "manifest_source_fingerprint": (
                                (manifest.get("manifest") or {}).get(
                                    "source_fingerprint"
                                )
                            ),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

            if args.operation == "refresh-new-template":
                if args.account != "primary":
                    raise RuntimeError("template refresh is primary-only")
                state = load_state(args.account)
                new_id = str(state["new_endpoint_id"])
                row = await endpoint(client, ctx, new_id)
                if int(row.get("workersMax") or -1) != 7:
                    raise RuntimeError("new endpoint is not at workersMax=7")
                template_id = str(row.get("templateId") or "")
                if not template_id:
                    raise RuntimeError("new endpoint has no templateId")
                if await configured_endpoint(ctx) != str(state["old_endpoint_id"]):
                    raise RuntimeError("settings no longer point to the old endpoint")
                os.environ["RUNPOD_API_KEY"] = ctx.api_key
                async with RunpodGraphQLClient() as graphql:
                    before = await graphql.get_template(template_id)
                    env_before = list(before.get("env") or [])
                    fingerprints = [
                        str(item.get("value") or "")
                        for item in env_before
                        if isinstance(item, dict)
                        and item.get("key") == "_FLASH_SOURCE_FINGERPRINT"
                    ]
                    if len(fingerprints) != 1 or not fingerprints[0]:
                        raise RuntimeError("new template source fingerprint is absent")
                    payload = {
                        key: before.get(key)
                        for key in (
                            "name",
                            "imageName",
                            "containerDiskInGb",
                            "dockerArgs",
                            "env",
                            "readme",
                        )
                    }
                    payload["id"] = template_id
                    payload["volumeInGb"] = 0
                    updated = await graphql.update_template(payload)
                    after = await graphql.get_template(template_id)
                stable_fields = (
                    "id",
                    "name",
                    "imageName",
                    "containerDiskInGb",
                    "dockerArgs",
                    "readme",
                )
                if any(before.get(key) != after.get(key) for key in stable_fields):
                    raise RuntimeError("template refresh changed stable configuration")
                env_key = lambda item: (str(item.get("key")), str(item.get("value")))
                if sorted(env_before, key=env_key) != sorted(
                    list(after.get("env") or []), key=env_key
                ):
                    raise RuntimeError("template refresh changed environment")
                verified_endpoint = await endpoint(client, ctx, new_id)
                if str(verified_endpoint.get("templateId") or "") != template_id:
                    raise RuntimeError("endpoint template binding changed")
                print(
                    json.dumps(
                        {
                            "operation": "refresh_new_template",
                            "endpoint_id": new_id,
                            "template_id": template_id,
                            "updated_template_id": updated.get("id"),
                            "image_name": after.get("imageName"),
                            "container_disk_gb": after.get("containerDiskInGb"),
                            "source_fingerprint": fingerprints[0],
                            "settings_endpoint_unchanged": True,
                            "template_configuration_unchanged": True,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return 0

        raise RuntimeError(f"unknown operation {args.operation}")
    finally:
        ctx.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=(
            "patch-old-to-one",
            "deploy-fresh-seven",
            "canary-new",
            "settings-swap",
            "delete-old",
            "promote-new-to-eight",
            "secondary-inplace",
            "canary-configured",
            "census",
            "inspect-new",
            "bisect-b1",
            "bisect-b2",
            "refresh-new-template",
        ),
    )
    parser.add_argument("--account", choices=("primary", "secondary"), required=True)
    return asyncio.run(operation(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
