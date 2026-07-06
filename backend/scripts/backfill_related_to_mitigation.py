#!/usr/bin/env python3
"""Backfill bounded related_to mitigation metadata on existing Neo4j edges.

New ingests write these fields directly. This script upgrades already-promoted
RELATES_TO edges without reingesting documents by using existing predicate,
source_predicates, confidence, and evidence phrase properties.

Dry-run is the default. Pass ``--apply`` to write updates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from neo4j import AsyncGraphDatabase


COUNT_QUERY = """
MATCH ()-[r:RELATES_TO]->()
WHERE r.edge_state IS NULL
   OR r.fallback IS NULL
   OR r.related_to_query_weight IS NULL
   OR (coalesce(r.predicate, 'related_to') = 'related_to' AND r.fallback_evidence_phrase IS NULL)
RETURN count(r) AS stale
"""


BACKFILL_QUERY = """
MATCH ()-[r:RELATES_TO]->()
WHERE r.edge_state IS NULL
   OR r.fallback IS NULL
   OR r.related_to_query_weight IS NULL
   OR (coalesce(r.predicate, 'related_to') = 'related_to' AND r.fallback_evidence_phrase IS NULL)
WITH r
LIMIT $batch_size
WITH r,
     coalesce(r.predicate, 'related_to') AS predicate,
     coalesce(r.source_predicates[0], r.source_predicate, '') AS source_predicate,
     coalesce(r.evidence_phrases[0], r.fallback_evidence_phrase, r.evidence_phrase, '') AS evidence_phrase
WITH r, predicate, source_predicate, evidence_phrase,
     CASE source_predicate
        WHEN 'part_of' THEN 'Structural'
        WHEN 'member_of' THEN 'Structural'
        WHEN 'uses' THEN 'Operational'
        WHEN 'runs_on' THEN 'Operational'
        WHEN 'trained_on' THEN 'Operational'
        WHEN 'implements' THEN 'Operational'
        WHEN 'depends_on' THEN 'Operational'
        WHEN 'produces' THEN 'Operational'
        WHEN 'stores' THEN 'Operational'
        WHEN 'detects' THEN 'Operational'
        WHEN 'classifies' THEN 'Operational'
        WHEN 'supports' THEN 'Operational'
        WHEN 'references' THEN 'Referential'
        WHEN 'derived_from' THEN 'Referential'
        WHEN 'represents' THEN 'Referential'
        WHEN 'maps_to' THEN 'Referential'
        WHEN 'causes' THEN 'Causal'
        WHEN 'preceded_by' THEN 'Causal'
        WHEN 'overlaps' THEN 'Causal'
        WHEN 'contradicts' THEN 'Conflict'
        WHEN 'excepts' THEN 'Conflict'
        WHEN 'overrides' THEN 'Conflict'
        WHEN 'created_by' THEN 'Provenance'
        WHEN 'works_for' THEN 'Affiliation'
        WHEN 'owns' THEN 'Affiliation'
        WHEN 'affiliated_with' THEN 'Affiliation'
        WHEN 'located_in' THEN 'Spatial'
        WHEN 'synonym_of' THEN 'Canonicalization'
        WHEN 'instance_of' THEN 'Canonicalization'
        ELSE ''
     END AS source_family
SET r.edge_state = coalesce(
        r.edge_state,
        CASE
            WHEN predicate <> 'related_to' AND coalesce(r.predicate_refined, false) THEN 'refined'
            WHEN predicate <> 'related_to' THEN 'typed'
            WHEN source_family <> '' THEN 'family'
            ELSE 'fallback'
        END
    ),
    r.fallback = coalesce(r.fallback, predicate = 'related_to'),
    r.fallback_family = CASE
        WHEN predicate = 'related_to' AND coalesce(r.fallback_family, '') = '' THEN source_family
        ELSE coalesce(r.fallback_family, '')
    END,
    r.relation_family = CASE
        WHEN predicate = 'related_to' AND source_family <> '' THEN source_family
        ELSE coalesce(r.relation_family, CASE WHEN predicate = 'related_to' THEN 'WeakAssociation' ELSE '' END)
    END,
    r.fallback_evidence_phrase = CASE
        WHEN predicate = 'related_to' THEN evidence_phrase
        ELSE coalesce(r.fallback_evidence_phrase, '')
    END,
    r.candidate_predicates = CASE
        WHEN predicate = 'related_to'
          AND source_predicate <> ''
          AND source_predicate <> 'related_to'
          AND size(coalesce(r.candidate_predicates, [])) = 0 THEN [source_predicate]
        ELSE coalesce(r.candidate_predicates, [])
    END,
    r.candidate_scores = CASE
        WHEN predicate = 'related_to'
          AND source_predicate <> ''
          AND source_predicate <> 'related_to'
          AND size(coalesce(r.candidate_scores, [])) = 0 THEN [toFloat(coalesce(r.confidence, 0.0))]
        ELSE coalesce(r.candidate_scores, [])
    END,
    r.candidate_score_sources = CASE
        WHEN predicate = 'related_to'
          AND source_predicate <> ''
          AND source_predicate <> 'related_to'
          AND size(coalesce(r.candidate_score_sources, [])) = 0 THEN ['source_predicate_backfill']
        ELSE coalesce(r.candidate_score_sources, [])
    END,
    r.related_to_query_weight = coalesce(r.related_to_query_weight, CASE WHEN predicate = 'related_to' THEN 0.5 ELSE 1.0 END),
    r.related_to_max_hops = coalesce(r.related_to_max_hops, CASE WHEN predicate = 'related_to' THEN 1 ELSE 2 END),
    r.related_to_mitigation_backfilled_at = timestamp()
RETURN count(r) AS updated
"""


SUMMARY_QUERY = """
MATCH ()-[r:RELATES_TO]->()
RETURN count(r) AS total,
       count(CASE WHEN r.edge_state = 'typed' THEN 1 END) AS typed,
       count(CASE WHEN r.edge_state = 'refined' THEN 1 END) AS refined,
       count(CASE WHEN r.edge_state = 'family' THEN 1 END) AS family,
       count(CASE WHEN r.edge_state = 'fallback' THEN 1 END) AS fallback,
       count(CASE WHEN coalesce(r.predicate, 'related_to') = 'related_to' THEN 1 END) AS related_to
"""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


async def _single_value(result: Any, key: str) -> Any:
    row = await result.single()
    return row[key] if row else None


async def run(
    *,
    uri: str,
    user: str,
    password: str,
    apply: bool,
    batch_size: int,
    max_batches: int,
) -> dict[str, Any]:
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    updated_total = 0
    try:
        async with driver.session() as session:
            stale = int(await _single_value(await session.run(COUNT_QUERY), "stale") or 0)
            if apply:
                for _idx in range(max_batches):
                    updated = int(
                        await _single_value(
                            await session.run(BACKFILL_QUERY, batch_size=batch_size),
                            "updated",
                        )
                        or 0
                    )
                    updated_total += updated
                    if updated == 0:
                        break
            summary_row = await (await session.run(SUMMARY_QUERY)).single()
            summary = dict(summary_row) if summary_row else {}
            return {
                "apply": apply,
                "stale_before": stale,
                "updated": updated_total,
                "summary": summary,
            }
    finally:
        await driver.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=_env("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=_env("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=_env("NEO4J_PASSWORD"))
    parser.add_argument("--apply", action="store_true")
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
    result = asyncio.run(
        run(
            uri=args.uri,
            user=args.user,
            password=args.password,
            apply=args.apply,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
