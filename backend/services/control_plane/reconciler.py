"""Artifact-driven reconciler — replaces the queue-count scheduling gate.

Cycle definition (per corpus):
  what should exist -> what exists -> what is missing -> is repair work
  staged for each missing artifact class? If not, stage it by invoking the
  existing planners with census-derived limits (full keyspace, no
  limit-orphans), then drive the existing executors.

Invariants:
- Actionability is decided from the run ledger + artifact census, NEVER from
  queue-row counts. The loop cannot report "nothing to do" while any run has
  missing artifacts.
- The reconciler owns state transitions; planners/executors are hands. Every
  invocation writes a `stage_attempts` receipt.
- Completion is expressed only by issuing a query-ready certificate from an
  all-clear census.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config import get_settings
from services.control_plane import ledger
from services.control_plane.certificate import (
    build_proof,
    issue_certificate_if_complete,
    load_certificate,
)
from services.control_plane.desired_state import (
    STAGE_DOCUMENT_PIPELINE,
    STAGE_EXTRACTION,
    STAGE_ORGAN_REPAIR,
    STAGE_GRAPH_PROMOTION,
    STAGE_SOURCE_PARSE,
    STAGE_SUMMARY,
    collect_doc_artifact_census,
)
from services.storage.record_status import with_active_records

logger = logging.getLogger(__name__)

STATE_COLLECTION = "control_plane_state"
_PLAN_LIMIT_CAP = 10_000
_PENDING_RUN_STATUSES = (
    ledger.RUN_STATUS_INTAKE,
    ledger.RUN_STATUS_RECONCILING,
    ledger.RUN_STATUS_DEGRADED,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _plan_limit(gap_count: int, *, floor: int = 500) -> int:
    """Census-derived planner limit: cover the whole known gap keyspace."""

    return max(floor, min(int(gap_count or 0) + 100, _PLAN_LIMIT_CAP))


async def _record_attempt(
    db: Any,
    *,
    corpus_id: str,
    stage: str,
    action: str,
    receipt: dict[str, Any] | None,
    started: float,
    run_id: str | None = None,
    doc_id: str | None = None,
    failure_class: str | None = None,
    status: str | None = None,
) -> None:
    try:
        await ledger.record_stage_attempt(
            db,
            corpus_id=corpus_id,
            run_id=run_id,
            doc_id=doc_id,
            stage=stage,
            action=action,
            status=status or str((receipt or {}).get("status") or "ok"),
            failure_class=failure_class,
            receipt=_compact_receipt(receipt),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 — receipts must not kill the loop
        logger.warning(
            "stage_attempt receipt write failed corpus=%s stage=%s: %s",
            corpus_id,
            stage,
            exc,
        )


def _compact_receipt(receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Keep receipts bounded: drop bulky job lists, keep counts/status."""

    if not isinstance(receipt, dict):
        return {}
    compact: dict[str, Any] = {}
    for key, value in receipt.items():
        if key in {"jobs", "rows", "items", "documents"}:
            compact[f"{key}_count"] = len(value) if isinstance(value, list) else value
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            compact[key] = value
        elif isinstance(value, dict) and len(value) <= 32:
            compact[key] = {
                k: v
                for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
    return compact


async def _detect_organ_gaps(
    db: Any, *, corpus_id: str
) -> dict[str, dict[str, Any]]:
    """Measure every extraction organ for a corpus and return the failing ones.

    This is the check that did not exist: the census counts ARTIFACTS (chunk
    rows, qdrant points, extraction rows) and so reported a corpus healthy while
    three of four organs inside those rows produced nothing.

    Only organs the document's LANE is accountable for can be a gap — holding
    the pod lane responsible for facets it never produced would plan a repair
    forever.
    """
    from services.control_plane.coverage_bridge import corpus_organ_gaps

    try:
        return await corpus_organ_gaps(db, corpus_id=corpus_id)
    except Exception as exc:  # noqa: BLE001 - never let a probe break a tick
        logger.warning(
            "organ gap detection failed corpus=%s: %s", corpus_id[:8], exc
        )
        return {}


async def _plan_gap_lanes(
    db: Any,
    *,
    corpus_id: str,
    user_id: str,
    gap_totals: dict[str, int],
    gap_doc_ids: dict[str, set[str]],
) -> dict[str, Any]:
    """Stage durable jobs for every artifact-gap lane found by the census."""

    receipts: dict[str, Any] = {}
    jobs_planned = 0

    if gap_totals.get(STAGE_SOURCE_PARSE):
        from services.ingestion.source_parse_jobs import plan_source_parse_jobs

        started = time.monotonic()
        receipt = await plan_source_parse_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=True,
            limit=_plan_limit(gap_totals[STAGE_SOURCE_PARSE]),
        )
        receipts[STAGE_SOURCE_PARSE] = receipt
        jobs_planned += int(receipt.get("planned") or 0)
        await _record_attempt(
            db,
            corpus_id=corpus_id,
            stage=STAGE_SOURCE_PARSE,
            action="plan",
            receipt=receipt,
            started=started,
        )

    if gap_totals.get(STAGE_DOCUMENT_PIPELINE):
        from services.ingestion.document_pipeline_jobs import (
            plan_document_pipeline_jobs,
        )

        started = time.monotonic()
        receipt = await plan_document_pipeline_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=True,
            limit=_plan_limit(gap_totals[STAGE_DOCUMENT_PIPELINE]),
        )
        receipts[STAGE_DOCUMENT_PIPELINE] = receipt
        jobs_planned += int(receipt.get("planned") or 0)
        await _record_attempt(
            db,
            corpus_id=corpus_id,
            stage=STAGE_DOCUMENT_PIPELINE,
            action="plan",
            receipt=receipt,
            started=started,
        )

    # ORGAN REPAIR — the reconciler owns extraction sub-stages, not just the
    # opaque "extraction" step. Runs BEFORE the extraction lane: an organ gap
    # means chunks exist but a sub-stage produced nothing, which is repaired by
    # recomputing that organ, NOT by re-extracting the document. Planning
    # extraction first would re-run whole documents to fix a lane that was
    # simply never wired.
    organ_gaps = await _detect_organ_gaps(db, corpus_id=corpus_id)
    if organ_gaps:
        from services.ingestion.organ_repair_jobs import plan_organ_repair_jobs

        started = time.monotonic()
        receipt = await plan_organ_repair_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            organ_gaps=organ_gaps,
            apply=True,
            limit=_plan_limit(len(organ_gaps)),
        )
        receipts[STAGE_ORGAN_REPAIR] = receipt
        jobs_planned += int(receipt.get("planned") or 0)
        await _record_attempt(
            db,
            corpus_id=corpus_id,
            stage=STAGE_ORGAN_REPAIR,
            action="plan",
            receipt=receipt,
            started=started,
        )

    if gap_totals.get(STAGE_EXTRACTION):
        from services.ingestion.extraction_jobs import plan_extraction_jobs

        started = time.monotonic()
        receipt = await plan_extraction_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=True,
            limit=_plan_limit(gap_totals[STAGE_EXTRACTION]),
        )
        receipts[STAGE_EXTRACTION] = receipt
        jobs_planned += int(receipt.get("planned") or 0)
        await _record_attempt(
            db,
            corpus_id=corpus_id,
            stage=STAGE_EXTRACTION,
            action="plan",
            receipt=receipt,
            started=started,
        )

    if gap_totals.get(STAGE_SUMMARY):
        from services.ingestion.summary_jobs import plan_summary_jobs

        started = time.monotonic()
        receipt = await plan_summary_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=True,
            limit=_plan_limit(gap_totals[STAGE_SUMMARY]),
            doc_ids=sorted(gap_doc_ids.get(STAGE_SUMMARY) or set()) or None,
        )
        receipts[STAGE_SUMMARY] = receipt
        jobs_planned += int(receipt.get("planned") or 0)
        await _record_attempt(
            db,
            corpus_id=corpus_id,
            stage=STAGE_SUMMARY,
            action="plan",
            receipt=receipt,
            started=started,
        )

    if gap_totals.get(STAGE_GRAPH_PROMOTION):
        from services.ingestion.graph_promotion_jobs import (
            plan_graph_promotion_jobs,
        )

        started = time.monotonic()
        receipt = await plan_graph_promotion_jobs(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            apply=True,
            limit=min(_plan_limit(gap_totals[STAGE_GRAPH_PROMOTION]), 5000),
        )
        receipts[STAGE_GRAPH_PROMOTION] = receipt
        jobs_planned += int(receipt.get("planned") or 0)
        await _record_attempt(
            db,
            corpus_id=corpus_id,
            stage=STAGE_GRAPH_PROMOTION,
            action="plan",
            receipt=receipt,
            started=started,
        )

    receipts["jobs_planned"] = jobs_planned
    return receipts


def _lane_run_flags(settings: Any) -> dict[str, bool]:
    """V2 executes all lanes by default; existing lane flags remain an
    explicit opt-out via CONTROL_PLANE_V2_RUN_ALL_LANES=false."""

    if bool(getattr(settings, "CONTROL_PLANE_V2_RUN_ALL_LANES", True)):
        return {
            "run_source_parse_jobs": True,
            "run_document_pipeline_jobs": True,
            "run_extraction_jobs": True,
            "run_summary_jobs": True,
            "run_document_summaries": True,
            "run_graph_jobs": True,
        }
    return {
        "run_source_parse_jobs": bool(
            getattr(settings, "INGEST_AUTO_REPAIR_RUN_SOURCE_PARSE", True)
        ),
        "run_document_pipeline_jobs": bool(
            getattr(settings, "INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE", False)
        ),
        "run_extraction_jobs": bool(
            getattr(settings, "INGEST_AUTO_REPAIR_RUN_EXTRACTION", False)
        ),
        "run_summary_jobs": bool(
            getattr(settings, "INGEST_AUTO_REPAIR_RUN_SUMMARIES", False)
        ),
        "run_document_summaries": bool(
            getattr(settings, "INGEST_AUTO_REPAIR_RUN_SUMMARIES", False)
        ),
        "run_graph_jobs": bool(
            getattr(settings, "INGEST_AUTO_REPAIR_RUN_GRAPH", True)
        ),
    }


async def reconcile_corpus(
    db: Any,
    *,
    ingestion_service: Any,
    corpus_id: str,
    user_id: str,
    doc_census_limit: int | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    """One full reconcile cycle for one corpus. Returns the cycle receipt."""

    settings = get_settings()
    qdrant_client = getattr(ingestion_service, "_qdrant", None)
    doc_census_limit = int(
        doc_census_limit
        or getattr(settings, "CONTROL_PLANE_V2_DOC_CENSUS_LIMIT", 100)
        or 100
    )

    # 1. Ledger: every active doc has a run row; consume outbox intents.
    backfill = await ledger.reconcile_runs_for_corpus(db, corpus_id=corpus_id)
    outbox_rows = await ledger.sweep_outbox(db, corpus_id=corpus_id)

    # 2. Choose the docs to census this cycle: outbox intents first, then the
    #    oldest not-yet-ready runs. Certified runs are skipped (cheap) until
    #    the periodic full recheck.
    pending_runs = await db[ledger.RUNS_COLLECTION].find(
        {"corpus_id": corpus_id, "status": {"$in": list(_PENDING_RUN_STATUSES)}},
        {"_id": 0, "run_id": 1, "doc_id": 1, "status": 1},
    ).sort("updated_at", 1).limit(doc_census_limit).to_list(length=None)
    doc_ids: list[str] = []
    seen: set[str] = set()
    for row in outbox_rows:
        doc_id = str(row.get("doc_id") or "")
        if doc_id and doc_id not in seen:
            doc_ids.append(doc_id)
            seen.add(doc_id)
    for row in pending_runs:
        doc_id = str(row.get("doc_id") or "")
        if doc_id and doc_id not in seen and len(doc_ids) < doc_census_limit:
            doc_ids.append(doc_id)
            seen.add(doc_id)

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "corpus_id": 1, "user_id": 1, "default_ingestion_config": 1},
    )

    # 3. Census: desired vs observed, exact IDs.
    gap_totals: dict[str, int] = {}
    gap_doc_ids: dict[str, set[str]] = {}
    censused = 0
    certificates_issued = 0
    docs_with_gaps = 0
    observation_failures = 0
    for doc_id in doc_ids:
        census = await collect_doc_artifact_census(
            db,
            qdrant_client,
            corpus_id=corpus_id,
            doc_id=doc_id,
            corpus=corpus,
        )
        censused += 1
        run_id = ledger.run_id_for(corpus_id=corpus_id, doc_id=doc_id)
        certificate = None
        if census.get("complete") and not census.get("excluded"):
            certificate = await issue_certificate_if_complete(db, census)
            if certificate:
                certificates_issued += 1
        elif census.get("excluded"):
            certificate = None
        if census.get("observation_errors"):
            observation_failures += 1
        missing = census.get("missing") or {}
        for stage in census.get("gap_stages") or []:
            gap_doc_ids.setdefault(stage, set()).add(doc_id)
        if census.get("gap_stages"):
            docs_with_gaps += 1
        gap_totals[STAGE_SOURCE_PARSE] = gap_totals.get(STAGE_SOURCE_PARSE, 0) + (
            1 if missing.get("chunk_source") else 0
        )
        gap_totals[STAGE_DOCUMENT_PIPELINE] = gap_totals.get(
            STAGE_DOCUMENT_PIPELINE, 0
        ) + len(missing.get("qdrant_child_ids") or []) + len(
            missing.get("summary_vector_ids") or []
        )
        gap_totals[STAGE_EXTRACTION] = gap_totals.get(STAGE_EXTRACTION, 0) + len(
            missing.get("extraction_ids") or []
        )
        gap_totals[STAGE_SUMMARY] = (
            gap_totals.get(STAGE_SUMMARY, 0)
            + len(missing.get("summary_ids") or [])
            + (1 if missing.get("document_summary") else 0)
        )
        gap_totals[STAGE_GRAPH_PROMOTION] = gap_totals.get(
            STAGE_GRAPH_PROMOTION, 0
        ) + len(missing.get("fact_ids") or [])

        proof = build_proof(
            census,
            certificate=certificate
            or (
                await load_certificate(
                    db,
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    contract=census.get("contract"),
                )
                if census.get("complete") and not census.get("excluded")
                else None
            ),
            recovery={
                "jobs_created": 0,
                "next_retry_at": (
                    _utcnow()
                    + timedelta(
                        seconds=float(
                            getattr(
                                settings, "INGEST_AUTO_REPAIR_POLL_SECONDS", 300.0
                            )
                            or 300.0
                        )
                    )
                ).isoformat(),
            },
        )
        await ledger.update_run_from_proof(db, run_id=run_id, proof=proof)

    # 4. Missing artifact + no valid job => create the job (via planners).
    gap_totals = {k: v for k, v in gap_totals.items() if v > 0}
    plan_receipts: dict[str, Any] = {}
    if gap_totals:
        plan_receipts = await _plan_gap_lanes(
            db,
            corpus_id=corpus_id,
            user_id=user_id,
            gap_totals=gap_totals,
            gap_doc_ids=gap_doc_ids,
        )

    # 5. Drive existing executors for every lane (leases/cost controls are
    #    owned by the executors themselves and unchanged).
    execution_receipt: dict[str, Any] | None = None
    # Consolidation (2026-08-11): the reconciler is PLANNER-ONLY by default.
    # Execution belongs to the single enrichment executor
    # (services/ingestion/enrichment_executor.py). Five overlapping
    # executors sharing lanes produced the lease wars and silent skips
    # this program spent a night diagnosing; one owner per concern now.
    # CONTROL_PLANE_V2_EXECUTE=true restores in-tick execution if ever
    # needed for an isolated environment.
    _execute_enabled = str(
        os.environ.get("CONTROL_PLANE_V2_EXECUTE", "false")
    ).strip().lower() in ("1", "true", "yes", "on")
    if execute and _execute_enabled and (gap_totals or outbox_rows or pending_runs):
        lane_flags = _lane_run_flags(settings)
        # Owner rule (2026-08-10), enforced by the control plane itself:
        # enrichment must NEVER hinder the fast lane. While this corpus has
        # inline batch work pending (queued/staged/running/recoverable), the
        # extraction/graph/summary repair lanes stay CLOSED here — by code,
        # not operator env discipline (V2 runs all lanes by default, so env
        # flags alone cannot express the rule). The moment the corpus's
        # inline drain finishes, the same gate opens automatically and the
        # enrichment pass proceeds corpus-wide. Fail toward the rule.
        try:
            pending_inline = await db["ingest_batch_items"].count_documents({
                "corpus_id": corpus_id,
                "status": {"$in": [
                    "queued", "staged", "running", "failed_recoverable",
                ]},
            })
        except Exception:  # noqa: BLE001
            pending_inline = 1
        if pending_inline:
            for gated in ("run_extraction_jobs", "run_summary_jobs", "run_graph_jobs"):
                lane_flags[gated] = False
        started = time.monotonic()
        try:
            execution_receipt = await ingestion_service.run_bounded_corpus_repair_cycle(
                corpus_id=corpus_id,
                user_id=user_id,
                apply=True,
                # Planning already happened with census-derived limits above.
                plan_source_parse_jobs=False,
                plan_document_pipeline_jobs=False,
                plan_extraction_jobs=False,
                plan_summary_jobs=False,
                plan_graph_jobs=False,
                run_source_parse_jobs=lane_flags["run_source_parse_jobs"],
                source_parse_job_run_limit=int(
                    getattr(settings, "INGEST_AUTO_REPAIR_SOURCE_PARSE_RUN_LIMIT", 25)
                ),
                run_document_pipeline_jobs=lane_flags["run_document_pipeline_jobs"],
                document_pipeline_job_run_limit=int(
                    getattr(settings, "INGEST_AUTO_REPAIR_DOCUMENT_RUN_LIMIT", 25)
                ),
                run_extraction_jobs=lane_flags["run_extraction_jobs"],
                extraction_job_run_limit=int(
                    getattr(settings, "INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT", 100)
                ),
                run_summary_jobs=lane_flags["run_summary_jobs"],
                summary_job_run_limit=int(
                    getattr(settings, "INGEST_AUTO_REPAIR_SUMMARY_RUN_LIMIT", 100)
                ),
                run_document_summaries=lane_flags["run_document_summaries"],
                run_graph_jobs=lane_flags["run_graph_jobs"],
                graph_run_limit=int(
                    getattr(settings, "INGEST_AUTO_REPAIR_GRAPH_RUN_LIMIT", 5)
                ),
            )
            await _record_attempt(
                db,
                corpus_id=corpus_id,
                stage="execute",
                action="run_bounded_corpus_repair_cycle",
                receipt=execution_receipt,
                started=started,
            )
        except Exception as exc:  # noqa: BLE001 — one corpus must not kill the tick
            logger.warning(
                "control-plane execution failed corpus=%s: %s", corpus_id, exc
            )
            await _record_attempt(
                db,
                corpus_id=corpus_id,
                stage="execute",
                action="run_bounded_corpus_repair_cycle",
                receipt={"error": f"{type(exc).__name__}: {exc}"},
                started=started,
                status="failed",
                failure_class=type(exc).__name__,
            )

    # 6. Outbox intents for censused docs are consumed.
    consumed = await ledger.mark_outbox_consumed(
        db,
        run_ids=[
            str(row.get("run_id") or "")
            for row in outbox_rows
            if str(row.get("doc_id") or "") in seen
        ],
    )

    receipt = {
        "status": "reconciled",
        "corpus_id": corpus_id,
        "ledger_backfill": backfill,
        "outbox_consumed": consumed,
        "docs_censused": censused,
        "docs_with_gaps": docs_with_gaps,
        "observation_failures": observation_failures,
        "certificates_issued": certificates_issued,
        "gap_totals": gap_totals,
        "jobs_planned": int(plan_receipts.get("jobs_planned") or 0),
        "executed": execution_receipt is not None,
    }
    await db[STATE_COLLECTION].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "corpus_id": corpus_id,
                "last_cycle_at": _utcnow(),
                "last_receipt": {
                    k: v for k, v in receipt.items() if k != "corpus_id"
                },
            }
        },
        upsert=True,
    )
    return receipt


async def corpus_has_actionable_work(db: Any, *, corpus_id: str) -> bool:
    """Actionability from the ledger + outbox — never from queue counts.

    Any pending/degraded run, unconsumed outbox intent, or active document
    without a run row means the reconciler must run for this corpus.
    """

    if await db[ledger.OUTBOX_COLLECTION].count_documents(
        {"corpus_id": corpus_id, "consumed_at": None}, limit=1
    ):
        return True
    if await db[ledger.RUNS_COLLECTION].count_documents(
        {"corpus_id": corpus_id, "status": {"$in": list(_PENDING_RUN_STATUSES)}},
        limit=1,
    ):
        return True
    # Active docs with no run row at all (pre-V2 documents) are owed work.
    run_rows = await db[ledger.RUNS_COLLECTION].count_documents(
        {"corpus_id": corpus_id}
    )
    from services.control_plane.desired_state import EXCLUDED_DOCUMENT_STAGES

    doc_rows = await db["documents"].count_documents(
        with_active_records(
            {
                "corpus_id": corpus_id,
                "ingest_stage": {"$nin": sorted(EXCLUDED_DOCUMENT_STAGES)},
            }
        )
    )
    return doc_rows > run_rows


async def run_reconcile_tick(
    db: Any,
    *,
    ingestion_service: Any,
    corpus_limit: int | None = None,
    _corpus_id: str | None = None,
) -> dict[str, Any]:
    """Resident-loop entry point: reconcile recently active corpora.

    A corpus is skipped only when it has zero actionable work in the ledger
    AND its periodic full recheck is not due. There is no queue-count gate.
    """

    settings = get_settings()
    limit = max(1, min(int(corpus_limit or getattr(settings, "INGEST_AUTO_REPAIR_CORPUS_LIMIT", 5) or 5), 100))
    corpus_filter: dict[str, Any] = {}
    if _corpus_id:
        corpus_filter["corpus_id"] = _corpus_id
    corpora = (
        await db["corpora"]
        .find(
            with_active_records(corpus_filter),
            {"_id": 0, "corpus_id": 1, "user_id": 1, "updated_at": 1},
        )
        .sort("updated_at", -1)
        .limit(limit)
        .to_list(length=limit)
    )
    recheck_seconds = float(
        getattr(settings, "CONTROL_PLANE_V2_RECHECK_SECONDS", 21_600.0) or 21_600.0
    )
    receipts: list[dict[str, Any]] = []
    scanned = 0
    for corpus in corpora:
        corpus_id = str(corpus.get("corpus_id") or "")
        user_id = str(corpus.get("user_id") or "")
        if not corpus_id:
            continue
        scanned += 1
        actionable = await corpus_has_actionable_work(db, corpus_id=corpus_id)
        if not actionable:
            state = await db[STATE_COLLECTION].find_one(
                {"corpus_id": corpus_id}, {"_id": 0, "last_full_recheck_at": 1}
            )
            last_recheck = (state or {}).get("last_full_recheck_at")
            recheck_due = (
                last_recheck is None
                or (_utcnow() - _as_utc(last_recheck)).total_seconds()
                >= recheck_seconds
            )
            if not recheck_due:
                receipts.append(
                    {
                        "corpus_id": corpus_id,
                        "status": "idle_all_certified",
                    }
                )
                continue
            # Periodic recheck: force certified runs back through the census
            # so post-completion artifact loss (e.g. deleted vectors) is
            # re-detected. Runs re-enter reconciling if their proof degrades.
            await db[ledger.RUNS_COLLECTION].update_many(
                {
                    "corpus_id": corpus_id,
                    "status": ledger.RUN_STATUS_QUERY_READY,
                },
                {"$set": {"status": ledger.RUN_STATUS_RECONCILING}},
            )
            await db[STATE_COLLECTION].update_one(
                {"corpus_id": corpus_id},
                {"$set": {"last_full_recheck_at": _utcnow()}},
                upsert=True,
            )
        try:
            receipts.append(
                await reconcile_corpus(
                    db,
                    ingestion_service=ingestion_service,
                    corpus_id=corpus_id,
                    user_id=user_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "control-plane reconcile failed corpus=%s: %s", corpus_id, exc
            )
            receipts.append(
                {
                    "corpus_id": corpus_id,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    changed = sum(
        1
        for r in receipts
        if r.get("status") == "reconciled"
        and (r.get("jobs_planned") or r.get("certificates_issued"))
    )
    return {
        "status": "ok",
        "scanned": scanned,
        "changed": changed,
        "corpora": receipts,
    }


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)
