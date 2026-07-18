"""Enable one secondary green worker for the owner ingestion pathway."""

from __future__ import annotations

import asyncio
import json

import httpx

from runpod_e2e_burst_operator import _health, _set_workers_max
from runpod_green_deploy_operator import GREEN_NAME, _matching, _mongo, _state


EXPECTED_ENDPOINT_ID = "8tafde7potcsjw"


async def main() -> None:
    mongo_client, _ = _mongo()
    try:
        from runpod_green_deploy_operator import settings_service

        accounts = await settings_service.get_system_runpod_flash_accounts()
        secondary = [row for row in accounts if row[0].name == "secondary"]
        if len(secondary) != 1:
            raise RuntimeError("secondary RunPod account must resolve exactly once")
        account, key = secondary[0]
        async with httpx.AsyncClient(timeout=60) as http:
            state = await _state(http, key)
            green = _matching(state.get("endpoints") or [], "name", GREEN_NAME)
            if len(green) != 1 or green[0].get("id") != EXPECTED_ENDPOINT_ID:
                raise RuntimeError("secondary green endpoint identity drift")
            before_green_health = await _health(
                http,
                key=key,
                endpoint_id=EXPECTED_ENDPOINT_ID,
                require_idle=True,
            )
            before_blue_health = await _health(
                http,
                key=key,
                endpoint_id=account.endpoint_id,
                require_idle=True,
            )
            blue_scale = await _set_workers_max(
                http,
                key=key,
                endpoint_id=account.endpoint_id,
                workers_max=7,
                expected_before=8,
            )
            try:
                green_scale = await _set_workers_max(
                    http,
                    key=key,
                    endpoint_id=EXPECTED_ENDPOINT_ID,
                    workers_max=1,
                    expected_before=0,
                )
            except Exception:
                await _set_workers_max(
                    http,
                    key=key,
                    endpoint_id=account.endpoint_id,
                    workers_max=8,
                    expected_before=7,
                )
                raise
            after_green_health = await _health(
                http,
                key=key,
                endpoint_id=EXPECTED_ENDPOINT_ID,
                require_idle=True,
            )
            after_blue_health = await _health(
                http,
                key=key,
                endpoint_id=account.endpoint_id,
                require_idle=True,
            )
        print(
            json.dumps(
                {
                    "schema_version": "polymath.owner_ingestion_green_scale.v1",
                    "account": account.name,
                    "endpoint_id": EXPECTED_ENDPOINT_ID,
                    "before_green_health": before_green_health,
                    "before_blue_health": before_blue_health,
                    "blue_scale": blue_scale,
                    "green_scale": green_scale,
                    "after_green_health": after_green_health,
                    "after_blue_health": after_blue_health,
                    "immutable_fields_changed": [],
                    "secrets_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        mongo_client.close()


asyncio.run(main())
