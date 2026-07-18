#!/usr/bin/env python3
"""Read-only evidence for cross-corpus content-identity collisions."""

from __future__ import annotations

import asyncio
import json
from collections import Counter

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings


E2E = "2c894530-8d57-4432-a6d4-bc14505a698b"
PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"


async def main() -> None:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = mongo[settings.MONGODB_DATABASE]
        e2e_docs = await db["documents"].find(
            {"corpus_id": E2E},
            {"_id": 0, "doc_id": 1, "filename": 1},
        ).to_list(length=None)
        e2e_doc_ids = sorted(
            str(row.get("doc_id") or "") for row in e2e_docs if row.get("doc_id")
        )
        protected_docs = await db["documents"].find(
            {"corpus_id": PROTECTED, "doc_id": {"$in": e2e_doc_ids}},
            {"_id": 0, "doc_id": 1, "filename": 1},
        ).to_list(length=None)
        shared = sorted(str(row["doc_id"]) for row in protected_docs)

        summary_rows = await db["summary_tree"].find(
            {"doc_id": {"$in": shared}},
            {"_id": 0, "corpus_id": 1, "doc_id": 1, "node_id": 1},
        ).to_list(length=None)
        summary_by_corpus = Counter(str(row.get("corpus_id") or "") for row in summary_rows)
        summary_by_doc = Counter(str(row.get("doc_id") or "") for row in summary_rows)

        async with driver.session() as session:
            node_result = await session.run(
                """
                MATCH (n)
                WHERE n.doc_id IN $doc_ids
                RETURN labels(n) AS labels, n.corpus_id AS corpus_id, count(n) AS count
                ORDER BY corpus_id, labels
                """,
                doc_ids=shared,
            )
            graph_nodes = [dict(row) async for row in node_result]
            rel_result = await session.run(
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
                doc_ids=shared,
            )
            graph_relationships = [dict(row) async for row in rel_result]

        result = {
            "schema_version": "runpod_e2e_isolation_collision_diagnosis.v1",
            "e2e_document_count": len(e2e_doc_ids),
            "shared_document_count": len(shared),
            "shared_doc_ids": shared,
            "shared_filenames": sorted(
                str(row.get("filename") or "") for row in protected_docs
            ),
            "summary_tree_rows_for_shared_docs_by_current_corpus": dict(
                sorted(summary_by_corpus.items())
            ),
            "summary_tree_rows_for_shared_docs_total": len(summary_rows),
            "summary_tree_rows_for_shared_docs_min_per_doc": min(
                summary_by_doc.values(), default=0
            ),
            "summary_tree_rows_for_shared_docs_max_per_doc": max(
                summary_by_doc.values(), default=0
            ),
            "graph_nodes_for_shared_doc_ids": graph_nodes,
            "graph_relationships_for_shared_doc_ids": graph_relationships,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        await driver.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
