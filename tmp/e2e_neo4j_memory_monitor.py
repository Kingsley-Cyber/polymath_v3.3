"""Measure live Neo4j transaction heap during the isolated item-4 rewrite."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
CAP_MIB = 716.8


def _classify(query: str) -> str:
    if "UNWIND $entity_ids AS entity_id" in query and "graph_degree" in query:
        return "aggregate_refresh_100"
    if "UNWIND $rows AS row" in query:
        return "row_write_100"
    if "DETACH DELETE" in query or "DELETE r" in query:
        return "bounded_delete_or_prune"
    return "other_product_query"


async def main() -> None:
    state = json.loads(STATE.read_text())
    corpus_id = str(state["corpus_id"])
    batch_id = str(state["batch_id"])
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    neo4j = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    maxima: dict[str, int] = {}
    samples: dict[str, int] = {}
    started = time.monotonic()
    try:
        db = mongo[settings.MONGODB_DATABASE]
        async with neo4j.session() as session:
            while time.monotonic() - started < 2400:
                result = await session.run(
                    """
                    SHOW TRANSACTIONS
                    YIELD currentQuery, estimatedUsedHeapMemory
                    WHERE currentQuery IS NOT NULL
                      AND NOT currentQuery STARTS WITH 'SHOW TRANSACTIONS'
                    RETURN currentQuery, estimatedUsedHeapMemory
                    """
                )
                async for row in result:
                    query = str(row.get("currentQuery") or "")
                    memory = int(row.get("estimatedUsedHeapMemory") or 0)
                    kind = _classify(query)
                    maxima[kind] = max(maxima.get(kind, 0), memory)
                    samples[kind] = samples.get(kind, 0) + 1
                item = await db["ingest_batch_items"].find_one(
                    {
                        "batch_id": batch_id,
                        "corpus_id": corpus_id,
                        "ordinal": 3,
                    },
                    {"_id": 0, "status": 1},
                )
                if str((item or {}).get("status") or "") in {
                    "done",
                    "failed",
                    "skipped",
                    "cancelled",
                }:
                    break
                await asyncio.sleep(0.01)
        aggregate_bytes = maxima.get("aggregate_refresh_100", 0)
        aggregate_mib = aggregate_bytes / 1024 / 1024
        receipt = {
            "schema_version": "e2e_neo4j_transaction_memory.v1",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "samples_by_query_family": samples,
            "peak_bytes_by_query_family": maxima,
            "aggregate_refresh_batch_size": 100,
            "aggregate_peak_mib": round(aggregate_mib, 6),
            "transaction_total_cap_mib": CAP_MIB,
            "aggregate_peak_percent_of_cap": round(
                aggregate_mib / CAP_MIB * 100.0 if CAP_MIB else 0.0,
                6,
            ),
            "headroom_mib": round(CAP_MIB - aggregate_mib, 6),
            "secret_values_emitted": 0,
        }
        print(json.dumps(receipt, indent=2, sort_keys=True))
        if samples.get("aggregate_refresh_100", 0) == 0:
            raise RuntimeError("no live 100-ID aggregate transaction was sampled")
    finally:
        await neo4j.close()
        mongo.close()


asyncio.run(main())
