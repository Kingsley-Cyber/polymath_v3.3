from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models.schemas import IngestionConfig
from services.ingestion import preflight


class _QdrantOk:
    async def get_collections(self):
        return []


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None


class _LocalClientOk:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, _url):
        return _Response()


@pytest.mark.asyncio
async def test_preflight_blocks_local_embedder_when_service_disabled(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "get_settings",
        lambda: SimpleNamespace(
            LOCAL_EMBEDDER_ENABLED=False,
            EMBEDDER_URL="http://embedder:80",
            SILICONFLOW_EMBEDDER_URL="",
            SILICONFLOW_API_KEY="",
            MODAL_ENABLED=False,
            MODAL_EMBEDDER_URL="",
        ),
    )

    result = await preflight.run_ingest_preflight(
        config=IngestionConfig(embed_mode="local", use_neo4j=False),
        qdrant_client=_QdrantOk(),
    )

    assert result["ok"] is False
    assert "embedding unavailable" in result["errors"][0]
    assert result["embedding"]["provider"] == "local"


@pytest.mark.asyncio
async def test_preflight_allows_healthy_local_embedder(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "get_settings",
        lambda: SimpleNamespace(
            LOCAL_EMBEDDER_ENABLED=True,
            EMBEDDER_URL="http://embedder:80",
            SILICONFLOW_EMBEDDER_URL="",
            SILICONFLOW_API_KEY="",
            MODAL_ENABLED=False,
            MODAL_EMBEDDER_URL="",
        ),
    )
    monkeypatch.setattr(preflight.httpx, "AsyncClient", _LocalClientOk)

    result = await preflight.run_ingest_preflight(
        config=IngestionConfig(embed_mode="local", use_neo4j=False),
        qdrant_client=_QdrantOk(),
    )

    assert result["ok"] is True
    assert result["embedding"]["mode"] == "local"
    assert result["qdrant"]["ok"] is True


@pytest.mark.asyncio
async def test_preflight_does_not_paid_probe_api_embeddings(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "get_settings",
        lambda: SimpleNamespace(
            LOCAL_EMBEDDER_ENABLED=False,
            EMBEDDER_URL="http://embedder:80",
            SILICONFLOW_EMBEDDER_URL="https://api.example/v1/embeddings",
            SILICONFLOW_API_KEY="sk-test",
            MODAL_ENABLED=False,
            MODAL_EMBEDDER_URL="",
        ),
    )
    client_mock = MagicMock()
    monkeypatch.setattr(preflight.httpx, "AsyncClient", client_mock)

    result = await preflight.run_ingest_preflight(
        config=IngestionConfig(embed_mode="api", use_neo4j=False),
        qdrant_client=_QdrantOk(),
    )

    assert result["ok"] is True
    assert result["embedding"]["provider"] == "api"
    assert result["embedding"]["live_probe"] is False
    client_mock.assert_not_called()


def test_local_graph_preflight_keeps_llm_graph_disabled(monkeypatch):
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda _name: object())

    result = preflight.check_local_graph_preflight(
        IngestionConfig(
            use_neo4j=True,
            graph_extraction_engine="local_gliner",
            llm_fallback_enabled=False,
        )
    )

    assert result["ok"] is True
    assert result["engine"] == "local_gliner"
    assert result["llm_graph_calls_enabled"] is False
