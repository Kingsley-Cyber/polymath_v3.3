# backend/services/health_service.py
# Health check service - moves database checks from router to service layer
# All functions are async. Import: from services.health_service import health_service

import logging
from datetime import datetime

import httpx
from bson import ObjectId
from config import get_settings
from models.schemas import HealthResponse, ServiceStatus
from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)
settings = get_settings()

_CORE_HEALTH_SERVICES = {
    "mongodb",
    "qdrant",
    "litellm",
    "redis",
}
_OLLAMA_MODEL_FIELDS = (
    "DEFAULT_COMPLETION_MODEL",
    "AGENTIC_MODEL",
    "HYDE_MODEL",
    "REASONING_MODEL",
)


def _model_uses_ollama(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith("ollama/")


def _ollama_required() -> bool:
    """Return True when static server config actively selects Ollama.

    Most deployments use LiteLLM/cloud chat models while keeping host-native
    Ollama available for local experiments. In that setup an offline host
    Ollama should appear in the service list but should not mark the whole API
    degraded.
    """
    return any(
        _model_uses_ollama(getattr(settings, field, ""))
        for field in _OLLAMA_MODEL_FIELDS
    )


class HealthService:
    """Service for health checks of all dependencies."""

    async def check_mongodb(self) -> ServiceStatus:
        """Check MongoDB connectivity."""
        start = datetime.utcnow()
        try:
            client = AsyncIOMotorClient(settings.MONGODB_URI)
            await client.admin.command("ping")
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            client.close()
            return ServiceStatus(status="ok", latency_ms=round(latency, 2))
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"MongoDB health check failed: {e}")
            return ServiceStatus(
                status="error", latency_ms=round(latency, 2), error=str(e)
            )

    async def check_qdrant(self) -> ServiceStatus:
        """Check Qdrant connectivity."""
        start = datetime.utcnow()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{settings.QDRANT_URL}/healthz", timeout=5.0
                )
                latency = (datetime.utcnow() - start).total_seconds() * 1000
                if response.status_code == 200:
                    return ServiceStatus(status="ok", latency_ms=round(latency, 2))
                return ServiceStatus(
                    status="error",
                    latency_ms=round(latency, 2),
                    error=f"HTTP {response.status_code}",
                )
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"Qdrant health check failed: {e}")
            return ServiceStatus(
                status="error", latency_ms=round(latency, 2), error=str(e)
            )

    async def check_neo4j(self) -> ServiceStatus:
        """Check Neo4j connectivity."""
        start = datetime.utcnow()
        try:
            driver = AsyncGraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )
            async with driver.session() as session:
                await session.run("RETURN 1")
            await driver.close()
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            return ServiceStatus(status="ok", latency_ms=round(latency, 2))
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"Neo4j health check failed: {e}")
            return ServiceStatus(
                status="error", latency_ms=round(latency, 2), error=str(e)
            )

    async def check_litellm(self) -> ServiceStatus:
        """
        Check LiteLLM proxy connectivity.

        Phase 19.3 — switched probe from `/health` to `/`. LiteLLM's `/health`
        endpoint is auth-gated AND runs provider sub-checks that 400 when any
        configured route has an unreachable backend (expected while users
        haven't populated every provider key yet). `/` is the public welcome
        endpoint — 200 iff the proxy is running, which is what we actually
        want to surface in the frontend health banner.
        """
        start = datetime.utcnow()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{settings.LITELLM_URL}/",
                    timeout=5.0,
                )
                latency = (datetime.utcnow() - start).total_seconds() * 1000
                if response.status_code == 200:
                    return ServiceStatus(status="ok", latency_ms=round(latency, 2))
                return ServiceStatus(
                    status="error",
                    latency_ms=round(latency, 2),
                    error=f"HTTP {response.status_code}",
                )
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"LiteLLM health check failed: {e}")
            return ServiceStatus(
                status="error", latency_ms=round(latency, 2), error=str(e)
            )

    async def check_ollama(self) -> ServiceStatus:
        """Check Ollama connectivity."""
        start = datetime.utcnow()
        required = _ollama_required()

        def _offline_status(error: str) -> ServiceStatus:
            status = "error" if required else "degraded"
            prefix = "Required Ollama unavailable" if required else "Optional host Ollama unavailable"
            return ServiceStatus(
                status=status,
                latency_ms=round((datetime.utcnow() - start).total_seconds() * 1000, 2),
                error=f"{prefix}: {error}",
            )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{settings.OLLAMA_URL}/api/tags", timeout=5.0
                )
                latency = (datetime.utcnow() - start).total_seconds() * 1000
                if response.status_code == 200:
                    return ServiceStatus(status="ok", latency_ms=round(latency, 2))
                return _offline_status(f"HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"Ollama health check failed: {e}")
            return _offline_status(str(e))

    async def check_modal(self) -> ServiceStatus:
        """Probe Modal cloud GPU embedder with a 1-text sample."""
        from services.embedder import probe_modal

        result = await probe_modal("health")
        if result["ok"]:
            return ServiceStatus(
                status="ok",
                latency_ms=result["latency_ms"],
            )
        return ServiceStatus(
            status="error",
            latency_ms=result["latency_ms"],
            error=result.get("error") or "unknown Modal error",
        )

    async def check_siliconflow(self) -> ServiceStatus:
        """Probe SiliconFlow cloud embeddings API with a 1-text sample."""
        from services.embedder import probe_siliconflow

        result = await probe_siliconflow("health")
        if result["ok"]:
            return ServiceStatus(
                status="ok",
                latency_ms=result["latency_ms"],
            )
        return ServiceStatus(
            status="error",
            latency_ms=result["latency_ms"],
            error=result.get("error") or "unknown SiliconFlow error",
        )

    async def check_redis(self) -> ServiceStatus:
        """Check Redis connectivity via PING."""
        start = datetime.utcnow()
        try:
            import redis.asyncio as aioredis

            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            pong = await r.ping()
            await r.close()
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            if pong:
                return ServiceStatus(status="ok", latency_ms=round(latency, 2))
            return ServiceStatus(
                status="error",
                latency_ms=round(latency, 2),
                error="Redis PING returned False",
            )
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"Redis health check failed: {e}")
            return ServiceStatus(
                status="error", latency_ms=round(latency, 2), error=str(e)
            )

    async def check_embedder(self) -> ServiceStatus:
        """Check embedder service connectivity."""
        start = datetime.utcnow()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{settings.EMBEDDER_URL}/health", timeout=5.0
                )
                latency = (datetime.utcnow() - start).total_seconds() * 1000
                if response.status_code == 200:
                    return ServiceStatus(status="ok", latency_ms=round(latency, 2))
                return ServiceStatus(
                    status="error",
                    latency_ms=round(latency, 2),
                    error=f"HTTP {response.status_code}",
                )
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"Embedder health check failed: {e}")
            return ServiceStatus(
                status="error", latency_ms=round(latency, 2), error=str(e)
            )

    async def check_reranker(self) -> ServiceStatus:
        """Check reranker service connectivity."""
        start = datetime.utcnow()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{settings.RERANKER_URL}/health", timeout=5.0
                )
                latency = (datetime.utcnow() - start).total_seconds() * 1000
                if response.status_code == 200:
                    return ServiceStatus(status="ok", latency_ms=round(latency, 2))
                return ServiceStatus(
                    status="error",
                    latency_ms=round(latency, 2),
                    error=f"HTTP {response.status_code}",
                )
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"Reranker health check failed: {e}")
            return ServiceStatus(
                status="error", latency_ms=round(latency, 2), error=str(e)
            )

    async def check_all_services(self) -> HealthResponse:
        """
        Check health status of all services.

        Returns status of MongoDB, Qdrant, Neo4j (if enabled), LiteLLM, and Ollama.
        """
        import asyncio

        # Define all health checks
        tasks = {
            "mongodb": self.check_mongodb(),
            "qdrant": self.check_qdrant(),
            "litellm": self.check_litellm(),
            "ollama": self.check_ollama(),
            "redis": self.check_redis(),
        }

        # Local model sidecars are opt-in Docker profiles. When disabled, skip
        # their network probes so API-first deployments do not look broken or
        # spend seconds waiting on DNS names that intentionally do not exist.
        if settings.LOCAL_EMBEDDER_ENABLED:
            tasks["embedder"] = self.check_embedder()
        if settings.LOCAL_RERANKER_ENABLED:
            tasks["reranker"] = self.check_reranker()

        # Add Neo4j only if enabled
        if settings.NEO4J_ENABLED:
            tasks["neo4j"] = self.check_neo4j()

        # Add Modal only if enabled
        if settings.MODAL_ENABLED:
            tasks["modal"] = self.check_modal()

        # Add SiliconFlow only if enabled
        if settings.SILICONFLOW_ENABLED:
            tasks["siliconflow"] = self.check_siliconflow()

        # Run all checks concurrently
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        # Build services dict
        services = {}
        for (name, _), result in zip(tasks.items(), results):
            if isinstance(result, Exception):
                services[name] = ServiceStatus(status="error", error=str(result))
            else:
                services[name] = result

        required_services = set(_CORE_HEALTH_SERVICES)
        if settings.NEO4J_ENABLED:
            required_services.add("neo4j")
        if settings.MODAL_ENABLED:
            required_services.add("modal")
        if settings.SILICONFLOW_ENABLED:
            required_services.add("siliconflow")
        if _ollama_required():
            required_services.add("ollama")
        if settings.LOCAL_EMBEDDER_ENABLED:
            required_services.add("embedder")
        if settings.LOCAL_RERANKER_ENABLED:
            required_services.add("reranker")

        # Determine overall status from required services only. Optional
        # services remain visible in `services` with their own status.
        required_errors = sum(
            1
            for name, service in services.items()
            if name in required_services and service.status == "error"
        )
        if required_errors == 0:
            overall_status = "ok"
        elif required_errors < len(required_services):
            overall_status = "degraded"
        else:
            overall_status = "error"

        return HealthResponse(status=overall_status, services=services)


# Global instance
health_service = HealthService()
