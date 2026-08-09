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


@router.get("/health/engine")
async def extraction_engine_health():
    """Human-frontend view of the extraction control plane: which sidecar is
    routed (and by which layer), its live health and release pins, the
    qualified-release registry, and the last routing change (audit trail).
    Deterministic and fail-safe: every field degrades to a labeled state
    rather than an error."""
    import json as _json
    import urllib.request as _rq

    from services.extraction.engine_routing import describe_route

    routing = describe_route()
    engine = {"reachable": False}
    url = routing.get("sidecar_url")
    if url:
        try:
            with _rq.urlopen(str(url).rstrip("/") + "/health", timeout=5) as resp:
                health = _json.loads(resp.read())
            engine = {
                "reachable": True,
                "release": health.get("release"),
                "device": health.get("device"),
                "model": health.get("model"),
                "release_matches_expectation": (
                    health.get("release") == routing.get("expected_release")
                    if routing.get("expected_release") else None
                ),
            }
        except Exception as exc:  # noqa: BLE001 — reachability is the datum
            engine = {"reachable": False,
                      "error": f"{type(exc).__name__}: {exc}"[:160]}
    return {"routing": routing, "engine": engine}


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
