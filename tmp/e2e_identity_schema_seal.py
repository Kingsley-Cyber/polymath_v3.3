"""Read-only live schema seal for corpus-qualified derived identities."""

from __future__ import annotations

import asyncio
import json

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def main() -> None:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    neo4j = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        database = mongo[settings.MONGODB_DATABASE]
        index_info = await database["summary_tree"].index_information()
        mongo_indexes = {
            name: {
                "keys": [list(value) for value in info.get("key", [])],
                "unique": bool(info.get("unique")),
            }
            for name, info in index_info.items()
        }
        compound = [
            name
            for name, info in index_info.items()
            if list(info.get("key", [])) == [("corpus_id", 1), ("node_id", 1)]
            and bool(info.get("unique"))
        ]
        legacy_unique = [
            name
            for name, info in index_info.items()
            if list(info.get("key", [])) == [("node_id", 1)]
            and bool(info.get("unique"))
        ]
        require(
            len(compound) == 1, f"summary_tree compound uniqueness drifted: {compound}"
        )
        require(
            not legacy_unique,
            f"legacy summary_tree uniqueness remains: {legacy_unique}",
        )

        async with neo4j.session() as session:
            rows = await (
                await session.run(
                    "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties "
                    "RETURN name, type, labelsOrTypes, properties ORDER BY name"
                )
            ).data()
            constraints = [dict(row) for row in rows]
            for label, content_property in (
                ("Document", "doc_id"),
                ("Chunk", "chunk_id"),
                ("Fact", "fact_id"),
            ):
                matching = [
                    row
                    for row in constraints
                    if row.get("labelsOrTypes") == [label]
                    and row.get("properties") == ["corpus_id", content_property]
                    and "UNIQUENESS" in str(row.get("type") or "")
                ]
                legacy = [
                    row
                    for row in constraints
                    if row.get("labelsOrTypes") == [label]
                    and row.get("properties") == [content_property]
                    and "UNIQUENESS" in str(row.get("type") or "")
                ]
                require(len(matching) == 1, f"{label} composite constraint drifted")
                require(not legacy, f"{label} legacy global constraint remains")

            duplicate_groups: dict[str, int] = {}
            for label, content_property in (
                ("Document", "doc_id"),
                ("Chunk", "chunk_id"),
                ("Fact", "fact_id"),
            ):
                result = await session.run(
                    f"MATCH (n:{label}) "
                    f"WITH n.corpus_id AS corpus_id, n.{content_property} AS content_id, count(*) AS copies "
                    "WHERE corpus_id IS NULL OR content_id IS NULL OR copies <> 1 "
                    "RETURN count(*) AS bad_groups"
                )
                row = await result.single()
                duplicate_groups[label] = int(row["bad_groups"] if row else -1)
            require(
                all(value == 0 for value in duplicate_groups.values()),
                f"derived identity groups invalid: {duplicate_groups}",
            )

        print(
            json.dumps(
                {
                    "schema_version": "e2e_identity_schema_seal.v1",
                    "read_only": True,
                    "summary_tree_indexes": mongo_indexes,
                    "neo4j_constraints": constraints,
                    "invalid_composite_identity_groups": duplicate_groups,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await neo4j.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
