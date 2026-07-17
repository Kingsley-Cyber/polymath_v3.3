# backend/routers/health.py
# GET /api/health - Returns status of all services
# Thin router: validate → call service → return

import logging

from fastapi import APIRouter, HTTPException
from models.schemas import HealthResponse
from services.health_service import health_service

router = APIRouter(prefix="/api", tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Check health status of all services.

    Returns status of MongoDB, Qdrant, Neo4j (if enabled), LiteLLM, and Ollama.
    """
    # Thin router: just call the health service
    return await health_service.check_all_services()


@router.get("/health/live")
async def liveness():
    """Shallow liveness probe — confirms the backend process is up WITHOUT
    pinging dependencies. Used by the container healthcheck so a slow or
    restarting dependency (Mongo/Qdrant/Neo4j/LiteLLM) cannot mark the backend
    container unhealthy when the app itself is fine."""
    return {"status": "alive"}


@router.post("/health/embedder/batch-ready")
async def embedder_batch_ready():
    """Fail-closed local-embedder preflight for an evaluation batch."""

    from services.embedder import preflight_local_embedder_for_eval_batch

    try:
        return await preflight_local_embedder_for_eval_batch()
    except Exception as exc:
        logger.error("Evaluation embedder preflight failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"evaluation embedder preflight failed: {exc}",
        ) from exc
