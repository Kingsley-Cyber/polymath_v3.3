"""Deterministic claim-to-graph promotion.

This module is the claims leg of S5 promote(): it reads already-minted local
claim records from Mongo and writes candidate graph artifacts to Neo4j.  It
does not call an LLM, re-extract text, mint entities, or accept knowledge.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from models.claim_record import ClaimArgumentV1, ClaimRecordV1
from pydantic import ValidationError
from services.graph.neo4j_writer import (
    GRAPH_WRITE_ROW_BATCH_SIZE,
    RELATION_FAMILY_MAP,
    _resolve_entity_id_redirects,
    corpus_content_key,
    entity_id_from_name,
)

LOCAL_EXTRACTION_SCHEMA_VERSION = "polymath.extract.local_extraction.v1"
CLAIMS_PROMOTE_VERSION = "polymath.promote.v2-claims"


def _empty_receipt(*, corpus_id: str, doc_id: str | None) -> dict[str, Any]:
    return {
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "promote_version": CLAIMS_PROMOTE_VERSION,
        "extract_schema_version": LOCAL_EXTRACTION_SCHEMA_VERSION,
        "claims_in": 0,
        "valid_claims": 0,
        "facts_written": 0,
        "supports_fact_edges": 0,
        "has_fact_edges": 0,
        "typed_edges": 0,
        "skipped": {
            "invalid_record": 0,
            "missing_chunk": 0,
            "unresolved_argument": 0,
            "unresolved_subject": 0,
            "unresolved_object": 0,
            "untyped": 0,
        },
    }


def _inc(receipt: dict[str, Any], key: str, amount: int = 1) -> None:
    receipt[key] = int(receipt.get(key) or 0) + int(amount or 0)


def _skip(receipt: dict[str, Any], reason: str, amount: int = 1) -> None:
    skipped = receipt.setdefault("skipped", {})
    skipped[reason] = int(skipped.get(reason) or 0) + int(amount or 0)


def _first_argument(claim: ClaimRecordV1, role: str) -> ClaimArgumentV1 | None:
    return next((arg for arg in claim.arguments if arg.role == role), None)


def _claim_entity_candidate_ids(claim: ClaimRecordV1) -> dict[tuple[str, str], str]:
    ids: dict[tuple[str, str], str] = {}
    for arg in claim.arguments:
        if arg.filler_kind != "entity_mention":
            continue
        surface = str(arg.surface or "").strip()
        if not surface:
            continue
        ids[(arg.role, arg.span_observation_id)] = entity_id_from_name(surface)
    return ids


async def _existing_entity_ids(session: Any, candidate_ids: list[str]) -> set[str]:
    if not candidate_ids:
        return set()
    redirects = await _resolve_entity_id_redirects(session, candidate_ids)
    resolved_ids = sorted(
        {
            str(redirects.get(entity_id, entity_id) or "")
            for entity_id in candidate_ids
            if str(redirects.get(entity_id, entity_id) or "")
        }
    )
    if not resolved_ids:
        return set()
    result = await session.run(
        """
        MATCH (e:Entity)
        WHERE e.entity_id IN $entity_ids
        RETURN e.entity_id AS entity_id
        """,
        entity_ids=resolved_ids,
    )
    return {str(row["entity_id"]) async for row in result}


def _resolved_arguments(
    claim: ClaimRecordV1,
    *,
    existing_ids: set[str],
    redirects: dict[str, str],
) -> dict[tuple[str, str], str]:
    resolved: dict[tuple[str, str], str] = {}
    for key, candidate_id in _claim_entity_candidate_ids(claim).items():
        entity_id = str(redirects.get(candidate_id, candidate_id) or "")
        if entity_id and entity_id in existing_ids:
            resolved[key] = entity_id
    return resolved


def _argument_entity_id(
    claim: ClaimRecordV1,
    *,
    role: str,
    resolved: dict[tuple[str, str], str],
) -> str | None:
    for arg in claim.arguments:
        if arg.role != role:
            continue
        entity_id = resolved.get((arg.role, arg.span_observation_id))
        if entity_id:
            return entity_id
    return None


def _fact_subject(claim: ClaimRecordV1) -> str:
    subject = _first_argument(claim, "subject")
    return str((subject.surface if subject else "") or "Claim").strip()


def _fact_row(
    *,
    corpus_id: str,
    doc_id: str,
    chunk_id: str,
    claim: ClaimRecordV1,
    entity_ids: list[str],
) -> dict[str, Any]:
    predicate = str(claim.normalized_predicate or claim.predicate_lemma or "").strip()
    return {
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "fact_id": claim.claim_id,
        "text": claim.canonical_proposition,
        "subject": _fact_subject(claim),
        "fact_type": claim.claim_type,
        "claim_type": claim.claim_type,
        "property_name": predicate,
        "value": claim.canonical_proposition,
        "polarity": claim.polarity,
        "modality": claim.modality,
        "assertion_mode": claim.assertion_mode,
        "typing_status": claim.typing_status,
        "scope_hash": claim.scope_hash,
        "knowledge_status": "candidate",
        "validation_status": "candidate",
        "extract_schema_version": LOCAL_EXTRACTION_SCHEMA_VERSION,
        "promote_version": CLAIMS_PROMOTE_VERSION,
        "entity_ids": entity_ids,
    }


def _edge_row(
    *,
    corpus_id: str,
    doc_id: str,
    chunk_id: str,
    claim: ClaimRecordV1,
    subject_id: str,
    object_id: str,
) -> dict[str, Any]:
    predicate = str(claim.normalized_predicate or "").strip()
    chunk_key = corpus_content_key(corpus_id, chunk_id)
    doc_key = corpus_content_key(corpus_id, doc_id)
    return {
        "subject_id": subject_id,
        "object_id": object_id,
        "predicate": predicate,
        "source_predicate": claim.predicate_surface,
        "relation_family": RELATION_FAMILY_MAP.get(predicate, "Claim"),
        "confidence": 1.0,
        "chunk_id": chunk_id,
        "chunk_key": chunk_key,
        "doc_id": doc_id,
        "doc_key": doc_key,
        "claim_id": claim.claim_id,
        "schema_version": LOCAL_EXTRACTION_SCHEMA_VERSION,
        "promote_version": CLAIMS_PROMOTE_VERSION,
        "validation_status": "candidate",
    }


async def _write_fact_rows(
    session: Any,
    *,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    totals = {"facts_written": 0, "supports_fact_edges": 0}
    for start in range(0, len(rows), GRAPH_WRITE_ROW_BATCH_SIZE):
        batch = rows[start : start + GRAPH_WRITE_ROW_BATCH_SIZE]
        result = await session.run(
            """
            UNWIND $rows AS row
            MATCH (c:Chunk {corpus_id: row.corpus_id, chunk_id: row.chunk_id})
            MERGE (f:Fact {corpus_id: row.corpus_id, fact_id: row.fact_id})
            SET f.doc_id = row.doc_id,
                f.chunk_id = row.chunk_id,
                f.text = row.text,
                f.subject = row.subject,
                f.fact_type = row.fact_type,
                f.claim_type = row.claim_type,
                f.property_name = row.property_name,
                f.value = row.value,
                f.evidence_phrase = row.text,
                f.confidence = 1.0,
                f.polarity = row.polarity,
                f.modality = row.modality,
                f.assertion_mode = row.assertion_mode,
                f.typing_status = row.typing_status,
                f.scope_hash = row.scope_hash,
                f.knowledge_status = row.knowledge_status,
                f.validation_status = row.validation_status,
                f.extract_schema_version = row.extract_schema_version,
                f.promote_version = row.promote_version
            MERGE (c)-[sf:SUPPORTS_FACT]->(f)
            SET sf.promote_version = row.promote_version,
                sf.extract_schema_version = row.extract_schema_version
            """,
            rows=batch,
        )
        summary = await result.consume()
        totals["facts_written"] += int(summary.counters.nodes_created or 0)
        totals["supports_fact_edges"] += int(
            summary.counters.relationships_created or 0
        )
    return totals


async def _write_has_fact_rows(
    session: Any,
    *,
    rows: list[dict[str, Any]],
) -> int:
    total = 0
    for start in range(0, len(rows), GRAPH_WRITE_ROW_BATCH_SIZE):
        batch = rows[start : start + GRAPH_WRITE_ROW_BATCH_SIZE]
        result = await session.run(
            """
            UNWIND $rows AS row
            MATCH (f:Fact {corpus_id: row.corpus_id, fact_id: row.fact_id})
            UNWIND row.entity_ids AS entity_id
            MATCH (e:Entity {entity_id: entity_id})
            MERGE (e)-[hf:HAS_FACT]->(f)
            SET hf.promote_version = row.promote_version,
                hf.extract_schema_version = row.extract_schema_version
            """,
            rows=batch,
        )
        summary = await result.consume()
        total += int(summary.counters.relationships_created or 0)
    return total


async def _write_edge_rows(
    session: Any,
    *,
    corpus_id: str,
    rows: list[dict[str, Any]],
) -> int:
    total = 0
    for start in range(0, len(rows), GRAPH_WRITE_ROW_BATCH_SIZE):
        batch = rows[start : start + GRAPH_WRITE_ROW_BATCH_SIZE]
        result = await session.run(
            """
            UNWIND $rows AS row
            MATCH (s:Entity {entity_id: row.subject_id})
            MATCH (o:Entity {entity_id: row.object_id})
            MERGE (s)-[r:RELATES_TO {predicate: row.predicate}]->(o)
            WITH row, r,
                 row.chunk_key IN coalesce(r.evidence_chunk_keys, []) AS chunk_seen,
                 row.doc_key IN coalesce(r.evidence_doc_keys, []) AS doc_seen,
                 $corpus_id IN coalesce(r.corpus_ids, []) AS corpus_seen
            SET r.source_predicate = coalesce(r.source_predicate, row.source_predicate),
                r.relation_family = coalesce(r.relation_family, row.relation_family),
                r.confidence = CASE
                    WHEN r.confidence IS NULL THEN toFloat(row.confidence)
                    ELSE r.confidence
                END,
                r.edge_strength = coalesce(r.edge_strength, 'candidate'),
                r.edge_state = coalesce(r.edge_state, 'candidate'),
                r.eligible_for_synthesis = coalesce(r.eligible_for_synthesis, true),
                r.promoted_by = 'claim_record.v1',
                r.promote_version = row.promote_version,
                r.extract_schema_version = coalesce(
                    r.extract_schema_version,
                    row.schema_version
                ),
                r.latest_doc_id = row.doc_id,
                r.latest_doc_key = row.doc_key,
                r.latest_chunk_id = row.chunk_id,
                r.latest_chunk_key = row.chunk_key,
                r.last_seen_at = timestamp()
            SET r.corpus_ids = CASE
                    WHEN corpus_seen THEN coalesce(r.corpus_ids, [])
                    WHEN r.corpus_ids IS NULL THEN [$corpus_id]
                    ELSE r.corpus_ids + [$corpus_id]
                END,
                r.evidence_chunk_ids = CASE
                    WHEN row.chunk_id IN coalesce(r.evidence_chunk_ids, []) THEN coalesce(r.evidence_chunk_ids, [])
                    WHEN r.evidence_chunk_ids IS NULL THEN [row.chunk_id]
                    ELSE r.evidence_chunk_ids + [row.chunk_id]
                END,
                r.evidence_chunk_keys = CASE
                    WHEN chunk_seen THEN coalesce(r.evidence_chunk_keys, [])
                    WHEN r.evidence_chunk_keys IS NULL THEN [row.chunk_key]
                    ELSE r.evidence_chunk_keys + [row.chunk_key]
                END,
                r.evidence_doc_ids = CASE
                    WHEN row.doc_id IN coalesce(r.evidence_doc_ids, []) THEN coalesce(r.evidence_doc_ids, [])
                    WHEN r.evidence_doc_ids IS NULL THEN [row.doc_id]
                    ELSE r.evidence_doc_ids + [row.doc_id]
                END,
                r.evidence_doc_keys = CASE
                    WHEN doc_seen THEN coalesce(r.evidence_doc_keys, [])
                    WHEN r.evidence_doc_keys IS NULL THEN [row.doc_key]
                    ELSE r.evidence_doc_keys + [row.doc_key]
                END,
                r.claim_ids = CASE
                    WHEN row.claim_id IN coalesce(r.claim_ids, []) THEN coalesce(r.claim_ids, [])
                    WHEN r.claim_ids IS NULL THEN [row.claim_id]
                    ELSE r.claim_ids + [row.claim_id]
                END,
                r.validation_statuses = CASE
                    WHEN row.validation_status IN coalesce(r.validation_statuses, []) THEN coalesce(r.validation_statuses, [])
                    WHEN r.validation_statuses IS NULL THEN [row.validation_status]
                    ELSE r.validation_statuses + [row.validation_status]
                END,
                r.extract_schema_versions = CASE
                    WHEN row.schema_version IN coalesce(r.extract_schema_versions, []) THEN coalesce(r.extract_schema_versions, [])
                    WHEN r.extract_schema_versions IS NULL THEN [row.schema_version]
                    ELSE r.extract_schema_versions + [row.schema_version]
                END,
                r.support_confidence_chunk_ids = CASE
                    WHEN chunk_seen THEN coalesce(r.support_confidence_chunk_ids, [])
                    WHEN r.support_confidence_chunk_ids IS NULL THEN [row.chunk_id]
                    ELSE r.support_confidence_chunk_ids + [row.chunk_id]
                END,
                r.support_confidence_chunk_keys = CASE
                    WHEN chunk_seen THEN coalesce(r.support_confidence_chunk_keys, [])
                    WHEN r.support_confidence_chunk_keys IS NULL THEN [row.chunk_key]
                    ELSE r.support_confidence_chunk_keys + [row.chunk_key]
                END,
                r.support_confidence_values_v2 = CASE
                    WHEN chunk_seen THEN coalesce(r.support_confidence_values_v2, [])
                    WHEN r.support_confidence_values_v2 IS NULL THEN [toFloat(row.confidence)]
                    ELSE r.support_confidence_values_v2 + [toFloat(row.confidence)]
                END
            SET r.support_count = CASE
                    WHEN size(coalesce(r.evidence_chunk_keys, [])) > size(coalesce(r.evidence_chunk_ids, []))
                    THEN size(coalesce(r.evidence_chunk_keys, []))
                    ELSE size(coalesce(r.evidence_chunk_ids, []))
                END,
                r.avg_confidence = CASE
                    WHEN size(coalesce(r.support_confidence_values_v2, [])) > 0
                    THEN reduce(total = 0.0, conf IN coalesce(r.support_confidence_values_v2, []) | total + toFloat(conf))
                         / size(coalesce(r.support_confidence_values_v2, []))
                    ELSE toFloat(coalesce(r.confidence, row.confidence, 0.0))
                END
            """,
            rows=batch,
            corpus_id=corpus_id,
        )
        summary = await result.consume()
        total += int(summary.counters.relationships_created or 0)
    return total


async def _existing_chunk_ids(
    session: Any,
    *,
    corpus_id: str,
    chunk_ids: list[str],
) -> set[str]:
    if not chunk_ids:
        return set()
    result = await session.run(
        """
        MATCH (c:Chunk)
        WHERE c.corpus_id = $corpus_id AND c.chunk_id IN $chunk_ids
        RETURN c.chunk_id AS chunk_id
        """,
        corpus_id=corpus_id,
        chunk_ids=sorted(set(chunk_ids)),
    )
    return {str(row["chunk_id"]) async for row in result}


async def _promote_doc_claims(
    db: Any,
    neo4j_driver: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, Any]:
    receipt = _empty_receipt(corpus_id=corpus_id, doc_id=doc_id)
    query = {
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "status": "ok",
        "schema_version": LOCAL_EXTRACTION_SCHEMA_VERSION,
        "claim_compilation.claims.0": {"$exists": True},
    }
    rows = await db["ghost_b_extractions"].find(
        query,
        {
            "_id": 0,
            "corpus_id": 1,
            "doc_id": 1,
            "chunk_id": 1,
            "schema_version": 1,
            "claim_compilation.claims": 1,
        },
    ).sort("chunk_id", 1).to_list(length=None)
    if not rows:
        receipt["status"] = "noop"
        return receipt

    raw_claims_by_chunk: list[tuple[str, dict[str, Any]]] = []
    candidate_entity_ids: set[str] = set()
    chunk_ids: set[str] = set()
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            continue
        chunk_ids.add(chunk_id)
        for raw_claim in ((row.get("claim_compilation") or {}).get("claims") or []):
            _inc(receipt, "claims_in")
            if not isinstance(raw_claim, dict):
                _skip(receipt, "invalid_record")
                continue
            raw_claims_by_chunk.append((chunk_id, raw_claim))
            for arg in raw_claim.get("arguments") or []:
                if not isinstance(arg, dict):
                    continue
                if arg.get("filler_kind") != "entity_mention":
                    continue
                surface = str(arg.get("surface") or "").strip()
                if surface:
                    candidate_entity_ids.add(entity_id_from_name(surface))

    async with neo4j_driver.session() as session:
        redirects = await _resolve_entity_id_redirects(
            session,
            sorted(candidate_entity_ids),
        )
        existing_entities = await _existing_entity_ids(
            session,
            sorted(candidate_entity_ids),
        )
        existing_chunks = await _existing_chunk_ids(
            session,
            corpus_id=corpus_id,
            chunk_ids=sorted(chunk_ids),
        )

        fact_rows: list[dict[str, Any]] = []
        has_fact_rows: list[dict[str, Any]] = []
        edge_rows: list[dict[str, Any]] = []
        for chunk_id, raw_claim in raw_claims_by_chunk:
            try:
                claim = ClaimRecordV1.model_validate(raw_claim)
            except (ValidationError, ValueError, TypeError):
                _skip(receipt, "invalid_record")
                continue
            _inc(receipt, "valid_claims")
            if chunk_id not in existing_chunks:
                _skip(receipt, "missing_chunk")
                continue

            resolved = _resolved_arguments(
                claim,
                existing_ids=existing_entities,
                redirects=redirects,
            )
            entity_ids = sorted(set(resolved.values()))
            unresolved_arguments = sum(
                1
                for arg in claim.arguments
                if arg.filler_kind == "entity_mention"
                and (arg.role, arg.span_observation_id) not in resolved
            )
            if unresolved_arguments:
                _skip(receipt, "unresolved_argument", unresolved_arguments)

            fact_rows.append(
                _fact_row(
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    claim=claim,
                    entity_ids=entity_ids,
                )
            )
            if entity_ids:
                has_fact_rows.append(fact_rows[-1])

            if claim.typing_status != "typed":
                _skip(receipt, "untyped")
                continue
            subject_id = _argument_entity_id(claim, role="subject", resolved=resolved)
            object_id = _argument_entity_id(claim, role="object", resolved=resolved)
            if not subject_id:
                _skip(receipt, "unresolved_subject")
                continue
            if not object_id:
                _skip(receipt, "unresolved_object")
                continue
            edge_rows.append(
                _edge_row(
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    claim=claim,
                    subject_id=subject_id,
                    object_id=object_id,
                )
            )

        fact_counts = await _write_fact_rows(session, rows=fact_rows)
        receipt.update(fact_counts)
        receipt["has_fact_edges"] = await _write_has_fact_rows(
            session,
            rows=has_fact_rows,
        )
        receipt["typed_edges"] = await _write_edge_rows(
            session,
            corpus_id=corpus_id,
            rows=edge_rows,
        )

    status = "done" if receipt["valid_claims"] else "noop"
    if receipt["valid_claims"] and receipt["facts_written"] < len(fact_rows):
        status = "partial" if receipt["facts_written"] else "done"
    receipt["status"] = status
    await db["ghost_b_extractions"].update_many(
        query,
        {
            "$set": {
                "claim_graph_promoted_at": datetime.utcnow(),
                "claim_graph_promote_version": CLAIMS_PROMOTE_VERSION,
                "claim_graph_promotion_receipt": receipt,
            }
        },
    )
    return receipt


async def promote_claims_to_graph(
    db: Any,
    neo4j_driver: Any,
    *,
    corpus_id: str,
    doc_id: str | None = None,
) -> dict[str, Any]:
    """Promote local claim records to candidate graph facts and typed edges."""

    if neo4j_driver is None:
        return {
            **_empty_receipt(corpus_id=corpus_id, doc_id=doc_id),
            "status": "blocked_no_neo4j",
        }
    if doc_id:
        return await _promote_doc_claims(
            db,
            neo4j_driver,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )

    pipeline = [
        {
            "$match": {
                "corpus_id": corpus_id,
                "status": "ok",
                "schema_version": LOCAL_EXTRACTION_SCHEMA_VERSION,
                "claim_compilation.claims.0": {"$exists": True},
            }
        },
        {"$group": {"_id": "$doc_id"}},
        {"$sort": {"_id": 1}},
    ]
    doc_rows = await db["ghost_b_extractions"].aggregate(pipeline).to_list(length=None)
    total = _empty_receipt(corpus_id=corpus_id, doc_id=None)
    total["docs"] = []
    for row in doc_rows:
        current_doc_id = str(row.get("_id") or "")
        if not current_doc_id:
            continue
        receipt = await _promote_doc_claims(
            db,
            neo4j_driver,
            corpus_id=corpus_id,
            doc_id=current_doc_id,
        )
        total["docs"].append(receipt)
        for key in (
            "claims_in",
            "valid_claims",
            "facts_written",
            "supports_fact_edges",
            "has_fact_edges",
            "typed_edges",
        ):
            _inc(total, key, int(receipt.get(key) or 0))
        for reason, count in (receipt.get("skipped") or {}).items():
            _skip(total, reason, int(count or 0))
    total["status"] = "done" if total["docs"] else "noop"
    return total

