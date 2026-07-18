#!/usr/bin/env python3
"""Fail-closed reversible RunPod endpoint scaling for the 15-doc E2E."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx

from runpod_green_deploy_operator import (
    GREEN_NAME,
    IMAGE_REF,
    SAVE_ENDPOINT,
    _assert_blue,
    _assert_green_endpoint,
    _assert_green_template,
    _deploy_one,
    _graphql,
    _matching,
    _mongo,
    _public_endpoint,
    _public_template,
    _state,
    settings_service,
)


RUNPOD_API_BASE = "https://api.runpod.ai/v2"
PRIOR_MAX = {
    "primary": {"blue": 8, "embed": 1, "green": 1},
    "secondary": {"blue": 8, "embed": 2, "green": 0},
}
BURST_GREEN_MAX = {"primary": 10, "secondary": 10}
ENDPOINT_INPUT_FIELDS = (
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


def _one(items: list[dict[str, Any]], *, key: str, value: str, label: str) -> dict[str, Any]:
    matches = _matching(items, key, value)
    if len(matches) != 1:
        raise RuntimeError(f"{label} must match exactly once; observed={len(matches)}")
    return matches[0]


def _green(state: dict[str, Any], account_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    endpoint = _one(
        state.get("endpoints") or [],
        key="name",
        value=GREEN_NAME,
        label=f"account={account_name} green",
    )
    template = _one(
        state.get("podTemplates") or [],
        key="id",
        value=str(endpoint.get("templateId") or ""),
        label=f"account={account_name} green template",
    )
    if template.get("imageName") != IMAGE_REF:
        raise RuntimeError(f"account={account_name} green image digest drifted")
    registry_id = str(template.get("containerRegistryAuthId") or "")
    if not registry_id:
        raise RuntimeError(f"account={account_name} green registry auth is missing")
    _assert_green_template(template, registry_id)
    return endpoint, template


async def _health(
    http: httpx.AsyncClient,
    *,
    key: str,
    endpoint_id: str,
    require_idle: bool,
) -> dict[str, Any]:
    response = await http.get(
        f"{RUNPOD_API_BASE}/{endpoint_id}/health",
        headers={"Authorization": f"Bearer {key}"},
    )
    response.raise_for_status()
    body = response.json()
    workers = body.get("workers") if isinstance(body, dict) else None
    jobs = body.get("jobs") if isinstance(body, dict) else None
    safe = {
        "endpoint_id": endpoint_id,
        "workers": workers if isinstance(workers, dict) else {},
        "jobs": jobs if isinstance(jobs, dict) else {},
    }
    if require_idle:
        active_jobs = int(safe["jobs"].get("inProgress") or 0) + int(
            safe["jobs"].get("inQueue") or 0
        )
        running_workers = int(safe["workers"].get("running") or 0)
        if active_jobs or running_workers:
            raise RuntimeError(
                f"endpoint={endpoint_id} is not idle: "
                f"active_jobs={active_jobs} running_workers={running_workers}"
            )
    return safe


async def _set_workers_max(
    http: httpx.AsyncClient,
    *,
    key: str,
    endpoint_id: str,
    workers_max: int,
    expected_before: int | None = None,
) -> dict[str, Any]:
    before_state = await _state(http, key)
    before = _one(
        before_state.get("endpoints") or [],
        key="id",
        value=endpoint_id,
        label=f"endpoint={endpoint_id}",
    )
    observed_before = int(before.get("workersMax"))
    if expected_before is not None and observed_before != expected_before:
        raise RuntimeError(
            f"endpoint={endpoint_id} workersMax drifted: "
            f"expected={expected_before} observed={observed_before}"
        )
    if int(before.get("workersMin")) != 0:
        raise RuntimeError(f"endpoint={endpoint_id} workersMin drifted from zero")
    if observed_before == workers_max:
        return {
            "action": "reused",
            "before": _public_endpoint(before),
            "after": _public_endpoint(before),
        }
    desired = dict(before)
    desired["workersMax"] = workers_max
    mutation_input = {field: desired.get(field) for field in ENDPOINT_INPUT_FIELDS}
    data = await _graphql(http, key, SAVE_ENDPOINT, {"input": mutation_input})
    returned = data.get("saveEndpoint") or {}
    if returned.get("id") != endpoint_id:
        raise RuntimeError(f"endpoint={endpoint_id} ID changed during scale")
    after_state = await _state(http, key)
    after = _one(
        after_state.get("endpoints") or [],
        key="id",
        value=endpoint_id,
        label=f"endpoint={endpoint_id} after scale",
    )
    if int(after.get("workersMax")) != workers_max:
        raise RuntimeError(f"endpoint={endpoint_id} did not reach workersMax={workers_max}")
    drift = {
        field: {"before": before.get(field), "after": after.get(field)}
        for field in ENDPOINT_INPUT_FIELDS
        if field != "workersMax" and before.get(field) != after.get(field)
    }
    if drift:
        raise RuntimeError(f"endpoint={endpoint_id} immutable field drift: {drift}")
    return {
        "action": "updated",
        "before": _public_endpoint(before),
        "after": _public_endpoint(after),
    }


async def _topology(
    http: httpx.AsyncClient,
    accounts: list[tuple[Any, str]],
    *,
    require_idle: bool,
) -> list[dict[str, Any]]:
    rows = []
    for account, key in accounts:
        state = await _state(http, key)
        blue = _assert_blue(account, state)
        embed = _one(
            state.get("endpoints") or [],
            key="id",
            value=account.embed_endpoint_id,
            label=f"account={account.name} embed",
        )
        greens = _matching(state.get("endpoints") or [], "name", GREEN_NAME)
        if len(greens) > 1:
            raise RuntimeError(f"account={account.name} has duplicate greens")
        health = []
        for endpoint in [blue, embed, *greens]:
            health.append(
                await _health(
                    http,
                    key=key,
                    endpoint_id=str(endpoint["id"]),
                    require_idle=require_idle,
                )
            )
        green_rows = []
        for endpoint in greens:
            verified, template = _green(state, account.name)
            green_rows.append(
                {
                    "endpoint": _public_endpoint(verified),
                    "template": _public_template(template),
                }
            )
        rows.append(
            {
                "account": account.name,
                "blue": _public_endpoint(blue),
                "embed": _public_endpoint(embed),
                "green": green_rows,
                "health": health,
            }
        )
    return rows


async def _rollback(
    http: httpx.AsyncClient,
    accounts: list[tuple[Any, str]],
) -> list[str]:
    errors: list[str] = []
    for account, key in accounts:
        try:
            state = await _state(http, key)
            greens = _matching(state.get("endpoints") or [], "name", GREEN_NAME)
            if len(greens) == 1:
                await _set_workers_max(
                    http,
                    key=key,
                    endpoint_id=str(greens[0]["id"]),
                    workers_max=PRIOR_MAX[account.name]["green"],
                )
            embed = _one(
                state.get("endpoints") or [],
                key="id",
                value=account.embed_endpoint_id,
                label=f"account={account.name} rollback embed",
            )
            blue = _assert_blue(account, state)
            await _set_workers_max(
                http,
                key=key,
                endpoint_id=str(embed["id"]),
                workers_max=PRIOR_MAX[account.name]["embed"],
            )
            await _set_workers_max(
                http,
                key=key,
                endpoint_id=str(blue["id"]),
                workers_max=PRIOR_MAX[account.name]["blue"],
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"account={account.name}: {type(exc).__name__}: {exc}")
    return errors


async def _scale_up(
    http: httpx.AsyncClient,
    accounts: list[tuple[Any, str]],
) -> dict[str, Any]:
    by_name = {account.name: (account, key) for account, key in accounts}
    if set(by_name) != {"primary", "secondary"}:
        raise RuntimeError("burst scale requires exactly primary and secondary accounts")
    before = await _topology(http, accounts, require_idle=True)
    for row in before:
        expected = PRIOR_MAX[row["account"]]
        if int(row["blue"]["workersMax"]) != expected["blue"]:
            raise RuntimeError(f"account={row['account']} blue prestate drifted")
        if int(row["embed"]["workersMax"]) != expected["embed"]:
            raise RuntimeError(f"account={row['account']} embed prestate drifted")
        green_max = [int(item["endpoint"]["workersMax"]) for item in row["green"]]
        expected_green = [] if row["account"] == "secondary" else [expected["green"]]
        if green_max != expected_green:
            raise RuntimeError(f"account={row['account']} green prestate drifted")
    primary_account, primary_key = by_name["primary"]
    primary_state = await _state(http, primary_key)
    primary_green, primary_template = _green(primary_state, "primary")
    _assert_green_endpoint(primary_green, str(primary_template["id"]))

    operations: list[dict[str, Any]] = []
    try:
        for account, key in accounts:
            state = await _state(http, key)
            blue = _assert_blue(account, state)
            embed = _one(
                state.get("endpoints") or [],
                key="id",
                value=account.embed_endpoint_id,
                label=f"account={account.name} embed",
            )
            operations.append(
                {
                    "account": account.name,
                    "surface": "blue",
                    **await _set_workers_max(
                        http,
                        key=key,
                        endpoint_id=str(blue["id"]),
                        workers_max=0,
                        expected_before=PRIOR_MAX[account.name]["blue"],
                    ),
                }
            )
            operations.append(
                {
                    "account": account.name,
                    "surface": "embed",
                    **await _set_workers_max(
                        http,
                        key=key,
                        endpoint_id=str(embed["id"]),
                        workers_max=0,
                        expected_before=PRIOR_MAX[account.name]["embed"],
                    ),
                }
            )
        secondary_account, secondary_key = by_name["secondary"]
        deployed = await _deploy_one(http, secondary_account, secondary_key)
        operations.append(
            {
                "account": "secondary",
                "surface": "green_deploy",
                **deployed,
            }
        )
        for account, key in accounts:
            state = await _state(http, key)
            green, _ = _green(state, account.name)
            operations.append(
                {
                    "account": account.name,
                    "surface": "green_scale",
                    **await _set_workers_max(
                        http,
                        key=key,
                        endpoint_id=str(green["id"]),
                        workers_max=BURST_GREEN_MAX[account.name],
                        expected_before=1,
                    ),
                }
            )
        after = await _topology(http, accounts, require_idle=False)
        for row in after:
            if int(row["blue"]["workersMax"]) != 0:
                raise RuntimeError(f"account={row['account']} blue is not max 0")
            if int(row["embed"]["workersMax"]) != 0:
                raise RuntimeError(f"account={row['account']} embed is not max 0")
            if len(row["green"]) != 1:
                raise RuntimeError(f"account={row['account']} green count drifted")
            if int(row["green"][0]["endpoint"]["workersMax"]) != BURST_GREEN_MAX[
                row["account"]
            ]:
                raise RuntimeError(f"account={row['account']} green max drifted")
        return {
            "before": before,
            "operations": operations,
            "after": after,
            "total_green_workers_max": sum(BURST_GREEN_MAX.values()),
            "image_ref": IMAGE_REF,
            "rollback_errors": [],
        }
    except Exception as exc:
        rollback_errors = await _rollback(http, accounts)
        raise RuntimeError(
            f"burst scale failed and rollback_errors={rollback_errors}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


async def _restore(
    http: httpx.AsyncClient,
    accounts: list[tuple[Any, str]],
) -> dict[str, Any]:
    before = await _topology(http, accounts, require_idle=True)
    operations: list[dict[str, Any]] = []
    for account, key in accounts:
        state = await _state(http, key)
        green, _ = _green(state, account.name)
        operations.append(
            {
                "account": account.name,
                "surface": "green",
                **await _set_workers_max(
                    http,
                    key=key,
                    endpoint_id=str(green["id"]),
                    workers_max=PRIOR_MAX[account.name]["green"],
                ),
            }
        )
    for account, key in accounts:
        state = await _state(http, key)
        embed = _one(
            state.get("endpoints") or [],
            key="id",
            value=account.embed_endpoint_id,
            label=f"account={account.name} restore embed",
        )
        blue = _assert_blue(account, state)
        for surface, endpoint, target in (
            ("embed", embed, PRIOR_MAX[account.name]["embed"]),
            ("blue", blue, PRIOR_MAX[account.name]["blue"]),
        ):
            operations.append(
                {
                    "account": account.name,
                    "surface": surface,
                    **await _set_workers_max(
                        http,
                        key=key,
                        endpoint_id=str(endpoint["id"]),
                        workers_max=target,
                    ),
                }
            )
    after = await _topology(http, accounts, require_idle=False)
    for row in after:
        expected = PRIOR_MAX[row["account"]]
        if int(row["blue"]["workersMax"]) != expected["blue"]:
            raise RuntimeError(f"account={row['account']} blue restore failed")
        if int(row["embed"]["workersMax"]) != expected["embed"]:
            raise RuntimeError(f"account={row['account']} embed restore failed")
        if int(row["green"][0]["endpoint"]["workersMax"]) != expected["green"]:
            raise RuntimeError(f"account={row['account']} green restore failed")
    return {"before": before, "operations": operations, "after": after}


async def run(action: str) -> dict[str, Any]:
    mongo_client, _ = _mongo()
    try:
        accounts = await settings_service.get_system_runpod_flash_accounts()
        async with httpx.AsyncClient(timeout=45) as http:
            if action == "scale-up":
                result = await _scale_up(http, accounts)
            elif action == "restore":
                result = await _restore(http, accounts)
            else:
                result = {
                    "topology": await _topology(
                        http,
                        accounts,
                        require_idle=action == "preflight",
                    )
                }
        return {
            "action": action,
            "result": result,
            "secret_values_emitted": 0,
        }
    finally:
        mongo_client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=("preflight", "census", "scale-up", "restore"),
        required=True,
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.action)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
