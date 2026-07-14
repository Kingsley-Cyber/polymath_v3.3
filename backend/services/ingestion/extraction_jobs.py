"""Durable chunk-level extraction job planner.

Ghost B already persists per-chunk outcomes in ``ghost_b_extractions``. This
module materializes an explicit ``extraction_jobs`` queue/read model from live
chunks plus those Ghost B rows so extraction gaps become inspectable,
idempotent, and eventually executable at chunk granularity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from pymongo import ReplaceOne, UpdateOne

from db.queue_integrity import bulk_upsert_durable_jobs
from services.extraction_provider_cards import safe_extraction_pool_contract
from services.ingestion.job_leases import (
    claim_runnable_jobs,
    reclaim_expired_running_jobs,
    retire_superseded_jobs,
)
from services.ingestion.stage_identity import (
    chunk_hash as stage_chunk_hash,
    extraction_stage_identity,
)

ACTIVE_STATUSES = {"queued", "running"}
BLOCKED_STATUSES = {"blocked_provider_contract"}
RETRYABLE_STATUSES = {"queued", "provider_failed", "validation_failed", "failed"}
TERMINAL_STATUSES = {"succeeded", "promoted", "skipped"}
RUNNABLE_STATUSES = {"queued", "provider_failed", "validation_failed", "failed"}
SUPERSEDABLE_STATUSES = ACTIVE_STATUSES | RETRYABLE_STATUSES | BLOCKED_STATUSES
DISABLED_EXTRACTION_REASONS = {
    "extraction_engine_off",
    "graph_extraction_disabled",
}
LIVE_EXTRACTION_CONFIG_FIELDS = (
    "extraction_engine",
    "models_linked",
    "extraction_models",
    "summary_models",
    "entity_schema",
    "relation_schema",
    "schema_strict",
    "schema_lens",
    "use_neo4j",
)


async def active_ingest_doc_ids(
    db: Any,
    *,
    corpus_id: str,
    now: datetime | None = None,
) -> set[str]:
    """Documents currently owned by a live source-batch lease.

    Chunk rows become visible before inline Ghost B staging is promoted. Repair
    planners must not interpret that short window as missing extraction work.
    """
    now = now or datetime.utcnow()
    rows = await db["ingest_batch_items"].find(
        {
            "corpus_id": corpus_id,
            "status": "running",
            "doc_id": {"$nin": [None, ""]},
            "lease_until": {"$gt": now},
        },
        {"_id": 0, "doc_id": 1},
    ).to_list(length=None)
    return {
        str(row.get("doc_id") or "")
        for row in rows
        if str(row.get("doc_id") or "")
    }

def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chunk_content_hash(chunk: dict[str, Any]) -> str:
    return hashlib.sha256(str(chunk.get("text") or "").encode("utf-8")).hexdigest()


def _entry_dict(entry: Any) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    if isinstance(entry, dict):
        return dict(entry)
    return dict(entry or {})


def extraction_provider_pool(doc: dict[str, Any] | None) -> tuple[str, list[dict[str, Any]]]:
    """Return the effective model pool used for Ghost B extraction.

    In cloud mode, ``models_linked`` means extraction borrows the summary pool.
    The job contract must track that resolved pool, otherwise a summary-pool
    model swap can leave stale extraction jobs looking current.
    """

    cfg = (doc or {}).get("ingestion_config") or {}
    if bool(cfg.get("models_linked")):
        return "summary_models", [_entry_dict(entry) for entry in (cfg.get("summary_models") or [])]
    return "extraction_models", [_entry_dict(entry) for entry in (cfg.get("extraction_models") or [])]


def with_live_extraction_config(
    doc: dict[str, Any],
    live_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay mutable corpus extraction controls without rewriting history."""

    if not live_config:
        return dict(doc)
    effective = dict(doc)
    ingestion_config = dict(doc.get("ingestion_config") or {})
    for field in LIVE_EXTRACTION_CONFIG_FIELDS:
        if field in live_config:
            ingestion_config[field] = live_config[field]
    effective["ingestion_config"] = ingestion_config
    if "schema_lens" in live_config:
        effective["schema_lens"] = live_config["schema_lens"]
    return effective


def extraction_provider_contract(doc: dict[str, Any] | None) -> dict[str, Any]:
    source, pool = extraction_provider_pool(doc)
    return safe_extraction_pool_contract(pool_source=source, pool=pool)


def _compact_candidate_lanes(provider_contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "lane": lane.get("lane"),
            "provider": lane.get("provider"),
            "provider_preset": lane.get("provider_preset"),
            "model": lane.get("model"),
            "schema_mode": lane.get("schema_mode"),
            "output_mode": lane.get("output_mode"),
            "json_repair_mode": lane.get("json_repair_mode"),
            "max_concurrent": lane.get("max_concurrent"),
            "concurrency_policy": lane.get("concurrency_policy"),
            "local_private": lane.get("local_private"),
        }
        for lane in (provider_contract.get("lanes") or [])
    ]


def extraction_contract_hash(doc: dict[str, Any] | None) -> str:
    doc = doc or {}
    cfg = doc.get("ingestion_config") or {}
    relevant = {
        "extraction_engine": cfg.get("extraction_engine"),
        "extraction_schema": cfg.get("extraction_schema"),
        "models_linked": cfg.get("models_linked"),
        "use_neo4j": cfg.get("use_neo4j"),
        "provider_contract": extraction_provider_contract(doc),
        "schema_lens": doc.get("schema_lens"),
    }
    return _stable_hash(relevant)


def extraction_job_id(
    *,
    corpus_id: str,
    doc_id: str,
    chunk_id: str,
    chunk_hash: str,
    contract_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{corpus_id}:{doc_id}:{chunk_id}:{chunk_hash}:{contract_hash}".encode("utf-8")
    ).hexdigest()
    return f"extract_{digest[:24]}"


def classify_extraction_status(row: dict[str, Any] | None) -> tuple[str, str]:
    if not row:
        return "queued", "missing_extraction"
    status = str(row.get("status") or "").lower()
    if status == "ok":
        if row.get("promoted_at"):
            return "promoted", "graph_promoted"
        return "succeeded", "ghost_b_ok"
    if status == "skipped":
        return "skipped", str(
            row.get("skip_reason")
            or row.get("reason")
            or "no_extractable_text_or_skipped_kind"
        )
    if status == "stale_chunk_reference":
        stale_reason = str(row.get("stale_reason") or "").lower()
        repair_action = str(row.get("repair_action") or "").lower()
        if (
            stale_reason == "stale_extraction_contract_mismatch"
            or repair_action == "requeue_with_current_contract"
        ):
            return "queued", "contract_changed"
        if (
            stale_reason == "stale_chunk_hash_mismatch"
            or repair_action == "clear_or_reextract_chunk"
        ):
            return "queued", "chunk_changed"
        return "skipped", "stale_chunk_reference"
    error_type = str(row.get("error_type") or "").lower()
    error_message = str(row.get("error_message") or row.get("error") or "").lower()
    combined = f"{error_type} {error_message}"
    if any(
        token in combined
        for token in (
            "json_schema_unsupported",
            "json_object_unsupported",
            "response_format",
            "unsupported structured",
        )
    ):
        return "blocked_provider_contract", "provider_contract_unsupported"
    if any(token in combined for token in ("validation", "schema", "json", "parse", "empty")):
        return "validation_failed", "validation_error"
    if any(
        token in combined
        for token in (
            "timeout",
            "rate",
            "429",
            "provider",
            "http",
            "connection",
            "network",
            "unavailable",
        )
    ):
        return "provider_failed", "provider_error"
    return "failed", "extraction_error"


def _doc_extraction_skip_reason(doc: dict[str, Any] | None) -> str | None:
    """Return why Ghost B extraction is not part of this document contract.

    The durable extraction queue must mirror the worker's contract. If a corpus
    is vectors-only or explicitly extraction-off, missing Ghost B rows are not
    repair work and must not make readiness look broken.
    """

    cfg = (doc or {}).get("ingestion_config") or {}
    if str(cfg.get("extraction_engine") or "").strip().lower() == "off":
        return "extraction_engine_off"
    if cfg.get("use_neo4j") is False:
        return "graph_extraction_disabled"
    return None


def build_extraction_job(
    *,
    chunk: dict[str, Any],
    doc: dict[str, Any] | None,
    extraction_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    corpus_id = str(chunk.get("corpus_id") or "")
    doc_id = str(chunk.get("doc_id") or "")
    chunk_id = str(chunk.get("chunk_id") or "")
    c_hash = str(chunk.get("chunk_hash") or chunk.get("text_hash") or "") or chunk_content_hash(chunk)
    contract_hash = extraction_contract_hash(doc)
    provider_contract = extraction_provider_contract(doc)
    skip_reason = _doc_extraction_skip_reason(doc)
    status, reason = (
        ("skipped", skip_reason)
        if skip_reason
        else classify_extraction_status(extraction_row)
    )
    stage_identity = extraction_stage_identity(
        chunk=chunk,
        doc=doc,
        extraction_contract_hash=contract_hash,
    )
    provider_route = {
        "model": (extraction_row or {}).get("model"),
        "lane": (extraction_row or {}).get("lane"),
        "provider": (extraction_row or {}).get("provider"),
        "schema_mode": (extraction_row or {}).get("schema_mode"),
        "output_mode": (extraction_row or {}).get("output_mode"),
        "json_repair_mode": (extraction_row or {}).get("json_repair_mode"),
        "semantic_verifier_mode": (extraction_row or {}).get("semantic_verifier_mode"),
        "pool_source": provider_contract.get("pool_source"),
        "routing_policy": provider_contract.get("routing_policy"),
        "pool_size": provider_contract.get("pool_size"),
        "candidate_lanes": _compact_candidate_lanes(provider_contract),
    }
    validation_errors = (extraction_row or {}).get("validation_errors")
    if not isinstance(validation_errors, list):
        validation_errors = (
            [(extraction_row or {}).get("error_message")]
            if status == "validation_failed" and (extraction_row or {}).get("error_message")
            else []
        )
    return {
        "job_id": extraction_job_id(
            corpus_id=corpus_id,
            doc_id=doc_id,
            chunk_id=chunk_id,
            chunk_hash=c_hash,
            contract_hash=contract_hash,
        ),
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "parent_id": chunk.get("parent_id"),
        "user_id": str((doc or {}).get("user_id") or chunk.get("user_id") or ""),
        "filename": (doc or {}).get("filename"),
        "chunk_hash": c_hash,
        "chunk_version": chunk.get("chunk_version") or chunk.get("updated_at"),
        "doc_version": (doc or {}).get("updated_at"),
        "extraction_contract_hash": contract_hash,
        "stage_identity": stage_identity,
        "provider_route": provider_route,
        "provider_contract": provider_contract,
        "status": status,
        "reason": reason,
        "attempt_count": int((extraction_row or {}).get("attempts") or 0),
        "validation_errors": validation_errors,
        "validation_summary": {
            "entity_schema_drops": int((extraction_row or {}).get("entity_drop_count") or 0),
            "relation_schema_drops": int((extraction_row or {}).get("relation_drop_count") or 0),
            "evidence_drops": int((extraction_row or {}).get("evidence_drop_count") or 0),
            "fact_drops": int((extraction_row or {}).get("fact_drop_count") or 0),
            "validation_rejections": int(
                (extraction_row or {}).get("validation_rejection_count") or 0
            ),
        },
        "raw_output_artifact_id": (extraction_row or {}).get("raw_output_artifact_id"),
        "raw_output_fingerprint": (extraction_row or {}).get("raw_output_fingerprint") or {},
        "prompt_hash": (extraction_row or {}).get("prompt_hash"),
        "prompt_chars": (extraction_row or {}).get("prompt_chars"),
        "promoted_at": (extraction_row or {}).get("promoted_at"),
        "source_status": (extraction_row or {}).get("status") if extraction_row else None,
    }


def _asdict(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        data = asdict(value)
        if hasattr(value, "validation_rejection_count"):
            data["validation_rejection_count"] = int(
                getattr(value, "validation_rejection_count") or 0
            )
        return data
    if isinstance(value, dict):
        return dict(value)
    return dict(value or {})


async def _extraction_identity_context(
    db: Any,
    *,
    doc_id: str,
    corpus_id: str,
    chunk_ids: list[str],
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str]:
    clean_chunk_ids = sorted({str(chunk_id) for chunk_id in chunk_ids if chunk_id})
    doc = await db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "filename": 1,
            "updated_at": 1,
            "source_identity": 1,
            "source_key": 1,
            "content_sha256": 1,
            "source_file_hash": 1,
            "ingestion_config": 1,
            "schema_lens": 1,
        },
    )
    if doc:
        corpus = await db["corpora"].find_one(
            {"corpus_id": corpus_id},
            {"_id": 0, "default_ingestion_config": 1},
        )
        doc = with_live_extraction_config(
            doc,
            dict((corpus or {}).get("default_ingestion_config") or {}),
        )
    chunks: dict[str, dict[str, Any]] = {}
    if clean_chunk_ids:
        rows = await db["chunks"].find(
            {
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "chunk_id": {"$in": clean_chunk_ids},
            },
            {
                "_id": 0,
                "chunk_id": 1,
                "parent_id": 1,
                "text": 1,
                "text_hash": 1,
                "chunk_hash": 1,
                "chunk_version": 1,
                "updated_at": 1,
            },
        ).to_list(length=len(clean_chunk_ids))
        chunks = {str(row.get("chunk_id") or ""): row for row in rows if row.get("chunk_id")}
    return doc, chunks, extraction_contract_hash(doc)


def _stamp_extraction_row_identity(
    row: dict[str, Any],
    *,
    chunk: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    contract_hash: str,
) -> None:
    if not chunk:
        return
    identity = extraction_stage_identity(
        chunk=chunk,
        doc=doc,
        extraction_contract_hash=contract_hash,
    )
    row["chunk_hash"] = stage_chunk_hash(chunk)
    row["chunk_version"] = identity.get("chunk_version")
    row["doc_version"] = identity.get("doc_version")
    row["extraction_contract_hash"] = contract_hash
    row["stage_identity"] = identity


def _ensure_extraction_artifact_id(
    row: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
    status: str | None = None,
) -> None:
    """Attach a compact, stable artifact handle to extraction output rows.

    Provider-backed Ghost B rows usually have a raw response hash. Deterministic
    extractors and failure paths may not, but graph promotion and repair queues
    still need a durable per-chunk artifact id. Derive one from stable stage
    identity and compact audit fields rather than storing raw prompt/output.
    """

    if str(row.get("raw_output_artifact_id") or "").strip():
        return
    fingerprint = row.get("raw_output_fingerprint")
    if isinstance(fingerprint, dict):
        raw_sha = str(fingerprint.get("sha256") or "").strip()
        if raw_sha:
            row["raw_output_artifact_id"] = f"sha256:{raw_sha}"
            return

    fallback = fallback or {}
    identity = row.get("stage_identity") if isinstance(row.get("stage_identity"), dict) else {}
    fallback_identity = (
        fallback.get("stage_identity")
        if isinstance(fallback.get("stage_identity"), dict)
        else {}
    )
    row["raw_output_artifact_id"] = "derived:" + _stable_hash(
        {
            "doc_id": row.get("doc_id") or fallback.get("doc_id"),
            "corpus_id": row.get("corpus_id") or fallback.get("corpus_id"),
            "chunk_id": row.get("chunk_id") or fallback.get("chunk_id"),
            "chunk_hash": (
                row.get("chunk_hash")
                or identity.get("chunk_hash")
                or fallback.get("chunk_hash")
                or fallback_identity.get("chunk_hash")
            ),
            "extraction_contract_hash": (
                row.get("extraction_contract_hash")
                or identity.get("extraction_contract_hash")
                or fallback.get("extraction_contract_hash")
                or fallback_identity.get("extraction_contract_hash")
            ),
            "status": status or row.get("status"),
            "model": row.get("model"),
            "provider": row.get("provider"),
            "lane": row.get("lane"),
            "attempts": row.get("attempts"),
            "prompt_hash": row.get("prompt_hash"),
            "error_type": row.get("error_type"),
        }
    )


def build_extraction_job_run_update(
    job: dict[str, Any],
    *,
    succeeded: bool = False,
    result: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    skipped_reason: str | None = None,
    error: Exception | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the job update produced by one bounded runner attempt."""

    now = now or datetime.utcnow()
    attempt_count = int(job.get("attempt_count") or 0) + 1
    base: dict[str, Any] = {
        "attempt_count": attempt_count,
        "updated_at": now,
        "last_run_at": now,
        "lease_until": None,
    }
    if succeeded:
        result = dict(result or {})
        _ensure_extraction_artifact_id(result, fallback=job, status="ok")
        route = dict(job.get("provider_route") or {})
        for key in (
            "model",
            "provider",
            "lane",
            "schema_mode",
            "output_mode",
            "json_repair_mode",
            "semantic_verifier_mode",
        ):
            if result.get(key) not in (None, ""):
                route[key] = result.get(key)
        return {
            **base,
            "status": "succeeded",
            "reason": "ghost_b_ok",
            "source_status": "ok",
            "provider_route": route,
            "validation_errors": [],
            "validation_summary": {
                "entity_schema_drops": int(result.get("entity_drop_count") or 0),
                "relation_schema_drops": int(result.get("relation_drop_count") or 0),
                "evidence_drops": int(result.get("evidence_drop_count") or 0),
                "fact_drops": int(result.get("fact_drop_count") or 0),
                "validation_rejections": int(result.get("validation_rejection_count") or 0),
            },
            "raw_output_artifact_id": result.get("raw_output_artifact_id"),
            "raw_output_fingerprint": result.get("raw_output_fingerprint") or {},
            "prompt_hash": result.get("prompt_hash"),
            "prompt_chars": result.get("prompt_chars"),
            "failure": None,
        }
    if skipped_reason:
        return {
            **base,
            "status": "skipped",
            "reason": skipped_reason,
            "source_status": "skipped",
            "validation_errors": [],
            "failure": None,
        }
    if error is not None:
        failure = {
            "error_type": type(error).__name__,
            "error_message": str(error)[:1000],
        }
    failure = dict(failure or {})
    _ensure_extraction_artifact_id(failure, fallback=job, status="error")
    status, reason = classify_extraction_status({"status": "error", **failure})
    route = dict(job.get("provider_route") or {})
    if failure.get("model"):
        route["model"] = failure.get("model")
    if failure.get("lane") is not None:
        route["lane"] = failure.get("lane")
    provider_card = failure.get("provider_card") if isinstance(failure.get("provider_card"), dict) else {}
    if failure.get("provider"):
        route["provider"] = failure.get("provider")
    if failure.get("schema_mode"):
        route["schema_mode"] = failure.get("schema_mode")
    if failure.get("output_mode"):
        route["output_mode"] = failure.get("output_mode")
    return {
        **base,
        "status": status,
        "reason": reason,
        "source_status": "error",
        "provider_route": route,
        "validation_errors": (
            [failure.get("error_message")]
            if status == "validation_failed" and failure.get("error_message")
            else []
        ),
        "failure": {
            "error_type": failure.get("error_type") or "unknown",
            "error_message": str(failure.get("error_message") or "")[:1000],
            "provider": failure.get("provider") or provider_card.get("provider"),
            "schema_mode": failure.get("schema_mode") or provider_card.get("schema_mode"),
            "output_mode": failure.get("output_mode"),
            "json_repair_mode": (
                failure.get("json_repair_mode")
                or provider_card.get("json_repair_mode")
            ),
            "semantic_verifier_mode": (
                failure.get("semantic_verifier_mode")
                or provider_card.get("semantic_verifier_mode")
            ),
            "provider_card": provider_card,
        },
        "raw_output_artifact_id": failure.get("raw_output_artifact_id"),
        "raw_output_fingerprint": failure.get("raw_output_fingerprint") or {},
        "prompt_hash": failure.get("prompt_hash"),
        "prompt_chars": failure.get("prompt_chars"),
    }


async def _count(db: Any, collection: str, query: dict[str, Any]) -> int:
    try:
        return await db[collection].count_documents(query)
    except Exception:
        return 0


def terminal_extraction_artifact_matches_job(
    job: dict[str, Any],
    extraction_row: dict[str, Any],
) -> bool:
    """Whether durable artifact truth makes a provider retry unnecessary.

    This is the pure decision already enforced by
    ``reconcile_terminal_extraction_jobs``: only an ``ok``/``skipped`` row
    for the same chunk and the exact same non-empty extraction contract can
    retire a runnable job. It is exported so burst/readiness receipts reuse
    this one truth instead of inventing a parallel retry policy.
    """

    if str(extraction_row.get("status") or "") not in {"ok", "skipped"}:
        return False
    job_chunk = str(job.get("chunk_id") or "")
    row_chunk = str(extraction_row.get("chunk_id") or "")
    if not job_chunk or job_chunk != row_chunk:
        return False
    job_contract = str(job.get("extraction_contract_hash") or "")
    row_contract = str(extraction_row.get("extraction_contract_hash") or "")
    return bool(job_contract and row_contract and job_contract == row_contract)


async def reconcile_terminal_extraction_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 5000,
) -> dict[str, int]:
    """Make retry/read-model rows agree with durable successful artifacts.

    A provider retry can succeed while an older ``extraction_jobs`` row still
    says ``validation_failed`` or ``queued``.  Running that stale row wastes a
    provider call and can replace a known-good artifact.  Only synchronize
    rows when the successful extraction carries the exact same contract hash;
    a provider/schema change must still produce a new job and a new result.
    """

    limit = max(1, min(int(limit or 5000), 50000))
    runnable_statuses = sorted(RETRYABLE_STATUSES | BLOCKED_STATUSES)
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "status": {"$in": runnable_statuses},
    }
    if user_id:
        query["user_id"] = user_id
    jobs = await db["extraction_jobs"].find(
        query,
        {
            "_id": 0,
            "job_id": 1,
            "chunk_id": 1,
            "extraction_contract_hash": 1,
        },
    ).limit(limit).to_list(length=limit)
    chunk_ids = sorted(
        {
            str(job.get("chunk_id") or "")
            for job in jobs
            if str(job.get("chunk_id") or "")
        }
    )
    if not chunk_ids:
        return {"scanned": len(jobs), "synchronized": 0}

    extraction_rows = await db["ghost_b_extractions"].find(
        {
            "corpus_id": corpus_id,
            "chunk_id": {"$in": chunk_ids},
            "status": {"$in": ["ok", "skipped"]},
        },
        {
            "_id": 0,
            "chunk_id": 1,
            "extraction_contract_hash": 1,
            "promoted_at": 1,
            "raw_output_artifact_id": 1,
            "raw_output_fingerprint": 1,
            "prompt_hash": 1,
            "prompt_chars": 1,
            "status": 1,
            "skip_reason": 1,
            "reason": 1,
        },
    ).to_list(length=len(chunk_ids))
    extraction_by_chunk = {
        str(row.get("chunk_id") or ""): row
        for row in extraction_rows
        if row.get("chunk_id")
    }
    now = datetime.utcnow()
    ops: list[UpdateOne] = []
    for job in jobs:
        row = extraction_by_chunk.get(str(job.get("chunk_id") or ""))
        if not row:
            continue
        if not terminal_extraction_artifact_matches_job(job, row):
            continue
        promoted_at = row.get("promoted_at")
        row_status = str(row.get("status") or "")
        if row_status == "skipped":
            terminal_status = "skipped"
            terminal_reason = str(
                row.get("skip_reason")
                or row.get("reason")
                or "no_extractable_text_or_skipped_kind"
            )
        else:
            terminal_status = "promoted" if promoted_at else "succeeded"
            terminal_reason = "graph_promoted" if promoted_at else "ghost_b_ok"
        set_fields: dict[str, Any] = {
            "status": terminal_status,
            "reason": terminal_reason,
            "source_status": row_status,
            "lease_until": None,
            "terminal_reconciled_at": now,
            "updated_at": now,
            "raw_output_artifact_id": row.get("raw_output_artifact_id"),
            "raw_output_fingerprint": row.get("raw_output_fingerprint") or {},
            "prompt_hash": row.get("prompt_hash"),
            "prompt_chars": row.get("prompt_chars"),
        }
        if promoted_at:
            set_fields["promoted_at"] = promoted_at
        ops.append(
            UpdateOne(
                {
                    "job_id": job["job_id"],
                    "status": {"$in": runnable_statuses},
                },
                {
                    "$set": set_fields,
                    "$unset": {"runner": "", "started_at": "", "failure": ""},
                },
            )
        )
    if not ops:
        return {"scanned": len(jobs), "synchronized": 0}
    result = await db["extraction_jobs"].bulk_write(ops, ordered=False)
    return {
        "scanned": len(jobs),
        "synchronized": int(getattr(result, "modified_count", 0) or 0),
    }


async def plan_extraction_jobs(
    db: Any,
    *,
    corpus_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 500,
    include_succeeded: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 500), 10000))
    doc_query: dict[str, Any] = {"corpus_id": corpus_id}
    if user_id:
        doc_query["user_id"] = user_id
    active_doc_ids = await active_ingest_doc_ids(
        db,
        corpus_id=corpus_id,
    )
    if active_doc_ids:
        doc_query["doc_id"] = {"$nin": sorted(active_doc_ids)}
    docs = await db["documents"].find(
        doc_query,
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "user_id": 1,
            "filename": 1,
            "ingestion_config": 1,
            "schema_lens": 1,
            "write_state": 1,
            "updated_at": 1,
        },
    ).to_list(length=None)
    live_config: dict[str, Any] = {}
    try:
        corpus = await db["corpora"].find_one(
            {"corpus_id": corpus_id},
            {"_id": 0, "default_ingestion_config": 1},
        )
        live_config = dict((corpus or {}).get("default_ingestion_config") or {})
    except Exception:
        live_config = {}
    docs_by_id = {
        str(doc.get("doc_id") or ""): with_live_extraction_config(doc, live_config)
        for doc in docs
    }
    if not docs_by_id:
        return {
            "status": "planned" if not apply else "complete",
            "apply": bool(apply),
            "corpus_id": corpus_id,
            "planned": 0,
            "counts": {},
            "jobs": [],
            "source_counts": {
                "active_ingest_docs_excluded": len(active_doc_ids),
            },
        }

    chunk_projection = {
        "_id": 0,
        "chunk_id": 1,
        "parent_id": 1,
        "doc_id": 1,
        "corpus_id": 1,
        "user_id": 1,
        "text": 1,
        "text_hash": 1,
        "chunk_hash": 1,
        "updated_at": 1,
    }
    jobs: list[dict[str, Any]] = []
    retirement_jobs: list[dict[str, Any]] = []
    seen_chunk_ids: set[str] = set()

    def _stage_planned_job(job: dict[str, Any]) -> None:
        if include_succeeded or job["status"] not in TERMINAL_STATUSES:
            jobs.append(job)
            return
        if job["status"] == "skipped" and job.get("reason") in DISABLED_EXTRACTION_REASONS:
            retirement_jobs.append(job)

    error_rows = await db["ghost_b_extractions"].find(
        {
            "corpus_id": corpus_id,
            "doc_id": {"$in": sorted(docs_by_id)},
            "status": {"$in": ["error", "stale_chunk_reference"]},
        },
        {"_id": 0},
    ).limit(limit).to_list(length=limit)
    error_by_chunk = {str(row.get("chunk_id") or ""): row for row in error_rows}
    if error_by_chunk:
        failed_chunks = await db["chunks"].find(
            {"corpus_id": corpus_id, "chunk_id": {"$in": sorted(error_by_chunk)}},
            chunk_projection,
        ).to_list(length=len(error_by_chunk))
        for chunk in failed_chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            doc = docs_by_id.get(str(chunk.get("doc_id") or ""))
            if not doc or not chunk_id:
                continue
            _stage_planned_job(
                build_extraction_job(
                    chunk=chunk,
                    doc=doc,
                    extraction_row=error_by_chunk.get(chunk_id),
                )
            )
            seen_chunk_ids.add(chunk_id)
            if len(jobs) >= limit:
                break

    remaining = max(limit - len(jobs), 0)
    scanned_chunk_count = 0
    loaded_extraction_row_count = 0
    priority_doc_ids = sorted(
        doc_id
        for doc_id, doc in docs_by_id.items()
        if (doc.get("write_state") or {}).get("qdrant_written") is True
        and (doc.get("write_state") or {}).get("neo4j_written") is not True
    )
    scan_doc_ids = priority_doc_ids or sorted(docs_by_id)
    last_chunk_id = ""
    # A fixed ``limit * 3`` window starves later gaps once its leading chunks
    # already have terminal artifacts. Advance deterministically through a
    # bounded keyset scan until this slice is full or the candidate set ends.
    max_chunks_to_scan = max(limit * 100, 10_000)
    while remaining > 0 and scanned_chunk_count < max_chunks_to_scan:
        batch_limit = min(max(remaining * 3, 100), max_chunks_to_scan - scanned_chunk_count)
        chunk_query: dict[str, Any] = {
            "corpus_id": corpus_id,
            "doc_id": {"$in": scan_doc_ids},
        }
        chunk_id_filter: dict[str, Any] = {}
        if seen_chunk_ids:
            chunk_id_filter["$nin"] = sorted(seen_chunk_ids)
        if last_chunk_id:
            chunk_id_filter["$gt"] = last_chunk_id
        if chunk_id_filter:
            chunk_query["chunk_id"] = chunk_id_filter
        scanned_chunks = await db["chunks"].find(
            chunk_query,
            chunk_projection,
        ).sort("chunk_id", 1).limit(batch_limit).to_list(length=batch_limit)
        if not scanned_chunks:
            break
        scanned_chunk_count += len(scanned_chunks)
        last_chunk_id = str(scanned_chunks[-1].get("chunk_id") or last_chunk_id)
        chunk_ids = [
            str(chunk.get("chunk_id") or "")
            for chunk in scanned_chunks
            if chunk.get("chunk_id")
        ]
        extraction_rows = await db["ghost_b_extractions"].find(
            {"corpus_id": corpus_id, "chunk_id": {"$in": chunk_ids}},
            {"_id": 0},
        ).to_list(length=None)
        loaded_extraction_row_count += len(extraction_rows)
        extraction_by_chunk = {
            str(row.get("chunk_id") or ""): row for row in extraction_rows
        }
        for chunk in scanned_chunks:
            doc = docs_by_id.get(str(chunk.get("doc_id") or ""))
            if not doc:
                continue
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen_chunk_ids:
                continue
            job = build_extraction_job(
                chunk=chunk,
                doc=doc,
                extraction_row=extraction_by_chunk.get(chunk_id),
            )
            _stage_planned_job(job)
            seen_chunk_ids.add(chunk_id)
            if len(jobs) >= limit:
                break
        remaining = max(limit - len(jobs), 0)

    counts: dict[str, int] = {}
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    result: dict[str, Any] = {
        "status": "planned" if not apply else "complete",
        "apply": bool(apply),
        "corpus_id": corpus_id,
        "planned": len(jobs),
        "counts": counts,
        "jobs": jobs[:100],
        "source_counts": {
            "chunks_scanned": scanned_chunk_count + len(error_by_chunk),
            "ghost_b_rows_loaded": len(error_rows) + loaded_extraction_row_count,
            "existing_jobs": await _count(db, "extraction_jobs", {"corpus_id": corpus_id}),
            "disabled_extraction_chunks": len(retirement_jobs),
            "priority_graph_gap_docs": len(priority_doc_ids),
            "active_ingest_docs_excluded": len(active_doc_ids),
        },
    }
    if not apply:
        return result

    now = datetime.utcnow()
    if jobs:
        ops = []
        for job in jobs:
            ops.append(
                UpdateOne(
                    {"job_id": job["job_id"]},
                    {
                        "$set": {
                            **job,
                            "updated_at": now,
                            "last_planned_at": now,
                        },
                        "$setOnInsert": {
                            "created_at": now,
                            "lease_until": None,
                        },
                    },
                    upsert=True,
                )
            )
        await bulk_upsert_durable_jobs(db["extraction_jobs"], ops)
    result["superseded"] = await retire_superseded_jobs(
        db,
        collection_name="extraction_jobs",
        jobs=[*jobs, *retirement_jobs],
        identity_fields=("corpus_id", "doc_id", "chunk_id"),
        supersedable_statuses=SUPERSEDABLE_STATUSES,
        now=now,
        reason="extraction_contract_disabled",
    )
    return result


async def _persist_extraction_rows(
    db: Any,
    *,
    doc_id: str,
    corpus_id: str,
    results: list[Any],
    failures: list[Any],
) -> None:
    now = datetime.utcnow()
    prepared_rows: list[dict[str, Any]] = []
    for result in results:
        row = _asdict(result)
        if not row.get("chunk_id"):
            continue
        row["doc_id"] = doc_id
        row["corpus_id"] = corpus_id
        row["status"] = "ok"
        row["updated_at"] = now
        prepared_rows.append(row)
    for failure in failures:
        row = _asdict(failure)
        if not row.get("chunk_id"):
            continue
        row["doc_id"] = doc_id
        row["corpus_id"] = corpus_id
        row["status"] = "error"
        row["updated_at"] = now
        prepared_rows.append(row)
    if not prepared_rows:
        return

    doc, chunks_by_id, contract_hash = await _extraction_identity_context(
        db,
        doc_id=doc_id,
        corpus_id=corpus_id,
        chunk_ids=[str(row.get("chunk_id") or "") for row in prepared_rows],
    )
    ops: list[ReplaceOne] = []
    for row in prepared_rows:
        _stamp_extraction_row_identity(
            row,
            chunk=chunks_by_id.get(str(row.get("chunk_id") or "")),
            doc=doc,
            contract_hash=contract_hash,
        )
        _ensure_extraction_artifact_id(row)
        ops.append(
            ReplaceOne(
                {"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": row["chunk_id"]},
                row,
                upsert=True,
            )
        )
    await db["ghost_b_extractions"].bulk_write(ops, ordered=False)


async def _persist_skipped_extraction_rows(
    db: Any,
    *,
    doc_id: str,
    corpus_id: str,
    chunk_ids: list[str],
    reason: str,
) -> int:
    """Persist non-extractable chunks as terminal extraction artifacts."""

    clean_ids = sorted({str(chunk_id) for chunk_id in chunk_ids if chunk_id})
    if not clean_ids:
        return 0
    doc, chunks_by_id, contract_hash = await _extraction_identity_context(
        db,
        doc_id=doc_id,
        corpus_id=corpus_id,
        chunk_ids=clean_ids,
    )
    existing_rows = await db["ghost_b_extractions"].find(
        {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "chunk_id": {"$in": clean_ids},
        },
        {"_id": 0},
    ).to_list(length=len(clean_ids))
    existing_by_chunk = {
        str(row.get("chunk_id") or ""): row
        for row in existing_rows
        if row.get("chunk_id")
    }
    now = datetime.utcnow()
    ops: list[ReplaceOne] = []
    for chunk_id in clean_ids:
        chunk = chunks_by_id.get(chunk_id)
        if not chunk:
            continue
        previous = dict(existing_by_chunk.get(chunk_id) or {})
        row = {
            **previous,
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "chunk_id": chunk_id,
            "parent_id": chunk.get("parent_id") or previous.get("parent_id"),
            "status": "skipped",
            "previous_status": str(previous.get("status") or "missing"),
            "skip_reason": reason,
            "reason": reason,
            "reconciled_at": now,
            "updated_at": now,
        }
        _stamp_extraction_row_identity(
            row,
            chunk=chunk,
            doc=doc,
            contract_hash=contract_hash,
        )
        _ensure_extraction_artifact_id(row, status="skipped")
        ops.append(
            ReplaceOne(
                {
                    "doc_id": doc_id,
                    "corpus_id": corpus_id,
                    "chunk_id": chunk_id,
                },
                row,
                upsert=True,
            )
        )
    if not ops:
        return 0
    result = await db["ghost_b_extractions"].bulk_write(ops, ordered=False)
    return int(
        (getattr(result, "modified_count", 0) or 0)
        + (getattr(result, "upserted_count", 0) or 0)
    )


async def _refresh_document_extraction_counts(
    db: Any,
    *,
    doc_id: str,
    corpus_id: str,
) -> dict[str, int]:
    ok_count = await db["ghost_b_extractions"].count_documents(
        {"doc_id": doc_id, "corpus_id": corpus_id, "status": "ok"}
    )
    error_count = await db["ghost_b_extractions"].count_documents(
        {"doc_id": doc_id, "corpus_id": corpus_id, "status": "error"}
    )
    sample = await db["ghost_b_extractions"].find(
        {"doc_id": doc_id, "corpus_id": corpus_id, "status": "error"},
        {"_id": 0},
    ).sort("updated_at", -1).limit(20).to_list(length=20)
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "ghost_b_staging_count": ok_count,
                "ghost_b_failure_count": error_count,
                "ghost_b_failures": sample,
                "updated_at": datetime.utcnow(),
            },
            "$unset": {"ghost_b_staging": ""},
        },
    )
    return {"ok": int(ok_count), "error": int(error_count)}


async def _mark_jobs(
    db: Any,
    *,
    updates: dict[str, dict[str, Any]],
) -> None:
    if not updates:
        return
    ops = [
        UpdateOne({"job_id": job_id}, {"$set": update})
        for job_id, update in updates.items()
    ]
    await db["extraction_jobs"].bulk_write(ops, ordered=False)


async def run_extraction_jobs(
    db: Any,
    *,
    qdrant_client: Any,
    corpus_id: str,
    user_id: str | None = None,
    limit: int = 25,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    """Run a bounded set of materialized chunk extraction jobs.

    This is intentionally a small executor, not a new ingestion pipeline. It
    reuses the existing Ghost B provider/validator path and only updates the
    selected chunk rows plus their durable job states.
    """

    from services.ingestion.graph_backfill import (
        _extract_tasks,
        _load_backfill_config,
        _run_ghost_b_backfill,
    )

    limit = max(1, min(int(limit or 25), 500))
    now = datetime.utcnow()
    reclaimed = await reclaim_expired_running_jobs(
        db,
        collection_name="extraction_jobs",
        corpus_id=corpus_id,
        user_id=user_id,
        now=now,
    )
    terminal_reconciliation = await reconcile_terminal_extraction_jobs(
        db,
        corpus_id=corpus_id,
        user_id=user_id,
        limit=max(1000, limit * 10),
    )
    allowed_statuses = [s for s in (statuses or sorted(RUNNABLE_STATUSES)) if s in RUNNABLE_STATUSES]
    if not allowed_statuses:
        allowed_statuses = sorted(RUNNABLE_STATUSES)
    active_doc_ids = await active_ingest_doc_ids(
        db,
        corpus_id=corpus_id,
        now=now,
    )
    query: dict[str, Any] = {"corpus_id": corpus_id, "status": {"$in": allowed_statuses}}
    if active_doc_ids:
        query["doc_id"] = {"$nin": sorted(active_doc_ids)}
    if user_id:
        query["user_id"] = user_id
    jobs = await db["extraction_jobs"].find(query, {"_id": 0}).sort("updated_at", 1).limit(limit).to_list(length=limit)
    if not jobs:
        return {
            "status": "complete",
            "corpus_id": corpus_id,
            "requested": limit,
            "claimed": 0,
            "reclaimed": reclaimed,
            "terminal_reconciliation": terminal_reconciliation,
            "active_ingest_docs_excluded": len(active_doc_ids),
            "counts": {},
            "docs": [],
        }

    candidate_count = len(jobs)
    jobs = await claim_runnable_jobs(
        db,
        collection_name="extraction_jobs",
        jobs=jobs,
        runnable_statuses=allowed_statuses,
        now=now,
        runner="extraction_jobs.run",
        increment_attempt=False,
    )
    if not jobs:
        return {
            "status": "complete",
            "corpus_id": corpus_id,
            "requested": limit,
            "candidates": candidate_count,
            "claimed": 0,
            "reclaimed": reclaimed,
            "terminal_reconciliation": terminal_reconciliation,
            "active_ingest_docs_excluded": len(active_doc_ids),
            "counts": {},
            "docs": [],
        }

    jobs_by_doc: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_doc.setdefault(str(job.get("doc_id") or ""), []).append(job)

    from config import get_settings

    configured_doc_concurrency = max(
        1,
        int(get_settings().EXTRACTION_REPAIR_MAX_ACTIVE_DOCS or 1),
    )
    effective_doc_concurrency = min(configured_doc_concurrency, len(jobs_by_doc))
    doc_semaphore = asyncio.Semaphore(max(1, effective_doc_concurrency))

    async def _run_doc_jobs(
        doc_id: str,
        doc_jobs: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        async with doc_semaphore:
            if doc_id in await active_ingest_doc_ids(
                db,
                corpus_id=corpus_id,
            ):
                deferred_at = datetime.utcnow()
                await _mark_jobs(
                    db,
                    updates={
                        job["job_id"]: {
                            "status": "queued",
                            "reason": "active_ingest_owned",
                            "runner": None,
                            "lease_until": None,
                            "updated_at": deferred_at,
                        }
                        for job in doc_jobs
                    },
                )
                return (
                    {
                        "doc_id": doc_id,
                        "status": "deferred",
                        "reason": "active_ingest_owned",
                    },
                    {"deferred": len(doc_jobs)},
                )
            chunk_ids = [
                str(job.get("chunk_id") or "")
                for job in doc_jobs
                if job.get("chunk_id")
            ]
            doc = await db["documents"].find_one(
                {"doc_id": doc_id, "corpus_id": corpus_id},
                {"_id": 0},
            )
            updates: dict[str, dict[str, Any]] = {}
            if not doc:
                for job in doc_jobs:
                    updates[job["job_id"]] = build_extraction_job_run_update(
                        job,
                        skipped_reason="document_missing",
                        now=now,
                    )
                await _mark_jobs(db, updates=updates)
                return (
                    {"doc_id": doc_id, "status": "skipped", "reason": "document_missing"},
                    {"skipped": len(updates)},
                )

            try:
                tasks, _all_chunk_ids = await _extract_tasks(
                    db=db,
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    chunk_ids=chunk_ids,
                )
                task_ids = {task.chunk_id for task in tasks}
                skipped_ids = set(chunk_ids) - task_ids
                for job in doc_jobs:
                    if str(job.get("chunk_id") or "") in skipped_ids:
                        updates[job["job_id"]] = build_extraction_job_run_update(
                            job,
                            skipped_reason="no_extractable_text_or_skipped_kind",
                            now=now,
                        )
                if skipped_ids:
                    await _persist_skipped_extraction_rows(
                        db,
                        doc_id=doc_id,
                        corpus_id=corpus_id,
                        chunk_ids=sorted(skipped_ids),
                        reason="no_extractable_text_or_skipped_kind",
                    )
                if tasks:
                    config = await _load_backfill_config(
                        db=db,
                        corpus_id=corpus_id,
                        doc=doc,
                    )
                    report = await _run_ghost_b_backfill(
                        db=db,
                        qdrant_client=qdrant_client,
                        corpus_id=corpus_id,
                        tasks=tasks,
                        config=config,
                    )
                    await _persist_extraction_rows(
                        db,
                        doc_id=doc_id,
                        corpus_id=corpus_id,
                        results=list(report.results),
                        failures=list(report.failures),
                    )
                    success_by_id = {
                        result.chunk_id: _asdict(result)
                        for result in report.results
                        if result.chunk_id
                    }
                    failure_by_id = {
                        failure.chunk_id: _asdict(failure)
                        for failure in report.failures
                        if failure.chunk_id
                    }
                    for job in doc_jobs:
                        chunk_id = str(job.get("chunk_id") or "")
                        if job["job_id"] in updates:
                            continue
                        if chunk_id in success_by_id:
                            updates[job["job_id"]] = build_extraction_job_run_update(
                                job,
                                succeeded=True,
                                result=success_by_id[chunk_id],
                                now=now,
                            )
                        elif chunk_id in failure_by_id:
                            updates[job["job_id"]] = build_extraction_job_run_update(
                                job,
                                failure=failure_by_id[chunk_id],
                                now=now,
                            )
                        else:
                            updates[job["job_id"]] = build_extraction_job_run_update(
                                job,
                                failure={
                                    "error_type": "NoResult",
                                    "error_message": (
                                        "Ghost B returned neither a result nor a failure "
                                        "for this chunk"
                                    ),
                                },
                                now=now,
                            )
                    metrics = dict(report.metrics or {})
                else:
                    metrics = {
                        "requested_chunks": len(chunk_ids),
                        "extracted_chunks": 0,
                        "failed_chunks": 0,
                    }

                doc_counts = await _refresh_document_extraction_counts(
                    db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                )
                await _mark_jobs(db, updates=updates)
                status_counts = {
                    status: sum(
                        1 for update in updates.values() if update["status"] == status
                    )
                    for status in sorted(
                        {update["status"] for update in updates.values()}
                    )
                }
                return (
                    {
                        "doc_id": doc_id,
                        "status": "complete",
                        "requested_chunks": len(chunk_ids),
                        "ran_chunks": len(tasks),
                        "job_counts": status_counts,
                        "document_counts": doc_counts,
                        "metrics": metrics,
                    },
                    status_counts,
                )
            except Exception as exc:  # noqa: BLE001
                for job in doc_jobs:
                    updates[job["job_id"]] = build_extraction_job_run_update(
                        job,
                        error=exc,
                        now=now,
                    )
                await _mark_jobs(db, updates=updates)
                status_counts: dict[str, int] = {}
                for update in updates.values():
                    status_counts[update["status"]] = (
                        status_counts.get(update["status"], 0) + 1
                    )
                return (
                    {
                        "doc_id": doc_id,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:1000],
                    },
                    status_counts,
                )

    completed_docs = await asyncio.gather(
        *(
            _run_doc_jobs(doc_id, doc_jobs)
            for doc_id, doc_jobs in jobs_by_doc.items()
        )
    )
    counts: dict[str, int] = {"claimed": len(jobs)}
    doc_results: list[dict[str, Any]] = []
    for doc_result, status_counts in completed_docs:
        doc_results.append(doc_result)
        for status, status_count in status_counts.items():
            counts[status] = counts.get(status, 0) + status_count

    return {
        "status": "complete",
        "corpus_id": corpus_id,
        "requested": limit,
        "claimed": len(jobs),
        "reclaimed": reclaimed,
        "terminal_reconciliation": terminal_reconciliation,
        "active_ingest_docs_excluded": len(active_doc_ids),
        "document_concurrency": {
            "configured": configured_doc_concurrency,
            "effective": effective_doc_concurrency,
        },
        "counts": counts,
        "docs": doc_results,
    }


async def list_extraction_jobs(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 100,
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), 1000))
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if statuses:
        query["status"] = {"$in": statuses}
    rows = await db["extraction_jobs"].find(query, {"_id": 0}).sort("updated_at", -1).limit(limit).to_list(length=limit)
    counts_rows = await db["extraction_jobs"].aggregate([
        {"$match": {"corpus_id": corpus_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]).to_list(length=None)
    return {
        "corpus_id": corpus_id,
        "counts": {str(row["_id"]): int(row["count"]) for row in counts_rows},
        "jobs": rows,
    }
