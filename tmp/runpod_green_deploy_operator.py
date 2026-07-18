#!/usr/bin/env python3
"""Fail-closed operator helper for the 2026-07-15 private green deployment.

This file is operational scratch and must never be staged. Run inside the
backend container so RunPod keys remain inside the encrypted-settings boundary.
Docker Hub credentials are accepted only as JSON on stdin for registry-auth.
No secret value is returned or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.settings import settings_service


GRAPHQL_URL = "https://api.runpod.io/graphql"
REGISTRY_AUTH_NAME = "polymath-dockerhub-private-20260715"
IMAGE_REF = (
    "king2eze/polymath-local-extraction@"
    "sha256:4cb084572687f772cab481adce649cf03c15283368c3541772f85465ee50f896"
)
GREEN_NAME = "polymath-local-extraction-green-3b66f55-deterministic-v1"
GPU_IDS = "ADA_24,AMPERE_24,-NVIDIA GeForce RTX 3090"

STATE_QUERY = """
query State {
  myself {
    endpoints {
      id name templateId gpuIds gpuCount idleTimeout scalerType scalerValue
      workersMin workersMax executionTimeoutMs allowedCudaVersions
      minCudaVersion flashBootType
    }
    containerRegistryCreds { id name }
    podTemplates {
      id name imageName containerDiskInGb dockerArgs
      containerRegistryAuthId isServerless env { key }
    }
  }
}
"""

SAVE_TEMPLATE = """
mutation SaveTemplate($input: SaveTemplateInput) {
  saveTemplate(input: $input) {
    id name imageName containerDiskInGb dockerArgs
    containerRegistryAuthId isServerless env { key }
  }
}
"""

SAVE_ENDPOINT = """
mutation SaveEndpoint($input: EndpointInput!) {
  saveEndpoint(input: $input) {
    id name templateId gpuIds gpuCount idleTimeout scalerType scalerValue
    workersMin workersMax executionTimeoutMs allowedCudaVersions
    minCudaVersion flashBootType
  }
}
"""

DELETE_ENDPOINT = """
mutation DeleteEndpoint($id: String!) {
  deleteEndpoint(id: $id)
}
"""

EMBED_ENDPOINT_NAME = "polymath-embed-qwen3"


def _mongo() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:  # noqa: BLE001
        db = client[settings.MONGODB_DATABASE]
    settings_service.attach(db)
    return client, db


async def _graphql(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        GRAPHQL_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"query": query, "variables": variables or {}},
    )
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"RunPod GraphQL returned non-JSON status={response.status_code}"
        ) from exc
    errors = body.get("errors") or []
    if response.status_code >= 400 or errors:
        messages = [str(item.get("message") or "")[:300] for item in errors]
        raise RuntimeError(
            f"RunPod GraphQL rejected status={response.status_code} errors={messages}"
        )
    return body.get("data") or {}


def _matching(items: list[dict[str, Any]], key: str, value: str) -> list[dict[str, Any]]:
    return [item for item in items if str(item.get(key) or "") == value]


def _public_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    return {key: endpoint.get(key) for key in sorted(endpoint)}


def _public_template(template: dict[str, Any]) -> dict[str, Any]:
    return {
        key: template.get(key)
        for key in (
            "id",
            "name",
            "imageName",
            "containerDiskInGb",
            "dockerArgs",
            "containerRegistryAuthId",
            "isServerless",
            "env",
        )
    }


def _assert_blue(account: Any, state: dict[str, Any]) -> dict[str, Any]:
    blue = _matching(state.get("endpoints") or [], "id", account.endpoint_id)
    if len(blue) != 1:
        raise RuntimeError(
            f"account={account.name} configured blue id must match exactly once"
        )
    return blue[0]


def _assert_registry_auth(state: dict[str, Any]) -> dict[str, Any]:
    matches = _matching(
        state.get("containerRegistryCreds") or [], "name", REGISTRY_AUTH_NAME
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"registry auth {REGISTRY_AUTH_NAME!r} must match exactly once; "
            f"observed={len(matches)}"
        )
    return matches[0]


def _assert_green_template(template: dict[str, Any], registry_auth_id: str) -> None:
    expected = {
        "imageName": IMAGE_REF,
        "containerDiskInGb": 64,
        "dockerArgs": "",
        "containerRegistryAuthId": registry_auth_id,
        "isServerless": True,
    }
    mismatches = {
        key: {"expected": value, "observed": template.get(key)}
        for key, value in expected.items()
        if template.get(key) != value
    }
    env_names = {str(item.get("key") or "") for item in template.get("env") or []}
    for required in ("POLYMATH_LOCAL_FILES_ONLY", "TOKENIZERS_PARALLELISM"):
        if required not in env_names:
            mismatches[f"env.{required}"] = {"expected": "present", "observed": "absent"}
    if mismatches:
        raise RuntimeError(f"green template drift: {mismatches}")


def _assert_green_endpoint(endpoint: dict[str, Any], template_id: str) -> None:
    expected = {
        "templateId": template_id,
        "gpuIds": GPU_IDS,
        "gpuCount": 1,
        "idleTimeout": 60,
        "scalerType": "REQUEST_COUNT",
        "scalerValue": 1,
        "workersMin": 0,
        "workersMax": 1,
        "executionTimeoutMs": 1800000,
        "minCudaVersion": "13.0",
        "flashBootType": "FLASHBOOT",
    }
    mismatches = {
        key: {"expected": value, "observed": endpoint.get(key)}
        for key, value in expected.items()
        if endpoint.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"green endpoint drift: {mismatches}")


async def _state(http: httpx.AsyncClient, key: str) -> dict[str, Any]:
    return (await _graphql(http, key, STATE_QUERY)).get("myself") or {}


async def _registry_auth(
    http: httpx.AsyncClient,
    accounts: list[tuple[Any, str]],
    credential: dict[str, Any],
) -> list[dict[str, Any]]:
    username = str(credential.get("Username") or "")
    password = str(credential.get("Secret") or "")
    if username != "king2eze" or not password:
        raise RuntimeError("Docker credential helper did not return the authorized namespace")
    results = []
    for account, key in accounts:
        state = await _state(http, key)
        _assert_blue(account, state)
        existing = _matching(
            state.get("containerRegistryCreds") or [], "name", REGISTRY_AUTH_NAME
        )
        if len(existing) > 1:
            raise RuntimeError(f"account={account.name} has duplicate registry auths")
        if existing:
            auth = existing[0]
            action = "reused"
        else:
            # RunPod's public Python SDK emits this mutation inline and does
            # not expose the registry input type. JSON string literals are
            # GraphQL-compatible and keep token punctuation escaped safely.
            mutation = (
                "mutation SaveRegistryAuth { saveRegistryAuth(input: {"
                f"name: {json.dumps(REGISTRY_AUTH_NAME)}, "
                f"username: {json.dumps(username)}, "
                f"password: {json.dumps(password)}"
                "}) { id name } }"
            )
            data = await _graphql(
                http,
                key,
                mutation,
            )
            auth = data.get("saveRegistryAuth") or {}
            action = "created"
        verified = _assert_registry_auth(await _state(http, key))
        if verified.get("id") != auth.get("id"):
            raise RuntimeError(f"account={account.name} registry auth verification drift")
        results.append(
            {
                "account": account.name,
                "blue_id": account.endpoint_id,
                "registry_auth": {"id": verified.get("id"), "name": verified.get("name")},
                "action": action,
            }
        )
    return results


async def _deploy_one(
    http: httpx.AsyncClient, account: Any, key: str
) -> dict[str, Any]:
    state = await _state(http, key)
    blue_before = _assert_blue(account, state)
    registry_auth = _assert_registry_auth(state)
    green_matches = _matching(state.get("endpoints") or [], "name", GREEN_NAME)
    if len(green_matches) > 1:
        raise RuntimeError(f"account={account.name} has duplicate green endpoints")
    template_name = f"{GREEN_NAME}-{account.name}"
    templates = _matching(state.get("podTemplates") or [], "name", template_name)
    if len(templates) > 1:
        raise RuntimeError(f"account={account.name} has duplicate green templates")

    if green_matches:
        green = green_matches[0]
        template = next(
            (
                item
                for item in state.get("podTemplates") or []
                if item.get("id") == green.get("templateId")
            ),
            None,
        )
        if template is None:
            raise RuntimeError(f"account={account.name} green template not visible")
        action = "reused"
    else:
        if templates:
            template = templates[0]
        else:
            data = await _graphql(
                http,
                key,
                SAVE_TEMPLATE,
                {
                    "input": {
                        "name": template_name,
                        "imageName": IMAGE_REF,
                        "containerDiskInGb": 64,
                        "dockerArgs": "",
                        "volumeInGb": 0,
                        "ports": "",
                        "env": [
                            {"key": "POLYMATH_LOCAL_FILES_ONLY", "value": "1"},
                            {"key": "TOKENIZERS_PARALLELISM", "value": "false"},
                        ],
                        "isServerless": True,
                        "containerRegistryAuthId": registry_auth["id"],
                        "startSsh": False,
                        "isPublic": False,
                        "readme": (
                            "Polymath private LocalExtractionV1 deterministic green "
                            "3b66f55"
                        ),
                    }
                },
            )
            template = data.get("saveTemplate") or {}
        _assert_green_template(template, str(registry_auth["id"]))
        data = await _graphql(
            http,
            key,
            SAVE_ENDPOINT,
            {
                "input": {
                    "name": GREEN_NAME,
                    "templateId": template["id"],
                    "gpuIds": GPU_IDS,
                    "gpuCount": 1,
                    "idleTimeout": 60,
                    "scalerType": "REQUEST_COUNT",
                    "scalerValue": 1,
                    "workersMin": 0,
                    "workersMax": 1,
                    "executionTimeoutMs": 1800000,
                    "allowedCudaVersions": "",
                    "minCudaVersion": "13.0",
                    "flashBootType": "FLASHBOOT",
                    "locations": "",
                    "networkVolumeId": "",
                }
            },
        )
        green = data.get("saveEndpoint") or {}
        action = "created"

    final = await _state(http, key)
    blue_after = _assert_blue(account, final)
    if blue_after != blue_before:
        raise RuntimeError(f"account={account.name} blue endpoint changed during green deploy")
    verified_green = _matching(final.get("endpoints") or [], "id", str(green.get("id") or ""))
    if len(verified_green) != 1:
        raise RuntimeError(f"account={account.name} green endpoint not uniquely visible")
    verified_template = next(
        (
            item
            for item in final.get("podTemplates") or []
            if item.get("id") == verified_green[0].get("templateId")
        ),
        None,
    )
    if verified_template is None:
        raise RuntimeError(f"account={account.name} verified template not visible")
    _assert_green_template(verified_template, str(registry_auth["id"]))
    _assert_green_endpoint(verified_green[0], str(verified_template["id"]))
    return {
        "account": account.name,
        "action": action,
        "blue_unchanged": True,
        "blue": _public_endpoint(blue_after),
        "green": _public_endpoint(verified_green[0]),
        "template": _public_template(verified_template),
        "registry_auth": {"id": registry_auth.get("id"), "name": registry_auth.get("name")},
    }


async def _census(
    http: httpx.AsyncClient, accounts: list[tuple[Any, str]]
) -> list[dict[str, Any]]:
    results = []
    for account, key in accounts:
        state = await _state(http, key)
        blue = _assert_blue(account, state)
        greens = _matching(state.get("endpoints") or [], "name", GREEN_NAME)
        results.append(
            {
                "account": account.name,
                "blue": _public_endpoint(blue),
                "green": [_public_endpoint(item) for item in greens],
                "endpoint_allocations": [
                    _public_endpoint(item)
                    for item in state.get("endpoints") or []
                ],
                "green_templates": [
                    _public_template(item)
                    for item in state.get("podTemplates") or []
                    if str(item.get("name") or "").startswith(GREEN_NAME)
                ],
                "registry_auth_matches": [
                    {"id": item.get("id"), "name": item.get("name")}
                    for item in _matching(
                        state.get("containerRegistryCreds") or [],
                        "name",
                        REGISTRY_AUTH_NAME,
                    )
                ],
            }
        )
    return results


async def _embed_capacity(
    http: httpx.AsyncClient,
    accounts: list[tuple[Any, str]],
    workers_max: int,
) -> list[dict[str, Any]]:
    if len(accounts) != 1 or accounts[0][0].name != "primary":
        raise RuntimeError("embed capacity mutation is authorized for primary only")
    if workers_max not in (1, 2):
        raise RuntimeError("embed workers max must be the authorized value 1 or 2")
    account, key = accounts[0]
    before_state = await _state(http, key)
    blue_before = _assert_blue(account, before_state)
    embeds = _matching(before_state.get("endpoints") or [], "name", EMBED_ENDPOINT_NAME)
    if len(embeds) != 1:
        raise RuntimeError("primary embed endpoint must match exactly once")
    before = embeds[0]
    if before.get("workersMin") != 0:
        raise RuntimeError("primary embed workersMin drifted from zero")
    if before.get("workersMax") not in (1, 2):
        raise RuntimeError("primary embed workersMax is outside the sealed 1/2 state")
    if before.get("workersMax") == workers_max:
        desired = dict(before)
        desired["idleTimeout"] = 60
        desired["scalerValue"] = 1
        if before == desired:
            action = "reused"
        else:
            action = "repairing-default-drift"
    else:
        action = "updated"
        desired = dict(before)
        desired["workersMax"] = workers_max
        desired["idleTimeout"] = 60
        desired["scalerValue"] = 1
    if action != "reused":
        input_fields = {
            key_name: desired.get(key_name)
            for key_name in (
                "id",
                "name",
                "templateId",
                "gpuIds",
                "gpuCount",
                "idleTimeout",
                "scalerType",
                "scalerValue",
                "workersMin",
                "workersMax",
                "executionTimeoutMs",
                "allowedCudaVersions",
                "minCudaVersion",
                "flashBootType",
            )
        }
        data = await _graphql(
            http,
            key,
            SAVE_ENDPOINT,
            {"input": input_fields},
        )
        returned = data.get("saveEndpoint") or {}
        if returned.get("id") != before.get("id"):
            raise RuntimeError("embed endpoint ID changed during capacity update")
    after_state = await _state(http, key)
    blue_after = _assert_blue(account, after_state)
    if blue_after != blue_before:
        raise RuntimeError("blue extraction changed during embed capacity update")
    after_matches = _matching(
        after_state.get("endpoints") or [], "id", str(before.get("id") or "")
    )
    if len(after_matches) != 1:
        raise RuntimeError("embed endpoint not uniquely visible after capacity update")
    after = after_matches[0]
    if after.get("workersMax") != workers_max:
        raise RuntimeError("embed workersMax did not reach requested value")
    drift = {
        key_name: {"expected": desired.get(key_name), "observed": after.get(key_name)}
        for key_name in sorted(desired)
        if desired.get(key_name) != after.get(key_name)
    }
    if drift:
        raise RuntimeError(f"embed capacity update changed immutable fields: {drift}")
    return [
        {
            "account": account.name,
            "action": action,
            "before": _public_endpoint(before),
            "after": _public_endpoint(after),
            "blue_unchanged": True,
        }
    ]


async def _abort_green(
    http: httpx.AsyncClient, accounts: list[tuple[Any, str]]
) -> list[dict[str, Any]]:
    if len(accounts) != 1 or accounts[0][0].name != "primary":
        raise RuntimeError("green abort is authorized for primary only")
    account, key = accounts[0]
    before_state = await _state(http, key)
    blue_before = _assert_blue(account, before_state)
    greens = _matching(before_state.get("endpoints") or [], "name", GREEN_NAME)
    if len(greens) > 1:
        raise RuntimeError("primary green endpoint is duplicated")
    if greens:
        green = greens[0]
        data = await _graphql(
            http,
            key,
            DELETE_ENDPOINT,
            {"id": green["id"]},
        )
        if "deleteEndpoint" not in data:
            raise RuntimeError("green endpoint delete returned no mutation field")
        action = "deleted"
    else:
        green = None
        action = "already_absent"
    after_state = await _state(http, key)
    blue_after = _assert_blue(account, after_state)
    if blue_after != blue_before:
        raise RuntimeError("blue extraction changed during green abort")
    remaining = _matching(after_state.get("endpoints") or [], "name", GREEN_NAME)
    if remaining:
        raise RuntimeError("green endpoint remains after abort")
    return [
        {
            "account": account.name,
            "action": action,
            "deleted_green_id": green and green.get("id"),
            "green_remaining": 0,
            "blue_unchanged": True,
            "blue": _public_endpoint(blue_after),
        }
    ]


async def run(args: argparse.Namespace) -> dict[str, Any]:
    mongo_client, _ = _mongo()
    try:
        accounts = await settings_service.get_system_runpod_flash_accounts()
        if args.account:
            accounts = [item for item in accounts if item[0].name == args.account]
            if len(accounts) != 1:
                raise RuntimeError(f"account {args.account!r} not uniquely configured")
        async with httpx.AsyncClient(timeout=45) as http:
            if args.action == "registry-auth":
                credential = json.load(sys.stdin)
                rows = await _registry_auth(http, accounts, credential)
            elif args.action == "deploy":
                rows = [await _deploy_one(http, account, key) for account, key in accounts]
            elif args.action == "embed-capacity":
                rows = await _embed_capacity(http, accounts, args.embed_workers_max)
            elif args.action == "abort-green":
                rows = await _abort_green(http, accounts)
            else:
                rows = await _census(http, accounts)
        return {
            "action": args.action,
            "image_ref": IMAGE_REF,
            "green_name": GREEN_NAME,
            "rows": rows,
            "secret_values_emitted": 0,
        }
    finally:
        mongo_client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=(
            "registry-auth",
            "deploy",
            "census",
            "embed-capacity",
            "abort-green",
        ),
        required=True,
    )
    parser.add_argument("--account", choices=("primary", "secondary"))
    parser.add_argument("--embed-workers-max", type=int, choices=(1, 2))
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), indent=2, sort_keys=True))
