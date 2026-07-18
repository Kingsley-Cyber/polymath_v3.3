"""One strict no-store LocalExtractionV1 canary per owner green endpoint."""

from __future__ import annotations

import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import ExtractionTask
from services.runpod_local_extraction import extract_entities
from services.settings import settings_service


ROUTES = (
    ("primary", "hk81nfl5cnwufx"),
    ("secondary", "8tafde7potcsjw"),
)


async def _one(account_name: str, endpoint_id: str) -> dict[str, object]:
    identity = f"owner-ingestion-{account_name}-green-canary"
    task = ExtractionTask(
        chunk_id=f"child:{identity}",
        doc_id=f"doc:{identity}",
        corpus_id=identity,
        text=(
            "In winter 1911, Maria Okafor did not abandon the lighthouse "
            "restoration project."
        ),
        metadata={"source_version_id": f"srcv:{identity}"},
    )
    report = await extract_entities(
        [task],
        endpoint_id=endpoint_id,
        account_name=account_name,
        return_report=True,
    )
    if report.failures or len(report.results) != 1:
        raise RuntimeError(f"{account_name} green canary result closure failed")
    result = report.results[0]
    if result.provider_card.get("endpoint") != endpoint_id:
        raise RuntimeError(f"{account_name} green endpoint identity drifted")
    if result.provider_card.get("account") != account_name:
        raise RuntimeError(f"{account_name} green account identity drifted")
    if result.schema_version != "polymath.extract.local_extraction.v1":
        raise RuntimeError(f"{account_name} green schema version drifted")
    remote_job = dict(report.metrics["remote_jobs"][0])
    return {
        "account": account_name,
        "endpoint_id": endpoint_id,
        "entities": len(result.entities),
        "claims": len((result.claim_compilation or {}).get("claims") or []),
        "temporal_captures": len(result.temporal_captures or []),
        "relations": len(result.relations),
        "job": {
            "job_id": remote_job.get("job_id"),
            "delay_time_ms": remote_job.get("delay_time_ms"),
            "execution_time_ms": remote_job.get("execution_time_ms"),
        },
        "journal": report.metrics.get("job_journal"),
        "ready": True,
    }


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        results = await asyncio.gather(*(_one(*route) for route in ROUTES))
        print(
            json.dumps(
                {
                    "schema_version": "polymath.owner_ingestion_green_canaries.v1",
                    "results": results,
                    "provider_jobs": len(results),
                    "store_writes": 0,
                    "secrets_emitted": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        client.close()


asyncio.run(main())
