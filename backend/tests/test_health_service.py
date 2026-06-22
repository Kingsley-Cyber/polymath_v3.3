import pytest

from models.schemas import ServiceStatus
from services import health_service as health_module
from services.health_service import HealthService


async def _ok() -> ServiceStatus:
    return ServiceStatus(status="ok", latency_ms=1.0)


async def _optional_ollama_down() -> ServiceStatus:
    return ServiceStatus(
        status="degraded",
        latency_ms=1.0,
        error="Optional host Ollama unavailable: test",
    )


@pytest.mark.asyncio
async def test_optional_ollama_does_not_degrade_overall_health(monkeypatch):
    monkeypatch.setattr(health_module.settings, "NEO4J_ENABLED", False)
    monkeypatch.setattr(health_module.settings, "MODAL_ENABLED", False)
    monkeypatch.setattr(health_module.settings, "SILICONFLOW_ENABLED", False)
    monkeypatch.setattr(health_module.settings, "LOCAL_EMBEDDER_ENABLED", False)
    monkeypatch.setattr(health_module.settings, "LOCAL_RERANKER_ENABLED", False)
    for field in health_module._OLLAMA_MODEL_FIELDS:
        monkeypatch.setattr(health_module.settings, field, "")

    service = HealthService()
    for name in (
        "check_mongodb",
        "check_qdrant",
        "check_litellm",
        "check_redis",
        "check_embedder",
        "check_reranker",
    ):
        monkeypatch.setattr(service, name, _ok)
    monkeypatch.setattr(service, "check_ollama", _optional_ollama_down)

    result = await service.check_all_services()

    assert result.status == "ok"
    assert result.services["ollama"].status == "degraded"
    assert result.services["reranker"].status == "degraded"
