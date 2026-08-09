"""Capability-aware Graph route authority.

Three planes (owner directive 2026-08-04):
  structural           — Document/Chunk navigation
  extracted_assertion  — Entity + MENTIONS + RELATES_TO / RelationAssertion
  qualified_fact       — Fact nodes (release-gated; may be empty)

facts=0 must NOT block entity/assertion Graph. Never silently return Hybrid
under the Graph label when no graph capability is available.
"""

from __future__ import annotations

import logging
from typing import Any

from models.graph_projection_ir import graph_capabilities_template

logger = logging.getLogger(__name__)

GRAPH_BLOCK_REASON = "qualified_graph_evidence_unavailable"
GRAPH_NO_CAPABILITY_REASON = "qualified_graph_evidence_unavailable"
QUALIFIED_FACT_UNAVAILABLE = "qualified_fact_authority_unavailable"


async def inspect_graph_capabilities(corpus_ids: list[str] | None) -> dict[str, Any]:
    """MEASURE Neo4j capabilities for selected corpora (fail-closed)."""

    ids = [str(c).strip() for c in (corpus_ids or []) if str(c).strip()]
    out = graph_capabilities_template(
        corpus_ids=ids,
        error=None,
    )
    if not ids:
        out["error"] = "no_corpus_ids"
        out["reason"] = "no_corpus_ids"
        return out
    try:
        from config import get_settings
        from neo4j import AsyncGraphDatabase

        settings = get_settings()
        if not getattr(settings, "NEO4J_ENABLED", False):
            out["error"] = "neo4j_disabled"
            out["reason"] = "neo4j_disabled"
            return out
        driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        try:
            async with driver.session() as session:
                doc_n = (
                    await (
                        await session.run(
                            "MATCH (d:Document) WHERE d.corpus_id IN $ids "
                            "RETURN count(d) AS n",
                            ids=ids,
                        )
                    ).single()
                )["n"]
                chunk_n = (
                    await (
                        await session.run(
                            "MATCH (c:Chunk) WHERE c.corpus_id IN $ids "
                            "RETURN count(c) AS n",
                            ids=ids,
                        )
                    ).single()
                )["n"]
                # Entity nodes are global; count via MENTIONS from corpus chunks.
                ent_rec = await (
                    await session.run(
                        """
                        MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
                        WHERE c.corpus_id IN $ids
                        RETURN count(DISTINCT e) AS entities, count(*) AS mentions
                        """,
                        ids=ids,
                    )
                ).single()
                rel_rec = await (
                    await session.run(
                        """
                        MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)-[r:RELATES_TO]-()
                        WHERE c.corpus_id IN $ids
                        RETURN count(r) AS relates
                        """,
                        ids=ids,
                    )
                ).single()
                assertion_n = 0
                try:
                    assertion_n = (
                        await (
                            await session.run(
                                """
                                MATCH (a:RelationAssertion)
                                WHERE a.corpus_id IN $ids
                                RETURN count(a) AS n
                                """,
                                ids=ids,
                            )
                        ).single()
                    )["n"]
                except Exception:
                    assertion_n = 0
                fact_n = (
                    await (
                        await session.run(
                            "MATCH (f:Fact) WHERE f.corpus_id IN $ids "
                            "RETURN count(f) AS n",
                            ids=ids,
                        )
                    ).single()
                )["n"]

            entities = int(ent_rec["entities"] or 0)
            mentions = int(ent_rec["mentions"] or 0)
            relates = int(rel_rec["relates"] or 0)
            assertion_nodes = int(assertion_n or 0)
            # Assertion plane ready if RelationAssertion nodes OR RELATES_TO edges.
            assertion_ready = assertion_nodes > 0 or relates > 0
            entity_ready = entities > 0 and mentions > 0
            structural_ready = int(doc_n or 0) > 0 and int(chunk_n or 0) > 0
            fact_ready = int(fact_n or 0) > 0

            if fact_ready:
                advertised = "graph_qualified_fact"
            elif assertion_ready:
                advertised = "graph_assertion"
            elif entity_ready:
                advertised = "graph_entity_navigation"
            elif structural_ready:
                advertised = "graph_structural"
            else:
                advertised = "blocked"

            out.update(
                {
                    "extraction_terminal": None,  # filled by caller from Mongo when known
                    "structural_ready": structural_ready,
                    "entity_ready": entity_ready,
                    "assertion_ready": assertion_ready,
                    "qualified_fact_ready": fact_ready,
                    "advertised_mode": advertised,
                    "authority_available": advertised != "blocked",
                    "counts": {
                        "document_nodes": int(doc_n or 0),
                        "chunk_nodes": int(chunk_n or 0),
                        "entity_nodes": entities,
                        "mention_edges": mentions,
                        "relates_to_edges": relates,
                        "assertion_nodes": assertion_nodes,
                        "fact_nodes": int(fact_n or 0),
                    },
                    "reason": (
                        ""
                        if advertised != "blocked"
                        else "no_structural_entity_or_assertion_projection"
                    ),
                }
            )
        finally:
            await driver.close()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["reason"] = "graph_capability_inspect_failed"
        logger.warning("Graph capability inspect failed closed: %s", exc)
    return out


# Backward-compatible alias used by earlier Graph block wiring.
async def count_qualified_neo4j_facts(corpus_ids: list[str] | None) -> dict[str, Any]:
    caps = await inspect_graph_capabilities(corpus_ids)
    return {
        "corpus_ids": caps.get("corpus_ids") or corpus_ids or [],
        "qualified_fact_nodes": (caps.get("counts") or {}).get("fact_nodes", 0),
        "authority_available": bool(caps.get("authority_available")),
        "capabilities": caps,
        "error": caps.get("error"),
    }


def blocked_graph_diagnostics(
    *,
    authority: dict[str, Any],
    original_query: str = "",
    standalone_query: str = "",
    require_qualified_facts: bool = False,
) -> dict[str, Any]:
    caps = authority.get("capabilities") or authority
    if require_qualified_facts and not caps.get("qualified_fact_ready"):
        reason = QUALIFIED_FACT_UNAVAILABLE
        available = []
        if caps.get("entity_ready"):
            available.append("entity_navigation")
        if caps.get("assertion_ready"):
            available.append("source_backed_relation_assertions")
        if caps.get("structural_ready"):
            available.append("structural_navigation")
        return {
            "status": "blocked",
            "reason": reason,
            "requested_mode": "graph",
            "available_modes": ["fast", "hybrid"],
            "available_graph_capabilities": available,
            "graph_authority": authority,
            "graph_capabilities": caps,
            "original_query": original_query,
            "standalone_query": standalone_query,
            "schema_records_as_citations": 0,
            "shadow_facts_used": 0,
            "facts_used": 0,
        }
    return {
        "status": "blocked",
        "reason": GRAPH_NO_CAPABILITY_REASON,
        "requested_mode": "graph",
        "available_modes": ["fast", "hybrid"],
        "available_graph_capabilities": [],
        "graph_authority": authority,
        "graph_capabilities": caps,
        "original_query": original_query,
        "standalone_query": standalone_query,
        "schema_records_as_citations": 0,
        "shadow_facts_used": 0,
        "facts_used": 0,
    }
