# backend/routers/health.py
# GET /api/health - Returns status of all services
# Thin router: validate → call service → return

import logging

from fastapi import APIRouter
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
