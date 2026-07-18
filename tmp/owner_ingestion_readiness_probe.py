"""Secret-safe, no-store owner-ingestion readiness probe.

Run inside the backend container. API keys stay behind settings_service and
only public endpoint/template/health fields are emitted.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_a import SummaryTask, summarize_parents
from services.ingestion.summary_provider_pool import resolve_summary_provider_pool
from services.settings import settings_service


GRAPHQL_URL = "https://api.runpod.io/graphql"
RUNPOD_API_BASE = "https://api.runpod.ai/v2"
GREEN_NAME = "polymath-local-extraction-green-3b66f55-deterministic-v1"
EXPECTED_IMAGE = (
    "king2eze/polymath-local-extraction@"
    "sha256:4cb084572687f772cab481adce649cf03c15283368c3541772f85465ee50f896"
)
EXPECTED_ENDPOINTS = {
    "primary": "hk81nfl5cnwufx",
    "secondary": "8tafde7potcsjw",
}
STATE_QUERY = """
query OwnerIngestionReadiness {
  myself {
    endpoints {
      id name templateId workersMin workersMax
    }
    podTemplates {
      id imageName
    }
  }
}
"""


async def _runpod_probe(http: httpx.AsyncClient) -> list[dict[str, object]]:
    accounts = await settings_service.get_system_runpod_flash_accounts()
    resolved = {account.name: (account, key) for account, key in accounts}
    if set(EXPECTED_ENDPOINTS) - set(resolved):
        raise RuntimeError("primary and secondary RunPod accounts must resolve")
    output: list[dict[str, object]] = []
    for name, expected_endpoint_id in EXPECTED_ENDPOINTS.items():
        _, api_key = resolved[name]
        response = await http.post(
            GRAPHQL_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": STATE_QUERY},
        )
        body = response.json()
        if response.status_code >= 400 or body.get("errors"):
            raise RuntimeError(f"RunPod GraphQL probe failed for {name}")
        myself = (body.get("data") or {}).get("myself") or {}
        matches = [
            row
            for row in myself.get("endpoints") or []
            if row.get("name") == GREEN_NAME
        ]
        if len(matches) != 1:
            raise RuntimeError(f"{name} green endpoint cardinality drift")
        endpoint = matches[0]
        if endpoint.get("id") != expected_endpoint_id:
            raise RuntimeError(f"{name} green endpoint identity drift")
        templates = [
            row
            for row in myself.get("podTemplates") or []
            if row.get("id") == endpoint.get("templateId")
        ]
        if len(templates) != 1 or templates[0].get("imageName") != EXPECTED_IMAGE:
            raise RuntimeError(f"{name} immutable green image drift")
        health_response = await http.get(
            f"{RUNPOD_API_BASE}/{expected_endpoint_id}/health",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        health_response.raise_for_status()
        health = health_response.json()
        workers = health.get("workers") if isinstance(health, dict) else None
        jobs = health.get("jobs") if isinstance(health, dict) else None
        if not isinstance(workers, dict) or not isinstance(jobs, dict):
            raise RuntimeError(f"{name} RunPod health shape drift")
        queue_depth = int(jobs.get("inQueue") or 0)
        in_progress = int(jobs.get("inProgress") or 0)
        unhealthy = int(workers.get("unhealthy") or 0)
        if queue_depth or in_progress or unhealthy:
            raise RuntimeError(f"{name} green endpoint is not idle/healthy")
        output.append(
            {
                "account": name,
                "endpoint_id": expected_endpoint_id,
                "template_id": endpoint.get("templateId"),
                "image": templates[0].get("imageName"),
                "workers_min": endpoint.get("workersMin"),
                "workers_max": endpoint.get("workersMax"),
                "workers": workers,
                "jobs": jobs,
                "queue_depth": queue_depth,
                "in_progress": in_progress,
                "authenticated_health": True,
            }
        )
    return output


async def _summary_canary(db, user_id: str) -> dict[str, object]:
    runtime = await settings_service.get_runtime_ingestion_settings(user_id)
    pool, resolution = await resolve_summary_provider_pool(
        configured_refs=runtime.summary.summary_models,
        runtime_refs=runtime.summary.summary_models,
        user_id=user_id,
        db=db,
    )
    if not resolution.get("flash_primary") or not resolution.get(
        "flash_key_available"
    ):
        raise RuntimeError("certified flash summary route is unavailable")
    task = SummaryTask(
        parent_id="owner-ingestion-readiness-canary",
        doc_id="owner-ingestion-readiness-canary",
        corpus_id="owner-ingestion-readiness-canary",
        source_tier="parent",
        text=(
            "A production ingestion readiness check verifies that source text "
            "can be summarized without writing a corpus record. The route must "
            "return a grounded concise summary and preserve its model identity."
        ),
        source_child_ids=[],
        child_boundaries="",
    )
    pool_status: dict[str, object] = {"resolution": resolution}
    results = await summarize_parents(
        [task],
        max_summary_tokens=96,
        pool=pool,
        global_max_concurrent=1,
        pool_status=pool_status,
    )
    if len(results) != 1 or results[0].validation_status != "valid":
        raise RuntimeError("one-call summary canary did not validate")
    return {
        "requested": 1,
        "accepted": 1,
        "model_id": results[0].summary_model,
        "validation_status": results[0].validation_status,
        "flash_primary": resolution.get("flash_primary"),
        "flash_key_available": resolution.get("flash_key_available"),
        "provider_calls": 1,
        "store_writes": 0,
    }


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        users = await db.users.find({}, {"_id": 1}).to_list(length=2)
        if len(users) != 1:
            raise RuntimeError("exactly one owner must resolve")
        user_id = str(users[0]["_id"])
        async with httpx.AsyncClient(timeout=60) as http:
            runpod = await _runpod_probe(http)
        summary = await _summary_canary(db, user_id)
        print(
            json.dumps(
                {
                    "schema_version": "polymath.owner_ingestion_readiness.v1",
                    "runpod_green": runpod,
                    "summary_canary": summary,
                    "secrets_emitted": 0,
                    "corpus_writes": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
