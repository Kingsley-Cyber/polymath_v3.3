"""Deterministic cleanup for historical junk Entity nodes in Neo4j.

Future writes are protected by services.graph.entity_cleaning. This module is
the one-time/periodic repair pass for old graph data that predates that guard.
It intentionally shares the same constants so write-time, query-time, and
cleanup-time behavior stay aligned.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from config import get_settings
from services.graph.entity_cleaning import (
    JUNK_ENTITY_EXACT_LOWER,
    JUNK_ENTITY_NAME_PATTERN,
)


def _settings_attr(settings: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = getattr(settings, name, None)
        if value:
            return value
    return default


def _driver():
    from neo4j import AsyncGraphDatabase

    settings = get_settings()
    return AsyncGraphDatabase.driver(
        _settings_attr(settings, "NEO4J_URI", "NEO4J_URL"),
        auth=(
            _settings_attr(settings, "NEO4J_USER", "NEO4J_USERNAME", default="neo4j"),
            _settings_attr(settings, "NEO4J_PASSWORD", "NEO4J_PASS"),
        ),
    )


_CANDIDATE_QUERY = """
MATCH (e:Entity)
WHERE coalesce(e.tombstone, false) = false
WITH e,
     toLower(coalesce(e.display_name, e.canonical_name, e.normalized_name, e.entity_id, '')) AS surface
WHERE surface <> ''
  AND (surface IN $junk_exact OR surface =~ $junk_pattern)
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(e)
WITH e, surface, count(DISTINCT c) AS mentions
OPTIONAL MATCH (e)-[r:RELATES_TO]-(:Entity)
WITH e, surface, mentions, count(DISTINCT r) AS relations
OPTIONAL MATCH (e)-[:HAS_FACT]->(f:Fact)
WITH e, surface, mentions, relations, count(DISTINCT f) AS facts
RETURN
  e.entity_id AS entity_id,
  coalesce(e.display_name, e.canonical_name, e.normalized_name, e.entity_id) AS display_name,
  coalesce(e.primary_entity_type, e.entity_type, '') AS entity_type,
  surface,
  mentions,
  relations,
  facts
ORDER BY mentions DESC, relations DESC, display_name ASC
LIMIT $limit
"""


_COUNT_QUERY = """
MATCH (e:Entity)
WHERE coalesce(e.tombstone, false) = false
WITH e,
     toLower(coalesce(e.display_name, e.canonical_name, e.normalized_name, e.entity_id, '')) AS surface
WHERE surface <> ''
  AND (surface IN $junk_exact OR surface =~ $junk_pattern)
RETURN count(e) AS candidates
"""


_BASELINE_QUERY = """
CALL { MATCH (e:Entity) WHERE coalesce(e.tombstone,false)=false RETURN count(e) AS live_entities }
CALL { MATCH (t:Entity) WHERE coalesce(t.tombstone,false)=true RETURN count(t) AS tombstones }
CALL { MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS relates_to }
CALL { MATCH ()-[r:MENTIONS]->() RETURN count(r) AS mentions }
CALL { MATCH (f:Fact) RETURN count(f) AS facts }
RETURN live_entities, tombstones, relates_to, mentions, facts
"""


_DELETE_BATCH_QUERY = """
MATCH (e:Entity)
WHERE e.entity_id IN $ids AND coalesce(e.tombstone, false) = false
WITH collect(e) AS nodes, count(e) AS matched
FOREACH (node IN nodes | DETACH DELETE node)
RETURN matched
"""


_DELETE_ORPHAN_FACTS = """
MATCH (f:Fact)
WHERE NOT EXISTS { MATCH (:Entity)-[:HAS_FACT]->(f) }
WITH collect(f) AS facts, count(f) AS matched
FOREACH (fact IN facts | DETACH DELETE fact)
RETURN matched
"""


async def _single_value(session, query: str, key: str, **params) -> int:
    row = await (await session.run(query, **params)).single()
    return int(row[key]) if row and row.get(key) is not None else 0


async def _baseline(session) -> dict[str, int]:
    row = await (await session.run(_BASELINE_QUERY)).single()
    return {k: int(row[k]) for k in row.keys()} if row else {}


async def find_candidates(session, *, limit: int) -> tuple[int, list[dict[str, Any]]]:
    params = {
        "junk_exact": list(JUNK_ENTITY_EXACT_LOWER),
        "junk_pattern": JUNK_ENTITY_NAME_PATTERN,
    }
    total = await _single_value(session, _COUNT_QUERY, "candidates", **params)
    result = await session.run(_CANDIDATE_QUERY, **params, limit=int(limit))
    rows = [dict(row) async for row in result]
    return total, rows


async def apply_cleanup(session, *, batch_size: int) -> dict[str, Any]:
    before = await _baseline(session)
    total_deleted = 0
    batches = 0
    sample_deleted: list[dict[str, Any]] = []

    while True:
        _total, rows = await find_candidates(session, limit=batch_size)
        if not rows:
            break
        ids = [row["entity_id"] for row in rows if row.get("entity_id")]
        if not ids:
            break
        if len(sample_deleted) < 20:
            sample_deleted.extend(rows[: 20 - len(sample_deleted)])
        deleted = await _single_value(
            session,
            _DELETE_BATCH_QUERY,
            "matched",
            ids=ids,
        )
        total_deleted += deleted
        batches += 1
        if deleted == 0:
            break

    orphan_facts_deleted = await _single_value(
        session,
        _DELETE_ORPHAN_FACTS,
        "matched",
    )
    after = await _baseline(session)
    remaining, examples = await find_candidates(session, limit=20)
    return {
        "before": before,
        "after": after,
        "deleted_entities": total_deleted,
        "deleted_orphan_facts": orphan_facts_deleted,
        "batches": batches,
        "remaining_candidates": remaining,
        "sample_deleted": sample_deleted[:20],
        "remaining_examples": examples,
    }


async def run(*, apply: bool = False, limit: int = 50, batch_size: int = 500) -> dict[str, Any]:
    driver = _driver()
    try:
        async with driver.session() as session:
            if apply:
                return await apply_cleanup(session, batch_size=batch_size)
            before = await _baseline(session)
            total, examples = await find_candidates(session, limit=limit)
            return {
                "mode": "dry_run",
                "baseline": before,
                "candidates": total,
                "examples": examples,
            }
    finally:
        await driver.close()


def _print_report(report: dict[str, Any]) -> None:
    import json

    print(json.dumps(report, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean historical junk Entity nodes from Neo4j.")
    parser.add_argument("--apply", action="store_true", help="Delete matching junk entities.")
    parser.add_argument("--limit", type=int, default=50, help="Dry-run example limit.")
    parser.add_argument("--batch-size", type=int, default=500, help="Apply batch size.")
    args = parser.parse_args()
    _print_report(
        asyncio.run(
            run(apply=args.apply, limit=args.limit, batch_size=args.batch_size)
        )
    )


if __name__ == "__main__":
    main()
