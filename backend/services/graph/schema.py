"""
Neo4j schema initialization — constraints and indexes (Phase 4).

Call initialize_schema() once at startup when NEO4J_ENABLED=True.
All DDL uses IF NOT EXISTS — safe to call multiple times (idempotent).
"""

import asyncio
import logging
from time import monotonic

from neo4j import AsyncDriver

logger = logging.getLogger(__name__)

_CONSTRAINTS = [
    "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (f:Fact) REQUIRE f.fact_id IS UNIQUE",
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.corpus_id)",
    # Brain View anchor + composite indexes — drive the books-as-clusters query.
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.is_cluster_anchor)",
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.filename)",
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.corpus_id, d.is_cluster_anchor)",
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.corpus_id, d.filename)",
    "CREATE INDEX IF NOT EXISTS FOR (c:Chunk) ON (c.corpus_id)",
    "CREATE INDEX IF NOT EXISTS FOR (c:Chunk) ON (c.doc_id)",
    "CREATE INDEX IF NOT EXISTS FOR (c:Chunk) ON (c.doc_id, c.chunk_id)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.normalized_name)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.canonical_name)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.display_name)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.entity_type)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.primary_entity_type)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.object_kind)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.object_kind_parent)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.object_kind_root)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.domain_type)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.domain_type_parent)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.domain_type_root)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.canonical_family)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.ontology_version)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.relation_family)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.predicate)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.confidence)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.edge_strength)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.eligible_for_synthesis)",
    # Bridge detection across multi-corpus selections — replaces full-graph scans.
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.corpus_ids)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.evidence_doc_ids)",
    # MENTIONS scoping — fast bridge lookup when shared entity spans books.
    "CREATE INDEX IF NOT EXISTS FOR ()-[m:MENTIONS]-() ON (m.corpus_id)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[m:MENTIONS]-() ON (m.doc_id)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.corpus_id)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.doc_id)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.chunk_id)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.doc_id, f.chunk_id)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.fact_type)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.property_name)",
]

_FULLTEXT_INDEXES = [
    (
        "entity_name_ft",
        "CREATE FULLTEXT INDEX entity_name_ft IF NOT EXISTS "
        "FOR (e:Entity) "
        "ON EACH [e.canonical_name, e.normalized_name, e.display_name, e.aliases]",
    ),
    (
        "fact_text_ft",
        "CREATE FULLTEXT INDEX fact_text_ft IF NOT EXISTS "
        "FOR (f:Fact) "
        "ON EACH [f.subject, f.property_name, f.value, f.fact_type, f.evidence_phrase]",
    ),
]


async def initialize_schema(driver: AsyncDriver) -> None:
    """Apply constraints and indexes. Safe to call on every startup."""
    async with driver.session() as session:
        for stmt in _CONSTRAINTS:
            try:
                await session.run(stmt)
                logger.debug("Constraint applied: %.70s", stmt)
            except Exception as exc:
                logger.warning("Constraint skipped (likely already exists): %s", exc)
        for stmt in _INDEXES:
            try:
                await session.run(stmt)
                logger.debug("Index applied: %.70s", stmt)
            except Exception as exc:
                logger.warning("Index skipped (likely already exists): %s", exc)
        for _name, stmt in _FULLTEXT_INDEXES:
            try:
                await session.run(stmt)
                logger.debug("Full-text index applied: %.70s", stmt)
            except Exception as exc:
                logger.warning("Full-text index skipped (likely already exists): %s", exc)
    logger.info("Neo4j schema initialization complete.")


async def wait_for_retrieval_indexes(
    driver: AsyncDriver,
    *,
    timeout_s: float = 15.0,
) -> dict[str, str]:
    """Wait briefly for retrieval-critical indexes to become ONLINE.

    Neo4j can return from CREATE INDEX while the index is still POPULATING.
    Retrieval code should not assume a clean install is graph-ready until the
    full-text indexes exist and are online.
    """
    required = {name for name, _ in _FULLTEXT_INDEXES}
    deadline = monotonic() + max(0.1, float(timeout_s))
    states: dict[str, str] = {}

    while True:
        async with driver.session() as session:
            result = await session.run(
                "SHOW INDEXES YIELD name, state "
                "WHERE name IN $names "
                "RETURN name, state",
                names=sorted(required),
            )
            states = {str(row["name"]): str(row["state"]) async for row in result}
        if required <= states.keys() and all(states.get(name) == "ONLINE" for name in required):
            return states
        if monotonic() >= deadline:
            missing = sorted(required - states.keys())
            not_online = {
                name: state for name, state in states.items() if state != "ONLINE"
            }
            raise RuntimeError(
                "Neo4j retrieval indexes are not ready: "
                f"missing={missing}, states={not_online}"
            )
        await asyncio.sleep(0.5)
