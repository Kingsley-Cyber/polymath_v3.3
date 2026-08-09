"""Desired-vs-observed artifact census. Exact ID joins, never counts.

This module answers, per document: what should exist, what exists, and what
is missing — as ID sets. It intentionally reuses the same eligibility
predicates the stage planners use (`parent_summary_required_clause`,
`_doc_extraction_skip_reason`, HRAG tier filters, noisy-kind exclusion) so a
census gap is by construction something a planner can create a job for.

Nothing here reads `ingest_stage`, `fully_enriched`, or `write_state` to
decide completeness. Those are display-only legacy fields.
"""

from __future__ import annotations

import logging
from typing import Any

from models.schemas import IngestionConfig
from services.ingestion.extraction_jobs import (
    _doc_extraction_skip_reason,
    classify_extraction_status,
    extraction_contract_hash,
    with_live_extraction_config,
)
from services.ingestion.section_classifier import (
    NOISY_KINDS,
    parent_summary_required_clause,
)
from services.ingestion.summary_jobs import (
    SUMMARY_TEXT_CLAUSE,
    summary_contract_hash,
)
from services.storage.qdrant_writer import _col_for_corpus
from services.storage.record_status import with_active_records

logger = logging.getLogger(__name__)

# Excluded document stages: these documents are intentionally not part of the
# retrieval contract, so they have no desired artifacts.
EXCLUDED_DOCUMENT_STAGES = {
    "skipped_duplicate",
    "skipped_nonsemantic",
    "unsupported_by_policy",
}
_HRAG_CHILD_TIERS = ("tier_a", "tier_b", "tier_b_plus")
_SUMMARY_QDRANT_KINDS = ("naive", "hrag")
# Extraction artifact states that satisfy the extraction desired state.
_EXTRACTION_SATISFIED = {"succeeded", "promoted", "skipped"}

CENSUS_SCHEMA_VERSION = "artifact_census.v1"

# Stage names used in gap receipts. These map 1:1 onto the durable queue that
# owns repair for that artifact class.
STAGE_SOURCE_PARSE = "source_parse"
STAGE_DOCUMENT_PIPELINE = "document_pipeline"
STAGE_SUMMARY = "summary"
STAGE_EXTRACTION = "extraction"
STAGE_GRAPH_PROMOTION = "graph_promotion"
# Extraction SUB-STAGE repair. Distinct from STAGE_EXTRACTION: an organ gap
# means the chunks exist but one organ inside them produced nothing, which is
# fixed by recomputing that organ, not by re-extracting the document.
STAGE_ORGAN_REPAIR = "organ_repair"

GAP_STAGE_BY_KEY = {
    "chunk_source": STAGE_SOURCE_PARSE,
    "qdrant_child_ids": STAGE_DOCUMENT_PIPELINE,
    "summary_ids": STAGE_SUMMARY,
    "document_summary": STAGE_SUMMARY,
    "summary_vector_ids": STAGE_DOCUMENT_PIPELINE,
    "extraction_ids": STAGE_EXTRACTION,
    "fact_ids": STAGE_GRAPH_PROMOTION,
}


def _target_collection_kinds(cfg: IngestionConfig) -> list[str]:
    kinds = [
        kind
        for kind in (cfg.target_qdrant_collections or ["hrag"])
        if kind in {"hrag", "naive"}
    ]
    return kinds or ["hrag"]


def compile_document_contract(
    doc: dict[str, Any],
    corpus: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compile the desired stage set for one document from durable config.

    The live corpus config overlays mutable extraction controls exactly the
    way `plan_extraction_jobs` does, so census and planner agree on whether
    extraction/graph are part of this document's contract.
    """

    live_config = dict((corpus or {}).get("default_ingestion_config") or {})
    effective_doc = with_live_extraction_config(doc, live_config)
    cfg = IngestionConfig(**(effective_doc.get("ingestion_config") or {}))
    extraction_skip_reason = _doc_extraction_skip_reason(effective_doc)
    # Which lane will extract this document decides which ORGANS it can be held
    # accountable for. `extraction_required` alone certified documents as
    # extracted while three of four organs produced nothing for 362,142 chunks;
    # the organ contract makes each sub-stage separately checkable.
    from services.control_plane.extraction_organs import (
        LANE_GRAPHIFY, compile_organ_contract,
    )

    return {
        "target_qdrant_collections": _target_collection_kinds(cfg),
        "extraction_required": extraction_skip_reason is None,
        "extraction_skip_reason": extraction_skip_reason,
        "graph_required": bool(cfg.use_neo4j),
        "extraction_contract_hash": extraction_contract_hash(effective_doc),
        "summary_contract_hash": summary_contract_hash(corpus),
        "organ_contract": compile_organ_contract(LANE_GRAPHIFY),
    }


async def _mongo_chunk_rows(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> list[dict[str, Any]]:
    # Active-record scoping is mandatory: doc_id is content-derived, so a
    # delete → re-ingest of the same file resurrects the document while old
    # chunk tombstones linger. Every stage planner filters them out; if the
    # census counted them, it would demand artifacts no planner can create.
    return await db["chunks"].find(
        with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
        {"_id": 0, "chunk_id": 1, "chunk_kind": 1, "source_tier": 1},
    ).to_list(length=None)


def _required_child_ids_for_kind(
    chunk_rows: list[dict[str, Any]],
    *,
    collection_kind: str,
) -> set[str]:
    """Mirror verify.py's expected-child rules: noisy kinds excluded, and the
    hrag collection stores only Tier A/B/B+ children."""

    required: set[str] = set()
    for row in chunk_rows:
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            continue
        kind = row.get("chunk_kind")
        if kind in NOISY_KINDS:
            continue
        if collection_kind == "hrag" and row.get("source_tier") not in _HRAG_CHILD_TIERS:
            continue
        required.add(chunk_id)
    return required


async def _qdrant_doc_point_ids(
    qdrant_client: Any,
    *,
    corpus_id: str,
    doc_id: str,
    collection_kind: str,
) -> tuple[set[str], set[str]]:
    """Return (child chunk_ids, summary parent_ids) indexed for this doc."""

    from qdrant_client import models as qmodels

    collection_name = _col_for_corpus(corpus_id, collection_kind)
    child_ids: set[str] = set()
    summary_parent_ids: set[str] = set()
    offset = None
    scroll_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="doc_id", match=qmodels.MatchValue(value=doc_id)
            ),
            qmodels.FieldCondition(
                key="corpus_id", match=qmodels.MatchValue(value=corpus_id)
            ),
        ]
    )
    while True:
        points, offset = await qdrant_client.scroll(
            collection_name=collection_name,
            scroll_filter=scroll_filter,
            limit=1024,
            offset=offset,
            with_payload=["chunk_id", "chunk_type", "parent_id"],
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            if payload.get("chunk_type") == "summary":
                parent_id = str(payload.get("parent_id") or "")
                if parent_id:
                    summary_parent_ids.add(parent_id)
                continue
            chunk_id = str(payload.get("chunk_id") or "")
            if chunk_id:
                child_ids.add(chunk_id)
        if offset is None:
            break
    return child_ids, summary_parent_ids


def _sorted_sample(values: set[str], limit: int = 2048) -> list[str]:
    return sorted(values)[: max(0, int(limit))]


async def collect_doc_artifact_census(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    doc_id: str,
    doc: dict[str, Any] | None = None,
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the desired-vs-observed artifact receipt for one document.

    Returns exact missing-ID sets keyed by artifact class. `complete` is True
    only when every desired ID set difference is empty AND no observation
    failed (a store we could not read is unknown, never healthy).
    """

    doc = doc or await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "filename": 1,
            "ingest_stage": 1,
            "ingestion_config": 1,
            "schema_lens": 1,
            "doc_profile.summary": 1,
            "status": 1,
        },
    )
    if not doc:
        return {
            "schema_version": CENSUS_SCHEMA_VERSION,
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "status": "document_not_found",
            "complete": False,
            "excluded": False,
            "missing": {},
            "required": {},
            "gap_stages": [],
            "observation_errors": ["document_not_found"],
        }
    corpus = corpus or await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "corpus_id": 1, "user_id": 1, "default_ingestion_config": 1},
    )
    contract = compile_document_contract(doc, corpus)

    excluded = str(doc.get("ingest_stage") or "") in EXCLUDED_DOCUMENT_STAGES or (
        str(doc.get("status") or "").lower() in {"deleted", "purged"}
    )
    if excluded:
        return {
            "schema_version": CENSUS_SCHEMA_VERSION,
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "status": "excluded",
            "complete": True,
            "excluded": True,
            "contract": contract,
            "missing": {},
            "required": {},
            "gap_stages": [],
            "observation_errors": [],
        }

    observation_errors: list[str] = []
    missing: dict[str, Any] = {
        "chunk_source": False,
        "qdrant_child_ids": [],
        "summary_ids": [],
        "summary_vector_ids": [],
        "document_summary": False,
        "extraction_ids": [],
        "fact_ids": [],
    }
    required: dict[str, int] = {}

    # --- Mongo chunks (parse/chunk stage output) ---------------------------
    chunk_rows = await _mongo_chunk_rows(db, corpus_id=corpus_id, doc_id=doc_id)
    chunk_ids = {str(r.get("chunk_id") or "") for r in chunk_rows if r.get("chunk_id")}
    required["chunks"] = len(chunk_ids)
    if not chunk_ids:
        # Nothing durable exists yet: the whole doc is a source_parse gap.
        missing["chunk_source"] = True

    # --- Qdrant child vectors ----------------------------------------------
    qdrant_child_missing: set[str] = set()
    summary_indexed_by_kind: dict[str, set[str]] = {}
    child_required_total: set[str] = set()
    for kind in contract["target_qdrant_collections"]:
        required_ids = _required_child_ids_for_kind(chunk_rows, collection_kind=kind)
        child_required_total.update(required_ids)
        try:
            indexed_children, indexed_summaries = await _qdrant_doc_point_ids(
                qdrant_client,
                corpus_id=corpus_id,
                doc_id=doc_id,
                collection_kind=kind,
            )
        except Exception as exc:  # noqa: BLE001
            observation_errors.append(
                f"qdrant.scroll({kind}): {type(exc).__name__}: {exc}"
            )
            summary_indexed_by_kind[kind] = set()
            continue
        summary_indexed_by_kind[kind] = indexed_summaries
        qdrant_child_missing.update(required_ids - indexed_children)
    required["qdrant_child_vectors"] = len(child_required_total)
    missing["qdrant_child_ids"] = _sorted_sample(qdrant_child_missing)

    # --- Parent summaries (text) and summary vectors ------------------------
    parent_rows = await db["parent_chunks"].find(
        with_active_records(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "$and": [parent_summary_required_clause()],
            }
        ),
        {"_id": 0, "parent_id": 1, "summary": 1},
    ).to_list(length=None)
    required_parent_ids = {
        str(r.get("parent_id") or "") for r in parent_rows if r.get("parent_id")
    }
    summarized_parent_ids = {
        str(r.get("parent_id") or "")
        for r in parent_rows
        if r.get("parent_id") and str(r.get("summary") or "").strip()
    }
    required["parent_summaries"] = len(required_parent_ids)
    missing_summary_text = required_parent_ids - summarized_parent_ids
    missing["summary_ids"] = _sorted_sample(missing_summary_text)

    summary_vector_missing: set[str] = set()
    for kind in contract["target_qdrant_collections"]:
        if kind not in _SUMMARY_QDRANT_KINDS:
            continue
        indexed = summary_indexed_by_kind.get(kind, set())
        summary_vector_missing.update(summarized_parent_ids - indexed)
    missing["summary_vector_ids"] = _sorted_sample(summary_vector_missing)

    # --- Document summary ----------------------------------------------------
    # Required only once parent-summary context can exist (mirrors
    # classify_document_summary_status: zero required parents never queues).
    doc_summary_text = str(
        ((doc.get("doc_profile") or {}).get("summary")) or ""
    ).strip()
    document_summary_required = bool(required_parent_ids)
    required["document_summary"] = 1 if document_summary_required else 0
    if document_summary_required and not doc_summary_text:
        missing["document_summary"] = True

    # --- Extraction artifacts ------------------------------------------------
    extraction_missing: set[str] = set()
    promotion_missing: set[str] = set()
    promotion_required: set[str] = set()
    if contract["extraction_required"] and chunk_ids:
        ghost_rows = await db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "_id": 0,
                "chunk_id": 1,
                "status": 1,
                "promoted_at": 1,
                "skip_reason": 1,
                "reason": 1,
                "stale_reason": 1,
                "repair_action": 1,
                "error_type": 1,
                "error_message": 1,
                "error": 1,
            },
        ).to_list(length=None)
        ghost_by_chunk = {
            str(r.get("chunk_id") or ""): r for r in ghost_rows if r.get("chunk_id")
        }
        for chunk_id in chunk_ids:
            status, _reason = classify_extraction_status(ghost_by_chunk.get(chunk_id))
            if status not in _EXTRACTION_SATISFIED:
                extraction_missing.add(chunk_id)
            elif contract["graph_required"] and status in {"succeeded", "promoted"}:
                promotion_required.add(chunk_id)
                if status == "succeeded":
                    # Extracted OK but never promoted into Neo4j.
                    promotion_missing.add(chunk_id)
        required["extractions"] = len(chunk_ids)
    else:
        required["extractions"] = 0
    missing["extraction_ids"] = _sorted_sample(extraction_missing)
    if contract["graph_required"]:
        missing["fact_ids"] = _sorted_sample(promotion_missing)
    required["graph_promotions"] = (
        len(promotion_required) if contract["graph_required"] else 0
    )

    gap_stages = sorted(
        {
            GAP_STAGE_BY_KEY[key]
            for key, value in missing.items()
            if key in GAP_STAGE_BY_KEY and (value is True or (isinstance(value, list) and value))
        }
    )
    complete = not gap_stages and not observation_errors
    return {
        "schema_version": CENSUS_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "status": "complete" if complete else "missing_artifacts",
        "complete": complete,
        "excluded": False,
        "contract": contract,
        "required": required,
        "missing": missing,
        "gap_stages": gap_stages,
        "observation_errors": observation_errors,
    }


async def active_document_ids(
    db: Any,
    *,
    corpus_id: str,
    limit: int | None = None,
) -> list[str]:
    """Documents that are part of the corpus retrieval contract.

    Excluded stages (duplicates/non-semantic) have no desired artifacts.
    Soft-deleted rows are excluded by record status.
    """

    from services.storage.record_status import with_active_records

    query = with_active_records(
        {
            "corpus_id": corpus_id,
            "ingest_stage": {"$nin": sorted(EXCLUDED_DOCUMENT_STAGES)},
        }
    )
    cursor = db["documents"].find(query, {"_id": 0, "doc_id": 1}).sort("doc_id", 1)
    if limit:
        cursor = cursor.limit(int(limit))
    rows = await cursor.to_list(length=limit)
    return [str(r.get("doc_id") or "") for r in rows if r.get("doc_id")]
