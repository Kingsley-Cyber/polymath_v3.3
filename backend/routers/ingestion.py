"""
Ingestion router — corpus management + durable backend ingest.

Endpoints:
  POST /api/corpora                                    — create corpus
  GET  /api/corpora                                    — list corpora (user-scoped)
  GET  /api/corpora/{corpus_id}                        — get corpus by ID
  POST /api/corpora/{corpus_id}/ingest                 — disabled browser upload
  POST /api/corpora/{corpus_id}/ingest-batches/local   — durable backend folder ingest
  GET  /api/ingestion/jobs/{doc_id}                    — poll ingest job status
  GET  /api/ingestion/jobs/{doc_id}/stream             — SSE stream ingest progress
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from config import get_settings
from models.schemas import (
    CorpusCreate,
    CorpusResponse,
    IngestionConfig,
    IngestJobResponse,
    ModelProfileRef,
    WriteState,
)
from pydantic import BaseModel, Field
from routers.auth import get_current_user
from services.ingestion_service import FrozenFieldError, ingestion_service
from services.ingestion import batches as ingest_batches
from services.ingestion import dedup
from utils.streaming import build_sse_done, build_sse_error

# Phase K — strong references to in-flight ingest tasks so asyncio doesn't GC
# them after the HTTP response returns. Entries clear themselves via
# add_done_callback.
_INGEST_BG_TASKS: set[asyncio.Task] = set()
_BACKFILL_BG_TASKS: set[asyncio.Task] = set()
_BACKGROUND_REPAIR_HEARTBEAT_SECONDS = 30
_BACKGROUND_REPAIR_LEASE_SECONDS = 120
_BACKGROUND_REPAIR_QUEUE_LEASE_SECONDS = 300
_BACKGROUND_REPAIR_LEGACY_STALE_SECONDS = 600
_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
# Slot primitives extracted to services/ingestion/admission.py so the
# MCP write surface (polymath_mcp/tools.py:_ingest_bytes) can share
# the same gate. Pre-extraction the MCP path bypassed the HTTP slot
# check entirely — see audit Bug #1. We re-export the same names
# (`_try_acquire_ingest_slot`, `_release_ingest_slot`,
# `_INGEST_ACTIVE_LIMIT`, `_INGEST_ACTIVE_COUNT`) so the existing
# source-code pin in test_ingest_slot_ordering.py keeps catching
# regressions.
from services.ingestion import admission as _admission

_INGEST_ACTIVE_LIMIT = _admission.INGEST_ACTIVE_LIMIT
_try_acquire_ingest_slot = _admission.try_acquire_ingest_slot
_release_ingest_slot = _admission.release_ingest_slot


def _get_active_count() -> int:
    """Diagnostic accessor — admin endpoint uses this."""
    return _admission.active_count()


async def _start_batch_runner_if_enabled(*, batch_id: str, user_id: str) -> bool:
    """Start a durable batch only from processes allowed to own ingest memory.

    Offline-ingest deployments run the public/query API with
    INGEST_RUNNERS_ENABLED=false and a separate 20 GB worker with the flag true.
    The query API still creates/resumes durable batches; the worker discovers
    them through startup/poll recovery.
    """
    # The public API and ingest worker are separate processes in offline-ingest
    # deployments. Persist the user's Run/Resume intent before checking process
    # ownership so the worker poller can distinguish it from a manifest-only
    # batch that was deliberately staged without execution.
    await ingestion_service.db["ingest_batches"].update_one(
        {"batch_id": batch_id, "user_id": user_id},
        {
            "$set": {
                "run_requested_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            },
            "$unset": {"run_deferred_reason": ""},
        },
    )
    if not bool(get_settings().INGEST_RUNNERS_ENABLED):
        logger.info(
            "Ingest runner start deferred: batch=%s "
            "INGEST_RUNNERS_ENABLED=false",
            batch_id[:8],
        )
        return False
    return ingest_batches.start_local_batch_runner(
        db=ingestion_service.db,
        ingestion_service=ingestion_service,
        batch_id=batch_id,
        user_id=user_id,
    )


# Keep this below frontend/nginx.conf's 300s proxy timeout. OCR is disabled,
# but layout-heavy documents can still take time before doc_id exists.
PARSE_DOC_ID_WAIT_SECONDS = 240.0


def _safe_ingest_error(exc: Exception) -> str:
    message = _SECRET_RE.sub("sk-...[redacted]", str(exc))
    return message[:1000] or exc.__class__.__name__


async def _heartbeat_background_repair(run_id: str) -> None:
    """Renew a durable repair lease while this process owns the task."""

    while True:
        await asyncio.sleep(_BACKGROUND_REPAIR_HEARTBEAT_SECONDS)
        now = datetime.utcnow()
        try:
            result = await ingestion_service.db["ingest_repair_runs"].update_one(
                {"run_id": run_id, "status": "running"},
                {
                    "$set": {
                        "heartbeat_at": now,
                        "updated_at": now,
                        "lease_expires_at": now
                        + timedelta(seconds=_BACKGROUND_REPAIR_LEASE_SECONDS),
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background repair heartbeat failed run=%s: %s", run_id, exc)
            continue
        if result.matched_count == 0:
            return


async def _attach_corpus_readiness(
    result: dict,
    *,
    corpus_id: str,
    context: str,
) -> dict:
    """Attach a fresh corpus-readiness snapshot after state-changing ops.

    Batch/job rows are run history. The UI's truth bar reads the materialized
    corpus readiness view, so every endpoint that mutates durable ingestion
    state should rematerialize it before returning.
    """

    try:
        from services.ingestion.readiness import materialize_corpus_readiness

        result["readiness"] = await materialize_corpus_readiness(
            ingestion_service.db,
            corpus_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "readiness refresh after %s failed corpus=%s: %s",
            context,
            corpus_id[:8],
            exc,
        )
    return result


async def _mark_ingest_failed(
    *,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    exc: Exception,
) -> None:
    db = ingestion_service.db
    if db is None:
        return
    message = _safe_ingest_error(exc)
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id, "user_id": user_id},
        {
            "$set": {
                "error": message,
                "updated_at": datetime.utcnow(),
            },
            "$addToSet": {
                "write_state.warnings": f"Ingest failed: {message}",
            },
        },
    )


class CorpusUpdate(BaseModel):
    """PUT /api/corpora/{corpus_id} request body — partial update."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    default_ingestion_config: IngestionConfig | None = None


class LocalIngestBatchRequest(BaseModel):
    """Create a durable backend-owned ingest batch from a local folder path."""

    root_path: str = Field(..., min_length=1)
    profile: Literal[
        "mac_safe", "mac_queryable_first", "rtx_assisted", "runpod_burst",
        "runpod_extract_first"
    ] | None = None
    recursive: bool = True
    extensions: list[str] | None = None
    max_files: int | None = Field(default=None, ge=1, le=20000)
    store_files: bool = True
    max_total_bytes: int | None = Field(default=None, ge=1, le=2 * 1024 * 1024 * 1024)
    use_neo4j: bool | None = None
    chunk_summarization: bool | None = None
    model: str = ""
    concurrency: int | None = Field(default=None, ge=1, le=32)
    summary_cost_authority_usd: Decimal | None = Field(
        default=None,
        gt=0,
        le=Decimal("10000"),
    )
    start: bool = True


class StaleIngestReconcileRequest(BaseModel):
    stale_after_minutes: int | None = Field(default=None, ge=1, le=1440)
    auto_backfill_graph: bool = True


class FailureMetadataReconcileRequest(BaseModel):
    apply: bool = False
    limit: int = Field(default=5000, ge=1, le=50000)


class SourceParseJobPlanRequest(BaseModel):
    apply: bool = False
    limit: int = Field(default=500, ge=1, le=10000)


class SourceParseJobRunRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=500)
    statuses: list[str] | None = None


class GraphPromotionPlanRequest(BaseModel):
    apply: bool = False
    limit: int = Field(default=100, ge=1, le=5000)
    max_chunks: int | None = Field(default=None, ge=1, le=50000)


class GraphPromotionRunRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=100)


class ExtractionJobPlanRequest(BaseModel):
    apply: bool = False
    limit: int = Field(default=500, ge=1, le=10000)
    include_succeeded: bool = False


class ExtractionJobRunRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=500)
    statuses: list[str] | None = None


class SummaryJobPlanRequest(BaseModel):
    apply: bool = False
    limit: int = Field(default=500, ge=1, le=10000)
    kinds: list[str] | None = None


class SummaryJobRunRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=500)
    statuses: list[str] | None = None
    kinds: list[str] | None = None
    summary_cost_authority_usd: Decimal | None = Field(
        default=None,
        gt=0,
        le=Decimal("10000"),
    )


class DocumentPipelineJobPlanRequest(BaseModel):
    apply: bool = False
    limit: int = Field(default=500, ge=1, le=10000)
    kinds: list[str] | None = None


class DocumentPipelineJobRunRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=500)
    statuses: list[str] | None = None
    kinds: list[str] | None = None


class IngestionJobControlRequest(BaseModel):
    action: Literal["retry", "supersede", "dead_letter"]
    reason: str = Field(min_length=3, max_length=500)


class CorpusRepairCycleRequest(BaseModel):
    apply: bool = False
    background: bool = False
    reconcile_failures: bool = True
    failure_reconcile_limit: int = Field(default=5000, ge=1, le=50000)
    backfill_promoted_extraction_marks_rows: bool = True
    promoted_extraction_marks_backfill_limit: int = Field(default=100, ge=0, le=50000)
    backfill_source_parse_stage_identity_rows: bool = True
    source_parse_stage_identity_backfill_limit: int = Field(default=1000, ge=0, le=50000)
    backfill_ghost_b_stage_identity_rows: bool = True
    ghost_b_stage_identity_backfill_limit: int = Field(default=1000, ge=0, le=50000)
    plan_source_parse_jobs: bool = True
    source_parse_job_plan_limit: int = Field(default=500, ge=1, le=10000)
    run_source_parse_jobs: bool = False
    source_parse_job_run_limit: int = Field(default=25, ge=1, le=500)
    plan_document_pipeline_jobs: bool = True
    document_pipeline_job_plan_limit: int = Field(default=500, ge=1, le=10000)
    run_document_pipeline_jobs: bool = False
    document_pipeline_job_run_limit: int = Field(default=25, ge=1, le=500)
    plan_graph_jobs: bool = True
    graph_plan_limit: int = Field(default=100, ge=1, le=5000)
    graph_max_chunks: int | None = Field(default=None, ge=1, le=50000)
    plan_extraction_jobs: bool = True
    extraction_job_plan_limit: int = Field(default=500, ge=1, le=10000)
    run_extraction_jobs: bool = False
    extraction_job_run_limit: int = Field(default=25, ge=1, le=500)
    plan_summary_jobs: bool = True
    summary_job_plan_limit: int = Field(default=500, ge=1, le=10000)
    backfill_summary_stage_identity_rows: bool = True
    summary_stage_identity_backfill_limit: int = Field(default=1000, ge=0, le=50000)
    run_summary_jobs: bool = False
    summary_job_run_limit: int = Field(default=25, ge=1, le=500)
    summary_cost_authority_usd: Decimal | None = Field(
        default=None,
        gt=0,
        le=Decimal("10000"),
    )
    run_document_summaries: bool = False
    document_summary_limit: int = Field(default=10, ge=1, le=500)
    run_graph_jobs: bool = False
    graph_run_limit: int = Field(default=3, ge=1, le=100)


class SummaryBackfillRequest(BaseModel):
    """Repair parent summaries for an already-ingested corpus."""

    generate: bool = True
    index: bool = True
    limit: int | None = Field(
        default=200,
        ge=0,
        le=5000,
        description="Max missing parent summaries to generate in this call.",
    )
    batch: int = Field(default=32, ge=1, le=128)
    doc_ids: list[str] | None = Field(
        default=None,
        description="Optional exact document scope for deterministic repair.",
    )
    index_existing_doc_summaries: bool = Field(
        default=False,
        description=(
            "Reindex every existing canonical parent summary in doc_ids; "
            "requires an explicit document scope."
        ),
    )
    background: bool = Field(
        default=False,
        description="Queue the bounded summary repair as a background repair run.",
    )
    summary_cost_authority_usd: Decimal | None = Field(
        default=None,
        gt=0,
        le=Decimal("10000"),
    )


class DocumentSummaryBackfillRequest(BaseModel):
    """Repair document-level summary profiles for an already-ingested corpus."""

    limit: int = Field(default=25, ge=1, le=500)
    doc_ids: list[str] | None = None
    summary_cost_authority_usd: Decimal | None = Field(
        default=None,
        gt=0,
        le=Decimal("10000"),
    )


class RescanIngestBatchRequest(BaseModel):
    start: bool = True


class ModelRefTestRequest(BaseModel):
    """Ad-hoc probe for ingestion model-pool entries.

    The entry is not persisted. When an existing corpus chip has api_key="[set]",
    corpus_id + pool_field + index let the backend load/decrypt the saved key.
    """

    kind: Literal["chat", "embedding"] = "chat"
    entry: ModelProfileRef
    corpus_id: str | None = None
    pool_field: Literal["summary_models", "extraction_models", "embedding_models"] | None = None
    index: int | None = Field(default=None, ge=0)


class ModelRefTestResult(BaseModel):
    ok: bool
    kind: Literal["chat", "embedding"]
    status: int | None = None
    latency_ms: int | None = None
    model: str | None = None
    base_url: str | None = None
    dimension: int | None = None
    error: str | None = None
    cached: bool = False


class ModelRefModelsResult(BaseModel):
    ok: bool
    status: int | None = None
    latency_ms: int | None = None
    base_url: str | None = None
    models: list[str] = Field(default_factory=list)
    error: str | None = None


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ingestion"])


def _safe_provider_text(text: str) -> str:
    return _SECRET_RE.sub("sk-...[redacted]", text.replace("\n", " "))[:1000]


async def _model_ref_for_test(
    body: ModelRefTestRequest,
    *,
    user_id: str,
) -> tuple[dict, str | None]:
    data = body.entry.model_dump()

    profile_id = str(data.get("profile_id") or "").strip()
    if profile_id:
        from services.settings import settings_service

        registry = await settings_service.get_ingestion_provider_registry_raw(user_id)
        saved = next(
            (
                entry
                for entry in registry
                if isinstance(entry, dict)
                and str(entry.get("profile_id") or "") == profile_id
            ),
            None,
        )
        if saved is None:
            return data, "Saved ingestion provider no longer exists in Settings."
        concurrency = data.get("max_concurrent")
        data = dict(saved)
        if concurrency is not None:
            data["max_concurrent"] = concurrency

    # Non-ASCII guard (2026-07-04): keys/urls pasted from formatted sources
    # arrive with smart dashes/quotes (em-dash \u2014 etc.). They end up in
    # HTTP headers, which must be ASCII — litellm then 500s with a cryptic
    # "'ascii' codec can't encode character" from deep inside the provider
    # client. Fail HERE with a message that names the field and character.
    for fld in ("api_key", "base_url", "model", "lifecycle_base_url", "lifecycle_api_key"):
        val = data.get(fld)
        if isinstance(val, str) and val and val != "[set]":
            cleaned = val.strip()
            for i, chx in enumerate(cleaned):
                if ord(chx) > 127:
                    return data, (
                        f"{fld} contains a non-ASCII character {chx!r} at "
                        f"position {i} — usually a smart dash/quote from "
                        "copy-paste. Re-paste it from the provider console "
                        "as plain text."
                    )
            data[fld] = cleaned

    masked_secret_fields = [
        field
        for field in ("api_key", "lifecycle_api_key")
        if data.get(field) == "[set]"
    ]
    if masked_secret_fields:
        if not (body.corpus_id and body.pool_field is not None and body.index is not None):
            return data, "Saved key is masked. Type a new key or save the corpus first."

        corpus = await ingestion_service._get_corpus_raw(body.corpus_id)
        if not corpus or corpus.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Corpus not found")

        pool = (corpus.get("default_ingestion_config") or {}).get(body.pool_field) or []
        requested_model = str(data.get("model") or "").strip()
        requested_base = str(data.get("base_url") or "").strip()

        def _same_entry(candidate: dict) -> bool:
            return (
                str(candidate.get("model") or "").strip() == requested_model
                and str(candidate.get("base_url") or "").strip() == requested_base
            )

        saved_entry = (
            pool[body.index]
            if body.index < len(pool) and isinstance(pool[body.index], dict)
            else None
        )
        if saved_entry is not None and not _same_entry(saved_entry):
            saved_entry = next(
                (candidate for candidate in pool if isinstance(candidate, dict) and _same_entry(candidate)),
                None,
            )
        if saved_entry is None:
            return data, "Saved key could not be resolved for this model chip."

        for field in masked_secret_fields:
            raw_key = saved_entry.get(field)
            if not raw_key:
                return data, f"No {field} is saved for this model chip."
            data[field] = raw_key

    if data.get("api_key") or data.get("lifecycle_api_key"):
        from services.secrets import decrypt

        for field in ("api_key", "lifecycle_api_key"):
            if data.get(field):
                plaintext = decrypt(data[field])
                if plaintext is not None:
                    data[field] = plaintext

    return data, None


async def _test_chat_model_ref(entry: dict, *, db: Any = None) -> ModelRefTestResult:
    settings = get_settings()
    model = str(entry.get("model") or "").strip()
    if not model:
        return ModelRefTestResult(ok=False, kind="chat", error="Model is required")

    if db is not None:
        from services.ingestion.provider_canary_cache import load_cached_canary

        cached = await load_cached_canary(db, entry=entry)
        if cached is not None:
            return ModelRefTestResult(
                ok=bool(cached.get("ok")),
                kind="chat",
                status=cached.get("status"),
                latency_ms=cached.get("latency_ms"),
                model=model,
                base_url=(entry.get("base_url") or "").strip() or None,
                error=cached.get("error_class") if not cached.get("ok") else None,
                cached=True,
            )

    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with ok."}],
        "temperature": 0,
        "max_tokens": 1,
        "stream": False,
    }
    base_url = (entry.get("base_url") or "").strip() or None
    if base_url:
        payload["api_base"] = base_url
    if entry.get("api_key"):
        payload["api_key"] = entry["api_key"]
    for key, value in (entry.get("extra_params") or {}).items():
        if key not in {"model", "messages", "response_format"}:
            payload[key] = value

    headers = {
        "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }
    started = time.monotonic()
    from services.ingestion.model_lifecycle import (
        ensure_model_lifecycle_ready,
        shutdown_model_lifecycle,
    )

    try:
        await ensure_model_lifecycle_ready([entry], purpose="model_ref_test")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{settings.LITELLM_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            result = ModelRefTestResult(
                ok=False,
                kind="chat",
                status=resp.status_code,
                latency_ms=latency_ms,
                model=model,
                base_url=base_url,
                error=_safe_provider_text(resp.text),
            )
        else:
            result = ModelRefTestResult(
                ok=True,
                kind="chat",
                status=resp.status_code,
                latency_ms=latency_ms,
                model=model,
                base_url=base_url,
            )
        if db is not None:
            from services.ingestion.provider_canary_cache import record_canary

            await record_canary(
                db,
                entry=entry,
                ok=result.ok,
                status=result.status,
                latency_ms=result.latency_ms,
                error_class=(
                    "rate_limited"
                    if result.status == 429
                    else (f"http_{result.status}" if not result.ok else None)
                ),
            )
        return result
    except httpx.TimeoutException:
        result = ModelRefTestResult(
            ok=False,
            kind="chat",
            model=model,
            base_url=base_url,
            error="Request timed out after 20s",
        )
        if db is not None:
            from services.ingestion.provider_canary_cache import record_canary

            await record_canary(
                db,
                entry=entry,
                ok=False,
                status=None,
                latency_ms=None,
                error_class="timeout",
            )
        return result
    except Exception as exc:
        logger.warning("ingestion model chat probe failed: %s", exc)
        result = ModelRefTestResult(
            ok=False,
            kind="chat",
            model=model,
            base_url=base_url,
            error=_safe_provider_text(str(exc)),
        )
        if db is not None:
            from services.ingestion.provider_canary_cache import record_canary

            await record_canary(
                db,
                entry=entry,
                ok=False,
                status=None,
                latency_ms=None,
                error_class=type(exc).__name__,
            )
        return result
    finally:
        await shutdown_model_lifecycle([entry], purpose="model_ref_test")


async def _test_embedding_model_ref(entry: dict) -> ModelRefTestResult:
    model = str(entry.get("model") or "").strip()
    base_url = (entry.get("base_url") or "").strip()
    api_key = (entry.get("api_key") or "").strip()
    if not model:
        return ModelRefTestResult(ok=False, kind="embedding", error="Model is required")
    if not base_url:
        return ModelRefTestResult(ok=False, kind="embedding", model=model, error="Base URL is required")
    if not api_key:
        return ModelRefTestResult(
            ok=False,
            kind="embedding",
            model=model,
            base_url=base_url,
            error="API key is required for embedding API pool entries",
        )

    url = base_url.rstrip("/")
    if not url.endswith("/embeddings"):
        url = url + "/embeddings"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                json={"input": ["health"], "model": model},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            return ModelRefTestResult(
                ok=False,
                kind="embedding",
                status=resp.status_code,
                latency_ms=latency_ms,
                model=model,
                base_url=base_url,
                error=_safe_provider_text(resp.text),
            )
        body_json = resp.json()
        vector = ((body_json.get("data") or [{}])[0] or {}).get("embedding") or []
        dimension = len(vector) if isinstance(vector, list) else None
        return ModelRefTestResult(
            ok=True,
            kind="embedding",
            status=resp.status_code,
            latency_ms=latency_ms,
            model=model,
            base_url=base_url,
            dimension=dimension,
        )
    except httpx.TimeoutException:
        return ModelRefTestResult(
            ok=False,
            kind="embedding",
            model=model,
            base_url=base_url,
            error="Request timed out after 20s",
        )
    except Exception as exc:
        logger.warning("ingestion model embedding probe failed: %s", exc)
        return ModelRefTestResult(
            ok=False,
            kind="embedding",
            model=model,
            base_url=base_url,
            error=_safe_provider_text(str(exc)),
        )


def _model_ids_from_models_payload(payload: dict) -> list[str]:
    raw = payload.get("data")
    if raw is None:
        raw = payload.get("models")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        model_id = None
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
        elif isinstance(item, str):
            model_id = item
        if model_id:
            out.append(str(model_id))
    return sorted(dict.fromkeys(out))


async def _list_model_ref_models(entry: dict) -> ModelRefModelsResult:
    base_url = (entry.get("base_url") or "").strip()
    if not base_url:
        return ModelRefModelsResult(
            ok=False,
            error="Base URL is required to list live provider models.",
        )
    url = base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    if entry.get("api_key"):
        headers["Authorization"] = f"Bearer {entry['api_key']}"

    from services.ingestion.model_lifecycle import (
        ensure_model_lifecycle_ready,
        shutdown_model_lifecycle,
    )

    started = time.monotonic()
    try:
        await ensure_model_lifecycle_ready([entry], purpose="model_ref_models")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            return ModelRefModelsResult(
                ok=False,
                status=resp.status_code,
                latency_ms=latency_ms,
                base_url=base_url,
                error=_safe_provider_text(resp.text),
            )
        models = _model_ids_from_models_payload(resp.json())
        return ModelRefModelsResult(
            ok=True,
            status=resp.status_code,
            latency_ms=latency_ms,
            base_url=base_url,
            models=models[:200],
        )
    except httpx.TimeoutException:
        return ModelRefModelsResult(
            ok=False,
            base_url=base_url,
            error="Request timed out after 20s",
        )
    except Exception as exc:
        logger.warning("ingestion model list probe failed: %s", exc)
        return ModelRefModelsResult(
            ok=False,
            base_url=base_url,
            error=_safe_provider_text(str(exc)),
        )
    finally:
        await shutdown_model_lifecycle([entry], purpose="model_ref_models")


def _resolve_ingest_progress(
    doc: dict,
    *,
    neo4j_enabled: bool | None = None,
) -> dict:
    """Return the externally visible ingest status/stage from write_state."""
    ws_raw = doc.get("write_state") or {}
    cfg = doc.get("ingestion_config") or {}
    target_qdrant_collections = cfg.get("target_qdrant_collections") or []
    qdrant_required = bool(target_qdrant_collections)
    summary_required = bool(
        cfg.get("chunk_summarization")
        and any(k in ("naive", "hrag") for k in target_qdrant_collections)
    )
    if neo4j_enabled is None:
        neo4j_enabled = bool(get_settings().NEO4J_ENABLED)
    neo4j_required = bool(cfg.get("use_neo4j")) and neo4j_enabled

    mongo_done = bool(ws_raw.get("mongo_written", False))
    qdrant_done = bool(ws_raw.get("qdrant_written", False))
    summaries_indexed_raw = ws_raw.get("summaries_indexed")
    summaries_indexed = (
        bool(summaries_indexed_raw)
        if summaries_indexed_raw is not None
        else qdrant_done
    )
    neo4j_done = bool(ws_raw.get("neo4j_written", False))
    verified = ws_raw.get("verified")
    verify_errors = ws_raw.get("verify_errors", []) or []
    warnings = ws_raw.get("warnings", []) or []
    error = doc.get("error")

    # Near-duplicate skip is a terminal, non-error outcome — surface it directly
    # so the UI shows "skipped" instead of a doc stuck in "processing" (its
    # write_state stays all-False because nothing is written).
    if doc.get("ingest_stage") == "skipped_duplicate":
        return {
            "status": "skipped_duplicate",
            "stage": "skipped_duplicate",
            "mongo_done": mongo_done,
            "qdrant_done": qdrant_done,
            "summaries_indexed": summaries_indexed,
            "neo4j_done": neo4j_done,
            "verified": verified,
            "verify_errors": verify_errors,
            "warnings": warnings,
            "error": doc.get("skipped_reason") or error,
        }

    if doc.get("ingest_stage") == "awaiting_summary":
        return {
            "status": "awaiting_summary",
            "stage": "awaiting_summary",
            "mongo_done": mongo_done,
            "qdrant_done": qdrant_done,
            "summaries_indexed": summaries_indexed,
            "neo4j_done": neo4j_done,
            "verified": verified,
            "verify_errors": verify_errors,
            "warnings": warnings,
            "error": doc.get("summary_pending_reason"),
        }

    ingest_stage = str(doc.get("ingest_stage") or "")
    if ingest_stage.startswith("queryable_with_pending_") or ingest_stage == "queryable":
        return {
            "status": ingest_stage,
            "stage": ingest_stage,
            "mongo_done": mongo_done,
            "qdrant_done": qdrant_done,
            "summaries_indexed": summaries_indexed,
            "neo4j_done": neo4j_done,
            "verified": verified,
            "verify_errors": verify_errors,
            "warnings": warnings,
            "error": doc.get("enrichment_pending_reason") or doc.get("summary_pending_reason"),
        }

    required_qdrant_done = (not qdrant_required) or qdrant_done
    required_summary_done = (not summary_required) or summaries_indexed
    required_neo4j_done = (not neo4j_required) or neo4j_done

    if error:
        status = "failed"
    elif verified is False:
        status = "failed"
    elif (
        mongo_done
        and required_qdrant_done
        and required_summary_done
        and required_neo4j_done
        and verified is True
    ):
        status = "done"
    else:
        status = "processing"

    if error:
        stage = "failed"
    elif verified is False:
        stage = "verify_failed"
    elif status == "done":
        stage = "verified"
    elif (
        mongo_done
        and required_qdrant_done
        and required_summary_done
        and required_neo4j_done
    ):
        stage = "verifying"
    elif summary_required and mongo_done and qdrant_done and not summaries_indexed:
        stage = "summary_indexing"
    elif (
        neo4j_required
        and mongo_done
        and required_qdrant_done
        and required_summary_done
        and not neo4j_done
    ):
        stage = "graph_extracting"
    elif qdrant_required and mongo_done and not qdrant_done:
        stage = "embedding"
    elif mongo_done:
        stage = "verifying"
    else:
        stage = "ingesting"

    return {
        "status": status,
        "stage": stage,
        "mongo_done": mongo_done,
        "qdrant_done": qdrant_done,
        "summaries_indexed": summaries_indexed,
        "neo4j_done": neo4j_done,
        "verified": verified,
        "verify_errors": verify_errors,
        "warnings": warnings,
        "error": error,
    }


@router.post("/corpora", response_model=CorpusResponse, status_code=201)
async def create_corpus(
    body: CorpusCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a new corpus. Returns the created corpus record."""
    doc = await ingestion_service.create_corpus(
        name=body.name,
        description=body.description,
        user_id=current_user["user_id"],
        ingestion_config=body.default_ingestion_config,
    )
    return CorpusResponse(
        corpus_id=doc["corpus_id"],
        name=doc["name"],
        description=doc.get("description"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        doc_count=doc.get("doc_count", 0),
        ready_doc_count=doc.get("ready_doc_count", 0),
        chunk_count=doc.get("chunk_count", 0),
        embedding_model_id=doc.get("embedding_model_id"),
        default_ingestion_config=IngestionConfig(**doc["default_ingestion_config"]),
        readiness=doc.get("readiness"),
    )


@router.get("/corpora", response_model=list[CorpusResponse])
async def list_corpora(
    current_user: dict = Depends(get_current_user),
):
    """List all corpora owned by the current user."""
    docs = await ingestion_service.list_corpora(user_id=current_user["user_id"])
    return [
        CorpusResponse(
            corpus_id=d["corpus_id"],
            name=d["name"],
            description=d.get("description"),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            doc_count=d.get("doc_count", 0),
            ready_doc_count=d.get("ready_doc_count", 0),
            chunk_count=d.get("chunk_count", 0),
            embedding_model_id=d.get("embedding_model_id"),
            default_ingestion_config=IngestionConfig(**d["default_ingestion_config"]),
            readiness=d.get("readiness"),
        )
        for d in docs
    ]


@router.post("/ingestion/model-ref/test", response_model=ModelRefTestResult)
async def test_ingestion_model_ref(
    body: ModelRefTestRequest,
    current_user: dict = Depends(get_current_user),
) -> ModelRefTestResult:
    """Probe an ingestion model-pool chip without persisting it."""
    entry, error = await _model_ref_for_test(body, user_id=current_user["user_id"])
    if error:
        return ModelRefTestResult(
            ok=False,
            kind=body.kind,
            model=entry.get("model"),
            base_url=entry.get("base_url"),
            error=error,
        )
    if body.kind == "embedding":
        return await _test_embedding_model_ref(entry)
    return await _test_chat_model_ref(entry, db=ingestion_service.db)


@router.post("/ingestion/model-ref/models", response_model=ModelRefModelsResult)
async def list_ingestion_model_ref_models(
    body: ModelRefTestRequest,
    current_user: dict = Depends(get_current_user),
) -> ModelRefModelsResult:
    """List live OpenAI-compatible models for an ingestion model-pool chip."""
    entry, error = await _model_ref_for_test(body, user_id=current_user["user_id"])
    if error:
        return ModelRefModelsResult(
            ok=False,
            base_url=entry.get("base_url"),
            error=error,
        )
    return await _list_model_ref_models(entry)


@router.get("/corpora/{corpus_id}/extraction-contract")
async def get_extraction_contract(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Resolved extraction contract for this corpus — EXACTLY as the worker
    resolves it (services/ingestion/extraction_contract.py), plus live sidecar
    probes. The Corpus Manager renders this as its truth line so the active
    workflow is visible in the same screen where models are configured (§13
    ground-truth correction: engine and pools lived on different screens and
    neither showed the resolved contract)."""
    import asyncio as _asyncio

    import httpx as _httpx

    from services.extraction_provider_cards import (
        resolve_extraction_provider_card,
        safe_extraction_pool_contract,
    )
    from services.ingestion.extraction_contract import resolve_extraction_contract
    from services.private_vllm_capacity import fetch_private_vllm_capacity
    from services.settings import settings_service as _ss

    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    cfg = IngestionConfig(**(corpus.get("default_ingestion_config") or {}))

    engine_global = "local"
    endpoints = []
    runpod_config = None
    try:
        ext = await _ss.get_system_extraction()
        engine_global = str(getattr(ext, "engine", "local") or "local")
        endpoints = list(ext.endpoints or [])
        runpod_config, _runpod_key = await _ss.get_system_runpod_flash(
            current_user["user_id"]
        )
    except Exception:  # noqa: BLE001 — resolver defaults are the floor
        pass

    enabled_urls = [
        e.url.strip().rstrip("/")
        for e in endpoints
        if e.enabled and e.url and e.url.strip()
    ]
    provider_pool_refs = (
        cfg.summary_models
        if cfg.models_linked
        else cfg.extraction_models
    )
    contract = resolve_extraction_contract(
        corpus_engine=getattr(cfg, "extraction_engine", None),
        global_engine=engine_global,
        models_linked=cfg.models_linked,
        summary_model_count=len(cfg.summary_models or []),
        extraction_model_count=len(cfg.extraction_models or []),
        enabled_endpoint_urls=enabled_urls,
        provider_pool_entries=provider_pool_refs,
    )

    async def _probe(url: str) -> bool:
        try:
            async with _httpx.AsyncClient(timeout=1.5) as cli:
                r = await cli.get(f"{url}/health")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    alive: dict[str, bool] = {}
    if contract.uses_legacy_local and enabled_urls:
        flags = await _asyncio.gather(*[_probe(u) for u in enabled_urls])
        alive = dict(zip(enabled_urls, flags))

    pool_refs = (
        cfg.extraction_models
        if contract.pool_source == "extraction_models"
        else cfg.summary_models
    )
    async def _capacity_status(m) -> dict | None:
        lifecycle = str(getattr(m, "lifecycle_base_url", "") or "").strip()
        if not lifecycle:
            return None
        lifecycle_api_key = getattr(m, "lifecycle_api_key", None)
        if lifecycle_api_key:
            from services.secrets import decrypt

            lifecycle_api_key = decrypt(lifecycle_api_key) or lifecycle_api_key
        try:
            capacity = await fetch_private_vllm_capacity(
                lifecycle,
                api_key=lifecycle_api_key,
                status_path=str(getattr(m, "lifecycle_status_path", "/status") or "/status"),
                timeout_s=1.5,
            )
            return {"ok": True, **capacity.to_dict()}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "ready": False,
                "error": str(exc)[:200],
            }

    pool_items = []
    safe_pool_contract = None
    if contract.uses_provider_llm and contract.pool_source != "none":
        safe_pool_contract = safe_extraction_pool_contract(
            pool_source=contract.pool_source,
            pool=list(pool_refs or []),
        )
        for m in pool_refs or []:
            card = resolve_extraction_provider_card(m)
            pool_items.append(
                {
                    "provider_preset": m.provider_preset,
                    "model": m.model,
                    "base_url": m.base_url,
                    "max_concurrent": m.max_concurrent,
                    "lifecycle_base_url": m.lifecycle_base_url,
                    "lifecycle_auto_start": m.lifecycle_auto_start,
                    "lifecycle_auto_stop": m.lifecycle_auto_stop,
                    "provider_card": card.to_safe_dict(),
                    "lifecycle_status": await _capacity_status(m)
                    if card.managed_vllm
                    else None,
                }
            )
    pool = (
        pool_items
        if contract.uses_provider_llm and contract.pool_source != "none"
        else []
    )

    contract_errors = list(contract.errors)
    if contract.engine == "runpod_flash":
        if runpod_config is None or not runpod_config.enabled:
            contract_errors.append("Runpod Flash is disabled in Settings")
        elif not runpod_config.endpoint_id.strip():
            contract_errors.append("Runpod Flash endpoint ID is missing in Settings")

    return {
        "engine": contract.engine,
        "source": contract.source,
        "models_linked": cfg.models_linked,
        "pool_source": contract.pool_source if contract.uses_provider_llm else "none",
        "routing_policy": (
            safe_pool_contract.get("routing_policy")
            if safe_pool_contract
            else None
        ),
        "lane_capacities": (
            safe_pool_contract.get("lane_capacities")
            if safe_pool_contract
            else []
        ),
        "pool": pool,
        "runpod_flash": (
            {
                "enabled": bool(runpod_config.enabled),
                "configured": bool(runpod_config.endpoint_id.strip()),
                "endpoint_id": runpod_config.endpoint_id.strip() or None,
                "endpoint_name": runpod_config.endpoint_name,
                "model_id": runpod_config.model_id,
                "request_batch_size": runpod_config.request_batch_size,
                "request_concurrency": runpod_config.request_concurrency,
                "max_workers": runpod_config.max_workers,
            }
            if runpod_config is not None
            else None
        ),
        "endpoints": [
            {
                "label": e.label,
                "url": (e.url or "").strip().rstrip("/"),
                "enabled": bool(e.enabled),
                "alive": alive.get((e.url or "").strip().rstrip("/")),
            }
            for e in endpoints
        ],
        "errors": contract_errors,
        "warnings": list(contract.warnings),
    }


@router.get("/corpora/{corpus_id}", response_model=CorpusResponse)
async def get_corpus(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a single corpus by ID."""
    doc = await ingestion_service.get_corpus(corpus_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return CorpusResponse(
        corpus_id=doc["corpus_id"],
        name=doc["name"],
        description=doc.get("description"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        doc_count=doc.get("doc_count", 0),
        ready_doc_count=doc.get("ready_doc_count", 0),
        chunk_count=doc.get("chunk_count", 0),
        embedding_model_id=doc.get("embedding_model_id"),
        default_ingestion_config=IngestionConfig(**doc["default_ingestion_config"]),
        readiness=doc.get("readiness"),
    )


@router.put("/corpora/{corpus_id}", response_model=CorpusResponse)
async def update_corpus(
    corpus_id: str,
    body: CorpusUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update corpus metadata (name, description, ingestion config)."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.default_ingestion_config is not None:
        # exclude_unset=True — only serialize fields the client actually sent.
        # Otherwise the frozen-field guard trips on Pydantic defaults that
        # happen to differ from existing Mongo values (Phase 21).
        updates["default_ingestion_config"] = body.default_ingestion_config.model_dump(
            exclude_unset=True
        )

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        doc = await ingestion_service.update_corpus(
            corpus_id, updates, user_id=current_user["user_id"]
        )
    except FrozenFieldError as exc:
        # Phase 21 — full FROZEN field lock tripped. Return structured 409
        # so the frontend can display a helpful dialog.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "frozen_field_locked",
                "fields_attempted": exc.fields,
                "reason": (
                    f"Corpus has {exc.doc_count} ingested documents. Frozen "
                    "fields can only be changed on an empty corpus."
                ),
                "solution": (
                    "Delete all documents OR create a new corpus with the "
                    "desired config."
                ),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not doc:
        raise HTTPException(status_code=404, detail="Corpus not found")

    return CorpusResponse(
        corpus_id=doc["corpus_id"],
        name=doc["name"],
        description=doc.get("description"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        doc_count=doc.get("doc_count", 0),
        ready_doc_count=doc.get("ready_doc_count", 0),
        chunk_count=doc.get("chunk_count", 0),
        embedding_model_id=doc.get("embedding_model_id"),
        default_ingestion_config=IngestionConfig(**doc["default_ingestion_config"]),
        readiness=doc.get("readiness"),
    )


@router.delete("/corpora/{corpus_id}")
async def delete_corpus(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a corpus and cascade: documents, chunks, Qdrant vectors, Neo4j nodes."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")

    # Zombie-batch guard (2026-07-04): deleting a corpus mid-ingest left its
    # batch workers running — one burned 127s of GPU embedding 963 children
    # into collections that no longer existed (embed_qdrant 404s). Cancel the
    # corpus's queued/running batch items FIRST so leases die cleanly.
    try:
        db = ingestion_service.db
        if db is not None:
            batch_ids = [
                b["batch_id"]
                async for b in db["ingest_batches"].find(
                    {"corpus_id": corpus_id,
                     "status": {"$in": ["queued", "running"]}},
                    {"batch_id": 1},
                )
            ]
            if batch_ids:
                res = await db["ingest_batch_items"].update_many(
                    {"batch_id": {"$in": batch_ids},
                     "status": {"$in": ["queued", "running", "leased"]}},
                    {"$set": {"status": "cancelled",
                              "error": "Corpus deleted while ingest active"}},
                )
                await db["ingest_batches"].update_many(
                    {"batch_id": {"$in": batch_ids}},
                    {"$set": {"status": "cancelled"}},
                )
                logger.info(
                    "Corpus delete: cancelled %d batch(es), %d item(s) for %s",
                    len(batch_ids), res.modified_count, corpus_id,
                )
    except Exception as exc:  # noqa: BLE001 — delete must still proceed
        logger.warning("Corpus delete: batch cancel failed (%s)", exc)

    deleted = await ingestion_service.delete_corpus(corpus_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete corpus")

    logger.info("Deleted corpus %s (cascade complete)", corpus_id)
    return {"status": "success", "message": "Corpus and all associated data deleted"}


@router.delete("/corpora/{corpus_id}/documents/{doc_id}")
async def delete_document(
    corpus_id: str,
    doc_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a single document and cascade: Qdrant points → Neo4j nodes →
    Mongo chunks → Mongo doc. Corpus aggregate counts are repaired on the
    follow-up corpus read/list call.
    """
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")

    deleted = await ingestion_service.delete_document(corpus_id, doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")

    logger.info(
        "Deleted document %s from corpus %s (cascade complete)",
        doc_id[:12],
        corpus_id[:8],
    )
    return {"status": "success", "doc_id": doc_id}


class ResolveDuplicatesRequest(BaseModel):
    apply: bool = False
    threshold: float | None = Field(default=None, ge=0.02, le=1.0)
    # "certain" (near-identical only — the safe-auto set), "likely", or None
    # (every detected redundant copy). Copies below the bar are reported but not
    # deleted, so the distinct-content cases stay safe by default.
    min_confidence: Literal["certain", "likely", "review"] | None = None
    keep_overrides: dict[str, str] = Field(default_factory=dict)
    # Restrict the operation to specific clusters by their canonical doc_id —
    # the panel's per-cluster "remove" action. None = every detected cluster.
    only_canonicals: list[str] | None = None


@router.get("/corpora/{corpus_id}/duplicates")
async def detect_duplicate_documents(
    corpus_id: str,
    threshold: float | None = Query(default=None, ge=0.02, le=1.0),
    current_user: dict = Depends(get_current_user),
):
    """DETECT — corpus-wide near-duplicate document scan (read-only).

    Deterministic shingle-Jaccard all-pairs clustering (services/ingestion/
    dedup.py). Returns every cluster of near-duplicate documents already inside
    the corpus, each with a suggested canonical copy to keep. Pair with
    POST .../duplicates/resolve to correct them.
    """
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    thr = threshold if threshold is not None else dedup.DEFAULT_DUPLICATE_THRESHOLD
    clusters = await dedup.find_duplicate_clusters(
        ingestion_service.db, corpus_id, threshold=thr
    )
    return {
        "corpus_id": corpus_id,
        "threshold": thr,
        **dedup.summarize_clusters(clusters),
    }


@router.post("/corpora/{corpus_id}/duplicates/resolve")
async def resolve_duplicate_documents(
    corpus_id: str,
    body: ResolveDuplicatesRequest,
    current_user: dict = Depends(get_current_user),
):
    """CORRECT — keep one canonical copy per near-duplicate cluster, cascade-
    delete the redundant ones (Qdrant -> Neo4j -> Mongo chunks -> Mongo doc).

    Dry-run by default (apply=false): returns exactly what WOULD be deleted.
    Set apply=true to execute. `keep_overrides` maps a cluster's suggested
    canonical_doc_id -> the doc_id you want to keep instead.
    """
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    thr = (
        body.threshold
        if body.threshold is not None
        else dedup.DEFAULT_DUPLICATE_THRESHOLD
    )
    clusters = await dedup.find_duplicate_clusters(
        ingestion_service.db, corpus_id, threshold=thr
    )
    result = await dedup.resolve_duplicate_clusters(
        ingestion_service,
        corpus_id,
        clusters,
        apply=body.apply,
        min_confidence=body.min_confidence,
        keep_overrides=body.keep_overrides,
        only_canonicals=body.only_canonicals,
    )
    return result


@router.post("/corpora/{corpus_id}/documents/{doc_id}/graph-backfill")
async def backfill_document_graph(
    corpus_id: str,
    doc_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Retry failed Ghost B chunks AND/OR flush staged extraction to Neo4j.

    Pt 9 — broadened from the original "retry failures only" contract.
    The endpoint now also handles the common Pt-8c-era failure mode
    where Phase 5/6 (embed/Qdrant) raised, run_ingest_job exited, and
    Neo4j was left unwritten even though Ghost B had completed and
    Mongo had `ghost_b_staging` populated. Returning `queued` whenever
    the doc has failures OR `neo4j_written=False` with staged extraction
    available; the underlying `backfill_failed_graph_chunks` decides
    which path runs (retry, flush, both, or genuine noop).
    """
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    doc = await ingestion_service.db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "ghost_b_failures": 1,
            "ghost_b_staging": 1,
            "ghost_b_staging_count": 1,
            "ghost_b_failure_count": 1,
            "ingestion_config": 1,
            "write_state": 1,
            "_id": 0,
        },
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    failures = doc.get("ghost_b_failures") or []
    failure_count = int(doc.get("ghost_b_failure_count") or len(failures))
    write_state = doc.get("write_state") or {}
    neo4j_written = bool(write_state.get("neo4j_written"))
    graph_replayable = (
        not neo4j_written
        and bool(write_state.get("mongo_written"))
        and bool(write_state.get("qdrant_written"))
        and bool((doc.get("ingestion_config") or {}).get("use_neo4j", True))
    )
    has_staging = bool(doc.get("ghost_b_staging_count") or doc.get("ghost_b_staging"))
    # Pt 9 — true noop only when there's genuinely nothing to do.
    if not failure_count and (neo4j_written or (not has_staging and not graph_replayable)):
        return {
            "status": "noop",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "failed_chunks": 0,
            "neo4j_written": neo4j_written,
            "has_staging": has_staging,
            "graph_replayable": graph_replayable,
        }

    async def _run() -> None:
        try:
            await ingestion_service.backfill_graph_failures(
                corpus_id=corpus_id,
                doc_id=doc_id,
                user_id=current_user["user_id"],
            )
        except Exception as exc:
            logger.exception("Graph backfill failed for doc %s: %s", doc_id, exc)
            ws = doc.get("write_state") or {}
            warnings = list(ws.get("warnings") or [])
            message = f"Ghost B backfill failed: {exc}"
            if message not in warnings:
                warnings.append(message)
            await ingestion_service.db["documents"].update_one(
                {"doc_id": doc_id, "corpus_id": corpus_id},
                {"$set": {"write_state.warnings": warnings}},
            )

    task = asyncio.create_task(_run())
    _BACKFILL_BG_TASKS.add(task)
    task.add_done_callback(_BACKFILL_BG_TASKS.discard)
    return {
        "status": "queued",
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "failed_chunks": failure_count,
    }


@router.post("/corpora/{corpus_id}/summaries/backfill")
async def backfill_corpus_summaries(
    corpus_id: str,
    body: SummaryBackfillRequest = SummaryBackfillRequest(),
    current_user: dict = Depends(get_current_user),
):
    """Generate/index missing parent summaries for an existing corpus.

    This repairs the retrieval breadth lane for corpora that were originally
    created with the balanced preset. It does not change frozen ingestion
    fields or require document deletion/reingest.
    """
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    if body.generate and body.summary_cost_authority_usd is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "summary_cost_authority_usd is required when summary generation "
                "is enabled"
            ),
        )
    if body.index_existing_doc_summaries and not body.doc_ids:
        raise HTTPException(
            status_code=400,
            detail="index_existing_doc_summaries requires explicit doc_ids",
        )
    if body.background:
        run_id = f"summary_backfill_manual_{corpus_id[:8]}_{uuid4().hex[:8]}"
        now = datetime.utcnow()
        await ingestion_service.db["ingest_repair_runs"].update_one(
            {"run_id": run_id},
            {
                "$setOnInsert": {"created_at": now},
                "$set": {
                    "run_id": run_id,
                    "kind": "summary_backfill_manual",
                    "status": "queued",
                    "corpus_id": corpus_id,
                    "user_id": current_user["user_id"],
                    "limit": body.limit,
                    "batch": body.batch,
                    "generate": body.generate,
                    "index": body.index,
                    "doc_ids": body.doc_ids,
                    "index_existing_doc_summaries": body.index_existing_doc_summaries,
                    "updated_at": now,
                },
            },
            upsert=True,
        )

        async def _run() -> None:
            started = datetime.utcnow()
            try:
                await ingestion_service.db["ingest_repair_runs"].update_one(
                    {"run_id": run_id},
                    {"$set": {"status": "running", "started_at": started, "updated_at": started}},
                )
                result = await ingestion_service.backfill_parent_summaries(
                    corpus_id,
                    user_id=current_user["user_id"],
                    generate=body.generate,
                    index=body.index,
                    limit=body.limit,
                    batch=body.batch,
                    doc_ids=body.doc_ids,
                    index_existing_doc_summaries=(
                        body.index_existing_doc_summaries
                    ),
                    summary_cost_run_id=run_id,
                    summary_cost_authority_usd=body.summary_cost_authority_usd,
                )
                finished = datetime.utcnow()
                status = "complete" if result.get("status") in {"healthy", "empty"} else "partial"
                await ingestion_service.db["ingest_repair_runs"].update_one(
                    {"run_id": run_id},
                    {
                        "$set": {
                            "status": status,
                            "result": result,
                            "counts": {
                                "generated": result.get("generated", 0),
                                "attempted": result.get("attempted", 0),
                                "indexed": result.get("indexed", 0),
                                "missing_summary_text": (
                                    (result.get("after") or {}).get("missing_summary_text")
                                ),
                            },
                            "completed_at": finished,
                            "updated_at": finished,
                        }
                    },
                )
                try:
                    from services.ingestion.readiness import materialize_corpus_readiness

                    await materialize_corpus_readiness(ingestion_service.db, corpus_id)
                except Exception as refresh_exc:  # noqa: BLE001
                    logger.warning(
                        "readiness refresh after summary backfill failed corpus=%s: %s",
                        corpus_id[:8],
                        refresh_exc,
                    )
            except Exception as exc:  # noqa: BLE001 - background repair records failure
                logger.exception("Summary backfill run %s failed: %s", run_id, exc)
                finished = datetime.utcnow()
                await ingestion_service.db["ingest_repair_runs"].update_one(
                    {"run_id": run_id},
                    {
                        "$set": {
                            "status": "failed",
                            "error": _safe_ingest_error(exc),
                            "completed_at": finished,
                            "updated_at": finished,
                        }
                    },
                )
                try:
                    from services.ingestion.readiness import materialize_corpus_readiness

                    await materialize_corpus_readiness(ingestion_service.db, corpus_id)
                except Exception as refresh_exc:  # noqa: BLE001
                    logger.warning(
                        "readiness refresh after failed summary backfill failed corpus=%s: %s",
                        corpus_id[:8],
                        refresh_exc,
                    )

        task = asyncio.create_task(_run())
        _BACKFILL_BG_TASKS.add(task)
        task.add_done_callback(_BACKFILL_BG_TASKS.discard)
        return {
            "status": "queued",
            "run_id": run_id,
            "corpus_id": corpus_id,
            "limit": body.limit,
            "batch": body.batch,
        }
    return await ingestion_service.backfill_parent_summaries(
        corpus_id,
        user_id=current_user["user_id"],
        generate=body.generate,
        index=body.index,
        limit=body.limit,
        batch=body.batch,
        doc_ids=body.doc_ids,
        index_existing_doc_summaries=body.index_existing_doc_summaries,
        summary_cost_run_id=f"summary_backfill_sync_{corpus_id[:8]}_{uuid4().hex[:8]}",
        summary_cost_authority_usd=body.summary_cost_authority_usd,
    )


@router.post("/corpora/{corpus_id}/summaries/document-backfill")
async def backfill_corpus_document_summaries(
    corpus_id: str,
    body: DocumentSummaryBackfillRequest = DocumentSummaryBackfillRequest(),
    current_user: dict = Depends(get_current_user),
):
    """Generate missing document-level summary profiles for an existing corpus."""
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    if body.summary_cost_authority_usd is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "summary_cost_authority_usd is required for document summary "
                "generation"
            ),
        )
    return await ingestion_service.backfill_document_summaries(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        limit=body.limit,
        doc_ids=body.doc_ids,
        summary_cost_run_id=f"document_summary_{corpus_id[:8]}_{uuid4().hex[:8]}",
        summary_cost_authority_usd=body.summary_cost_authority_usd,
    )


@router.get("/corpora/{corpus_id}/ingestion-audit")
async def ingestion_audit(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Aggregate ingestion health, Ghost B quality, and backfill needs."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    audit = await ingestion_service.get_ingestion_audit(corpus_id)
    # Active near-duplicate surfacing — a corpus carrying redundant copies of a
    # document is a retrieval-quality defect (it over-weights repeated concepts).
    # The scan is O(n^2) over documents; run it inline only for modest corpora so
    # this health endpoint stays fast. Larger corpora scan on demand via
    # GET .../duplicates (or the dedupe_corpus.py CLI).
    try:
        doc_count = await ingestion_service.db["documents"].count_documents(
            {"corpus_id": corpus_id}
        )
        if doc_count > 200:
            audit["duplicates"] = {
                "status": "scan_on_demand",
                "doc_count": doc_count,
                "hint": f"GET /api/corpora/{corpus_id}/duplicates",
            }
        else:
            clusters = await dedup.find_duplicate_clusters(
                ingestion_service.db, corpus_id
            )
            audit["duplicates"] = {
                "cluster_count": len(clusters),
                "duplicate_document_count": sum(len(c.redundant) for c in clusters),
                "redundant_chunk_count": sum(
                    m.chunk_count for c in clusters for m in c.redundant
                ),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "duplicate scan in audit failed for %s: %s", corpus_id[:8], exc
        )
        audit["duplicates"] = {"error": str(exc)}
    return audit


@router.post("/corpora/{corpus_id}/ingestion/reconcile-failures")
async def reconcile_failure_metadata(
    corpus_id: str,
    body: FailureMetadataReconcileRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Classify stale Ghost B failures and realign document failure counters."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or FailureMetadataReconcileRequest()
    from services.ingestion.failure_reconciliation import (
        reconcile_ghost_b_failure_metadata,
    )

    result = await reconcile_ghost_b_failure_metadata(
        ingestion_service.db,
        corpus_id=corpus_id,
        apply=body.apply,
        limit=body.limit,
    )
    if body.apply:
        try:
            from services.ingestion.readiness import materialize_corpus_readiness

            result["readiness"] = await materialize_corpus_readiness(
                ingestion_service.db,
                corpus_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "readiness refresh after failure reconciliation failed corpus=%s: %s",
                corpus_id[:8],
                exc,
            )
    result["user_id"] = current_user["user_id"]
    return result


@router.get("/corpora/{corpus_id}/ingestion/idempotency-audit")
async def audit_corpus_idempotency(
    corpus_id: str,
    group_limit: int = Query(default=25, ge=1, le=200),
    missing_limit: int = Query(default=25, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    """Return exact source/content duplicate groups and source-identity gaps."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    result = await ingestion_service.audit_corpus_idempotency(
        corpus_id=corpus_id,
        group_limit=group_limit,
        missing_limit=missing_limit,
    )
    result["user_id"] = current_user["user_id"]
    return result


@router.get("/corpora/{corpus_id}/ingestion/source-parse-jobs")
async def list_source_parse_jobs(
    corpus_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    status: list[str] | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """List materialized source/parse jobs for this corpus."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.list_source_parse_jobs(
        corpus_id=corpus_id,
        limit=limit,
        statuses=status,
    )


@router.post("/corpora/{corpus_id}/ingestion/source-parse-jobs/plan")
async def plan_source_parse_jobs(
    corpus_id: str,
    body: SourceParseJobPlanRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Materialize durable source/parse jobs from ingest batch manifests."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or SourceParseJobPlanRequest()
    result = await ingestion_service.plan_source_parse_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        apply=body.apply,
        limit=body.limit,
    )
    if body.apply:
        return await _attach_corpus_readiness(
            result,
            corpus_id=corpus_id,
            context="source parse job planning",
        )
    return result


@router.post("/corpora/{corpus_id}/ingestion/source-parse-jobs/run")
async def run_source_parse_jobs(
    corpus_id: str,
    body: SourceParseJobRunRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Resume eligible source/parse jobs via durable ingest batch runners."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or SourceParseJobRunRequest()
    result = await ingestion_service.run_source_parse_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        limit=body.limit,
        statuses=body.statuses,
    )
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context="source parse job run",
    )


@router.get("/corpora/{corpus_id}/ingestion/graph-promotion-jobs")
async def list_graph_promotion_jobs(
    corpus_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    status: list[str] | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """List durable graph-promotion jobs for this corpus."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.list_graph_promotion_jobs(
        corpus_id=corpus_id,
        limit=limit,
        statuses=status,
    )


@router.post("/corpora/{corpus_id}/ingestion/graph-promotion-jobs/plan")
async def plan_graph_promotion_jobs(
    corpus_id: str,
    body: GraphPromotionPlanRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Materialize graph gaps as durable promotion jobs."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or GraphPromotionPlanRequest()
    result = await ingestion_service.plan_graph_promotion_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        apply=body.apply,
        limit=body.limit,
        max_chunks=body.max_chunks,
    )
    if body.apply:
        return await _attach_corpus_readiness(
            result,
            corpus_id=corpus_id,
            context="graph promotion job planning",
        )
    return result


@router.post("/corpora/{corpus_id}/ingestion/graph-promotion-jobs/run")
async def run_graph_promotion_jobs(
    corpus_id: str,
    body: GraphPromotionRunRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Run queued graph-promotion jobs through the existing graph backfill path."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or GraphPromotionRunRequest()
    result = await ingestion_service.run_graph_promotion_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        limit=body.limit,
    )
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context="graph promotion job run",
    )


@router.get("/corpora/{corpus_id}/ingestion/extraction-jobs")
async def list_extraction_jobs(
    corpus_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    status: list[str] | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """List materialized chunk-level extraction jobs for a corpus."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.list_extraction_jobs(
        corpus_id=corpus_id,
        limit=limit,
        statuses=status,
    )


@router.post("/corpora/{corpus_id}/ingestion/extraction-jobs/plan")
async def plan_extraction_jobs(
    corpus_id: str,
    body: ExtractionJobPlanRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Plan/materialize durable chunk-level extraction jobs from live chunks.

    Dry-run by default. Applied plans create/update rows in ``extraction_jobs``
    but do not call providers yet.
    """
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or ExtractionJobPlanRequest()
    result = await ingestion_service.plan_extraction_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        apply=body.apply,
        limit=body.limit,
        include_succeeded=body.include_succeeded,
    )
    if body.apply:
        return await _attach_corpus_readiness(
            result,
            corpus_id=corpus_id,
            context="extraction job planning",
        )
    return result


@router.post("/corpora/{corpus_id}/ingestion/extraction-jobs/run")
async def run_extraction_jobs(
    corpus_id: str,
    body: ExtractionJobRunRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Run a bounded set of materialized chunk-level extraction jobs."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or ExtractionJobRunRequest()
    result = await ingestion_service.run_extraction_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        limit=body.limit,
        statuses=body.statuses,
    )
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context="extraction job run",
    )


@router.get("/corpora/{corpus_id}/ingestion/summary-jobs")
async def list_summary_jobs(
    corpus_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    status: list[str] | None = Query(default=None),
    kind: list[str] | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """List materialized parent/document summary jobs for a corpus."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.list_summary_jobs(
        corpus_id=corpus_id,
        limit=limit,
        statuses=status,
        kinds=kind,
    )


@router.post("/corpora/{corpus_id}/ingestion/summary-jobs/plan")
async def plan_summary_jobs(
    corpus_id: str,
    body: SummaryJobPlanRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Plan/materialize durable parent/document summary jobs from live gaps."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or SummaryJobPlanRequest()
    result = await ingestion_service.plan_summary_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        apply=body.apply,
        limit=body.limit,
        kinds=body.kinds,
    )
    if body.apply:
        return await _attach_corpus_readiness(
            result,
            corpus_id=corpus_id,
            context="summary job planning",
        )
    return result


@router.post("/corpora/{corpus_id}/ingestion/summary-jobs/run")
async def run_summary_jobs(
    corpus_id: str,
    body: SummaryJobRunRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Run a bounded slice of durable parent/document summary jobs."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or SummaryJobRunRequest()
    if body.summary_cost_authority_usd is None:
        raise HTTPException(
            status_code=400,
            detail="summary_cost_authority_usd is required for summary job execution",
        )
    summary_cost_run_id = f"summary_jobs_{corpus_id[:8]}_{uuid4().hex[:8]}"
    result = await ingestion_service.run_summary_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        limit=body.limit,
        statuses=body.statuses,
        kinds=body.kinds,
        summary_cost_run_id=summary_cost_run_id,
        summary_cost_authority_usd=body.summary_cost_authority_usd,
    )
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context="summary job run",
    )


@router.post("/corpora/{corpus_id}/ingestion/commander/cycle")
async def run_corpus_commander_cycle(
    corpus_id: str,
    apply: bool = Query(default=True),
    source_parse_plan_limit: int = Query(default=500, ge=1, le=5000),
    document_pipeline_plan_limit: int = Query(default=500, ge=1, le=5000),
    extraction_plan_limit: int = Query(default=500, ge=1, le=5000),
    summary_plan_limit: int = Query(default=500, ge=1, le=5000),
    graph_plan_limit: int = Query(default=500, ge=1, le=5000),
    max_plan_pages: int = Query(default=100, ge=1, le=500),
    source_parse_run_slices: int = Query(default=0, ge=0, le=100),
    source_parse_run_limit: int = Query(default=25, ge=1, le=500),
    document_pipeline_run_slices: int = Query(default=0, ge=0, le=100),
    document_pipeline_run_limit: int = Query(default=25, ge=1, le=500),
    extraction_run_slices: int = Query(default=0, ge=0, le=100),
    extraction_run_limit: int = Query(default=100, ge=1, le=500),
    summary_run_slices: int = Query(default=0, ge=0, le=100),
    summary_run_limit: int = Query(default=25, ge=1, le=500),
    graph_run_slices: int = Query(default=0, ge=0, le=100),
    graph_run_limit: int = Query(default=5, ge=1, le=100),
    summary_cost_authority_usd: float | None = Query(default=None, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """Run one owned control-plane reconcile cycle for a corpus."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    result = await ingestion_service.run_corpus_commander_cycle(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        apply=apply,
        source_parse_plan_limit=source_parse_plan_limit,
        document_pipeline_plan_limit=document_pipeline_plan_limit,
        extraction_plan_limit=extraction_plan_limit,
        summary_plan_limit=summary_plan_limit,
        graph_plan_limit=graph_plan_limit,
        max_plan_pages=max_plan_pages,
        source_parse_run_slices=source_parse_run_slices,
        source_parse_run_limit=source_parse_run_limit,
        document_pipeline_run_slices=document_pipeline_run_slices,
        document_pipeline_run_limit=document_pipeline_run_limit,
        extraction_run_slices=extraction_run_slices,
        extraction_run_limit=extraction_run_limit,
        summary_run_slices=summary_run_slices,
        summary_run_limit=summary_run_limit,
        graph_run_slices=graph_run_slices,
        graph_run_limit=graph_run_limit,
        summary_cost_authority_usd=summary_cost_authority_usd,
    )
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context="corpus commander cycle",
    )


@router.get("/corpora/{corpus_id}/ingestion/document-pipeline-jobs")
async def list_document_pipeline_jobs(
    corpus_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    status: list[str] | None = Query(default=None),
    kind: list[str] | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """List materialized document-stage chunk/persist/embed jobs for a corpus."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.list_document_pipeline_jobs(
        corpus_id=corpus_id,
        limit=limit,
        statuses=status,
        kinds=kind,
    )


@router.post("/corpora/{corpus_id}/ingestion/document-pipeline-jobs/plan")
async def plan_document_pipeline_jobs(
    corpus_id: str,
    body: DocumentPipelineJobPlanRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Plan/materialize document-stage chunk/persist/embed jobs from live gaps."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or DocumentPipelineJobPlanRequest()
    result = await ingestion_service.plan_document_pipeline_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        apply=body.apply,
        limit=body.limit,
        kinds=body.kinds,
    )
    if body.apply:
        return await _attach_corpus_readiness(
            result,
            corpus_id=corpus_id,
            context="document pipeline job planning",
        )
    return result


@router.post("/corpora/{corpus_id}/ingestion/document-pipeline-jobs/run")
async def run_document_pipeline_jobs(
    corpus_id: str,
    body: DocumentPipelineJobRunRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Run/reconcile a bounded slice of document-stage chunk/persist/embed jobs."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or DocumentPipelineJobRunRequest()
    result = await ingestion_service.run_document_pipeline_jobs(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        limit=body.limit,
        statuses=body.statuses,
        kinds=body.kinds,
    )
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context="document pipeline job run",
    )


@router.get("/corpora/{corpus_id}/ingestion/durable-jobs")
async def list_corpus_durable_jobs(
    corpus_id: str,
    lane: Literal["source", "document", "extraction", "summary", "graph"] | None = None,
    status: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """Inspect durable queue/audit history independently from artifact truth."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    from services.ingestion.job_control import list_jobs

    return await list_jobs(
        ingestion_service.db,
        corpus_id=corpus_id,
        lane=lane,
        statuses=status,
        limit=limit,
    )


@router.post("/corpora/{corpus_id}/ingestion/durable-jobs/{lane}/{job_id}/control")
async def control_corpus_durable_job(
    corpus_id: str,
    lane: Literal["source", "document", "extraction", "summary", "graph"],
    job_id: str,
    body: IngestionJobControlRequest,
    current_user: dict = Depends(get_current_user),
):
    """Explicitly retry, supersede, or dead-letter one durable job."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    from services.ingestion.job_control import control_job

    try:
        result = await control_job(
            ingestion_service.db,
            corpus_id=corpus_id,
            lane=lane,
            job_id=job_id,
            action=body.action,
            reason=body.reason,
            operator_user_id=str(current_user.get("user_id") or "unknown"),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context=f"durable job {body.action}",
    )


@router.post("/corpora/{corpus_id}/ingestion/repair-cycle")
async def run_corpus_repair_cycle(
    corpus_id: str,
    body: CorpusRepairCycleRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Run one bounded corpus repair cycle.

    Dry-run by default. Applied cycles reconcile failure metadata, materialize
    graph-promotion jobs, and optionally run a small number of queued graph jobs.
    """
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or CorpusRepairCycleRequest()
    if (
        (body.apply or body.background)
        and (body.run_summary_jobs or body.run_document_summaries)
        and body.summary_cost_authority_usd is None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "summary_cost_authority_usd is required when a repair cycle "
                "executes provider-backed summary work"
            ),
        )
    if body.background:
        if not body.apply:
            raise HTTPException(
                status_code=400,
                detail="background repair requires apply=true",
            )
        reconciled_at = datetime.utcnow()
        legacy_stale_before = reconciled_at - timedelta(
            seconds=_BACKGROUND_REPAIR_LEGACY_STALE_SECONDS
        )
        await ingestion_service.db["ingest_repair_runs"].update_many(
            {
                "corpus_id": corpus_id,
                "kind": "corpus_repair_cycle_background",
                "status": {"$in": ["queued", "running"]},
                "$or": [
                    {"lease_expires_at": {"$lt": reconciled_at}},
                    {
                        "lease_expires_at": {"$exists": False},
                        "updated_at": {"$lt": legacy_stale_before},
                    },
                    {
                        "lease_expires_at": {"$exists": False},
                        "updated_at": {"$exists": False},
                    },
                ],
            },
            {
                "$set": {
                    "status": "failed",
                    "completion_reason": "orphaned_background_task_lease_expired",
                    "error": (
                        "The background repair lease expired; durable artifacts "
                        "were preserved for deterministic replanning."
                    ),
                    "completed_at": reconciled_at,
                    "updated_at": reconciled_at,
                    "reconciled_at": reconciled_at,
                },
                "$unset": {"lease_expires_at": ""},
            },
        )
        active = await ingestion_service.db["ingest_repair_runs"].find_one(
            {
                "corpus_id": corpus_id,
                "kind": "corpus_repair_cycle_background",
                "status": {"$in": ["queued", "running"]},
            },
            {"_id": 0, "run_id": 1, "status": 1, "started_at": 1, "updated_at": 1},
        )
        if active:
            return {
                "status": "already_running",
                "run_id": active.get("run_id"),
                "corpus_id": corpus_id,
                "active": active,
            }

        run_id = f"corpus_repair_bg_{corpus_id[:8]}_{uuid4().hex[:8]}"
        now = datetime.utcnow()
        request_payload = body.model_dump()
        await ingestion_service.db["ingest_repair_runs"].update_one(
            {"run_id": run_id},
            {
                "$setOnInsert": {"created_at": now},
                "$set": {
                    "run_id": run_id,
                    "kind": "corpus_repair_cycle_background",
                    "status": "queued",
                    "corpus_id": corpus_id,
                    "user_id": current_user["user_id"],
                    "request": request_payload,
                    "updated_at": now,
                    "lease_expires_at": now
                    + timedelta(seconds=_BACKGROUND_REPAIR_QUEUE_LEASE_SECONDS),
                },
            },
            upsert=True,
        )
        try:
            from services.ingestion.readiness import materialize_corpus_readiness

            readiness = await materialize_corpus_readiness(ingestion_service.db, corpus_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "readiness refresh after queueing background repair failed corpus=%s: %s",
                corpus_id[:8],
                exc,
            )
            readiness = None

        async def _run() -> None:
            started = datetime.utcnow()
            heartbeat_task: asyncio.Task | None = None
            try:
                await ingestion_service.db["ingest_repair_runs"].update_one(
                    {"run_id": run_id},
                    {
                        "$set": {
                            "status": "running",
                            "started_at": started,
                            "heartbeat_at": started,
                            "updated_at": started,
                            "lease_expires_at": started
                            + timedelta(seconds=_BACKGROUND_REPAIR_LEASE_SECONDS),
                        }
                    },
                )
                heartbeat_task = asyncio.create_task(_heartbeat_background_repair(run_id))
                try:
                    from services.ingestion.readiness import materialize_corpus_readiness

                    await materialize_corpus_readiness(ingestion_service.db, corpus_id)
                except Exception as refresh_exc:  # noqa: BLE001
                    logger.warning(
                        "readiness refresh after starting background repair failed corpus=%s: %s",
                        corpus_id[:8],
                        refresh_exc,
                    )
                result = await ingestion_service.run_bounded_corpus_repair_cycle(
                    corpus_id=corpus_id,
                    user_id=current_user["user_id"],
                    apply=True,
                    reconcile_failures=body.reconcile_failures,
                    failure_reconcile_limit=body.failure_reconcile_limit,
                    backfill_promoted_extraction_marks_rows=(
                        body.backfill_promoted_extraction_marks_rows
                    ),
                    promoted_extraction_marks_backfill_limit=(
                        body.promoted_extraction_marks_backfill_limit
                    ),
                    backfill_source_parse_stage_identity_rows=(
                        body.backfill_source_parse_stage_identity_rows
                    ),
                    source_parse_stage_identity_backfill_limit=(
                        body.source_parse_stage_identity_backfill_limit
                    ),
                    backfill_ghost_b_stage_identity_rows=body.backfill_ghost_b_stage_identity_rows,
                    ghost_b_stage_identity_backfill_limit=(
                        body.ghost_b_stage_identity_backfill_limit
                    ),
                    plan_source_parse_jobs=body.plan_source_parse_jobs,
                    source_parse_job_plan_limit=body.source_parse_job_plan_limit,
                    run_source_parse_jobs=body.run_source_parse_jobs,
                    source_parse_job_run_limit=body.source_parse_job_run_limit,
                    plan_document_pipeline_jobs=body.plan_document_pipeline_jobs,
                    document_pipeline_job_plan_limit=body.document_pipeline_job_plan_limit,
                    run_document_pipeline_jobs=body.run_document_pipeline_jobs,
                    document_pipeline_job_run_limit=body.document_pipeline_job_run_limit,
                    plan_graph_jobs=body.plan_graph_jobs,
                    graph_plan_limit=body.graph_plan_limit,
                    graph_max_chunks=body.graph_max_chunks,
                    plan_extraction_jobs=body.plan_extraction_jobs,
                    extraction_job_plan_limit=body.extraction_job_plan_limit,
                    run_extraction_jobs=body.run_extraction_jobs,
                    extraction_job_run_limit=body.extraction_job_run_limit,
                    plan_summary_jobs=body.plan_summary_jobs,
                    summary_job_plan_limit=body.summary_job_plan_limit,
                    backfill_summary_stage_identity_rows=(
                        body.backfill_summary_stage_identity_rows
                    ),
                    summary_stage_identity_backfill_limit=(
                        body.summary_stage_identity_backfill_limit
                    ),
                    run_summary_jobs=body.run_summary_jobs,
                    summary_job_run_limit=body.summary_job_run_limit,
                    run_document_summaries=body.run_document_summaries,
                    document_summary_limit=body.document_summary_limit,
                    run_graph_jobs=body.run_graph_jobs,
                    graph_run_limit=body.graph_run_limit,
                    record_run=False,
                    summary_cost_run_id=run_id,
                    summary_cost_authority_usd=body.summary_cost_authority_usd,
                )
                finished = datetime.utcnow()
                await ingestion_service.db["ingest_repair_runs"].update_one(
                    {"run_id": run_id},
                    {
                        "$set": {
                            "status": result.get("status") or "complete",
                            "result": result,
                            "counts": result.get("summary") or {},
                            "completed_at": finished,
                            "updated_at": finished,
                        },
                        "$unset": {"lease_expires_at": ""},
                    },
                )
                try:
                    from services.ingestion.readiness import materialize_corpus_readiness

                    await materialize_corpus_readiness(ingestion_service.db, corpus_id)
                except Exception as refresh_exc:  # noqa: BLE001
                    logger.warning(
                        "readiness refresh after background repair failed corpus=%s: %s",
                        corpus_id[:8],
                        refresh_exc,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Background corpus repair %s failed: %s", run_id, exc)
                finished = datetime.utcnow()
                await ingestion_service.db["ingest_repair_runs"].update_one(
                    {"run_id": run_id},
                    {
                        "$set": {
                            "status": "failed",
                            "error": _safe_ingest_error(exc),
                            "completed_at": finished,
                            "updated_at": finished,
                        },
                        "$unset": {"lease_expires_at": ""},
                    },
                )
                try:
                    from services.ingestion.readiness import materialize_corpus_readiness

                    await materialize_corpus_readiness(ingestion_service.db, corpus_id)
                except Exception as refresh_exc:  # noqa: BLE001
                    logger.warning(
                        "readiness refresh after failed background repair failed corpus=%s: %s",
                        corpus_id[:8],
                        refresh_exc,
                    )
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

        task = asyncio.create_task(_run())
        _BACKFILL_BG_TASKS.add(task)
        task.add_done_callback(_BACKFILL_BG_TASKS.discard)
        response = {
            "status": "queued",
            "run_id": run_id,
            "corpus_id": corpus_id,
            "background": True,
            "request": request_payload,
        }
        if readiness is not None:
            response["readiness"] = readiness
        return response

    summary_cost_run_id = (
        f"corpus_repair_{corpus_id[:8]}_{uuid4().hex[:8]}"
        if body.run_summary_jobs or body.run_document_summaries
        else None
    )
    return await ingestion_service.run_bounded_corpus_repair_cycle(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        apply=body.apply,
        reconcile_failures=body.reconcile_failures,
        failure_reconcile_limit=body.failure_reconcile_limit,
        backfill_promoted_extraction_marks_rows=body.backfill_promoted_extraction_marks_rows,
        promoted_extraction_marks_backfill_limit=body.promoted_extraction_marks_backfill_limit,
        backfill_source_parse_stage_identity_rows=body.backfill_source_parse_stage_identity_rows,
        source_parse_stage_identity_backfill_limit=body.source_parse_stage_identity_backfill_limit,
        backfill_ghost_b_stage_identity_rows=body.backfill_ghost_b_stage_identity_rows,
        ghost_b_stage_identity_backfill_limit=body.ghost_b_stage_identity_backfill_limit,
        plan_source_parse_jobs=body.plan_source_parse_jobs,
        source_parse_job_plan_limit=body.source_parse_job_plan_limit,
        run_source_parse_jobs=body.run_source_parse_jobs,
        source_parse_job_run_limit=body.source_parse_job_run_limit,
        plan_document_pipeline_jobs=body.plan_document_pipeline_jobs,
        document_pipeline_job_plan_limit=body.document_pipeline_job_plan_limit,
        run_document_pipeline_jobs=body.run_document_pipeline_jobs,
        document_pipeline_job_run_limit=body.document_pipeline_job_run_limit,
        plan_graph_jobs=body.plan_graph_jobs,
        graph_plan_limit=body.graph_plan_limit,
        graph_max_chunks=body.graph_max_chunks,
        plan_extraction_jobs=body.plan_extraction_jobs,
        extraction_job_plan_limit=body.extraction_job_plan_limit,
        run_extraction_jobs=body.run_extraction_jobs,
        extraction_job_run_limit=body.extraction_job_run_limit,
        plan_summary_jobs=body.plan_summary_jobs,
        summary_job_plan_limit=body.summary_job_plan_limit,
        backfill_summary_stage_identity_rows=body.backfill_summary_stage_identity_rows,
        summary_stage_identity_backfill_limit=body.summary_stage_identity_backfill_limit,
        run_summary_jobs=body.run_summary_jobs,
        summary_job_run_limit=body.summary_job_run_limit,
        run_document_summaries=body.run_document_summaries,
        document_summary_limit=body.document_summary_limit,
        run_graph_jobs=body.run_graph_jobs,
        graph_run_limit=body.graph_run_limit,
        summary_cost_run_id=summary_cost_run_id,
        summary_cost_authority_usd=body.summary_cost_authority_usd,
    )


@router.post("/corpora/{corpus_id}/ingest-batches/local")
async def create_local_ingest_batch(
    corpus_id: str,
    body: LocalIngestBatchRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a durable backend-owned batch from a server-visible folder.

    This is the recovery path for large local libraries: the backend writes a
    manifest to Mongo before processing any file, then leases/resumes items
    from that manifest instead of relying on a browser tab to remember the
    remaining file list.
    """
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    corpus_summary_enabled = bool(
        (corpus.get("default_ingestion_config") or {}).get("chunk_summarization")
    )
    summary_enabled = (
        body.chunk_summarization
        if body.chunk_summarization is not None
        else corpus_summary_enabled
    )
    if summary_enabled and body.summary_cost_authority_usd is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "summary_cost_authority_usd is required when chunk_summarization "
                "is enabled"
            ),
        )
    try:
        batch = await ingest_batches.create_local_batch(
            db=ingestion_service.db,
            corpus_id=corpus_id,
            user_id=current_user["user_id"],
            root_path=body.root_path,
            recursive=body.recursive,
            extensions=body.extensions,
            max_files=body.max_files,
            store_files=body.store_files,
            max_total_bytes=body.max_total_bytes,
            use_neo4j=body.use_neo4j,
            chunk_summarization=body.chunk_summarization,
            model=body.model,
            concurrency=body.concurrency,
            profile=body.profile,
            summary_cost_authority_usd=body.summary_cost_authority_usd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    started = False
    if body.start:
        started = await _start_batch_runner_if_enabled(
            batch_id=batch["batch_id"],
            user_id=current_user["user_id"],
        )
    return {**batch, "runner_started": started}


@router.post("/corpora/{corpus_id}/ingest-batches/upload")
async def create_upload_ingest_batch(
    corpus_id: str,
    files: list[UploadFile] = File(...),
    use_neo4j: bool | None = Form(default=None),
    chunk_summarization: bool | None = Form(default=None),
    model: str = Form(default=""),
    concurrency: int | None = Form(default=1),
    profile: Literal[
        "mac_safe", "mac_queryable_first", "rtx_assisted", "runpod_burst",
        "runpod_extract_first"
    ] | None = Form(default=None),
    start: bool = Form(default=True),
    summary_cost_authority_usd: Decimal | None = Form(default=None, gt=0, le=10000),
    current_user: dict = Depends(get_current_user),
):
    """Create a durable browser-upload batch for quick one-off files."""
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    corpus_summary_enabled = bool(
        (corpus.get("default_ingestion_config") or {}).get("chunk_summarization")
    )
    summary_enabled = (
        chunk_summarization
        if chunk_summarization is not None
        else corpus_summary_enabled
    )
    if summary_enabled and summary_cost_authority_usd is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "summary_cost_authority_usd is required when chunk_summarization "
                "is enabled"
            ),
        )
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > 25:
        raise HTTPException(
            status_code=400,
            detail="Quick upload accepts at most 25 files. Use Backend Folder for large batches.",
        )

    payloads: list[dict] = []
    try:
        for upload in files:
            data = await upload.read()
            payloads.append(
                {
                    "filename": upload.filename or "upload",
                    "content_type": upload.content_type,
                    "data": data,
                }
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read upload: {exc}") from exc
    finally:
        for upload in files:
            try:
                await upload.close()
            except Exception:
                pass

    try:
        batch = await ingest_batches.create_upload_batch(
            db=ingestion_service.db,
            corpus_id=corpus_id,
            user_id=current_user["user_id"],
            files=payloads,
            use_neo4j=use_neo4j,
            chunk_summarization=chunk_summarization,
            model=model,
            concurrency=concurrency,
            profile=profile,
            summary_cost_authority_usd=summary_cost_authority_usd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    started = False
    if start:
        started = await _start_batch_runner_if_enabled(
            batch_id=batch["batch_id"],
            user_id=current_user["user_id"],
        )
    return {**batch, "runner_started": started}


@router.get("/ingestion/ingest-source/browse")
async def browse_ingest_source(
    path: str = "",
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Server-side folder browser for the mounted ingest source.

    Owner design (2026-07-04): the UI's folder picker cannot read absolute
    host paths (browser sandbox), so the BACKEND lists the /ingest-source
    mount and the picker submits a server path to the durable folder-batch
    runner — no byte upload, no HTTP timeout, full queue semantics for any
    file count. JAILED: resolved paths must stay under /ingest-source; the
    mount is read-only by compose.
    """
    import os as _os

    ROOT = "/ingest-source"
    rel = (path or "").strip().lstrip("/")
    target = _os.path.realpath(_os.path.join(ROOT, rel))
    if target != ROOT and not target.startswith(ROOT + _os.sep):
        raise HTTPException(status_code=400, detail="Path escapes ingest source")
    if not _os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Folder not found (is the drive mounted?)")
    dirs, files = [], []
    try:
        with _os.scandir(target) as it:
            for e in sorted(it, key=lambda x: x.name.lower()):
                if e.name.startswith("."):
                    continue
                if e.is_dir(follow_symlinks=False):
                    try:
                        n = sum(1 for c in _os.scandir(e.path)
                                if c.is_file() and not c.name.startswith("."))
                    except Exception:  # noqa: BLE001
                        n = -1
                    dirs.append({"name": e.name, "path": e.path, "file_count": n})
                elif e.is_file(follow_symlinks=False):
                    files.append({"name": e.name, "path": e.path,
                                  "size": e.stat().st_size})
    except PermissionError:
        raise HTTPException(status_code=403, detail="Folder not readable")
    return {"root": ROOT, "path": target, "dirs": dirs[:500], "files": files[:1000],
            "truncated": len(dirs) > 500 or len(files) > 1000}


@router.get("/corpora/{corpus_id}/ingest-batches")
async def list_ingest_batches(
    corpus_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    include_archived: bool = Query(
        default=False,
        description="Include terminal failed/cancelled historical batches.",
    ),
    current_user: dict = Depends(get_current_user),
):
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingest_batches.list_batches(
        ingestion_service.db,
        corpus_id,
        user_id=current_user["user_id"],
        limit=limit,
        include_archived=include_archived,
    )


@router.get("/ingest-batches/{batch_id}")
async def get_ingest_batch(
    batch_id: str,
    include_items: bool = True,
    current_user: dict = Depends(get_current_user),
):
    """Batch detail. Pass include_items=false for poll loops — with a 498-item
    batch the full payload is ~585 KB, and progress displays only need the
    counts/status summary."""
    batch = await ingest_batches.get_batch(
        ingestion_service.db,
        batch_id,
        user_id=current_user["user_id"],
        include_items=include_items,
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@router.post("/ingest-batches/{batch_id}/pause")
async def pause_ingest_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Operator quiesce: runners stop claiming new items, in-flight docs
    finish, and poll recovery will NOT restart the batch until Resume."""
    batch = await ingest_batches.get_batch(
        ingestion_service.db,
        batch_id,
        user_id=current_user["user_id"],
        include_items=False,
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    now = datetime.utcnow()
    await ingestion_service.db[ingest_batches.BATCHES].update_one(
        {"batch_id": batch_id, "user_id": current_user["user_id"]},
        {
            "$set": {
                "status": ingest_batches.BATCH_PAUSED,
                "pause_requested_at": now,
                "updated_at": now,
            }
        },
    )
    refreshed = await ingest_batches.get_batch(
        ingestion_service.db,
        batch_id,
        user_id=current_user["user_id"],
        include_items=False,
    )
    return refreshed or batch


@router.post("/ingest-batches/{batch_id}/resume")
async def resume_ingest_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    batch = await ingest_batches.get_batch(
        ingestion_service.db,
        batch_id,
        user_id=current_user["user_id"],
        include_items=False,
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if batch.get("status") == ingest_batches.BATCH_PAUSED:
        # Clear the paused latch FIRST: refresh_batch_counts preserves
        # paused, and the poller skips paused batches — without this flip
        # neither the runner start below nor poll recovery would act.
        await ingestion_service.db[ingest_batches.BATCHES].update_one(
            {"batch_id": batch_id, "user_id": current_user["user_id"]},
            {
                "$set": {
                    "status": ingest_batches.BATCH_QUEUED,
                    "updated_at": datetime.utcnow(),
                },
                "$unset": {"pause_requested_at": ""},
            },
        )
    await ingest_batches.reconcile_stale_items(
        ingestion_service.db,
        batch_id=batch_id,
        user_id=current_user["user_id"],
    )
    await ingest_batches.requeue_failed_items_for_resume(
        ingestion_service.db,
        batch_id=batch_id,
        user_id=current_user["user_id"],
    )
    started = await _start_batch_runner_if_enabled(
        batch_id=batch_id,
        user_id=current_user["user_id"],
    )
    refreshed = await ingest_batches.get_batch(
        ingestion_service.db,
        batch_id,
        user_id=current_user["user_id"],
    )
    return {**(refreshed or batch), "runner_started": started}


@router.post("/ingest-batches/{batch_id}/rescan")
async def rescan_ingest_batch(
    batch_id: str,
    body: RescanIngestBatchRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Append new files from the original local folder manifest root."""
    batch = await ingest_batches.get_batch(
        ingestion_service.db,
        batch_id,
        user_id=current_user["user_id"],
        include_items=False,
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    try:
        refreshed = await ingest_batches.append_new_files_to_batch(
            db=ingestion_service.db,
            batch_id=batch_id,
            user_id=current_user["user_id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    body = body or RescanIngestBatchRequest()
    started = False
    if body.start and int(refreshed.get("appended_items") or 0) > 0:
        await ingest_batches.reconcile_stale_items(
            ingestion_service.db,
            batch_id=batch_id,
            user_id=current_user["user_id"],
        )
        started = await _start_batch_runner_if_enabled(
            batch_id=batch_id,
            user_id=current_user["user_id"],
        )

    full_batch = await ingest_batches.get_batch(
        ingestion_service.db,
        batch_id,
        user_id=current_user["user_id"],
    )
    return {
        **(full_batch or refreshed),
        "appended_items": refreshed.get("appended_items", 0),
        "discovered_files": refreshed.get("discovered_files", 0),
        "runner_started": started,
    }


@router.post("/corpora/{corpus_id}/ingestion/reconcile-stale")
async def reconcile_stale_ingestion(
    corpus_id: str,
    body: StaleIngestReconcileRequest | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Mark stale durable items resumable and queue graph-only repairs.

    Partial docs that are already in Mongo/Qdrant but never reached Neo4j are
    repaired through the graph backfill endpoint, which now supports replaying
    Ghost B from Mongo chunks even when staging is missing.
    """
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    body = body or StaleIngestReconcileRequest()
    batch_result = await ingest_batches.reconcile_stale_items(
        ingestion_service.db,
        user_id=current_user["user_id"],
        stale_after_minutes=body.stale_after_minutes,
    )
    cutoff = datetime.utcnow() - timedelta(
        minutes=int(body.stale_after_minutes or get_settings().INGEST_STALE_JOB_MINUTES)
    )
    cursor = ingestion_service.db["documents"].find(
        {
            "corpus_id": corpus_id,
            "user_id": current_user["user_id"],
            "$or": [
                {"updated_at": {"$lt": cutoff}},
                {"updated_at": {"$exists": False}, "created_at": {"$lt": cutoff}},
            ],
            "error": {"$exists": False},
            "write_state.verified": None,
            "write_state.mongo_written": True,
        },
        {
            "_id": 0,
            "doc_id": 1,
            "filename": 1,
            "ingestion_config": 1,
            "write_state": 1,
        },
    )
    graph_repairs_queued = 0
    marked_recoverable = 0
    inspected: list[dict] = []
    async for doc in cursor:
        ws = doc.get("write_state") or {}
        cfg = doc.get("ingestion_config") or {}
        qdrant_required = bool(cfg.get("target_qdrant_collections") or [])
        qdrant_done = (not qdrant_required) or bool(ws.get("qdrant_written"))
        neo4j_required = bool(cfg.get("use_neo4j")) and bool(get_settings().NEO4J_ENABLED)
        neo4j_done = (not neo4j_required) or bool(ws.get("neo4j_written"))
        doc_id = str(doc.get("doc_id") or "")
        if not doc_id:
            continue
        if qdrant_done and neo4j_required and not neo4j_done and body.auto_backfill_graph:
            async def _run_backfill(did: str = doc_id) -> None:
                try:
                    await ingestion_service.backfill_graph_failures(
                        corpus_id=corpus_id,
                        doc_id=did,
                        user_id=current_user["user_id"],
                    )
                except Exception as exc:
                    logger.exception("Stale graph repair failed doc=%s: %s", did, exc)

            task = asyncio.create_task(_run_backfill())
            _BACKFILL_BG_TASKS.add(task)
            task.add_done_callback(_BACKFILL_BG_TASKS.discard)
            graph_repairs_queued += 1
            inspected.append({"doc_id": doc_id, "action": "graph_repair_queued"})
        elif not qdrant_done or not neo4j_done:
            message = (
                "Ingest stalled without recoverable server-side bytes; "
                "re-upload the file or run it through a durable local ingest batch."
            )
            warnings = list(ws.get("warnings") or [])
            if message not in warnings:
                warnings.append(message)
            await ingestion_service.db["documents"].update_one(
                {"doc_id": doc_id, "corpus_id": corpus_id},
                {"$set": {
                    "error": message,
                    "write_state.warnings": warnings,
                    "updated_at": datetime.utcnow(),
                }},
            )
            marked_recoverable += 1
            inspected.append({"doc_id": doc_id, "action": "marked_failed_recoverable"})
    result = {
        **batch_result,
        "graph_repairs_queued": graph_repairs_queued,
        "marked_failed_recoverable": marked_recoverable,
        "inspected": inspected,
    }
    return await _attach_corpus_readiness(
        result,
        corpus_id=corpus_id,
        context="stale ingestion reconciliation",
    )


@router.get("/ingestion/health")
async def ingestion_health(
    current_user: dict = Depends(get_current_user),
):
    """Pt 9 — cross-cutting write-state distribution.

    Surfaces the kind of stuck-state we discovered in the Pt 8 era:
    docs with Mongo+Ghost-B done but `qdrant_written=False` and/or
    `neo4j_written=False`. The `stuck_neo4j_with_staged_extraction`
    count is the actionable bucket — those docs are exactly what the
    `/graph-backfill` endpoint can repair without re-ingesting.
    """
    db = ingestion_service.db
    if db is None:
        return {"error": "db unavailable"}
    total = await db["documents"].count_documents({})
    mongo_true = await db["documents"].count_documents({"write_state.mongo_written": True})
    qdrant_true = await db["documents"].count_documents({"write_state.qdrant_written": True})
    neo4j_true = await db["documents"].count_documents({"write_state.neo4j_written": True})
    stuck_count = await db["documents"].count_documents({
        "write_state.neo4j_written": {"$ne": True},
        "$or": [
            {"ghost_b_staging_count": {"$gt": 0}},
            {"ghost_b_staging.0": {"$exists": True}},
        ],
    })
    qdrant_stuck = await db["documents"].count_documents({
        "write_state.qdrant_written": {"$ne": True},
        "write_state.mongo_written": True,
    })
    cursor = db["documents"].find(
        {
            "write_state.neo4j_written": {"$ne": True},
            "$or": [
                {"ghost_b_staging_count": {"$gt": 0}},
                {"ghost_b_staging.0": {"$exists": True}},
            ],
        },
        {"_id": 0, "doc_id": 1, "corpus_id": 1, "filename": 1, "ghost_b_metrics.success_rate": 1},
    ).limit(20)
    actionable = []
    async for d in cursor:
        actionable.append({
            "doc_id": d.get("doc_id"),
            "corpus_id": d.get("corpus_id"),
            "filename": d.get("filename"),
            "ghost_b_success_rate": (d.get("ghost_b_metrics") or {}).get("success_rate"),
        })
    return {
        "docs_total": total,
        "mongo_written": {"true": mongo_true, "false": total - mongo_true},
        "qdrant_written": {"true": qdrant_true, "false": total - qdrant_true},
        "neo4j_written": {"true": neo4j_true, "false": total - neo4j_true},
        "stuck_neo4j_with_staged_extraction": stuck_count,
        "stuck_qdrant_after_mongo": qdrant_stuck,
        "actionable_via_graph_backfill": actionable,
    }


@router.post("/ingestion/docling/unload")
async def unload_docling(
    current_user: dict = Depends(get_current_user),
):
    """Release the optional Docling sidecar's heavy converter immediately."""
    from services.ingestion import docling_adapter

    try:
        return await docling_adapter.unload_docling_sidecar()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Docling sidecar unload unavailable: {_safe_ingest_error(exc)}",
        ) from exc


@router.post("/corpora/{corpus_id}/graph-cache/warm")
async def warm_graph_cache(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Schedule graph analytics cache warmup after a controlled ingest batch."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.warm_graph_cache(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
    )


@router.post("/corpora/{corpus_id}/preflight")
async def preflight_documents(
    corpus_id: str,
    files: list[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Parse/chunk files without writes to estimate ingestion size and risk."""
    corpus = await ingestion_service._get_corpus_raw(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    cfg = IngestionConfig(**(corpus.get("default_ingestion_config") or {}))
    results = []
    for upload in files:
        data = await upload.read()
        if not data:
            results.append(
                {
                    "filename": upload.filename or "upload",
                    "error": "Uploaded file is empty",
                }
            )
            continue
        try:
            results.append(
                await ingestion_service.preflight_document(
                    data=data,
                    filename=upload.filename or "upload",
                    corpus_id=corpus_id,
                    ingestion_config=cfg,
                )
            )
        except Exception as exc:
            logger.exception("Preflight failed for %s: %s", upload.filename, exc)
            results.append(
                {
                    "filename": upload.filename or "upload",
                    "error": str(exc),
                }
            )
    totals = {
        "files": len(results),
        "child_count": sum(int(r.get("child_count") or 0) for r in results),
        "parent_count": sum(int(r.get("parent_count") or 0) for r in results),
        "estimated_llm_calls": sum(int(r.get("estimated_llm_calls") or 0) for r in results),
    }
    recommended_batch_size = 25
    if totals["child_count"] > 5000 or len(files) > 100:
        recommended_batch_size = 10
    return {
        "corpus_id": corpus_id,
        "totals": totals,
        "recommended_batch_size": recommended_batch_size,
        "files": results,
    }


@router.get("/corpora/{corpus_id}/documents")
async def list_documents(
    corpus_id: str,
    limit: int = 100,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
):
    """List all documents in a corpus."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")

    docs = await ingestion_service.list_documents(
        corpus_id,
        user_id=current_user["user_id"],
        limit=limit,
        offset=offset,
    )
    return docs


@router.post("/corpora/{corpus_id}/ingest")
async def ingest_document(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Browser multipart ingest is intentionally disabled.

    Ingest must enter through the durable backend-owned batch manifest:
    POST /api/corpora/{corpus_id}/ingest-batches/local
    """
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    raise HTTPException(
        status_code=410,
        detail=(
            "Browser upload ingest is disabled. Use the durable backend folder "
            "batch endpoint: POST /api/corpora/{corpus_id}/ingest-batches/local "
            "or quick upload endpoint: POST /api/corpora/{corpus_id}/ingest-batches/upload."
        ),
    )


@router.get("/ingestion/jobs/{doc_id}", response_model=IngestJobResponse)
async def get_job_status(
    doc_id: str,
    corpus_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    """Poll the status of an ingest job by doc_id."""
    doc = await ingestion_service.get_job_status(
        doc_id,
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")

    ws_raw = doc.get("write_state", {})
    progress = _resolve_ingest_progress(doc)
    return IngestJobResponse(
        job_id=doc.get("file_id", doc_id),
        doc_id=doc["doc_id"],
        corpus_id=doc["corpus_id"],
        filename=doc.get("filename", ""),
        source_tier=doc.get("source_tier"),
        status=progress["status"],
        write_state=WriteState(**ws_raw) if ws_raw else WriteState(),
        chunk_count=doc.get("chunk_count", 0),
        parent_count=int(doc.get("parent_count") or len(doc.get("parent_chunks", []))),
        error=progress["error"],
    )


@router.get("/ingestion/jobs/{doc_id}/stream")
async def stream_job_progress(
    doc_id: str,
    corpus_id: str | None = None,
    current_user: dict = Depends(get_current_user),
):
    """
    SSE stream of ingest job progress.

    Yields JSON status events every 500ms until the job completes or fails.
    Terminal event: data: [DONE]
    """

    async def event_generator():
        last_status: str | None = None
        while True:
            doc = await ingestion_service.get_job_status(
                doc_id,
                corpus_id=corpus_id,
                user_id=current_user["user_id"],
            )
            if not doc:
                yield build_sse_error("Job not found")
                return

            ws_raw = doc.get("write_state", {})
            progress = _resolve_ingest_progress(doc)

            payload = json.dumps(
                {
                    "type": "progress",
                    "doc_id": doc["doc_id"],
                    "corpus_id": doc["corpus_id"],
                    "filename": doc.get("filename", ""),
                    "status": progress["status"],
                    "stage": progress["stage"],
                    "source_tier": doc.get("source_tier"),
                    "chunk_count": doc.get("chunk_count", 0),
                    "parent_count": int(
                        doc.get("parent_count") or len(doc.get("parent_chunks", []))
                    ),
                    "write_state": {
                        "mongo_written": progress["mongo_done"],
                        "qdrant_written": progress["qdrant_done"],
                        "summaries_indexed": progress["summaries_indexed"],
                        "neo4j_written": progress["neo4j_done"],
                        "warnings": progress["warnings"],
                        "verified": progress["verified"],
                        "verify_errors": progress["verify_errors"],
                    },
                    "error": progress["error"],
                }
            )

            # Only emit if status changed (avoids flooding)
            if payload != last_status:
                yield f"data: {payload}\n\n"
                last_status = payload

            if progress["status"] in (
                "done",
                "failed",
                "queryable",
                "queryable_with_pending_summary",
                "queryable_with_pending_graph",
                "queryable_with_pending_summary_and_graph",
            ):
                yield build_sse_done()
                return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/documents")
async def list_all_documents(
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
):
    """List all documents across all user's corpora with embedded status."""
    docs = await ingestion_service.list_all_user_documents(
        user_id=current_user["user_id"],
        limit=limit,
    )
    result = []
    for d in docs:
        ws = d.get("write_state", {})
        ingested_at = d.get("ingested_at", "")
        if hasattr(ingested_at, "isoformat"):
            ingested_at = ingested_at.isoformat()
        result.append(
            {
                "doc_id": d.get("doc_id", ""),
                "corpus_id": d.get("corpus_id", ""),
                "filename": d.get("filename", ""),
                "chunk_count": d.get("chunk_count", 0),
                "parent_count": int(
                    d.get("parent_count") or len(d.get("parent_chunks", []))
                ),
                "embedded": bool(ws.get("qdrant_written", False)),
                "write_state": ws,
                "ingested_at": str(ingested_at),
            }
        )
    return result


# ── Mass upload sessions (owner order 2026-07-19): stream files to LOCAL
# DISK staging, then finalize as ONE normal durable local batch. No file-
# count cap — the browser sends one file per request, bytes go chunk-wise
# straight to disk (RAM-bounded), and the finalize path reuses the exact
# machinery of backend-folder ingestion (governors, routes, verify gates).
import re as _re
from pathlib import Path
from fastapi import Request as _Request


def _upload_session_dir(session_id: str) -> Path:
    root = Path(get_settings().INGEST_DROP_OFF_DIR) / "mass-upload"
    safe = _re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid session id")
    return root / safe


@router.post("/corpora/{corpus_id}/upload-sessions")
async def create_upload_session(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    session_id = f"{corpus_id[:8]}-{uuid4().hex[:12]}"
    target = _upload_session_dir(session_id)
    target.mkdir(parents=True, exist_ok=True)
    return {"session_id": session_id, "staged_files": 0}


@router.post("/corpora/{corpus_id}/upload-sessions/{session_id}/files")
async def stage_upload_file(
    corpus_id: str,
    session_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    target_dir = _upload_session_dir(session_id)
    if not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Upload session not found")
    filename = Path(str(file.filename or "upload")).name
    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".md", ".epub", ".txt"}:
        raise HTTPException(status_code=400, detail=f"Unsupported extension: {ext or '(none)'}")
    dest = target_dir / filename
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
    finally:
        await file.close()
    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Empty file: {filename}")
    staged = sum(1 for p in target_dir.iterdir() if p.is_file())
    return {"filename": filename, "bytes": written, "staged_files": staged}


@router.post("/corpora/{corpus_id}/upload-sessions/{session_id}/finalize")
async def finalize_upload_session(
    corpus_id: str,
    session_id: str,
    concurrency: int = Form(default=6),
    profile: Literal[
        "mac_safe", "mac_queryable_first", "rtx_assisted", "runpod_burst",
        "runpod_extract_first"
    ] = Form(default="runpod_extract_first"),
    chunk_summarization: bool | None = Form(default=None),
    summary_cost_authority_usd: Decimal | None = Form(default=None, gt=0, le=10000),
    current_user: dict = Depends(get_current_user),
):
    """Turn a staged session folder into ONE durable local batch."""
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    target_dir = _upload_session_dir(session_id)
    if not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Upload session not found")
    n_files = sum(1 for p in target_dir.iterdir() if p.is_file())
    if n_files == 0:
        raise HTTPException(status_code=400, detail="Session has no staged files")
    corpus_summary_enabled = bool(
        (corpus.get("default_ingestion_config") or {}).get("chunk_summarization")
    )
    summary_enabled = (
        chunk_summarization if chunk_summarization is not None else corpus_summary_enabled
    )
    if summary_enabled and summary_cost_authority_usd is None:
        summary_cost_authority_usd = Decimal(n_files) * Decimal("0.50")
    batch = await ingest_batches.create_local_batch(
        db=ingestion_service.db,
        corpus_id=corpus_id,
        user_id=str(current_user["user_id"]),
        root_path=str(target_dir),
        profile=profile,
        recursive=True,
        extensions=[".pdf", ".md", ".epub", ".txt"],
        store_files=True,
        use_neo4j=True,
        chunk_summarization=summary_enabled,
        model="",
        concurrency=max(1, min(8, int(concurrency))),
        summary_cost_authority_usd=summary_cost_authority_usd if summary_enabled else None,
    )
    started = await _start_batch_runner_if_enabled(
        batch_id=batch["batch_id"],
        user_id=str(current_user["user_id"]),
    )
    return {
        "batch_id": batch.get("batch_id"),
        "total": batch.get("total"),
        "staged_files": n_files,
        "runner_started": started,
    }
