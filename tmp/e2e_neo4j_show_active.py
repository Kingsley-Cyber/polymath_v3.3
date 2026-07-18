import asyncio
import json

from neo4j import AsyncGraphDatabase

from config import get_settings


def classify(query):
    if "UNWIND $entity_ids AS entity_id" in query and "graph_degree" in query:
        return "aggregate_refresh_100"
    if "UNWIND $rows AS row" in query:
        return "row_write_100"
    if "NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }" in query:
        return "orphan_delete_100"
    if "DETACH DELETE" in query or "DELETE r" in query:
        return "bounded_delete_or_prune"
    return "other"


async def main():
    settings = get_settings()
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                """
                SHOW TRANSACTIONS
                YIELD currentQuery, estimatedUsedHeapMemory, elapsedTime, status
                WHERE currentQuery IS NOT NULL
                  AND NOT currentQuery STARTS WITH 'SHOW TRANSACTIONS'
                RETURN currentQuery, estimatedUsedHeapMemory, elapsedTime, status
                """
            )
            rows = []
            async for row in result:
                rows.append(
                    {
                        "family": classify(str(row.get("currentQuery") or "")),
                        "estimated_heap_bytes": int(row.get("estimatedUsedHeapMemory") or 0),
                        "elapsed": str(row.get("elapsedTime") or ""),
                        "status": row.get("status"),
                    }
                )
            print(json.dumps(rows, indent=2, sort_keys=True))
    finally:
        await driver.close()


asyncio.run(main())
