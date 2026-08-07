"""Project Relex relations into RelationAssertion nodes (extracted_assertion plane).

Does not create Fact nodes. Does not weaken the qualified-fact gate.
Entity/MENTIONS/RELATES_TO remain owned by neo4j_writer.write_document_graph.
"""

from __future__ import annotations

import logging
from typing import Any

from models.graph_projection_ir import (
    GraphAssertionNode,
    GraphAssertionSupportEdge,
    GraphProjectionIR,
    deterministic_assertion_id,
)
from services.graph.neo4j_writer import entity_id_from_name
from services.ingestion.graph_backfill import _rehydrate_ghost_b_staging

logger = logging.getLogger(__name__)


def build_assertion_ir_from_ghost_rows(
    *,
    corpus_id: str,
    document_id: str,
    ghost_rows: list[dict[str, Any]],
) -> GraphProjectionIR:
    assertions: list[GraphAssertionNode] = []
    supports: list[GraphAssertionSupportEdge] = []
    unresolved_predicates: list[str] = []

    for row in ghost_rows:
        chunk_id = str(row.get("chunk_id") or "")
        local = row.get("local_extraction") or {}
        for rel in row.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            status = str(rel.get("validation_status") or "").lower()
            if status.startswith("reject"):
                continue
            subject = str(rel.get("subject") or "").strip()
            obj = str(rel.get("object") or "").strip()
            predicate = str(
                rel.get("source_predicate") or rel.get("predicate") or "related_to"
            ).strip()
            if not subject or not obj or not predicate:
                continue
            if str(rel.get("object_kind") or "entity") != "entity":
                continue
            sid = entity_id_from_name(subject, "Entity")
            oid = entity_id_from_name(obj, "Entity")
            if not sid or not oid:
                continue
            aid = deterministic_assertion_id(
                corpus_id=corpus_id,
                chunk_id=chunk_id,
                subject_entity_id=sid,
                predicate_id=predicate,
                object_entity_id=oid,
            )
            assertions.append(
                GraphAssertionNode(
                    assertion_id=aid,
                    corpus_id=corpus_id,
                    subject_entity_id=sid,
                    predicate_id=predicate,
                    object_entity_id=oid,
                    source_document_id=document_id,
                    source_chunk_id=chunk_id,
                    evidence_text=str(rel.get("evidence_phrase") or "")[:2000],
                    confidence=float(rel.get("confidence") or 0.0),
                    acceptance_lane=status or "accepted",
                    assertion_status="accepted",
                    extractor_release=str(local.get("extractor_release") or ""),
                    model_hash=str(local.get("model_hash") or ""),
                    contract_hash=str(local.get("contract") or ""),
                    authority="extracted_assertion",
                    canonical=False,
                )
            )
            if chunk_id:
                supports.append(
                    GraphAssertionSupportEdge(
                        chunk_id=chunk_id,
                        assertion_id=aid,
                        corpus_id=corpus_id,
                    )
                )

    ir = GraphProjectionIR(
        corpus_id=corpus_id,
        document_id=document_id,
        extraction_release="graphify-cpu-pipeline-v1",
        assertion_nodes=tuple(assertions),
        assertion_support_edges=tuple(supports),
        unresolved_predicates=tuple(sorted(set(unresolved_predicates))),
        expected_counts={
            "assertion_nodes": len(assertions),
            "assertion_support_edges": len(supports),
        },
    )
    return ir


async def write_relation_assertions(
    driver: Any,
    ir: GraphProjectionIR,
) -> dict[str, int]:
    """MERGE RelationAssertion nodes and SUPPORTS_ASSERTION / SUBJECT_OF / OBJECT."""

    if not ir.assertion_nodes:
        return {"assertion_nodes": 0, "support_edges": 0}

    rows = [a.model_dump() for a in ir.assertion_nodes]
    supports = [s.model_dump() for s in ir.assertion_support_edges]
    async with driver.session() as session:
        await session.run(
            """
            UNWIND $rows AS row
            MERGE (a:RelationAssertion {assertion_id: row.assertion_id})
            SET a += row
            WITH a, row
            MERGE (s:Entity {entity_id: row.subject_entity_id})
            ON CREATE SET s.canonical_name = row.subject_entity_id
            MERGE (o:Entity {entity_id: row.object_entity_id})
            ON CREATE SET o.canonical_name = row.object_entity_id
            MERGE (s)-[:SUBJECT_OF]->(a)
            MERGE (a)-[:OBJECT]->(o)
            """,
            rows=rows,
        )
        if supports:
            await session.run(
                """
                UNWIND $supports AS row
                MATCH (a:RelationAssertion {assertion_id: row.assertion_id})
                MATCH (c:Chunk {chunk_id: row.chunk_id, corpus_id: row.corpus_id})
                MERGE (c)-[:SUPPORTS_ASSERTION]->(a)
                """,
                supports=supports,
            )
    return {
        "assertion_nodes": len(rows),
        "support_edges": len(supports),
        "projection_hash": ir.projection_hash(),
    }


async def project_document_assertions_from_mongo(
    db: Any,
    driver: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, Any]:
    """Project accepted assertions from the post-gate KnowledgeArtifactBundle view.

    Never depends on qualified Fact records. Uses accepted relations only
    (facts=[] remains valid).
    """

    from services.ingestion.knowledge_bundle import (
        assertion_rows_from_bundle,
        bundle_from_extraction_row,
    )

    rows = await db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    ).to_list(5000)
    # Rebuild ghost-shaped rows from bundles so projection never keys on facts[].
    bundle_ghost: list[dict[str, Any]] = []
    for row in rows:
        bundle = bundle_from_extraction_row(row)
        accepted = assertion_rows_from_bundle(bundle)
        if not accepted and not (row.get("relations") or []):
            continue
        bundle_ghost.append(
            {
                **row,
                "relations": accepted
                or [
                    r
                    for r in (row.get("relations") or [])
                    if str(r.get("validation_status") or "")
                    .lower()
                    .startswith("accept")
                ],
                "facts": [],  # Fact plane is release-gated elsewhere
                "_knowledge_bundle_hash": bundle.audit.bundle_hash,
            }
        )
    ir = build_assertion_ir_from_ghost_rows(
        corpus_id=corpus_id, document_id=doc_id, ghost_rows=bundle_ghost or rows
    )
    written = await write_relation_assertions(driver, ir)
    _ = _rehydrate_ghost_b_staging
    return {
        "doc_id": doc_id,
        "ghost_rows": len(rows),
        "bundles": len(bundle_ghost),
        "ir_assertions": len(ir.assertion_nodes),
        **written,
    }
