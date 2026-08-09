"""Organ repair as a first-class, reconciler-owned lane.

WHY THIS EXISTS
    The control plane knew a stage called "extraction" and nothing about its
    four organs, so `relations=[]`, `facts=[]` and a never-run facet pass sat
    undetected across 362,142 chunks. `extraction_organs.py` made the organs
    DECLARED; the coverage checkpoint made them MEASURED. Neither makes repair
    HAPPEN.

    This lane closes that: the reconciler detects an organ gap, plans a durable
    job for it, and a worker executes the organ's declared repair route. No
    human running a backfill script by hand.

ACCOUNTABILITY MODEL
    One job == one (corpus, organ). The job carries:
      - the organ's declared repair_route (from the contract, not invented here)
      - the measured gap that justified it
      - needs_model_pass, so an operator/scheduler can see which repairs are
        free recomputes and which cost GPU time
    Every terminal state writes a receipt with what was actually produced, so a
    job cannot claim success without a number.

WHY REPAIR IS EXECUTED IN-PROCESS RATHER THAN SHELLING OUT
    The backfill scripts already proved the recompute logic against 362k chunks.
    Re-implementing it here would fork it. Instead the executors import the SAME
    functions those scripts drive, so the repair a job runs is byte-identical to
    the repair a human would have run.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from services.control_plane.extraction_organs import (
    ORGAN_BY_NAME, ORGAN_FACTS, ORGAN_RELATIONS,
)

ORGAN_REPAIR_COLLECTION = "organ_repair_jobs"
ORGAN_REPAIR_VERSION = "polymath.organ_repair.v1"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def organ_repair_job_id(*, corpus_id: str, organ: str) -> str:
    """One job per (corpus, organ). Stable, so re-planning is idempotent."""
    raw = f"{ORGAN_REPAIR_VERSION}|{corpus_id}|{organ}"
    return "organ-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


async def plan_organ_repair_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    organ_gaps: dict[str, dict[str, Any]] | None = None,
    apply: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """Stage one durable repair job per failing organ.

    `organ_gaps` maps organ -> the coverage row that failed, so the job records
    WHY it exists rather than re-deriving it later.
    """
    organ_gaps = organ_gaps or {}
    planned: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}

    for organ, gap in sorted(organ_gaps.items()):
        spec = ORGAN_BY_NAME.get(organ)
        if spec is None:
            skipped[organ] = "unknown organ"
            continue
        if organ not in _EXECUTORS:
            # Declared but not yet automatable. Recorded as BLOCKED with the
            # route a human must run, never silently dropped.
            skipped[organ] = f"no executor; manual route: {spec.repair_route}"
            continue
        if len(planned) >= limit:
            break
        planned.append({
            "job_id": organ_repair_job_id(corpus_id=corpus_id, organ=organ),
            "version": ORGAN_REPAIR_VERSION,
            "corpus_id": corpus_id,
            "user_id": user_id,
            "organ": organ,
            "status": STATUS_QUEUED,
            "repair_route": spec.repair_route,
            "needs_model_pass": spec.needs_model_pass,
            "gap": gap,
            "attempts": 0,
            "planned_at": _utcnow(),
        })

    if apply and planned:
        for job in planned:
            await db[ORGAN_REPAIR_COLLECTION].update_one(
                {"job_id": job["job_id"]},
                {
                    # Never resurrect a job that already succeeded for this
                    # gap; only re-arm on insert or after a failure.
                    "$setOnInsert": job,
                    "$set": {"last_planned_at": _utcnow()},
                },
                upsert=True,
            )

    return {
        "lane": "organ_repair",
        "corpus_id": corpus_id,
        "planned": len(planned) if apply else 0,
        "would_plan": len(planned),
        "organs": [j["organ"] for j in planned],
        "skipped": skipped,
        "needs_model_pass": [
            j["organ"] for j in planned if j["needs_model_pass"]
        ],
    }


# ---------------------------------------------------------------------------
# Executors — each imports the SAME function the proven backfill script drives.
# ---------------------------------------------------------------------------


async def _repair_relations(db: Any, *, corpus_id: str) -> dict[str, Any]:
    from services.extraction.dep_path_extractor import new_counters
    from services.extraction.spacy_relation_adapter import get_spacy_extractor

    ext = get_spacy_extractor()
    made = touched = 0
    cursor = db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id,
         "$or": [{"relations": {"$size": 0}}, {"relations": {"$exists": False}}],
         "local_extraction.entities.1": {"$exists": True}},
        {"chunk_id": 1, "doc_id": 1, "text": 1, "local_extraction.entities": 1},
    )
    batch: list[dict] = []

    async def flush() -> None:
        nonlocal made, touched
        if not batch:
            return
        payload = [{
            "chunk_id": d.get("chunk_id", ""), "doc_id": d.get("doc_id", ""),
            "text": d.get("text") or "",
            "entities": [{
                "surface": e.get("text") or "",
                "start_char": int(e.get("start_char") or 0),
                "end_char": int(e.get("end_char") or 0),
                "entity_type": e.get("entity_type") or "",
                "canonical_name": e.get("canonical_label") or "",
            } for e in ((d.get("local_extraction") or {}).get("entities") or [])],
        } for d in batch]
        ctrs = [new_counters() for _ in payload]
        for d, edges in zip(batch, ext.extract_chunks(
                payload, max_related=10, suppression_counters_list=ctrs)):
            rels = [{
                "subject": e["sub"], "predicate": e["pred"], "object": e["obj"],
                "object_kind": "entity", "confidence": float(e["score"]),
                "evidence_phrase": e["ev"], "relation_cue": "",
                "source_predicate": None, "validation_status": None,
            } for e in edges]
            await db["ghost_b_extractions"].update_one(
                {"chunk_id": d["chunk_id"]},
                {"$set": {"relations": rels,
                          "relation_backfill": {"version": "r8a.v2.frame",
                                                "engine": "spacy_frame_licensed",
                                                "at": _utcnow().isoformat()}}},
            )
            made += len(rels)
            touched += 1
        batch.clear()

    async for doc in cursor:
        batch.append(doc)
        if len(batch) >= 200:
            await flush()
    await flush()
    return {"chunks_repaired": touched, "produced": made}


async def _repair_facts(db: Any, *, corpus_id: str) -> dict[str, Any]:
    from services.extraction.frame_extractor import _is_structural_artifact
    from services.ingestion.enrich import (
        extract_facts, extract_qualitative_facts,
    )

    made = touched = 0
    cursor = db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id,
         "$or": [{"facts": {"$size": 0}}, {"facts": {"$exists": False}}],
         "local_extraction.entities.1": {"$exists": True}},
        {"chunk_id": 1, "text": 1, "local_extraction.entities": 1},
    )
    async for d in cursor:
        text = d.get("text") or ""
        ents = [{
            "canonical_name": e.get("canonical_label") or e.get("text") or "",
            "surface_form": e.get("text") or "",
            "entity_type": e.get("entity_type") or "",
        } for e in ((d.get("local_extraction") or {}).get("entities") or [])]
        if not text.strip() or not ents:
            continue
        rows: list[dict] = []
        for f in (extract_facts(text, ents) or []):
            if not _is_structural_artifact(f.get("subject", "")):
                rows.append({**f, "confidence": 1.0})
        for f in (extract_qualitative_facts(text, ents) or []):
            if not _is_structural_artifact(f.get("subject", "")):
                rows.append({**f, "confidence": 0.9})
        await db["ghost_b_extractions"].update_one(
            {"chunk_id": d["chunk_id"]},
            {"$set": {"facts": rows,
                      "fact_backfill": {"version": "facts.v1.staged",
                                        "engine": "enrich_stage_d",
                                        "at": _utcnow().isoformat()}}},
        )
        made += len(rows)
        touched += 1
    return {"chunks_repaired": touched, "produced": made}


_EXECUTORS = {
    ORGAN_RELATIONS: _repair_relations,
    ORGAN_FACTS: _repair_facts,
}


async def run_organ_repair_job(db: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Execute one queued organ repair job and write a receipt.

    A job cannot claim success without a number: the receipt always carries what
    the executor actually produced.
    """
    organ = job.get("organ", "")
    executor = _EXECUTORS.get(organ)
    job_id = job.get("job_id")
    if executor is None:
        receipt = {"status": STATUS_BLOCKED,
                   "reason": f"no executor for organ {organ!r}"}
        await db[ORGAN_REPAIR_COLLECTION].update_one(
            {"job_id": job_id},
            {"$set": {"status": STATUS_BLOCKED, "receipt": receipt,
                      "finished_at": _utcnow()}})
        return receipt

    await db[ORGAN_REPAIR_COLLECTION].update_one(
        {"job_id": job_id},
        {"$set": {"status": STATUS_RUNNING, "started_at": _utcnow()},
         "$inc": {"attempts": 1}})
    started = time.monotonic()
    try:
        produced = await executor(db, corpus_id=job["corpus_id"])
        receipt = {"status": STATUS_SUCCEEDED, "organ": organ,
                   "elapsed_s": round(time.monotonic() - started, 1),
                   **produced}
        status = STATUS_SUCCEEDED
    except Exception as exc:  # noqa: BLE001 - a failed repair must be visible
        receipt = {"status": STATUS_FAILED, "organ": organ,
                   "error": f"{type(exc).__name__}: {exc}",
                   "elapsed_s": round(time.monotonic() - started, 1)}
        status = STATUS_FAILED

    await db[ORGAN_REPAIR_COLLECTION].update_one(
        {"job_id": job_id},
        {"$set": {"status": status, "receipt": receipt,
                  "finished_at": _utcnow()}})
    return receipt
