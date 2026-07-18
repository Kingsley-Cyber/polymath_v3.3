"""One strict, no-store LocalExtractionV1 canary for the secondary green."""

from __future__ import annotations

import argparse
import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import ExtractionTask
from services.runpod_local_extraction import extract_entities
from services.settings import settings_service


async def run(endpoint_id: str) -> dict:
    settings = get_settings()
    mongo_client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = mongo_client.get_default_database()
    except Exception:  # noqa: BLE001
        database = mongo_client[settings.MONGODB_DATABASE]
    settings_service.attach(database)
    task = ExtractionTask(
        chunk_id="child:runpod-e2e-secondary-canary",
        doc_id="doc:runpod-e2e-secondary-canary",
        corpus_id="runpod-e2e-secondary-canary-20260715",
        text=(
            "In winter 1911, Maria Okafor did not abandon the lighthouse "
            "restoration project."
        ),
        metadata={"source_version_id": "srcv:runpod-e2e-secondary-canary"},
    )
    try:
        report = await extract_entities(
            [task],
            endpoint_id=endpoint_id,
            account_name="secondary",
            return_report=True,
        )
    finally:
        mongo_client.close()
    if report.failures or len(report.results) != 1:
        raise RuntimeError("secondary canary result closure failed")
    result = report.results[0]
    if result.provider_card.get("endpoint") != endpoint_id:
        raise RuntimeError("secondary canary endpoint identity drifted")
    if result.provider_card.get("account") != "secondary":
        raise RuntimeError("secondary canary account identity drifted")
    if result.relations:
        raise RuntimeError("secondary canary relation disposition drifted")
    if result.schema_version != "polymath.extract.local_extraction.v1":
        raise RuntimeError("secondary canary schema version drifted")
    remote_job = dict(report.metrics["remote_jobs"][0])
    return {
        "all_green": True,
        "account": "secondary",
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
        "job_journal": report.metrics.get("job_journal"),
        "secret_values_emitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-id", required=True)
    args = parser.parse_args()
    if not args.endpoint_id.isalnum():
        raise RuntimeError("secondary endpoint id is malformed")
    print(json.dumps(asyncio.run(run(args.endpoint_id)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
