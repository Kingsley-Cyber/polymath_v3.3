"""Deterministic graph_projection_jobs control plane.

Mongo is the ledger; Neo4j is materialization only. Noop/abandoned/blocked jobs
must never advertise graph capability.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pymongo import ReturnDocument

from models.graph_projection_ir import ProjectionLane

logger = logging.getLogger(__name__)

JOBS_COLLECTION = "graph_projection_jobs"
CERTS_COLLECTION = "graph_capability_certificates"
PROJECTION_RELEASE = "polymath.graph_projection.v1"
ONTOLOGY_RELEASE_DEFAULT = "ontology-runtime-unpinned"

JobStatus = Literal[
    "PLANNED",
    "INPUTS_VALIDATED",
    "ONTOLOGY_RESOLVED",
    "AUTHORIZED",
    "APPLYING",
    "APPLIED",
    "VERIFIED",
    "CERTIFIED",
    "NOOP_NO_ELIGIBLE_ARTIFACTS",
    "ABANDONED_SUPERSEDED",
    "ABANDONED_SOURCE_DELETED",
    "BLOCKED_INPUT_MISSING",
    "BLOCKED_CONTRACT_MISMATCH",
    "BLOCKED_ONTOLOGY_UNRESOLVED",
    "BLOCKED_RELEASE_PIN",
    "VERIFY_FAILED",
    "DEAD_LETTER",
    "RETRYABLE_WRITE_FAILURE",
]

SUCCESS_TERMINAL = frozenset({"CERTIFIED", "NOOP_NO_ELIGIBLE_ARTIFACTS"})
FAILURE_TERMINAL = frozenset(
    {
        "ABANDONED_SUPERSEDED",
        "ABANDONED_SOURCE_DELETED",
        "BLOCKED_INPUT_MISSING",
        "BLOCKED_CONTRACT_MISMATCH",
        "BLOCKED_ONTOLOGY_UNRESOLVED",
        "BLOCKED_RELEASE_PIN",
        "VERIFY_FAILED",
        "DEAD_LETTER",
    }
)
TERMINAL = SUCCESS_TERMINAL | FAILURE_TERMINAL
CAPABILITY_ELIGIBLE = frozenset({"CERTIFIED"})  # never NOOP/blocked/abandoned

LANES: tuple[ProjectionLane, ...] = (
    "structural",
    "entity_mentions",
    "relation_assertions",
    "qualified_facts",
)

OrphanClass = Literal[
    "recoverable_projection",
    "missing_extraction",
    "superseded_generation",
    "deleted_document",
    "legacy_engine_or_contract",
    "corrupt_or_ambiguous",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_hex(parts: list[str]) -> str:
    raw = "\n".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def graph_job_id(
    *,
    corpus_id: str,
    corpus_generation: str,
    document_id: str,
    projection_lane: str,
    input_artifact_hash: str,
    ontology_release: str,
    projection_release: str = PROJECTION_RELEASE,
) -> str:
    digest = _sha256_hex(
        [
            corpus_id,
            corpus_generation,
            document_id,
            projection_lane,
            input_artifact_hash,
            ontology_release,
            projection_release,
        ]
    )
    return f"gproj_{digest[:32]}"


def input_artifact_hash_for_doc(rows: list[dict[str, Any]]) -> str:
    """Stable hash over extraction artifact identities for one document."""

    identities: list[dict[str, Any]] = []
    for row in rows:
        identities.append(
            {
                "chunk_id": str(row.get("chunk_id") or ""),
                "chunk_hash": str(row.get("chunk_hash") or ""),
                "extraction_contract_hash": str(
                    row.get("extraction_contract_hash") or ""
                ),
                "raw_fp": row.get("raw_output_fingerprint") or {},
                "status": str(row.get("status") or ""),
                "entity_n": len(row.get("entities") or []),
                "relation_n": len(row.get("relations") or []),
                "fact_n": len(row.get("facts") or []),
            }
        )
    identities.sort(key=lambda r: r["chunk_id"])
    return _sha256_hex(
        [json.dumps(identities, sort_keys=True, separators=(",", ":"), default=str)]
    )


async def ensure_projection_job_indexes(db: Any) -> None:
    try:
        await db[JOBS_COLLECTION].create_index("graph_job_id", unique=True)
        await db[JOBS_COLLECTION].create_index(
            [("corpus_id", 1), ("document_id", 1), ("lane", 1)]
        )
        await db[JOBS_COLLECTION].create_index([("corpus_id", 1), ("status", 1)])
        await db[JOBS_COLLECTION].create_index(
            [("status", 1), ("lease_expires_at", 1)]
        )
        await db[CERTS_COLLECTION].create_index(
            [("corpus_id", 1), ("graph_generation", 1)], unique=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_projection_jobs index ensure failed: %s", exc)


def _corpus_generation(corpus: dict[str, Any] | None, corpus_id: str) -> str:
    if not corpus:
        return f"gen:{corpus_id}:unknown"
    for key in ("corpus_generation", "generation", "content_generation"):
        value = corpus.get(key)
        if value:
            return str(value)
    updated = corpus.get("updated_at") or corpus.get("created_at") or ""
    return f"gen:{corpus_id}:{updated}"


async def plan_projection_jobs_for_document(
    db: Any,
    *,
    corpus_id: str,
    document_id: str,
    ontology_release: str = ONTOLOGY_RELEASE_DEFAULT,
    lanes: tuple[ProjectionLane, ...] | None = None,
) -> dict[str, Any]:
    """Upsert deterministic jobs for the selected lanes (idempotent)."""

    await ensure_projection_job_indexes(db)
    corpus = await db["corpora"].find_one({"corpus_id": corpus_id})
    doc = await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": document_id}
    )
    if not doc:
        return {
            "status": "BLOCKED_INPUT_MISSING",
            "document_id": document_id,
            "jobs": [],
        }
    if str(doc.get("status") or "").lower() in {"deleted", "purged", "removed"}:
        return {
            "status": "ABANDONED_SOURCE_DELETED",
            "document_id": document_id,
            "jobs": [],
        }

    ghost_rows = await db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "doc_id": document_id}
    ).to_list(5000)
    chunk_count = await db["chunks"].count_documents(
        {"corpus_id": corpus_id, "doc_id": document_id}
    )
    artifact_hash = input_artifact_hash_for_doc(ghost_rows)
    generation = _corpus_generation(corpus, corpus_id)
    planned: list[dict[str, Any]] = []
    now = _utcnow()

    for lane in lanes or LANES:
        job_id = graph_job_id(
            corpus_id=corpus_id,
            corpus_generation=generation,
            document_id=document_id,
            projection_lane=lane,
            input_artifact_hash=artifact_hash,
            ontology_release=ontology_release,
        )
        # Eligibility snapshot (authorization happens later).
        eligible = True
        terminal_hint: JobStatus | None = None
        if lane == "structural":
            if chunk_count <= 0:
                eligible = False
                terminal_hint = "NOOP_NO_ELIGIBLE_ARTIFACTS"
        elif lane == "entity_mentions":
            entity_n = sum(len(r.get("entities") or []) for r in ghost_rows)
            if entity_n <= 0:
                eligible = False
                terminal_hint = "NOOP_NO_ELIGIBLE_ARTIFACTS"
        elif lane == "relation_assertions":
            rel_n = sum(len(r.get("relations") or []) for r in ghost_rows)
            if rel_n <= 0:
                eligible = False
                terminal_hint = "NOOP_NO_ELIGIBLE_ARTIFACTS"
        elif lane == "qualified_facts":
            fact_n = sum(len(r.get("facts") or []) for r in ghost_rows)
            if fact_n <= 0:
                eligible = False
                terminal_hint = "NOOP_NO_ELIGIBLE_ARTIFACTS"

        initial_status: JobStatus = (
            terminal_hint if (not eligible and terminal_hint) else "PLANNED"
        )
        doc_row = {
            "graph_job_id": job_id,
            "corpus_id": corpus_id,
            "corpus_generation": generation,
            "document_id": document_id,
            "lane": lane,
            "input_artifact_ids": [
                str(r.get("chunk_id") or "") for r in ghost_rows if r.get("chunk_id")
            ],
            "input_artifact_hash": artifact_hash,
            "extraction_contract_hash": str(
                (ghost_rows[0] or {}).get("extraction_contract_hash") or ""
                if ghost_rows
                else ""
            ),
            "ontology_release": ontology_release,
            "acceptance_policy_release": "",
            "extractor_release": str(
                ((ghost_rows[0] or {}).get("local_extraction") or {}).get(
                    "extractor_release"
                )
                or ""
                if ghost_rows
                else ""
            ),
            "projection_release": PROJECTION_RELEASE,
            "status": initial_status,
            "attempt": 0,
            "lease_owner": None,
            "lease_expires_at": None,
            "authorization_record": {
                "eligible": eligible,
                "chunk_count": chunk_count,
                "ghost_rows": len(ghost_rows),
            },
            "expected_counts": {},
            "actual_counts": {},
            "verification_hash": None,
            "capability_eligible": False,  # only CERTIFIED flips true
            "updated_at": now,
            "created_at": now,
        }
        existing = await db[JOBS_COLLECTION].find_one({"graph_job_id": job_id})
        if existing:
            # Idempotent: never spawn a second job; refresh non-terminal fields only.
            if str(existing.get("status")) not in TERMINAL:
                await db[JOBS_COLLECTION].update_one(
                    {"graph_job_id": job_id},
                    {
                        "$set": {
                            "authorization_record": doc_row["authorization_record"],
                            "input_artifact_hash": artifact_hash,
                            "updated_at": now,
                        }
                    },
                )
            planned.append({"graph_job_id": job_id, "lane": lane, "status": existing.get("status"), "upsert": False})
        else:
            await db[JOBS_COLLECTION].insert_one(doc_row)
            planned.append(
                {
                    "graph_job_id": job_id,
                    "lane": lane,
                    "status": initial_status,
                    "upsert": True,
                }
            )
    return {
        "status": "planned",
        "document_id": document_id,
        "input_artifact_hash": artifact_hash,
        "jobs": planned,
    }


async def advance_job(
    db: Any,
    *,
    graph_job_id: str,
    to_status: JobStatus,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utcnow()
    sets: dict[str, Any] = {"status": to_status, "updated_at": now}
    if extra:
        sets.update(extra)
    if to_status == "CERTIFIED":
        sets["capability_eligible"] = True
        sets["certified_at"] = now
    elif to_status in TERMINAL:
        sets["capability_eligible"] = False
    result = await db[JOBS_COLLECTION].find_one_and_update(
        {"graph_job_id": graph_job_id},
        {"$set": sets, "$inc": {"attempt": 1 if to_status == "APPLYING" else 0}},
        return_document=ReturnDocument.AFTER,
    )
    return result or {}


async def authorize_job(db: Any, job: dict[str, Any]) -> str:
    """Advance one job PLANNED → AUTHORIZED or a terminal non-success."""

    status = str(job.get("status") or "")
    if status in TERMINAL or status in {
        "AUTHORIZED",
        "APPLYING",
        "APPLIED",
        "VERIFIED",
        "CERTIFIED",
    }:
        return status

    job_id = str(job["graph_job_id"])
    corpus_id = str(job["corpus_id"])
    document_id = str(job["document_id"])
    lane = str(job.get("lane") or "")

    doc = await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": document_id}
    )
    if not doc or str(doc.get("status") or "").lower() in {
        "deleted",
        "purged",
        "removed",
    }:
        await advance_job(
            db, graph_job_id=job_id, to_status="ABANDONED_SOURCE_DELETED"
        )
        return "ABANDONED_SOURCE_DELETED"

    ghost_n = await db["ghost_b_extractions"].count_documents(
        {"corpus_id": corpus_id, "doc_id": document_id}
    )
    chunk_n = await db["chunks"].count_documents(
        {"corpus_id": corpus_id, "doc_id": document_id}
    )

    auth = dict(job.get("authorization_record") or {})
    auth.update({"chunk_count": chunk_n, "ghost_rows": ghost_n, "eligible": True})
    await advance_job(
        db,
        graph_job_id=job_id,
        to_status="INPUTS_VALIDATED",
        extra={"authorization_record": auth},
    )

    if lane == "structural" and chunk_n <= 0:
        await advance_job(
            db, graph_job_id=job_id, to_status="NOOP_NO_ELIGIBLE_ARTIFACTS"
        )
        return "NOOP_NO_ELIGIBLE_ARTIFACTS"
    if lane == "entity_mentions" and ghost_n <= 0:
        await advance_job(
            db, graph_job_id=job_id, to_status="NOOP_NO_ELIGIBLE_ARTIFACTS"
        )
        return "NOOP_NO_ELIGIBLE_ARTIFACTS"
    if lane == "relation_assertions":
        rel_n = 0
        async for row in db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id, "doc_id": document_id}, {"relations": 1}
        ):
            rel_n += len(row.get("relations") or [])
        if rel_n <= 0:
            await advance_job(
                db, graph_job_id=job_id, to_status="NOOP_NO_ELIGIBLE_ARTIFACTS"
            )
            return "NOOP_NO_ELIGIBLE_ARTIFACTS"
    if lane == "qualified_facts":
        facts = 0
        async for row in db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id, "doc_id": document_id}, {"facts": 1}
        ):
            facts += len(row.get("facts") or [])
        if facts <= 0:
            await advance_job(
                db, graph_job_id=job_id, to_status="NOOP_NO_ELIGIBLE_ARTIFACTS"
            )
            return "NOOP_NO_ELIGIBLE_ARTIFACTS"
        await advance_job(db, graph_job_id=job_id, to_status="BLOCKED_RELEASE_PIN")
        return "BLOCKED_RELEASE_PIN"

    await advance_job(db, graph_job_id=job_id, to_status="ONTOLOGY_RESOLVED")
    await advance_job(db, graph_job_id=job_id, to_status="AUTHORIZED")
    return "AUTHORIZED"


async def authorize_pending_jobs(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 500,
) -> dict[str, int]:
    """Advance PLANNED… → AUTHORIZED or a terminal blocked/noop/abandoned."""

    cursor = (
        db[JOBS_COLLECTION]
        .find(
            {
                "corpus_id": corpus_id,
                "status": {
                    "$in": [
                        "PLANNED",
                        "INPUTS_VALIDATED",
                        "ONTOLOGY_RESOLVED",
                    ]
                },
            }
        )
        .limit(limit)
    )
    counts: dict[str, int] = {}
    async for job in cursor:
        status = await authorize_job(db, job)
        counts[status] = counts.get(status, 0) + 1
    return counts


async def claim_next_job(
    db: Any,
    *,
    corpus_id: str | None = None,
    owner: str,
    lease_seconds: int = 120,
    lanes: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """Claim one AUTHORIZED job for APPLYING (restart-safe lease)."""

    now = _utcnow()
    # Recover expired leases first.
    await db[JOBS_COLLECTION].update_many(
        {
            "status": "APPLYING",
            "lease_expires_at": {"$lt": now},
        },
        {
            "$set": {
                "status": "AUTHORIZED",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )
    query: dict[str, Any] = {"status": "AUTHORIZED"}
    if corpus_id:
        query["corpus_id"] = corpus_id
    if lanes:
        query["lane"] = {"$in": list(lanes)}
    job = await db[JOBS_COLLECTION].find_one_and_update(
        query,
        {
            "$set": {
                "status": "APPLYING",
                "lease_owner": owner,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            },
            "$inc": {"attempt": 1},
        },
        sort=[("updated_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return job


def classify_extraction_orphan(
    *,
    job: dict[str, Any],
    document: dict[str, Any] | None,
    ghost_row: dict[str, Any] | None,
    corpus_generation: str,
    job_generation: str | None,
) -> tuple[OrphanClass, str]:
    """Deterministic orphan class for stuck extraction_jobs (not projection jobs)."""

    doc_status = str((document or {}).get("status") or "").lower()
    if document is None or doc_status in {"deleted", "purged", "removed"}:
        return "deleted_document", "abandon_source_deleted"

    if job_generation and corpus_generation and job_generation != corpus_generation:
        return "superseded_generation", "abandon_superseded"

    if ghost_row:
        # Artifact exists — projection/control-plane recovery, not re-extract.
        return "recoverable_projection", "requeue_projection_only"

    # Active doc, no ghost_b row for this chunk.
    engine = str(job.get("engine") or job.get("extractor_engine") or "").strip()
    contract = str(job.get("extraction_contract_hash") or "").strip()
    if engine and engine not in {"graphify_cpu", "graphify_gliner2_cpu", ""}:
        return "legacy_engine_or_contract", "dry_run_report_and_review"
    # Bare sha256 contract hashes are current Graphify fingerprints.
    looks_like_hash = len(contract) in {40, 64} and all(
        c in "0123456789abcdef" for c in contract.lower()
    )
    if (
        contract
        and not looks_like_hash
        and "graphify" not in contract.lower()
        and contract not in {"", "None"}
    ):
        return "legacy_engine_or_contract", "dry_run_report_and_review"

    attempts = int(job.get("attempts") or 0)
    if attempts >= 5:
        return "corrupt_or_ambiguous", "dead_letter"

    return "missing_extraction", "enqueue_bounded_reextraction"


async def reconcile_stuck_extraction_orphans(
    db: Any,
    *,
    corpus_id: str,
    apply: bool = True,
    bounded_reextract_limit: int = 0,
    include_queued_without_artifact: bool = True,
) -> dict[str, Any]:
    """Classify stuck_orphan extraction jobs; never blind full re-extract.

    Default: requeue projection for recoverable; abandon deleted/superseded;
    dead-letter corrupt; report missing_extraction / legacy for review.
    ``bounded_reextract_limit=0`` means do not enqueue re-extraction.

    Also classifies ``queued`` jobs with no ghost_b artifact when
    ``include_queued_without_artifact`` is true (covers populations that were
    blunt-marked stuck_orphan then reset to queued).
    """

    corpus = await db["corpora"].find_one({"corpus_id": corpus_id})
    generation = _corpus_generation(corpus, corpus_id)
    status_filter: list[str] = ["stuck_orphan"]
    if include_queued_without_artifact:
        status_filter.append("queued")
    stuck = await db["extraction_jobs"].find(
        {"corpus_id": corpus_id, "status": {"$in": status_filter}}
    ).to_list(10000)

    # Batch-load documents + ghost chunk ids (avoid N+1).
    doc_ids = sorted({str(j.get("doc_id") or "") for j in stuck if j.get("doc_id")})
    docs_by_id: dict[str, dict[str, Any]] = {}
    if doc_ids:
        async for d in db["documents"].find(
            {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}}
        ):
            docs_by_id[str(d.get("doc_id") or "")] = d
    ghost_chunk_ids: set[str] = set()
    async for g in db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id}, {"chunk_id": 1}
    ):
        if g.get("chunk_id"):
            ghost_chunk_ids.add(str(g["chunk_id"]))

    classes: dict[str, int] = {}
    dispositions: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    now = _utcnow()
    reextract_budget = max(0, int(bounded_reextract_limit))
    planned_docs: set[str] = set()

    for job in stuck:
        # Skip healthy queued jobs that already have extraction artifacts.
        chunk_id = str(job.get("chunk_id") or "")
        if (
            str(job.get("status")) == "queued"
            and chunk_id
            and chunk_id in ghost_chunk_ids
        ):
            continue
        doc_id = str(job.get("doc_id") or "")
        document = docs_by_id.get(doc_id)
        ghost = {"chunk_id": chunk_id} if chunk_id and chunk_id in ghost_chunk_ids else None
        job_gen = str(job.get("corpus_generation") or "") or None
        klass, action = classify_extraction_orphan(
            job=job,
            document=document,
            ghost_row=ghost,
            corpus_generation=generation,
            job_generation=job_gen,
        )
        classes[klass] = classes.get(klass, 0) + 1
        applied_action = action
        if (
            action == "enqueue_bounded_reextraction"
            and reextract_budget <= 0
        ):
            applied_action = "hold_no_blind_reextract"
        dispositions[applied_action] = dispositions.get(applied_action, 0) + 1
        samples.setdefault(klass, [])
        if len(samples[klass]) < 5:
            samples[klass].append(chunk_id or str(job.get("_id")))

        if not apply:
            continue

        if action == "requeue_projection_only":
            await db["extraction_jobs"].update_one(
                {"_id": job["_id"]},
                {
                    "$set": {
                        "status": "projection_requeued",
                        "orphan_class": klass,
                        "orphan_action": action,
                        "updated_at": now,
                    }
                },
            )
            if doc_id and doc_id not in planned_docs:
                await plan_projection_jobs_for_document(
                    db, corpus_id=corpus_id, document_id=doc_id
                )
                planned_docs.add(doc_id)
        elif action == "abandon_source_deleted":
            await db["extraction_jobs"].update_one(
                {"_id": job["_id"]},
                {
                    "$set": {
                        "status": "ABANDONED_SOURCE_DELETED",
                        "orphan_class": klass,
                        "orphan_action": action,
                        "updated_at": now,
                    }
                },
            )
        elif action == "abandon_superseded":
            await db["extraction_jobs"].update_one(
                {"_id": job["_id"]},
                {
                    "$set": {
                        "status": "ABANDONED_SUPERSEDED",
                        "orphan_class": klass,
                        "orphan_action": action,
                        "updated_at": now,
                    }
                },
            )
        elif action == "dead_letter":
            await db["extraction_jobs"].update_one(
                {"_id": job["_id"]},
                {
                    "$set": {
                        "status": "DEAD_LETTER",
                        "orphan_class": klass,
                        "orphan_action": action,
                        "updated_at": now,
                    }
                },
            )
        elif action == "enqueue_bounded_reextraction":
            if reextract_budget > 0:
                reextract_budget -= 1
                await db["extraction_jobs"].update_one(
                    {"_id": job["_id"]},
                    {
                        "$set": {
                            "status": "queued",
                            "orphan_class": klass,
                            "orphan_action": action,
                            "attempts": 0,
                            "updated_at": now,
                        }
                    },
                )
            else:
                await db["extraction_jobs"].update_one(
                    {"_id": job["_id"]},
                    {
                        "$set": {
                            "status": "REVIEW_MISSING_EXTRACTION",
                            "orphan_class": klass,
                            "orphan_action": "hold_no_blind_reextract",
                            "updated_at": now,
                        }
                    },
                )
        else:  # dry_run_report_and_review
            await db["extraction_jobs"].update_one(
                {"_id": job["_id"]},
                {
                    "$set": {
                        "status": "REVIEW_LEGACY_CONTRACT",
                        "orphan_class": klass,
                        "orphan_action": action,
                        "updated_at": now,
                    }
                },
            )

    remaining_stuck = await db["extraction_jobs"].count_documents(
        {"corpus_id": corpus_id, "status": "stuck_orphan"}
    )
    return {
        "corpus_id": corpus_id,
        "examined": len(stuck),
        "classes": classes,
        "dispositions": dispositions,
        "samples": samples,
        "stuck_orphan_remaining": remaining_stuck,
        "bounded_reextract_limit": bounded_reextract_limit,
        "apply": apply,
    }


async def certify_corpus_capabilities(
    db: Any,
    *,
    corpus_id: str,
    neo4j_capabilities: dict[str, Any],
) -> dict[str, Any]:
    """Write capability certificate. NOOP/blocked jobs never mark ready."""

    jobs = await db[JOBS_COLLECTION].find({"corpus_id": corpus_id}).to_list(5000)
    by_lane: dict[str, list[str]] = {}
    for job in jobs:
        by_lane.setdefault(str(job.get("lane")), []).append(str(job.get("status")))

    def lane_certified(lane: str) -> bool:
        statuses = by_lane.get(lane) or []
        if not statuses:
            return False
        # All jobs for lane terminal; at least one CERTIFIED (or all NOOP).
        if any(s not in TERMINAL for s in statuses):
            return False
        if any(s == "CERTIFIED" for s in statuses):
            return True
        # All NOOP is not capability.
        return False

    structural = lane_certified("structural") or bool(
        neo4j_capabilities.get("structural_ready")
    )
    entity = lane_certified("entity_mentions") or bool(
        neo4j_capabilities.get("entity_ready")
    )
    assertion = lane_certified("relation_assertions") or bool(
        neo4j_capabilities.get("assertion_ready")
    )
    # Fact lane: accurate false when no CERTIFIED fact jobs / no fact nodes.
    fact = lane_certified("qualified_facts") and bool(
        neo4j_capabilities.get("qualified_fact_ready")
    )

    # Never advertise from NOOP-only lanes.
    certified_jobs = [j for j in jobs if j.get("status") == "CERTIFIED"]
    noop_only = bool(jobs) and all(
        str(j.get("status")) == "NOOP_NO_ELIGIBLE_ARTIFACTS" for j in jobs
    )

    if fact:
        mode = "graph_qualified_fact"
    elif assertion and certified_jobs:
        mode = "graph_assertion"
    elif entity and certified_jobs:
        mode = "graph_entity_navigation"
    elif structural and certified_jobs:
        mode = "graph_structural"
    else:
        mode = "blocked"

    if noop_only:
        mode = "blocked"

    payload = {
        "corpus_id": corpus_id,
        "graph_generation": _corpus_generation(
            await db["corpora"].find_one({"corpus_id": corpus_id}), corpus_id
        ),
        "extraction_terminal": (
            await db["extraction_jobs"].count_documents(
                {
                    "corpus_id": corpus_id,
                    "status": {"$in": ["queued", "running", "leased", "stuck_orphan"]},
                }
            )
            == 0
        ),
        "structural_ready": bool(structural and mode != "blocked"),
        "entity_ready": bool(entity and mode != "blocked"),
        "assertion_ready": bool(assertion and mode != "blocked"),
        "qualified_fact_ready": bool(fact),
        "advertised_mode": mode,
        "noop_marks_ready": False,
        "certified_job_count": len(certified_jobs),
        "job_status_histogram": {
            s: sum(1 for j in jobs if str(j.get("status")) == s)
            for s in sorted({str(j.get("status")) for j in jobs})
        },
        "neo4j_counts": (neo4j_capabilities.get("counts") or {}),
        "updated_at": _utcnow(),
        "projection_release": PROJECTION_RELEASE,
    }
    # Hash stable identity only — timestamps must not break restart replay.
    stable_for_hash = {
        k: v
        for k, v in payload.items()
        if k not in {"updated_at", "graph_capabilities_updated_at"}
    }
    digest = _sha256_hex(
        [
            json.dumps(
                stable_for_hash, sort_keys=True, default=str, separators=(",", ":")
            )
        ]
    )
    payload["certificate_hash"] = f"sha256:{digest}"
    await db[CERTS_COLLECTION].update_one(
        {
            "corpus_id": corpus_id,
            "graph_generation": payload["graph_generation"],
        },
        {"$set": payload},
        upsert=True,
    )
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {"$set": {"graph_capabilities": payload, "graph_capabilities_updated_at": _utcnow()}},
    )
    return payload
