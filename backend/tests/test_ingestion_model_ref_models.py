from unittest.mock import AsyncMock

import pytest

from routers import ingestion as ingestion_router


def test_model_ids_from_openai_models_payload_are_deduped_and_sorted():
    payload = {
        "data": [
            {"id": "polymath-extract"},
            {"id": "qwen3-30b-a3b-2507"},
            {"id": "polymath-extract"},
        ]
    }

    assert ingestion_router._model_ids_from_models_payload(payload) == [
        "polymath-extract",
        "qwen3-30b-a3b-2507",
    ]


@pytest.mark.asyncio
async def test_list_model_ref_models_calls_base_models_endpoint(monkeypatch):
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "polymath-extract"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            calls.append((url, dict(headers or {})))
            return FakeResponse()

    monkeypatch.setattr(ingestion_router.httpx, "AsyncClient", FakeClient)

    result = await ingestion_router._list_model_ref_models(
        {
            "base_url": "http://192.168.1.83:8000/v1",
            "api_key": "secret",
            "lifecycle_auto_start": False,
            "lifecycle_auto_stop": False,
        }
    )

    assert result.ok is True
    assert result.models == ["polymath-extract"]
    assert calls == [
        (
            "http://192.168.1.83:8000/v1/models",
            {"Authorization": "Bearer secret"},
        )
    ]


@pytest.mark.asyncio
async def test_background_repair_heartbeat_renews_expiring_lease(monkeypatch):
    update_one = AsyncMock()
    update_one.return_value.matched_count = 0

    class FakeDatabase:
        def __getitem__(self, name):
            assert name == "ingest_repair_runs"
            return type("Collection", (), {"update_one": update_one})()

    monkeypatch.setattr(ingestion_router.ingestion_service, "_db", FakeDatabase())
    monkeypatch.setattr(ingestion_router.asyncio, "sleep", AsyncMock())

    await ingestion_router._heartbeat_background_repair("repair-1")

    query, update = update_one.await_args.args
    assert query == {"run_id": "repair-1", "status": "running"}
    assert update["$set"]["heartbeat_at"] == update["$set"]["updated_at"]
    assert (
        update["$set"]["lease_expires_at"]
        > update["$set"]["heartbeat_at"]
    )
