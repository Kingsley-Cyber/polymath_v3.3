"""
Ingestion router — corpus management + document upload/ingest.

Endpoints:
  POST /api/corpora                                    — create corpus
  GET  /api/corpora                                    — list corpora (user-scoped)
  GET  /api/corpora/{corpus_id}                        — get corpus by ID
  POST /api/corpora/{corpus_id}/ingest                 — upload + ingest a file
  GET  /api/ingestion/jobs/{doc_id}                    — poll ingest job status
  GET  /api/ingestion/jobs/{doc_id}/stream             — SSE stream ingest progress
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
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
    raw_key = data.get("api_key")

    if raw_key == "[set]":
        if not (body.corpus_id and body.pool_field is not None and body.index is not None):
            return data, "Saved API key is masked. Type a new key or save the corpus first."

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
            return data, "Saved API key could not be resolved for this model chip."

        raw_key = saved_entry.get("api_key")
        if not raw_key:
            return data, "No API key is saved for this model chip."
        data["api_key"] = raw_key

    if data.get("api_key"):
        from services.secrets import decrypt

        plaintext = decrypt(data["api_key"])
        if plaintext is not None:
            data["api_key"] = plaintext

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
    try:
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
    if neo4j_enabled is None:
        neo4j_enabled = bool(get_settings().NEO4J_ENABLED)
    neo4j_required = bool(cfg.get("use_neo4j")) and neo4j_enabled

    mongo_done = bool(ws_raw.get("mongo_written", False))
    qdrant_done = bool(ws_raw.get("qdrant_written", False))
    neo4j_done = bool(ws_raw.get("neo4j_written", False))
    verified = ws_raw.get("verified")
    verify_errors = ws_raw.get("verify_errors", []) or []
    warnings = ws_raw.get("warnings", []) or []
    error = doc.get("error")

    required_qdrant_done = (not qdrant_required) or qdrant_done
    required_neo4j_done = (not neo4j_required) or neo4j_done

    if error:
        status = "failed"
    elif verified is False:
        status = "failed"
    elif (
        mongo_done
        and required_qdrant_done
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
    elif mongo_done and required_qdrant_done and required_neo4j_done:
        stage = "verifying"
    elif (
        neo4j_required
        and mongo_done
        and required_qdrant_done
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
        {"ghost_b_failures": 1, "ghost_b_staging": 1, "write_state": 1, "_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    failures = doc.get("ghost_b_failures") or []
    write_state = doc.get("write_state") or {}
    neo4j_written = bool(write_state.get("neo4j_written"))
    has_staging = bool(doc.get("ghost_b_staging"))
    # Pt 9 — true noop only when there's genuinely nothing to do.
    if not failures and (neo4j_written or not has_staging):
        return {
            "status": "noop",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "failed_chunks": 0,
            "neo4j_written": neo4j_written,
            "has_staging": has_staging,
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
        "failed_chunks": len(failures),
    }


@router.get("/corpora/{corpus_id}/ingestion-audit")
async def ingestion_audit(
    corpus_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Aggregate ingestion health, Ghost B quality, and backfill needs."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.get_ingestion_audit(corpus_id)


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
        "ghost_b_staging.0": {"$exists": True},
    })
    qdrant_stuck = await db["documents"].count_documents({
        "write_state.qdrant_written": {"$ne": True},
        "write_state.mongo_written": True,
    })
    cursor = db["documents"].find(
        {
            "write_state.neo4j_written": {"$ne": True},
            "ghost_b_staging.0": {"$exists": True},
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


@router.post("/corpora/{corpus_id}/ingest", response_model=IngestJobResponse)
async def ingest_document(
    corpus_id: str,
    file: UploadFile = File(...),
    use_neo4j: bool | None = Form(default=None),
    chunk_summarization: bool | None = Form(default=None),
    # Phase 24 — empty default. Real model selection comes from the
    # corpus's IngestionConfig (summary_models / extraction_models pool
    # entries). The form param survives for back-compat with curl callers
    # passing an explicit model; empty just means "use corpus defaults".
    model: str = Form(default=""),
    # Phase 21 — per-ingest mutable overrides. All optional. Not persisted
    # onto the corpus. Plaintext values flow straight into the worker; the
    # Fernet encrypt path never sees them.
    embed_mode: str | None = Form(default=None),
    embed_base_url: str | None = Form(default=None),
    embed_api_key: str | None = Form(default=None),
    embed_max_concurrent: int | None = Form(default=None),
    summary_model: str | None = Form(default=None),
    summary_base_url: str | None = Form(default=None),
    summary_api_key: str | None = Form(default=None),
    extraction_model: str | None = Form(default=None),
    extraction_base_url: str | None = Form(default=None),
    extraction_api_key: str | None = Form(default=None),
    current_user: dict = Depends(get_current_user),
):
    """
    Upload and ingest a document into a corpus.

    The corpus's own `default_ingestion_config` (chip pools, schema, etc.) is
    the base configuration. Optional multipart form fields `use_neo4j` and
    `chunk_summarization` override just those two flags for this single ingest.
    Everything else — summary_models, extraction_models, schema, chunk sizes —
    comes from the corpus itself.

    Supports: PDF, HTML, plain text, Markdown.
    Returns job result synchronously (async job queue is Phase 5+).
    """
    # IMPORTANT: get the *raw* corpus so ingestion_config's api_key fields still
    # carry their Fernet ciphertext. The public `get_corpus` masks them to
    # "[set]", which would defeat decryption at worker time and silently kill
    # the summary/extraction ghost pools.
    corpus = await ingestion_service._get_corpus_raw(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")

    # ── Order matters here ───────────────────────────────────────────
    # Pre-fix: file.read() ran FIRST, then config build, then slot
    # acquire. A 500-file batch upload would materialize 500 × ~10MB
    # = ~5GB of file bytes into RAM (UploadFile's SpooledTemporaryFile
    # gets fully read into a single `bytes` object) BEFORE any of
    # them could be rejected by the slot gate. With the gate at
    # INGEST_MAX_ACTIVE_JOBS=16, the other 484 requests would just
    # 429 but their RAM was already paid for, easily OOM-killing
    # the container on dense uploads.
    #
    # Post-fix: build the ingestion config (cheap dict ops on the
    # already-fetched corpus row), then acquire the slot, then read
    # the file body. If the slot acquire 429s, the file body stays
    # as a spooled temp file (large files on disk, small in RAM)
    # and gets cleaned up when FastAPI's request handler returns.
    base_cfg_dict = corpus.get("default_ingestion_config") or {}
    if use_neo4j is not None:
        base_cfg_dict["use_neo4j"] = use_neo4j
    if chunk_summarization is not None:
        base_cfg_dict["chunk_summarization"] = chunk_summarization
    cfg = IngestionConfig(**base_cfg_dict)

    # Phase 21 — collect per-ingest mutable overrides. None values are dropped
    # by build_effective_config; the router only forwards what the caller set.
    overrides: dict = {}
    for name, val in (
        ("embed_mode", embed_mode),
        ("embed_base_url", embed_base_url),
        ("embed_api_key", embed_api_key),
        ("embed_max_concurrent", embed_max_concurrent),
    ):
        if val is not None:
            overrides[name] = val
    # Flat-scalar summary / extraction overrides synthesize a single-entry
    # pool that shadows the corpus's persisted multi-entry pool for this
    # ingest only. Key material stays plaintext — ephemeral, not Fernet'd.
    if any(v is not None for v in (summary_model, summary_base_url, summary_api_key)):
        overrides["summary_models"] = [{
            "provider_preset": "",
            "model": summary_model or "",
            "base_url": summary_base_url,
            "api_key": summary_api_key,
            "max_concurrent": 1,
            "extra_params": {},
        }]
    if any(v is not None for v in (extraction_model, extraction_base_url, extraction_api_key)):
        overrides["extraction_models"] = [{
            "provider_preset": "",
            "model": extraction_model or "",
            "base_url": extraction_base_url,
            "api_key": extraction_api_key,
            "max_concurrent": 1,
            "extra_params": {},
        }]

    # Acquire the ingest slot BEFORE reading the file body. The slot
    # is released by the background `_run()` task's finally block once
    # it starts; if we fail to even start `_run()` (empty body, read
    # error), the explicit release below keeps the slot accounting
    # honest.
    if not await _try_acquire_ingest_slot():
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many active ingest jobs ({_INGEST_ACTIVE_LIMIT}). "
                "Wait for current uploads to finish or lower upload concurrency."
            ),
        )

    try:
        data = await file.read()
    except Exception:
        # Read failure (network drop mid-upload, malformed multipart) —
        # release the slot we just acquired so the next request can
        # use it.
        await _release_ingest_slot()
        raise

    if not data:
        # Empty body — release slot, return 400 (same response as
        # pre-fix).
        await _release_ingest_slot()
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Phase K — Non-blocking ingest. Worker runs in the background; we wait
    # only for docling parse so the content-derived doc_id exists before the
    # client opens the progress stream. Text-native files usually resolve in a
    # few seconds, but layout-heavy documents can take longer.
    doc_id_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
    resolved_doc_id: str | None = None

    def _resolve_doc_id(did: str) -> None:
        nonlocal resolved_doc_id
        resolved_doc_id = did
        if not doc_id_future.done():
            doc_id_future.set_result(did)

    async def _run() -> IngestJobResponse | None:
        try:
            return await ingestion_service.ingest(
                data=data,
                filename=file.filename or "upload",
                corpus_id=corpus_id,
                user_id=current_user["user_id"],
                ingestion_config=cfg,
                model=model,
                ingest_overrides=overrides or None,
                on_doc_id=_resolve_doc_id,
            )
        except Exception as exc:
            logger.exception("Ingest failed for corpus %s: %s", corpus_id, exc)
            if resolved_doc_id:
                try:
                    await _mark_ingest_failed(
                        doc_id=resolved_doc_id,
                        corpus_id=corpus_id,
                        user_id=current_user["user_id"],
                        exc=exc,
                    )
                except Exception as mark_exc:
                    logger.warning(
                        "Failed to persist ingest failure doc=%s corpus=%s: %s",
                        resolved_doc_id[:12],
                        corpus_id,
                        mark_exc,
                    )
            # Surface the error through the future so the HTTP response
            # doesn't hang if parse itself failed.
            if not doc_id_future.done():
                doc_id_future.set_exception(exc)
            return None
        finally:
            await _release_ingest_slot()

    task = asyncio.create_task(_run())
    _INGEST_BG_TASKS.add(task)
    task.add_done_callback(_INGEST_BG_TASKS.discard)

    # Wait for parse-resolved doc_id. Keep the cap below the frontend/nginx
    # proxy timeout so large PDFs do not surface as false 504 failures while
    # still leaving room for the response to return.
    try:
        doc_id = await asyncio.wait_for(
            doc_id_future,
            timeout=PARSE_DOC_ID_WAIT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Ingest parse phase exceeded {int(PARSE_DOC_ID_WAIT_SECONDS)}s. "
                "The worker is still running; poll /api/corpora/{corpus_id}/documents "
                "for status."
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingest parse failed: {exc}")

    return IngestJobResponse(
        job_id=doc_id,
        doc_id=doc_id,
        corpus_id=corpus_id,
        filename=file.filename or "upload",
        source_tier=None,
        status="processing",
        write_state=WriteState(),
        chunk_count=0,
        parent_count=0,
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
        parent_count=len(doc.get("parent_chunks", [])),
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
                    "parent_count": len(doc.get("parent_chunks", [])),
                    "write_state": {
                        "mongo_written": progress["mongo_done"],
                        "qdrant_written": progress["qdrant_done"],
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

            if progress["status"] in ("done", "failed"):
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
                "parent_count": len(d.get("parent_chunks", [])),
                "embedded": bool(ws.get("qdrant_written", False)),
                "write_state": ws,
                "ingested_at": str(ingested_at),
            }
        )
    return result
