#!/usr/bin/env python3
"""Read-only identity/index census for the isolation RED."""

from __future__ import annotations

import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings


async def main() -> None:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = mongo[settings.MONGODB_DATABASE]
        mongo_indexes = {}
        for name in ("documents", "chunks", "parents", "summary_tree"):
            indexes = await db[name].list_indexes().to_list(length=None)
            mongo_indexes[name] = [
                {
                    "name": row.get("name"),
                    "key": dict(row.get("key") or {}),
                    "unique": bool(row.get("unique", False)),
                    "partialFilterExpression": row.get("partialFilterExpression"),
                }
                for row in indexes
            ]
        async with driver.session() as session:
            constraints_result = await session.run(
                "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties "
                "RETURN name, type, labelsOrTypes, properties ORDER BY name"
            )
            constraints = [dict(row) async for row in constraints_result]
            indexes_result = await session.run(
                "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, state "
                "WHERE any(label IN labelsOrTypes WHERE label IN ['Document', 'Chunk']) "
                "RETURN name, type, entityType, labelsOrTypes, properties, state ORDER BY name"
            )
            indexes = [dict(row) async for row in indexes_result]
        print(
            json.dumps(
                {
                    "schema_version": "runpod_e2e_identity_index_census.v1",
                    "mongo_indexes": mongo_indexes,
                    "neo4j_constraints": constraints,
                    "neo4j_document_chunk_indexes": indexes,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    finally:
        await driver.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
