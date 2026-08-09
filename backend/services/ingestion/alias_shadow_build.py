"""Production ingest → alias SHADOW builder (owner-ordered wiring, 2026-08-09).

Runs the alias identity phases (candidates → gate → document clustering →
corpus clustering → schema projection) over a corpus's durable extraction
rows and rewrites the corpus's SHADOW collections. Everything stays on the
shadow side: production schema writes and historical backfill remain behind
the ALIAS_RETRIEVAL_PRODUCTION_* hard locks. The query-side shadow lane
(ALIAS_RETRIEVAL_SHADOW_ENABLED) consumes these records read-only.

The phase composition mirrors the retired fixture pipeline
(fixture_knowledge_pipeline.run_phase1_fixture_pipeline), which remains the
reference for the phase order; its pure helpers are reused directly.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from models.alias_identity import AliasCandidateV1
from services.ingestion.alias_candidates import collect_alias_candidates
from services.ingestion.alias_corpus_clustering import build_inventory, cluster_corpus_entities
from services.ingestion.alias_document_clustering import cluster_document_entities
from services.ingestion.alias_gate import run_alias_gate
from services.ingestion.alias_parent_aggregation import ChildAliasEvidence
from services.ingestion.alias_retrieval_shadow import register_shadow_schema_records
from services.ingestion.alias_schema_projection import project_corpus_entity_to_shadow_schema
from services.ingestion.fixture_knowledge_pipeline import (
    _ACRONYM_RE,
    _inventory_from_doc_entities,
    SHADOW_COLLECTIONS,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.utcnow()


async def rebuild_corpus_alias_shadow(db: Any, *, corpus_id: str) -> dict[str, int]:
    """Rebuild the full alias shadow for one corpus from ghost_b_extractions.

    Idempotent: deletes and rewrites the corpus's shadow rows. Pure-CPU
    phase work; never touches production schema collections.
    """
    ghost_rows = [
        row
        async for row in db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id},
            {"_id": 0, "doc_id": 1, "chunk_id": 1, "text": 1, "entities": 1},
        )
    ]
    parent_by_chunk: dict[str, str] = {}
    async for chunk in db["chunks"].find(
        {"corpus_id": corpus_id}, {"_id": 0, "chunk_id": 1, "parent_id": 1}
    ):
        parent_by_chunk[chunk["chunk_id"]] = str(chunk.get("parent_id") or "")

    all_candidates: list[AliasCandidateV1] = []
    all_incomplete: list[Any] = []
    text_by_chunk: dict[str, str] = {}
    for row in ghost_rows:
        text = str(row.get("text") or "")
        text_by_chunk[row["chunk_id"]] = text
        batch = collect_alias_candidates(
            text,
            row.get("entities") or [],
            document_id=row["doc_id"],
            chunk_id=row["chunk_id"],
        )
        all_candidates.extend(batch.candidates)
        all_incomplete.extend(batch.incomplete)

    gate = run_alias_gate(all_candidates, incomplete=all_incomplete)
    decisions_by_id = {d.alias_candidate_id: d for d in gate.decisions}
    candidates_by_id = {c.alias_candidate_id: c for c in all_candidates}

    child_evidence = [
        ChildAliasEvidence(
            candidate=cand,
            decision=decisions_by_id[cand.alias_candidate_id],
            parent_id=parent_by_chunk.get(cand.chunk_id)
            or f"{cand.document_id}_p0",
            child_id=cand.chunk_id,
            child_text=text_by_chunk.get(cand.chunk_id, ""),
        )
        for cand in all_candidates
        if cand.alias_candidate_id in decisions_by_id
    ]
    doc_batch = cluster_document_entities(child_evidence)

    inventories = _inventory_from_doc_entities(
        doc_batch.entities, decisions_by_id, candidates_by_id
    )
    for index, inventory in enumerate(list(inventories)):
        pairs = list(inventory.acronym_pairs)
        for row in ghost_rows:
            if row["doc_id"] != inventory.entity.document_id:
                continue
            for match in _ACRONYM_RE.finditer(str(row.get("text") or "")):
                pairs.append((match.group("short"), match.group("long")))
        inventories[index] = build_inventory(
            inventory.entity,
            trusted_alias_surfaces=inventory.trusted_alias_surfaces,
            temporal_former_names=inventory.temporal_former_names,
            retrieval_surfaces=inventory.retrieval_surfaces,
            acronym_pairs=pairs,
            curated_canonical=inventory.curated_canonical,
            supporting_alias_decision_ids=inventory.supporting_alias_decision_ids,
            is_proper_name=inventory.is_proper_name,
            related_terms=inventory.related_terms,
            description_surfaces=inventory.description_surfaces,
        )

    corpus_batch = cluster_corpus_entities(inventories, corpus_id=corpus_id)
    shadow_records = [
        project_corpus_entity_to_shadow_schema(ent)
        for ent in corpus_batch.corpus_entities
    ]

    now = _utcnow()
    writes = {
        SHADOW_COLLECTIONS["alias_candidates"]: [
            {**c.model_dump(), "corpus_id": corpus_id, "created_at": now}
            for c in all_candidates
        ],
        SHADOW_COLLECTIONS["alias_decisions"]: [
            {**d.model_dump(), "corpus_id": corpus_id, "created_at": now}
            for d in gate.decisions
        ],
        SHADOW_COLLECTIONS["parent_alias_bundles"]: [
            {**b.model_dump(), "corpus_id": corpus_id, "created_at": now}
            for b in doc_batch.parent_bundles
        ],
        SHADOW_COLLECTIONS["document_entities"]: [
            {**e.model_dump(), "corpus_id": corpus_id, "created_at": now}
            for e in doc_batch.entities
        ],
        SHADOW_COLLECTIONS["corpus_entities"]: [
            {**e.model_dump(), "corpus_id": corpus_id, "created_at": now}
            for e in corpus_batch.corpus_entities
        ],
        SHADOW_COLLECTIONS["schema_shadow"]: [
            {
                **r.model_dump(),
                "corpus_id": corpus_id,
                "created_at": now,
                "identity_authority": False,
                "note": "shadow_only_do_not_overwrite_production_schemas",
            }
            for r in shadow_records
        ],
    }
    for collection, rows in writes.items():
        await db[collection].delete_many({"corpus_id": corpus_id})
        if rows:
            await db[collection].insert_many([dict(row) for row in rows])

    # In-process registry for same-process consumers (tests, backend-local
    # rebuilds); cross-process consumers hydrate from the durable rows.
    register_shadow_schema_records(corpus_id, shadow_records)

    counts = {name.rsplit("_", 1)[0]: len(rows) for name, rows in writes.items()}
    counts["shadow_schema_records"] = len(shadow_records)
    logger.info(
        "alias shadow rebuilt corpus=%s candidates=%d entities=%d schema_records=%d",
        corpus_id[:8], len(all_candidates), len(corpus_batch.corpus_entities),
        len(shadow_records),
    )
    return counts
