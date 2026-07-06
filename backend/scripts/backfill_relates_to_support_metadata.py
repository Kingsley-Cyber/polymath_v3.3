#!/usr/bin/env python3
"""Backfill materialized RELATES_TO support metadata in Neo4j.

This is intentionally bounded: each transaction updates at most --batch-size
relationships, then the script loops until no stale RELATES_TO edge remains.
Existing edges did not store per-support confidence history, so avg_confidence
falls back to the edge confidence unless support_confidence_values exists.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from neo4j import AsyncGraphDatabase


PROMOTE_VERSION = "polymath.promote.v1"
DEFAULT_SCHEMA_VERSION = "polymath.extract.v1"
GENERIC_ENTITY_TERMS = [
    "agent",
    "attention",
    "class",
    "data",
    "example",
    "field",
    "memory",
    "method",
    "model",
    "object",
    "process",
    "system",
    "user",
    "value",
]


BACKFILL_QUERY = """
MATCH ()-[r:RELATES_TO]->()
WHERE r.support_count IS NULL
   OR r.avg_confidence IS NULL
   OR r.extract_schema_version IS NULL
   OR r.promote_version IS NULL
WITH r
LIMIT $batch_size
SET r.support_count = size(coalesce(r.evidence_chunk_ids, [])),
    r.avg_confidence = CASE
        WHEN size(coalesce(r.support_confidence_values, [])) = 0 THEN toFloat(coalesce(r.confidence, 0.0))
        ELSE reduce(total = 0.0, conf IN coalesce(r.support_confidence_values, []) | total + toFloat(conf))
             / size(coalesce(r.support_confidence_values, []))
    END,
    r.extract_schema_version = coalesce(r.extract_schema_version, $default_schema_version),
    r.promote_version = coalesce(r.promote_version, $promote_version),
    r.support_backfilled_at = timestamp()
RETURN count(r) AS updated
"""


COUNT_QUERY = """
MATCH ()-[r:RELATES_TO]->()
RETURN count(r) AS rels,
       count(r.support_count) AS with_support_count,
       count(r.avg_confidence) AS with_avg_confidence,
       count(r.extract_schema_version) AS with_schema_version,
       count(r.promote_version) AS with_promote_version
"""


ENTITY_BACKFILL_QUERY = """
MATCH (e:Entity)
WHERE e.source_corpora IS NULL
   OR e.corpus_count IS NULL
   OR e.mentions IS NULL
   OR e.graph_degree IS NULL
   OR e.generic_entity IS NULL
   OR e.graph_expansion_allowed IS NULL
   OR e.ambiguous IS NULL
   OR e.needs_review IS NULL
WITH e
LIMIT $batch_size
WITH e, toLower(coalesce(e.canonical_name, e.normalized_name, e.display_name, '')) AS entity_name
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(e)
WITH e, entity_name,
     count(DISTINCT c.chunk_id) AS mentions,
     [cid IN collect(DISTINCT c.corpus_id) WHERE cid IS NOT NULL] AS source_corpora
OPTIONAL MATCH (e)-[r:RELATES_TO]-()
WITH e, entity_name, mentions, source_corpora, count(DISTINCT r) AS graph_degree,
     entity_name IN $generic_terms AS is_generic
SET e.source_corpora = source_corpora,
    e.corpus_count = size(source_corpora),
    e.mentions = mentions,
    e.graph_degree = graph_degree,
    e.generic_entity = coalesce(e.generic_entity, is_generic),
    e.ambiguous = coalesce(e.ambiguous, is_generic),
    e.needs_review = coalesce(e.needs_review, is_generic),
    e.graph_expansion_allowed = CASE
        WHEN is_generic THEN false
        ELSE coalesce(e.graph_expansion_allowed, true)
    END,
    e.graph_metadata_backfilled_at = timestamp()
RETURN count(e) AS updated
"""


ENTITY_COUNT_QUERY = """
MATCH (e:Entity)
RETURN count(e) AS entities,
       count(e.source_corpora) AS with_source_corpora,
       count(e.corpus_count) AS with_corpus_count,
       count(e.mentions) AS with_mentions,
       count(e.graph_degree) AS with_graph_degree,
       count(e.generic_entity) AS with_generic_entity,
       count(e.graph_expansion_allowed) AS with_graph_expansion_allowed,
       count(CASE WHEN e.generic_entity = true THEN 1 END) AS generic_entities
"""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


async def _single_value(result: Any, key: str) -> Any:
    row = await result.single()
    return row[key] if row else None


async def backfill(*, uri: str, user: str, password: str, batch_size: int, max_batches: int) -> None:
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    rel_total = 0
    entity_total = 0
    try:
        async with driver.session() as session:
            for batch_idx in range(max_batches):
                result = await session.run(
                    BACKFILL_QUERY,
                    batch_size=batch_size,
                    default_schema_version=DEFAULT_SCHEMA_VERSION,
                    promote_version=PROMOTE_VERSION,
                )
                updated = int(await _single_value(result, "updated") or 0)
                rel_total += updated
                print(f"rel_batch={batch_idx + 1} updated={updated} total={rel_total}")
                if updated == 0:
                    break

            result = await session.run(COUNT_QUERY)
            row = await result.single()
            print({"relations": dict(row) if row else {}})

            for batch_idx in range(max_batches):
                result = await session.run(
                    ENTITY_BACKFILL_QUERY,
                    batch_size=batch_size,
                    generic_terms=GENERIC_ENTITY_TERMS,
                )
                updated = int(await _single_value(result, "updated") or 0)
                entity_total += updated
                print(f"entity_batch={batch_idx + 1} updated={updated} total={entity_total}")
                if updated == 0:
                    break

            result = await session.run(ENTITY_COUNT_QUERY)
            row = await result.single()
            print({"entities": dict(row) if row else {}})
    finally:
        await driver.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=_env("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=_env("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=_env("NEO4J_PASSWORD"))
    parser.add_argument("--batch-size", type=int, default=int(_env("BACKFILL_BATCH_SIZE", "5000")))
    parser.add_argument("--max-batches", type=int, default=int(_env("BACKFILL_MAX_BATCHES", "1000")))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.password:
        raise SystemExit("NEO4J_PASSWORD is required")
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.max_batches <= 0:
        raise SystemExit("--max-batches must be positive")
    asyncio.run(
        backfill(
            uri=args.uri,
            user=args.user,
            password=args.password,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
        )
    )


if __name__ == "__main__":
    main()
