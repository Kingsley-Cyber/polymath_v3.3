"""Route-aware readiness bridge: artifact census -> ReadinessDecision.

P4 of the book-ingestion repair program. The control plane owns ONE route
gate table (``models.release_state.ROUTE_ARTIFACT_REQUIREMENTS``); this
module translates the durable artifact census (materialized
``corpus_readiness`` + extraction artifacts + the release registry) into
the ``present_artifacts`` set that ``ReadinessDecision.decide()`` consumes.

Design rules (owner decision):
- There is no universal query-ready boolean. A corpus may be
  vector_ready / hierarchical_ready / graph_not_ready simultaneously.
- Ordinary retrieval continues while graph writes are blocked; the graph
  routes fail closed until the release registry carries an active passing
  release pin.
- Census translation never raises at query entries: any failure degrades
  to a blocked decision with an exact reason, never to a 500.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from models.release_state import ReadinessDecision

logger = logging.getLogger(__name__)

CORPUS_CERTIFICATE_SCHEMA_VERSION = "corpus_certificate.v1"

#: Routes every certificate records a verdict for (owner §4.4).
CERTIFICATE_ROUTES: tuple[str, ...] = (
    "vector_search",
    "hybrid_search",
    "curated_chat",
    "entity_lookup",
    "graph_read",
    "graph_write",
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _readiness_record(db: Any, corpus_id: str) -> dict[str, Any]:
    """Materialized readiness first; a stale/missing record recomputes."""

    from services.ingestion.readiness import (
        compute_corpus_readiness,
        get_materialized_corpus_readiness,
    )

    record = await get_materialized_corpus_readiness(db, corpus_id)
    if record is None or record.get("stale"):
        record = await compute_corpus_readiness(db, corpus_id)
    return record or {}


async def corpus_artifact_census(db: Any, corpus_id: str) -> dict[str, Any]:
    """Translate durable artifacts into the census vocabulary.

    Artifact names match ``ROUTE_ARTIFACT_REQUIREMENTS`` exactly.
    """

    record = await _readiness_record(db, corpus_id)
    docs = record.get("documents") or {}
    chunks = record.get("chunks") or {}
    summaries = record.get("summaries") or {}
    graph = record.get("graph") or {}

    present: set[str] = set()

    doc_total = _int(docs.get("total"))
    queryable = _int(docs.get("queryable"))
    chunk_total = _int(chunks.get("total"))

    # Child records + embeddings + Qdrant projection (write_state.qdrant_written).
    if chunk_total > 0 and queryable > 0:
        present.add("child_vectors")
    # Canonical retrieval parent summaries.
    if _int(summaries.get("retrieval_parent_done")) > 0:
        present.add("parent_records")
    # Document summaries / section tree (hierarchical descent).
    if _int(summaries.get("document_done")) > 0 or _int(
        summaries.get("summary_tree_index_ready")
    ) > 0:
        present.add("summary_records")
    # Every active document can descend to child evidence.
    if doc_total > 0 and queryable >= doc_total:
        present.add("evidence_obligations")
    if _int(docs.get("lexicon_ready")) > 0:
        present.add("entity_lexicon")

    # Canonical Graphify extraction rows are the extraction-inspection truth.
    extraction_rows = await db["ghost_b_extractions"].count_documents(
        {"corpus_id": corpus_id, "status": {"$in": ["ok", "skipped"]}}
    )
    if extraction_rows > 0:
        present.add("extraction_artifact")

    # Graph projection presence; release authority is a SEPARATE artifact.
    graph_promoted = _int(graph.get("promoted"))
    if graph_promoted > 0:
        present.add("graph_projection")

    release_pin = None
    release_reason = None
    try:
        from services.control_plane.release_registry import load_release_registry

        resolution = load_release_registry()
        release_reason = resolution.reason
        if resolution.status == "ok" and resolution.release_pin is not None:
            release_pin = resolution.release_pin
            present.add("release_pin_match")
            present.add("active_release")
            if resolution.release_pin.graph_write_promotion == "passed":
                present.add("graph_write_promotion")
                present.add("extraction_qualification")
    except Exception as exc:  # noqa: BLE001 — census never raises at entries
        release_reason = f"registry_error:{type(exc).__name__}"

    return {
        "corpus_id": corpus_id,
        "present": sorted(present),
        "release_pin": release_pin,
        "release_reason": release_reason,
        "counts": {
            "documents": doc_total,
            "queryable": queryable,
            "chunks": chunk_total,
            "extraction_rows": int(extraction_rows),
            "graph_promoted": graph_promoted,
        },
    }


def decide_corpus_route_from_census(
    census: dict[str, Any],
    *,
    route: str,
    certificate_id: str | None = None,
) -> ReadinessDecision:
    """Pure decision from an already-computed census (no IO)."""

    return ReadinessDecision.decide(
        corpus_id=str(census.get("corpus_id") or ""),
        route=route,
        present_artifacts=set(census.get("present") or ()),
        certificate_id=certificate_id,
        release_pins={},
        release=census.get("release_pin"),
    )


async def decide_corpus_route(
    db: Any,
    corpus_id: str,
    route: str,
    *,
    certificate_id: str | None = None,
) -> ReadinessDecision:
    census = await corpus_artifact_census(db, corpus_id)
    return decide_corpus_route_from_census(
        census, route=route, certificate_id=certificate_id
    )


def decision_payload(decision: ReadinessDecision) -> dict[str, Any]:
    """JSON-safe relay shape for API/MCP responses."""

    return {
        "corpus_id": decision.corpus_id,
        "route": decision.route,
        "allowed": decision.allowed,
        "mode": decision.mode,
        "required_artifacts": list(decision.required_artifacts),
        "missing_artifacts": list(decision.missing_artifacts),
        "certificate_id": decision.certificate_id,
        "reasons": list(decision.reasons),
    }


async def route_readiness_payload(
    db: Any,
    corpus_ids: list[str],
    route: str,
) -> dict[str, Any]:
    """Per-corpus decisions for one route. Never raises.

    Failures degrade to a blocked decision with the exact reason so query
    entries can relay the state instead of failing the whole request.
    """

    decisions: dict[str, Any] = {}
    for cid in list(corpus_ids or []):
        cid = str(cid or "")
        if not cid:
            continue
        try:
            decision = await decide_corpus_route(db, cid, route)
            decisions[cid] = decision_payload(decision)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "route readiness census failed corpus=%s route=%s: %s",
                cid[:12],
                route,
                exc,
            )
            decisions[cid] = {
                "corpus_id": cid,
                "route": route,
                "allowed": False,
                "mode": "blocked",
                "required_artifacts": [],
                "missing_artifacts": [],
                "certificate_id": None,
                "reasons": [f"census_error:{type(exc).__name__}"],
            }
    return {"route": route, "decisions": decisions}


# ---------------------------------------------------------------------------
# Certificates (§4.4)
# ---------------------------------------------------------------------------


def _canonical_json(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


async def build_corpus_certificate(db: Any, corpus_id: str) -> dict[str, Any]:
    """Assemble the §4.4 certificate from durable artifacts.

    Every release identity comes from stamped artifacts or live contract
    hashes — never from descriptive stamps or inferred values.
    """

    from config import get_settings

    from services.extraction.gliner2_cpu_provider import (
        MODEL_ID,
        MODEL_REVISION,
        PROVIDER_RELEASE,
        provider_release_hash,
    )
    from services.extraction.graphify_pipeline import PIPELINE_RELEASE
    from services.extraction.graphify_reducer import REDUCER_RELEASE
    from services.extraction.graphify_relations import RELATION_RELEASE

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {
            "_id": 0,
            "corpus_id": 1,
            "name": 1,
            "generation": 1,
            "default_ingestion_config": 1,
        },
    )
    cfg = ((corpus or {}).get("default_ingestion_config")) or {}
    census = await corpus_artifact_census(db, corpus_id)

    try:
        graphify_row = await db["graphify_stage_artifacts"].find_one(
            {"corpus_id": corpus_id},
            {"_id": 0, "release": 1, "output_hash": 1, "stage": 1},
            sort=[("updated_at", -1)],
        )
    except (KeyError, TypeError):
        graphify_row = None

    route_readiness: dict[str, Any] = {}
    for route in CERTIFICATE_ROUTES:
        decision = decide_corpus_route_from_census(census, route=route)
        route_readiness[route] = decision_payload(decision)

    body: dict[str, Any] = {
        "schema_version": CORPUS_CERTIFICATE_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "corpus_name": (corpus or {}).get("name"),
        "corpus_generation": (corpus or {}).get("generation"),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "source_census": census["counts"],
        "present_artifacts": census["present"],
        "graphify_release": {
            "extractor_engine": "graphify_cpu",
            "pipeline_release": PIPELINE_RELEASE,
            "provider_release": PROVIDER_RELEASE,
            "provider_release_hash": provider_release_hash(),
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "latest_stage": (graphify_row or {}).get("stage"),
            "latest_stage_release": (graphify_row or {}).get("release"),
            "latest_output_hash": (graphify_row or {}).get("output_hash"),
        },
        "ontology_release": {
            "reducer_release": REDUCER_RELEASE,
            "provider_release_hash": provider_release_hash(),
        },
        "acceptance_policy_release": {
            "relation_release": RELATION_RELEASE,
        },
        "summary_algorithm_release": {
            "product": "deterministic_summary.v1",
            "model": "deterministic:v1",
            "provider_required": False,
        },
        "embedding_release": {
            "embedding_model_id": cfg.get("embedding_model_id"),
            "embedding_model": cfg.get("embedding_model"),
            "embedding_dimension": cfg.get("embedding_dimension"),
            "embed_mode": cfg.get("embed_mode"),
        },
        "qdrant_projection_release": {
            "queryable_documents": census["counts"]["queryable"],
            "rebuildable": True,
        },
        "graph_projection_release": (
            {
                "promoted_documents": census["counts"]["graph_promoted"],
                "release_gate": str(get_settings().GRAPH_PROMOTION_RELEASE_GATE),
                "active_release": census["release_pin"] is not None,
                "registry_reason": census["release_reason"],
            }
            if census["counts"]["graph_promoted"] > 0
            else None
        ),
        "route_readiness": route_readiness,
    }
    certificate_id = "cert_" + hashlib.sha256(_canonical_json(body)).hexdigest()[:32]
    body["certificate_id"] = certificate_id
    return body
async def materialize_corpus_certificate(db: Any, corpus_id: str) -> dict[str, Any]:
    certificate = await build_corpus_certificate(db, corpus_id)
    await db["corpus_certificates"].replace_one(
        {"corpus_id": corpus_id}, certificate, upsert=True
    )
    return certificate
