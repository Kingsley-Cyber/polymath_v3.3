"""One no-write live B7 adapter canary; prints only safe counts/identities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from models.identifier_recipes import source_version_id
from services.ghost_b import ExtractionTask
from services.runpod_local_extraction import extract_entities
from services.settings import settings_service


TEXT = (
    "Discounting does not reduce reference prices in winter 1911. "
    "It is recommended during the 2018 drought summer."
)
CORPUS_ID = "b7-no-write-canary"
DOCUMENT_ID = "doc:b7-no-write-canary"
CHILD_ID = "child:b7-no-write-canary"


def _safe_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _durable_counts(database: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for collection in (
        "documents",
        "parent_chunks",
        "ghost_b_extractions",
        "ingest_batches",
        "ingest_batch_items",
    ):
        counts[collection] = await database[collection].count_documents(
            {
                "$or": [
                    {"corpus_id": CORPUS_ID},
                    {"doc_id": DOCUMENT_ID},
                ]
            }
        )
    return counts


async def _settings_fingerprint(database: Any) -> str:
    rows = await database["settings"].find(
        {},
        {
            "_id": 1,
            "user_id": 1,
            "updated_at": 1,
            "ingestion.runpod_flash": 1,
        },
    ).sort("_id", 1).to_list(length=None)
    return _safe_hash(rows)


async def main() -> None:
    green_endpoint = os.environ["GREEN_ENDPOINT_ID"].strip()
    primary_blue = os.environ["PRIMARY_BLUE_ENDPOINT_ID"].strip()
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        settings_service.attach(database)
        accounts = await settings_service.get_system_runpod_flash_accounts()
        matches = [
            account
            for account, key in accounts
            if account.enabled and account.endpoint_id == primary_blue and key
        ]
        if len(matches) != 1:
            raise RuntimeError("primary RunPod account did not resolve exactly once")
        account_name = matches[0].name
        content_hash = "sha256:" + hashlib.sha256(TEXT.encode()).hexdigest()
        source_id = source_version_id(DOCUMENT_ID, content_hash)
        task = ExtractionTask(
            chunk_id=CHILD_ID,
            doc_id=DOCUMENT_ID,
            corpus_id=CORPUS_ID,
            text=TEXT,
            metadata={"source_version_id": source_id},
        )
        before = await _durable_counts(database)
        settings_before = await _settings_fingerprint(database)
        report = await extract_entities(
            [task],
            endpoint_id=green_endpoint,
            account_name=account_name,
            return_report=True,
        )
        after = await _durable_counts(database)
        settings_after = await _settings_fingerprint(database)
        if before != after or any(after.values()):
            raise AssertionError("adapter canary created unexpected durable rows")
        if settings_before != settings_after:
            raise AssertionError("adapter canary changed Settings")
        if report.failures or len(report.results) != 1:
            raise AssertionError("adapter canary result closure failed")
        result = report.results[0]
        local = result.local_extraction or {}
        claims = result.claim_compilation or {}
        if local.get("schema_version") != "local_extraction.v1":
            raise AssertionError("local extraction schema missing")
        if claims.get("schema_version") != "claim_compilation.v1":
            raise AssertionError("claim compilation schema missing")
        if result.relations or (local.get("relations") or []):
            raise AssertionError("rejected relation lane emitted data")
        receipt = {
            "schema_version": "polymath.b7_adapter_live_canary.v1",
            "account_name": account_name,
            "endpoint_id": green_endpoint,
            "wire_contract": report.metrics.get("wire_contract"),
            "chunks": len(report.results),
            "entities": len(local.get("entities") or []),
            "predicates": len(local.get("predicates") or []),
            "claims": len(claims.get("claims") or []),
            "relations": 0,
            "temporal_captures": [
                row.get("text") for row in result.temporal_captures
            ],
            "remote_jobs": report.metrics.get("remote_jobs"),
            "durable_counts_before": before,
            "durable_counts_after": after,
            "settings_unchanged": settings_before == settings_after,
        }
        print(json.dumps(receipt, indent=2, sort_keys=True))
    finally:
        client.close()


asyncio.run(main())
