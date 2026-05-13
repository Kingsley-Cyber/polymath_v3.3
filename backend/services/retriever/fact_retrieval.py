"""
Pt 10a (Cluster 1) — Fact-centric retrieval from Neo4j.

Facts are pre-distilled answer units written by Ghost B during ingestion.
They live as `:Fact` nodes connected via `HAS_FACT` (from Entity) and
`SUPPORTS_FACT` (from Chunk). This module reads them at query time and
returns SourceFact objects that BYPASS the cross-encoder reranker — facts
already carry confidence and evidence_phrase from Ghost B, so cross-encoder
ranking against chunk text would invert the quality signal.

The retriever feeds Facts into a `[Key Facts]` LLM prompt section ahead of
the `[Source Excerpts]` section, so the model has structured answers
available before it tries to synthesize from chunk text.

v1 (Pt 10a) filters to `fact_type='property'` for definitional queries.
Week 2 will broaden to threshold/quantity/category/etc. with intent-based
type routing.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from config import get_settings
from models.schemas import SourceFact
from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)


class FactRetrieval:
    """Pulls Fact nodes from Neo4j for the entities mentioned by the seed chunks."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._driver = None
        if self._settings.NEO4J_ENABLED:
            try:
                self._driver = AsyncGraphDatabase.driver(
                    self._settings.NEO4J_URI,
                    auth=(self._settings.NEO4J_USER, self._settings.NEO4J_PASSWORD),
                )
            except Exception as e:
                logger.error("FactRetrieval: failed to init Neo4j driver: %s", e)

    async def retrieve_facts_for_entities(
        self,
        entity_names: List[str],
        corpus_ids: Optional[List[str]] = None,
        fact_types: Optional[List[str]] = None,
        limit: int = 8,
    ) -> List[SourceFact]:
        """Pull facts about the given entities, optionally filtered by fact_type.

        Pt 10a v1 — initial caller passes `fact_types=['property']` for
        definitional queries. Pass `None` to allow any fact_type through.
        Entity matching is case-insensitive across display_name,
        canonical_name, and normalized_name to absorb LLM-extraction variants.
        """
        if not self._settings.NEO4J_ENABLED or not self._driver:
            return []
        if not entity_names:
            return []

        entity_names_lc = [str(n).lower().strip() for n in entity_names if n]
        entity_names_lc = [n for n in entity_names_lc if n]
        if not entity_names_lc:
            return []

        cypher = """
        MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
        OPTIONAL MATCH (f)<-[:SUPPORTS_FACT]-(c:Chunk)
        WHERE (
              toLower(coalesce(e.display_name, ''))    IN $entity_names_lc
           OR toLower(coalesce(e.canonical_name, ''))  IN $entity_names_lc
           OR toLower(coalesce(e.normalized_name, '')) IN $entity_names_lc
        )
        """
        if corpus_ids:
            cypher += "  AND f.corpus_id IN $corpus_ids\n"
        if fact_types:
            cypher += "  AND f.fact_type IN $fact_types\n"
        cypher += """
        RETURN
            f.fact_id         AS fact_id,
            f.subject         AS subject,
            f.fact_type       AS fact_type,
            f.property_name   AS property_name,
            f.value           AS value,
            f.unit            AS unit,
            f.condition       AS condition,
            f.confidence      AS confidence,
            f.evidence_phrase AS evidence_phrase,
            c.chunk_id        AS chunk_id,
            f.doc_id          AS doc_id,
            f.corpus_id       AS corpus_id
        ORDER BY f.confidence DESC
        LIMIT $limit
        """

        try:
            async with self._driver.session() as session:
                result = await session.run(
                    cypher,
                    entity_names_lc=entity_names_lc,
                    corpus_ids=corpus_ids or [],
                    fact_types=fact_types or [],
                    limit=limit,
                )
                rows = [dict(r) async for r in result]

            facts: List[SourceFact] = []
            for row in rows:
                facts.append(
                    SourceFact(
                        fact_id=row.get("fact_id") or "",
                        subject=row.get("subject") or "",
                        fact_type=row.get("fact_type") or "",
                        property_name=row.get("property_name"),
                        value=row.get("value"),
                        unit=row.get("unit"),
                        condition=row.get("condition"),
                        confidence=float(row.get("confidence") or 0.0),
                        evidence_phrase=row.get("evidence_phrase"),
                        chunk_id=row.get("chunk_id"),
                        doc_id=row.get("doc_id"),
                        corpus_id=row.get("corpus_id"),
                    )
                )
            logger.info(
                "Fact retrieval: %d facts for %d seed entities, types=%s",
                len(facts),
                len(entity_names_lc),
                fact_types,
            )
            return facts
        except Exception as e:
            logger.error("Fact retrieval failed: %s", e)
            return []

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()


# Module-level singleton — mirrors mode_a_expansion's lifecycle.
fact_retrieval = FactRetrieval()
