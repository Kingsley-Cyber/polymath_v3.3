#!/usr/bin/env python3
"""Read-only census of cross-corpus content-ID reuse in protected corpora."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings


BASELINE = Path(
    "/data/ingest-files/runpod-job-journals/e2e-protected-existing-baseline.json"
)
OUTPUT = Path("/tmp/e2e_protected_identity_collision_census.json")


def stable_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def atomic_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True, default=str).encode() + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


async def grouped_collisions(
    collection: Any,
    *,
    protected_ids: list[str],
    identity_field: str,
    extra_match: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"corpus_id": {"$in": protected_ids}}
    if extra_match:
        match.update(extra_match)
    rows = await collection.aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": f"${identity_field}",
                    "corpus_ids": {"$addToSet": "$corpus_id"},
                    "row_count": {"$sum": 1},
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$corpus_ids"}, 1]}}},
            {"$sort": {"_id": 1}},
        ],
        allowDiskUse=True,
    ).to_list(length=None)
    return [
        {
            identity_field: str(row.get("_id") or ""),
            "corpus_ids": sorted(str(value) for value in row.get("corpus_ids") or []),
            "row_count": int(row.get("row_count") or 0),
        }
        for row in rows
        if row.get("_id")
    ]


async def main() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    protected_ids = sorted(baseline["frozen_existing_corpus_ids"])
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = mongo[settings.MONGODB_DATABASE]
        document_collisions = await grouped_collisions(
            db["documents"],
            protected_ids=protected_ids,
            identity_field="doc_id",
        )
        collided_doc_ids = [row["doc_id"] for row in document_collisions]
        document_rows = await db["documents"].find(
            {
                "corpus_id": {"$in": protected_ids},
                "doc_id": {"$in": collided_doc_ids},
            },
            {
                "_id": 0,
                "corpus_id": 1,
                "doc_id": 1,
                "filename": 1,
                "ingest_stage": 1,
                "write_state.verified": 1,
                "source_identity.content_sha256": 1,
            },
        ).sort([("doc_id", 1), ("corpus_id", 1)]).to_list(length=None)

        chunk_collisions = await grouped_collisions(
            db["chunks"],
            protected_ids=protected_ids,
            identity_field="chunk_id",
            extra_match={"doc_id": {"$in": collided_doc_ids}},
        )
        tree_collisions = await grouped_collisions(
            db["summary_tree"],
            protected_ids=protected_ids,
            identity_field="node_id",
            extra_match={"doc_id": {"$in": collided_doc_ids}},
        )

        mongo_instance_counts: list[dict[str, Any]] = []
        for collection_name in ("chunks", "parent_chunks", "summary_tree"):
            rows = await db[collection_name].aggregate(
                [
                    {
                        "$match": {
                            "corpus_id": {"$in": protected_ids},
                            "doc_id": {"$in": collided_doc_ids},
                        }
                    },
                    {
                        "$group": {
                            "_id": {
                                "corpus_id": "$corpus_id",
                                "doc_id": "$doc_id",
                            },
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"_id.doc_id": 1, "_id.corpus_id": 1}},
                ],
                allowDiskUse=True,
            ).to_list(length=None)
            for row in rows:
                mongo_instance_counts.append(
                    {
                        "collection": collection_name,
                        "corpus_id": str(row["_id"]["corpus_id"]),
                        "doc_id": str(row["_id"]["doc_id"]),
                        "count": int(row.get("count") or 0),
                    }
                )

        async with driver.session() as session:
            nodes_result = await session.run(
                """
                MATCH (n)
                WHERE n.doc_id IN $doc_ids AND (n:Document OR n:Chunk OR n:Fact)
                RETURN n.doc_id AS doc_id, n.corpus_id AS corpus_id,
                       labels(n) AS labels, count(n) AS count
                ORDER BY doc_id, corpus_id, labels
                """,
                doc_ids=collided_doc_ids,
            )
            neo4j_nodes = [dict(row) async for row in nodes_result]
            relationships_result = await session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE a.doc_id IN $doc_ids OR b.doc_id IN $doc_ids OR r.doc_id IN $doc_ids
                RETURN type(r) AS type,
                       r.corpus_id AS relationship_corpus_id,
                       a.corpus_id AS start_corpus_id,
                       b.corpus_id AS end_corpus_id,
                       count(r) AS count
                ORDER BY type, relationship_corpus_id, start_corpus_id, end_corpus_id
                """,
                doc_ids=collided_doc_ids,
            )
            neo4j_relationships = [dict(row) async for row in relationships_result]

        pair_counts = Counter()
        for row in document_collisions:
            corpora = row["corpus_ids"]
            for left_index, left in enumerate(corpora):
                for right in corpora[left_index + 1 :]:
                    pair_counts[(left, right)] += 1

        result: dict[str, Any] = {
            "schema_version": "runpod_e2e_protected_identity_collision_census.v1",
            "protected_corpus_ids": protected_ids,
            "document_collision_group_count": len(document_collisions),
            "document_collision_instance_count": sum(
                len(row["corpus_ids"]) for row in document_collisions
            ),
            "document_collisions": document_collisions,
            "document_rows": document_rows,
            "corpus_pair_collision_counts": [
                {"left": left, "right": right, "doc_count": count}
                for (left, right), count in sorted(pair_counts.items())
            ],
            "chunk_collision_group_count": len(chunk_collisions),
            "chunk_collisions": chunk_collisions,
            "summary_tree_collision_group_count": len(tree_collisions),
            "summary_tree_collisions": tree_collisions,
            "mongo_instance_counts": mongo_instance_counts,
            "neo4j_nodes_for_collided_doc_ids": neo4j_nodes,
            "neo4j_relationships_for_collided_doc_ids": neo4j_relationships,
        }
        result["content_sha256"] = hashlib.sha256(stable_bytes(result)).hexdigest()
        atomic_write(OUTPUT, result)
        print(
            json.dumps(
                {
                    "output": str(OUTPUT),
                    "content_sha256": result["content_sha256"],
                    "document_collision_group_count": result[
                        "document_collision_group_count"
                    ],
                    "document_collision_instance_count": result[
                        "document_collision_instance_count"
                    ],
                    "chunk_collision_group_count": result[
                        "chunk_collision_group_count"
                    ],
                    "summary_tree_collision_group_count": result[
                        "summary_tree_collision_group_count"
                    ],
                    "corpus_pair_collision_counts": result[
                        "corpus_pair_collision_counts"
                    ],
                    "neo4j_node_groups": len(neo4j_nodes),
                    "neo4j_relationship_groups": len(neo4j_relationships),
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await driver.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
