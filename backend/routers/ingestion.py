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
from typing import Literal

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


def _start_batch_runner_if_enabled(*, batch_id: str, user_id: str) -> bool:
    """Start a durable batch only from processes allowed to own ingest memory.

    Offline-ingest deployments run the public/query API with
    INGEST_RUNNERS_ENABLED=false and a separate 20 GB worker with the flag true.
    The query API still creates/resumes durable batches; the worker discovers
    them through startup/poll recovery.
    """
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
    profile: Literal["mac_safe", "mac_queryable_first", "rtx_assisted"] | None = None
    recursive: bool = True
    extensions: list[str] | None = None
    max_files: int | None = Field(default=None, ge=1, le=20000)
    store_files: bool = True
    max_total_bytes: int | None = Field(default=None, ge=1, le=2 * 1024 * 1024 * 1024)
    use_neo4j: bool | None = None
    chunk_summarization: bool | None = None
    model: str = ""
    concurrency: int | None = Field(default=None, ge=1, le=32)
    start: bool = True


class StaleIngestReconcileRequest(BaseModel):
    stale_after_minutes: int | None = Field(default=None, ge=1, le=1440)
    auto_backfill_graph: bool = True


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


async def _test_chat_model_ref(entry: dict) -> ModelRefTestResult:
    settings = get_settings()
    model = str(entry.get("model") or "").strip()
    if not model:
        return ModelRefTestResult(ok=False, kind="chat", error="Model is required")

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
            return ModelRefTestResult(
                ok=False,
                kind="chat",
                status=resp.status_code,
                latency_ms=latency_ms,
                model=model,
                base_url=base_url,
                error=_safe_provider_text(resp.text),
            )
        return ModelRefTestResult(
            ok=True,
            kind="chat",
            status=resp.status_code,
            latency_ms=latency_ms,
            model=model,
            base_url=base_url,
        )
    except httpx.TimeoutException:
        return ModelRefTestResult(
            ok=False,
            kind="chat",
            model=model,
            base_url=base_url,
            error="Request timed out after 20s",
        )
    except Exception as exc:
        logger.warning("ingestion model chat probe failed: %s", exc)
        return ModelRefTestResult(
            ok=False,
            kind="chat",
            model=model,
            base_url=base_url,
            error=_safe_provider_text(str(exc)),
        )
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
    return await _test_chat_model_ref(entry)


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

    from services.extraction_provider_cards import resolve_extraction_provider_card
    from services.ingestion.extraction_contract import resolve_extraction_contract
    from services.private_vllm_capacity import fetch_private_vllm_capacity
    from services.settings import settings_service as _ss

    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    cfg = IngestionConfig(**(corpus.get("default_ingestion_config") or {}))

    engine_global = "local"
    endpoints = []
    try:
        ext = await _ss.get_system_extraction()
        engine_global = str(getattr(ext, "engine", "local") or "local")
        endpoints = list(ext.endpoints or [])
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
        try:
            capacity = await fetch_private_vllm_capacity(
                lifecycle,
                api_key=getattr(m, "lifecycle_api_key", None),
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
    if contract.uses_provider_llm and contract.pool_source != "none":
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

    return {
        "engine": contract.engine,
        "source": contract.source,
        "models_linked": cfg.models_linked,
        "pool_source": contract.pool_source if contract.uses_provider_llm else "none",
        "pool": pool,
        "endpoints": [
            {
                "label": e.label,
                "url": (e.url or "").strip().rstrip("/"),
                "enabled": bool(e.enabled),
                "alive": alive.get((e.url or "").strip().rstrip("/")),
            }
            for e in endpoints
        ],
        "errors": list(contract.errors),
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
        doc = await ingestion_service.update_corpus(corpus_id, updates)
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
    return await ingestion_service.backfill_parent_summaries(
        corpus_id,
        user_id=current_user["user_id"],
        generate=body.generate,
        index=body.index,
        limit=body.limit,
        batch=body.batch,
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
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    started = False
    if body.start:
        started = _start_batch_runner_if_enabled(
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
    profile: Literal["mac_safe", "mac_queryable_first", "rtx_assisted"] | None = Form(default=None),
    start: bool = Form(default=True),
    current_user: dict = Depends(get_current_user),
):
    """Create a durable browser-upload batch for quick one-off files."""
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
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
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    started = False
    if start:
        started = _start_batch_runner_if_enabled(
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
    await ingest_batches.reconcile_stale_items(
        ingestion_service.db,
        batch_id=batch_id,
        user_id=current_user["user_id"],
    )
    started = _start_batch_runner_if_enabled(
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
        started = _start_batch_runner_if_enabled(
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
    return {
        **batch_result,
        "graph_repairs_queued": graph_repairs_queued,
        "marked_failed_recoverable": marked_recoverable,
        "inspected": inspected,
    }


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
