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
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from config import get_settings
from models.schemas import (
    CorpusCreate,
    CorpusResponse,
    IngestionConfig,
    IngestJobResponse,
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
_INGEST_ACTIVE_LIMIT = max(1, int(get_settings().INGEST_MAX_ACTIVE_JOBS))
_INGEST_ACTIVE_COUNT = 0
_INGEST_ADMISSION_LOCK = asyncio.Lock()

# Keep this below frontend/nginx.conf's 300s proxy timeout. OCR is disabled,
# but layout-heavy documents can still take time before doc_id exists.
PARSE_DOC_ID_WAIT_SECONDS = 240.0


async def _try_acquire_ingest_slot() -> bool:
    global _INGEST_ACTIVE_COUNT
    async with _INGEST_ADMISSION_LOCK:
        if _INGEST_ACTIVE_COUNT >= _INGEST_ACTIVE_LIMIT:
            return False
        _INGEST_ACTIVE_COUNT += 1
        return True


async def _release_ingest_slot() -> None:
    global _INGEST_ACTIVE_COUNT
    async with _INGEST_ADMISSION_LOCK:
        _INGEST_ACTIVE_COUNT = max(0, _INGEST_ACTIVE_COUNT - 1)


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


def _build_ephemeral_ingest_config(
    *,
    corpus: dict,
    use_neo4j: bool | None,
    chunk_summarization: bool | None,
    embed_mode: str | None,
    embed_base_url: str | None,
    embed_api_key: str | None,
    embed_max_concurrent: int | None,
    summary_model: str | None,
    summary_base_url: str | None,
    summary_api_key: str | None,
    extraction_model: str | None,
    extraction_base_url: str | None,
    extraction_api_key: str | None,
) -> tuple[IngestionConfig, dict]:
    """Build the one-request config/overrides without persisting them."""
    base_cfg_dict = dict(corpus.get("default_ingestion_config") or {})
    if use_neo4j is not None:
        base_cfg_dict["use_neo4j"] = use_neo4j
    if chunk_summarization is not None:
        base_cfg_dict["chunk_summarization"] = chunk_summarization
    cfg = IngestionConfig(**base_cfg_dict)

    overrides: dict = {}
    for name, val in (
        ("embed_mode", embed_mode),
        ("embed_base_url", embed_base_url),
        ("embed_api_key", embed_api_key),
        ("embed_max_concurrent", embed_max_concurrent),
    ):
        if val is not None:
            overrides[name] = val
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
    return cfg, overrides


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ingestion"])


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
    """Retry only failed Ghost B chunks for a document and patch Neo4j."""
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    doc = await ingestion_service.db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"ghost_b_failures": 1, "write_state": 1, "_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    failures = doc.get("ghost_b_failures") or []
    if not failures:
        return {
            "status": "noop",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "failed_chunks": 0,
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


@router.post("/corpora/{corpus_id}/documents/{doc_id}/vector-recovery")
async def recover_document_vectors(
    corpus_id: str,
    doc_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Recover Qdrant/vector readiness for a document that already reached Mongo."""
    corpus = await ingestion_service.get_corpus(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    doc = await ingestion_service.db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"write_state": 1, "_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    ws = doc.get("write_state") or {}
    if not ws.get("mongo_written"):
        raise HTTPException(
            status_code=409,
            detail="Document is not Mongo-ready; vector recovery cannot run.",
        )
    return await ingestion_service.recover_document_vectors(
        corpus_id=corpus_id,
        doc_id=doc_id,
        user_id=current_user["user_id"],
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
    return await ingestion_service.get_ingestion_audit(corpus_id)


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


@router.post("/corpora/{corpus_id}/entity-quality/backfill")
async def backfill_entity_quality(
    corpus_id: str,
    batch_size: int = 500,
    force: bool = False,
    current_user: dict = Depends(get_current_user),
):
    """Classify existing Neo4j Entity labels without deleting graph data."""
    existing = await ingestion_service.get_corpus(corpus_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return await ingestion_service.backfill_entity_quality(
        corpus_id=corpus_id,
        user_id=current_user["user_id"],
        batch_size=batch_size,
        force=force,
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


@router.post("/corpora/{corpus_id}/batch-ingest")
async def batch_ingest_documents(
    corpus_id: str,
    files: list[UploadFile] = File(...),
    use_neo4j: bool | None = Form(default=None),
    chunk_summarization: bool | None = Form(default=None),
    model: str = Form(default=""),
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
    """Spool many files to disk and enqueue durable background ingestion."""
    corpus = await ingestion_service._get_corpus_raw(corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    if not files:
        raise HTTPException(status_code=400, detail="No files supplied")
    cfg, overrides = _build_ephemeral_ingest_config(
        corpus=corpus,
        use_neo4j=use_neo4j,
        chunk_summarization=chunk_summarization,
        embed_mode=embed_mode,
        embed_base_url=embed_base_url,
        embed_api_key=embed_api_key,
        embed_max_concurrent=embed_max_concurrent,
        summary_model=summary_model,
        summary_base_url=summary_base_url,
        summary_api_key=summary_api_key,
        extraction_model=extraction_model,
        extraction_base_url=extraction_base_url,
        extraction_api_key=extraction_api_key,
    )
    admission_warnings: list[str] = []
    preflight = await ingestion_service.preflight_ingest(
        corpus=corpus,
        corpus_id=corpus_id,
        ingestion_config=cfg,
        ingest_overrides=overrides or None,
    )
    if not preflight.get("ok"):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Ingest preflight failed. No files were queued.",
                "errors": preflight.get("errors", []),
                "preflight": preflight,
            },
        )
    admission_warnings.extend(preflight.get("warnings") or [])
    try:
        return await ingestion_service.create_batch_ingest(
            corpus_id=corpus_id,
            user_id=current_user["user_id"],
            uploads=files,
            ingestion_config=cfg,
            model=model,
            ingest_overrides=overrides or None,
            warnings=admission_warnings,
            preflight=preflight,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/ingestion/resource-profile")
async def ingestion_resource_profile(
    current_user: dict = Depends(get_current_user),
):
    """Detected CPU/RAM/GPU profile plus active batch queue metrics."""
    return await ingestion_service.get_ingestion_resource_profile()


@router.get("/ingestion/batches/{batch_id}")
async def get_ingestion_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    batch = await ingestion_service.get_ingestion_batch(
        batch_id,
        user_id=current_user["user_id"],
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@router.get("/ingestion/batches/{batch_id}/stream")
async def stream_ingestion_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    """SSE stream for durable batch ingestion progress."""

    async def event_generator():
        last_payload: str | None = None
        while True:
            batch = await ingestion_service.get_ingestion_batch(
                batch_id,
                user_id=current_user["user_id"],
            )
            if not batch:
                yield build_sse_error("Batch not found")
                return
            payload = json.dumps(
                {
                    "type": "batch_progress",
                    "batch_id": batch_id,
                    "status": batch.get("status"),
                    "current_phase": batch.get("current_phase"),
                    "total_files": batch.get("total_files", 0),
                    "queued_count": batch.get("queued_count", 0),
                    "processing_count": batch.get("processing_count", 0),
                    "vector_ready_count": batch.get("vector_ready_count", 0),
                    "graph_ready_count": batch.get("graph_ready_count", 0),
                    "graph_partial_count": batch.get("graph_partial_count", 0),
                    "needs_backfill_count": batch.get("needs_backfill_count", 0),
                    "failed_count": batch.get("failed_count", 0),
                    "cancelled_count": batch.get("cancelled_count", 0),
                    "queue_metrics": batch.get("queue_metrics", {}),
                },
                default=str,
            )
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if batch.get("status") in {"completed", "failed", "cancelled"}:
                yield build_sse_done()
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/ingestion/batches/{batch_id}/pause")
async def pause_ingestion_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        return await ingestion_service.pause_ingestion_batch(
            batch_id,
            user_id=current_user["user_id"],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found") from exc


@router.post("/ingestion/batches/{batch_id}/resume")
async def resume_ingestion_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        return await ingestion_service.resume_ingestion_batch(
            batch_id,
            user_id=current_user["user_id"],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found") from exc


@router.post("/ingestion/batches/{batch_id}/cancel")
async def cancel_ingestion_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        return await ingestion_service.cancel_ingestion_batch(
            batch_id,
            user_id=current_user["user_id"],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found") from exc


@router.post("/ingestion/batches/{batch_id}/retry-failed")
async def retry_failed_ingestion_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        return await ingestion_service.retry_failed_ingestion_batch(
            batch_id,
            user_id=current_user["user_id"],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found") from exc


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

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

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

    preflight = await ingestion_service.preflight_ingest(
        corpus=corpus,
        corpus_id=corpus_id,
        ingestion_config=cfg,
        ingest_overrides=overrides or None,
    )
    if not preflight.get("ok"):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Ingest preflight failed. No parse, summary, embedding, or graph work was started.",
                "errors": preflight.get("errors", []),
                "preflight": preflight,
            },
        )

    if not await _try_acquire_ingest_slot():
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many active ingest jobs ({_INGEST_ACTIVE_LIMIT}). "
                "Wait for current uploads to finish or lower upload concurrency."
            ),
        )

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
    return IngestJobResponse(
        job_id=doc.get("file_id", doc_id),
        doc_id=doc["doc_id"],
        corpus_id=doc["corpus_id"],
        filename=doc.get("filename", ""),
        source_tier=doc.get("source_tier"),
        status=(
            "done"
            if ws_raw.get("vector_ready")
            or (ws_raw.get("mongo_written") and ws_raw.get("qdrant_written"))
            else "processing"
        ),
        write_state=WriteState(**ws_raw) if ws_raw else WriteState(),
        chunk_count=doc.get("chunk_count", 0),
        parent_count=len(doc.get("parent_chunks", [])),
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
            mongo_done = ws_raw.get("mongo_written", False)
            qdrant_done = ws_raw.get("qdrant_written", False)
            neo4j_done = ws_raw.get("neo4j_written", False)
            verified = ws_raw.get("verified")  # None until verify runs
            verify_errors = ws_raw.get("verify_errors", []) or []
            warnings = ws_raw.get("warnings", []) or []
            error = doc.get("error")

            if error:
                status = "failed"
            elif mongo_done and qdrant_done and verified is not None:
                # Only emit "done" once verification has completed.
                status = "done"
            else:
                status = "processing"

            # Determine current pipeline stage
            if error:
                stage = "failed"
            elif verified is False:
                stage = "verify_failed"
            elif verified is True:
                stage = "verified"
            elif neo4j_done and qdrant_done and mongo_done:
                stage = "verifying"
            elif qdrant_done and mongo_done:
                stage = "graph_extracting" if not neo4j_done else "verifying"
            elif mongo_done:
                stage = "embedding"
            else:
                stage = "ingesting"

            payload = json.dumps(
                {
                    "type": "progress",
                    "doc_id": doc["doc_id"],
                    "corpus_id": doc["corpus_id"],
                    "filename": doc.get("filename", ""),
                    "status": status,
                    "stage": stage,
                    "source_tier": doc.get("source_tier"),
                    "chunk_count": doc.get("chunk_count", 0),
                    "parent_count": len(doc.get("parent_chunks", [])),
                    "write_state": {
                        "mongo_written": mongo_done,
                        "qdrant_written": qdrant_done,
                        "neo4j_written": neo4j_done,
                        "warnings": warnings,
                        "verified": verified,
                        "verify_errors": verify_errors,
                    },
                    "error": error,
                }
            )

            # Only emit if status changed (avoids flooding)
            if payload != last_status:
                yield f"data: {payload}\n\n"
                last_status = payload

            if status in ("done", "failed"):
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
