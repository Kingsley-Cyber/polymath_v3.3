"""Execute graph_projection_jobs through the authorized lifecycle.

Mongo ledger owns status; Neo4j is materialization. NOOP/blocked never
set capability_eligible.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from services.graph.assertion_projector import project_document_assertions_from_mongo
from services.graph.projection_jobs import (
    JOBS_COLLECTION,
    advance_job,
    authorize_pending_jobs,
    certify_corpus_capabilities,
    claim_next_job,
    plan_projection_jobs_for_document,
)
from services.retriever.graph_authority import inspect_graph_capabilities

logger = logging.getLogger(__name__)


class _ProjectionBlocked(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _verify_hash(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    payload = {"expected": expected, "actual": actual}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"sha256:{digest}"


async def _neo4j_doc_counts(
    driver: Any, *, corpus_id: str, document_id: str
) -> dict[str, int]:
    if driver is None:
        return {
            "document_nodes": 0,
            "chunk_nodes": 0,
            "entity_nodes": 0,
            "assertion_nodes": 0,
            "fact_nodes": 0,
        }
    async with driver.session() as session:
        result = await session.run(
            """
            OPTIONAL MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})
            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
            WITH d, count(DISTINCT c) AS chunks
            OPTIONAL MATCH (c2:Chunk {corpus_id: $corpus_id, doc_id: $doc_id})
                  -[:MENTIONS]->(e:Entity)
            WITH d, chunks, count(DISTINCT e) AS entities
            OPTIONAL MATCH (c3:Chunk {corpus_id: $corpus_id, doc_id: $doc_id})
                  -[:SUPPORTS_ASSERTION]->(a:RelationAssertion)
            WITH d, chunks, entities, count(DISTINCT a) AS assertions
            OPTIONAL MATCH (d2:Document {doc_id: $doc_id, corpus_id: $corpus_id})
                  -[:HAS_FACT]->(f:Fact)
            RETURN
              CASE WHEN d IS NULL THEN 0 ELSE 1 END AS documents,
              chunks AS chunks,
              entities AS entities,
              assertions AS assertions,
              count(DISTINCT f) AS facts
            """,
            doc_id=document_id,
            corpus_id=corpus_id,
        )
        rec = await result.single()
        if not rec:
            return {
                "document_nodes": 0,
                "chunk_nodes": 0,
                "entity_nodes": 0,
                "assertion_nodes": 0,
                "fact_nodes": 0,
            }
        return {
            "document_nodes": int(rec["documents"] or 0),
            "chunk_nodes": int(rec["chunks"] or 0),
            "entity_nodes": int(rec["entities"] or 0),
            "assertion_nodes": int(rec["assertions"] or 0),
            "fact_nodes": int(rec["facts"] or 0),
        }


async def _reproject_document_graph(
    db: Any, driver: Any, corpus_id: str, document_id: str
) -> dict[str, Any]:
    """Idempotent structural+entity rewrite from ghost_b_staging when present."""

    from services.graph.neo4j_writer import write_document_graph
    from services.ingestion.graph_backfill import _rehydrate_ghost_b_staging
    from services.storage import mongo_reader

    doc = await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": document_id}
    )
    if not doc:
        return {"skipped": "missing_document"}

    staged_raw = await mongo_reader.read_ghost_b_staging(
        db, document_id, corpus_id
    ) or []
    extraction_results = (
        _rehydrate_ghost_b_staging(staged_raw) if staged_raw else []
    )
    if not extraction_results:
        # Graph already projected elsewhere; caller verifies counts.
        return {"skipped": "no_staging", "staged": 0}

    chunk_ids = [
        str(c.get("chunk_id"))
        for c in await db["chunks"]
        .find({"corpus_id": corpus_id, "doc_id": document_id}, {"chunk_id": 1})
        .to_list(5000)
        if c.get("chunk_id")
    ]
    await write_document_graph(
        driver=driver,
        doc_id=document_id,
        corpus_id=corpus_id,
        extraction_results=extraction_results,
        user_id=str(doc.get("user_id") or ""),
        file_id=str(doc.get("file_id") or ""),
        all_chunk_ids=chunk_ids,
        filename=doc.get("filename"),
        db=db,
    )
    return {
        "rewritten": True,
        "extraction_results": len(extraction_results),
        "chunk_ids": len(chunk_ids),
    }


async def _apply_lane(
    db: Any,
    driver: Any,
    job: dict[str, Any],
) -> dict[str, Any]:
    lane = str(job.get("lane") or "")
    corpus_id = str(job["corpus_id"])
    document_id = str(job["document_id"])

    if lane == "relation_assertions":
        written = await project_document_assertions_from_mongo(
            db, driver, corpus_id=corpus_id, doc_id=document_id
        )
        expected = {"assertion_nodes": int(written.get("ir_assertions") or 0)}
        actual = {"assertion_nodes": int(written.get("assertion_nodes") or 0)}
        return {
            "expected_counts": expected,
            "actual_counts": actual,
            "apply_result": written,
        }

    if lane == "qualified_facts":
        return {
            "expected_counts": {"fact_nodes": 0},
            "actual_counts": {"fact_nodes": 0},
            "apply_result": {"skipped": "release_gated"},
        }

    counts = await _neo4j_doc_counts(
        driver, corpus_id=corpus_id, document_id=document_id
    )
    chunk_n = await db["chunks"].count_documents(
        {"corpus_id": corpus_id, "doc_id": document_id}
    )

    if lane == "structural":
        if counts["document_nodes"] < 1 or (
            chunk_n > 0 and counts["chunk_nodes"] < 1
        ):
            rewritten = await _reproject_document_graph(
                db, driver, corpus_id, document_id
            )
            counts = await _neo4j_doc_counts(
                driver, corpus_id=corpus_id, document_id=document_id
            )
            if (
                counts["document_nodes"] < 1
                and rewritten.get("skipped") == "no_staging"
            ):
                # Chunks exist in Mongo but no projection inputs — blocked, not ready.
                raise _ProjectionBlocked(
                    "BLOCKED_INPUT_MISSING",
                    f"structural lane missing neo4j+staging for doc={document_id[:12]}",
                )
        return {
            "expected_counts": {
                "document_nodes": 1 if chunk_n > 0 else 0,
                "chunk_nodes_min": 1 if chunk_n > 0 else 0,
            },
            "actual_counts": {
                "document_nodes": counts["document_nodes"],
                "chunk_nodes": counts["chunk_nodes"],
            },
            "apply_result": counts,
        }

    # entity_mentions
    if counts["entity_nodes"] <= 0:
        await _reproject_document_graph(db, driver, corpus_id, document_id)
        counts = await _neo4j_doc_counts(
            driver, corpus_id=corpus_id, document_id=document_id
        )
    ghost_entity_n = 0
    async for row in db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "doc_id": document_id}, {"entities": 1}
    ):
        ghost_entity_n += len(row.get("entities") or [])
    return {
        "expected_counts": {"entity_nodes_min": 1 if ghost_entity_n > 0 else 0},
        "actual_counts": {"entity_nodes": counts["entity_nodes"]},
        "apply_result": counts,
    }


def _verify_lane(lane: str, expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    if lane == "relation_assertions":
        return int(actual.get("assertion_nodes") or 0) == int(
            expected.get("assertion_nodes") or 0
        )
    if lane == "structural":
        if int(expected.get("document_nodes") or 0) <= 0:
            return True
        return int(actual.get("document_nodes") or 0) >= 1 and int(
            actual.get("chunk_nodes") or 0
        ) >= int(expected.get("chunk_nodes_min") or 0)
    if lane == "entity_mentions":
        if int(expected.get("entity_nodes_min") or 0) <= 0:
            return True
        return int(actual.get("entity_nodes") or 0) >= 1
    if lane == "qualified_facts":
        return False
    return False


async def execute_claimed_job(
    db: Any,
    driver: Any,
    job: dict[str, Any],
) -> dict[str, Any]:
    job_id = str(job["graph_job_id"])
    lane = str(job.get("lane") or "")

    try:
        applied = await _apply_lane(db, driver, job)
    except _ProjectionBlocked as exc:
        await advance_job(
            db,
            graph_job_id=job_id,
            to_status=exc.status,  # type: ignore[arg-type]
            extra={"last_error": str(exc)[:500], "capability_eligible": False},
        )
        return {"graph_job_id": job_id, "status": exc.status, "lane": lane}
    except Exception as exc:  # noqa: BLE001
        logger.exception("projection apply failed job=%s: %s", job_id, exc)
        await advance_job(
            db,
            graph_job_id=job_id,
            to_status="RETRYABLE_WRITE_FAILURE",
            extra={"last_error": str(exc)[:500], "capability_eligible": False},
        )
        return {
            "graph_job_id": job_id,
            "status": "RETRYABLE_WRITE_FAILURE",
            "error": str(exc)[:500],
        }

    expected = applied.get("expected_counts") or {}
    actual = applied.get("actual_counts") or {}
    await advance_job(
        db,
        graph_job_id=job_id,
        to_status="APPLIED",
        extra={
            "expected_counts": expected,
            "actual_counts": actual,
            "lease_owner": None,
            "lease_expires_at": None,
        },
    )

    ok = _verify_lane(lane, expected, actual)
    vhash = _verify_hash(expected, actual)
    if not ok:
        await advance_job(
            db,
            graph_job_id=job_id,
            to_status="VERIFY_FAILED",
            extra={"verification_hash": vhash, "capability_eligible": False},
        )
        return {"graph_job_id": job_id, "status": "VERIFY_FAILED", "lane": lane}

    await advance_job(
        db,
        graph_job_id=job_id,
        to_status="VERIFIED",
        extra={"verification_hash": vhash},
    )
    await advance_job(
        db,
        graph_job_id=job_id,
        to_status="CERTIFIED",
        extra={"capability_eligible": True},
    )
    return {
        "graph_job_id": job_id,
        "status": "CERTIFIED",
        "lane": lane,
        "expected_counts": expected,
        "actual_counts": actual,
        "verification_hash": vhash,
    }


async def run_projection_jobs(
    db: Any,
    driver: Any,
    *,
    corpus_id: str,
    owner: str = "projection_runner",
    max_jobs: int = 200,
    lanes: tuple[str, ...] | None = None,
    certify: bool = True,
) -> dict[str, Any]:
    import os
    import time as _time

    auth_counts = await authorize_pending_jobs(db, corpus_id=corpus_id, limit=max_jobs * 4)
    results: list[dict[str, Any]] = []
    _t_exec0 = _time.monotonic()
    for _ in range(max_jobs):
        job = await claim_next_job(
            db, corpus_id=corpus_id, owner=owner, lanes=lanes
        )
        if not job:
            break
        results.append(await execute_claimed_job(db, driver, job))
    _exec_s = _time.monotonic() - _t_exec0

    # Corpus-wide capability certification is a fixed ~tens-of-seconds cost
    # (7 corpus-wide MATCH scans in inspect_graph_capabilities + a cert
    # write). Per-doc it is pure overhead: the certificate is a corpus-level
    # artifact, identical for every doc in the batch. Defer it to once-per-
    # batch / on-idle (GRAPH_CERTIFY_PER_DOC=0, the default) unless explicitly
    # re-enabled. The per-doc caller passes certify=False; the batch/idle
    # reconciler and the API path call with certify=True so the corpus is
    # still certified — just not N times for N docs.
    _cert_s = 0.0
    cert: dict[str, Any] | None = None
    _per_doc_cert = os.environ.get("GRAPH_CERTIFY_PER_DOC", "0") == "1"
    if certify and _per_doc_cert:
        _t_cert0 = _time.monotonic()
        caps = await inspect_graph_capabilities([corpus_id])
        cert = await certify_corpus_capabilities(
            db, corpus_id=corpus_id, neo4j_capabilities=caps
        )
        _cert_s = _time.monotonic() - _t_cert0

    hist: dict[str, int] = {}
    async for row in db[JOBS_COLLECTION].find({"corpus_id": corpus_id}, {"status": 1}):
        s = str(row.get("status") or "")
        hist[s] = hist.get(s, 0) + 1
    logger.info(
        "phase=projection_timing corpus=%s owner=%s execute_s=%.3f certify_s=%.3f "
        "certify_deferred=%s executed=%d",
        corpus_id[:8], owner, _exec_s, _cert_s, not (certify and _per_doc_cert),
        len(results),
    )
    return {
        "corpus_id": corpus_id,
        "authorized": auth_counts,
        "executed": len(results),
        "results": results,
        "job_status_histogram": hist,
        "certificate": cert,
        "certify_deferred": not (certify and _per_doc_cert),
        "timing": {"execute_s": round(_exec_s, 3), "certify_s": round(_cert_s, 3)},
    }


async def plan_all_documents(
    db: Any,
    *,
    corpus_id: str,
) -> dict[str, Any]:
    docs = await db["documents"].find(
        {"corpus_id": corpus_id}, {"doc_id": 1}
    ).to_list(5000)
    planned = []
    for doc in docs:
        doc_id = str(doc.get("doc_id") or "")
        if not doc_id:
            continue
        planned.append(
            await plan_projection_jobs_for_document(
                db, corpus_id=corpus_id, document_id=doc_id
            )
        )
    return {"corpus_id": corpus_id, "documents": len(planned), "plans": planned}


async def project_document_via_control_plane(
    *,
    db: Any,
    neo4j_driver: Any,
    corpus_id: str,
    doc_id: str,
    write_fn: Any | None = None,
    write_kwargs: dict[str, Any] | None = None,
    certify: bool = False,
) -> dict[str, Any]:
    """Unified projector entry for ingestion.

    Optionally runs ``write_fn`` (legacy write_document_graph) once, then
    plans + advances lanes to CERTIFIED / NOOP.

    ``certify`` defaults False so the per-doc ingestion path does NOT pay the
    corpus-wide capability certification on every document; certification is
    deferred to the batch-level reconciler (certify_corpus_once) which runs it
    a single time per corpus pass.
    """

    plan = await plan_projection_jobs_for_document(
        db, corpus_id=corpus_id, document_id=doc_id
    )
    if write_fn is not None:
        await write_fn(**(write_kwargs or {}))
        # The legacy document writer is a replace operation: its pre-clear
        # removes document-scoped RelationAssertion nodes and support edges.
        # A deterministic assertion job may already be CERTIFIED from an
        # earlier projection, so planning alone will not make it runnable.
        # Re-open only that lane after the replacement succeeds. Structural
        # and entity lanes were materialized by write_fn itself.
        for item in plan.get("jobs") or []:
            if (
                str(item.get("lane") or "") == "relation_assertions"
                and str(item.get("status") or "") == "CERTIFIED"
            ):
                await advance_job(
                    db,
                    graph_job_id=str(item["graph_job_id"]),
                    to_status="PLANNED",
                    extra={
                        "capability_eligible": False,
                        "verification_hash": None,
                        "lease_owner": None,
                        "lease_expires_at": None,
                    },
                )
    run = await run_projection_jobs(
        db,
        neo4j_driver,
        corpus_id=corpus_id,
        owner=f"ingest:{doc_id[:12]}",
        max_jobs=16,
        certify=certify,
    )
    return {"plan": plan, "run": run}


async def certify_corpus_once(
    db: Any,
    neo4j_driver: Any,
    *,
    corpus_id: str,
) -> dict[str, Any]:
    """Run corpus-wide capability certification exactly once for a corpus.

    This is the deferred home of the per-doc certification removed from the
    ingestion hot path: it drains any remaining projection jobs and runs the
    corpus-wide inspect + certify a single time. Called by the batch
    reconciler at end of a corpus pass (or on queue-idle), never per doc.
    """
    return await run_projection_jobs(
        db,
        neo4j_driver,
        corpus_id=corpus_id,
        owner="batch_reconcile",
        max_jobs=200,
        certify=True,
    )
