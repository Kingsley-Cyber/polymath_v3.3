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

_DERIVED_IDENTITY_CONSTRAINTS = [
    (
        "Document",
        ("corpus_id", "doc_id"),
        "CREATE CONSTRAINT document_corpus_doc_id_unique IF NOT EXISTS "
        "FOR (d:Document) REQUIRE (d.corpus_id, d.doc_id) IS UNIQUE",
    ),
    (
        "Chunk",
        ("corpus_id", "chunk_id"),
        "CREATE CONSTRAINT chunk_corpus_chunk_id_unique IF NOT EXISTS "
        "FOR (c:Chunk) REQUIRE (c.corpus_id, c.chunk_id) IS UNIQUE",
    ),
    (
        "Fact",
        ("corpus_id", "fact_id"),
        "CREATE CONSTRAINT fact_corpus_fact_id_unique IF NOT EXISTS "
        "FOR (f:Fact) REQUIRE (f.corpus_id, f.fact_id) IS UNIQUE",
    ),
]

_ENTITY_CONSTRAINT = (
    "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE"
)

_LEGACY_SINGLE_PROPERTY_IDENTITIES = {
    ("Document", ("doc_id",)),
    ("Chunk", ("chunk_id",)),
    ("Fact", ("fact_id",)),
}

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.corpus_id)",
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.corpus_id, d.doc_id)",
    # Brain View anchor + composite indexes — drive the books-as-clusters query.
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.is_cluster_anchor)",
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.filename)",
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.corpus_id, d.is_cluster_anchor)",
    "CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.corpus_id, d.filename)",
    "CREATE INDEX IF NOT EXISTS FOR (c:Chunk) ON (c.corpus_id)",
    "CREATE INDEX IF NOT EXISTS FOR (c:Chunk) ON (c.corpus_id, c.chunk_id)",
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
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.generic_entity)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.graph_expansion_allowed)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.corpus_count)",
    "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.graph_degree)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.relation_family)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.predicate)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.confidence)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.edge_strength)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.eligible_for_synthesis)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.edge_state)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.fallback)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.support_count)",
    # Bridge detection across multi-corpus selections — replaces full-graph scans.
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.corpus_ids)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.evidence_doc_ids)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.evidence_doc_keys)",
    # MENTIONS scoping — fast bridge lookup when shared entity spans books.
    "CREATE INDEX IF NOT EXISTS FOR ()-[m:MENTIONS]-() ON (m.corpus_id)",
    "CREATE INDEX IF NOT EXISTS FOR ()-[m:MENTIONS]-() ON (m.doc_id)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.corpus_id)",
    "CREATE INDEX IF NOT EXISTS FOR (f:Fact) ON (f.corpus_id, f.fact_id)",
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
        # Derived artifacts are corpus instances.  Create the composite
        # constraints first, then remove the legacy global content-ID
        # constraints.  These operations are deliberately fail-closed: a
        # process must not begin graph writes under an ambiguous identity
        # schema.  Entity remains globally unique by design.
        for _label, _properties, stmt in _DERIVED_IDENTITY_CONSTRAINTS:
            await session.run(stmt)
            logger.debug("Composite constraint applied: %.70s", stmt)
        await session.run(_ENTITY_CONSTRAINT)

        constraints = await session.run(
            "SHOW CONSTRAINTS YIELD name, entityType, labelsOrTypes, properties "
            "RETURN name, entityType, labelsOrTypes, properties"
        )
        rows = [dict(row) async for row in constraints]
        for row in rows:
            labels = tuple(str(value) for value in (row.get("labelsOrTypes") or []))
            properties = tuple(str(value) for value in (row.get("properties") or []))
            identity = (labels[0], properties) if len(labels) == 1 else None
            if identity not in _LEGACY_SINGLE_PROPERTY_IDENTITIES:
                continue
            name = str(row.get("name") or "")
            if not name:
                raise RuntimeError(f"legacy identity constraint has no name: {row!r}")
            escaped_name = name.replace("`", "``")
            await session.run(f"DROP CONSTRAINT `{escaped_name}` IF EXISTS")
            logger.info("Dropped legacy global identity constraint: %s", name)

        verified_result = await session.run(
            "SHOW CONSTRAINTS YIELD labelsOrTypes, properties "
            "RETURN labelsOrTypes, properties"
        )
        verified_rows = [dict(row) async for row in verified_result]
        observed = {
            (
                tuple(str(value) for value in (row.get("labelsOrTypes") or [])),
                tuple(str(value) for value in (row.get("properties") or [])),
            )
            for row in verified_rows
        }
        missing = [
            (label, properties)
            for label, properties, _stmt in _DERIVED_IDENTITY_CONSTRAINTS
            if ((label,), properties) not in observed
        ]
        legacy_remaining = [
            (labels, properties)
            for labels, properties in observed
            if len(labels) == 1
            and (labels[0], properties) in _LEGACY_SINGLE_PROPERTY_IDENTITIES
        ]
        if missing or legacy_remaining:
            raise RuntimeError(
                "Neo4j derived-identity schema seal failed: "
                f"missing_composites={missing}, legacy_remaining={legacy_remaining}"
            )
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
                logger.warning(
                    "Full-text index skipped (likely already exists): %s", exc
                )
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
        if required <= states.keys() and all(
            states.get(name) == "ONLINE" for name in required
        ):
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
