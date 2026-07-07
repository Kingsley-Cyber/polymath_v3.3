from __future__ import annotations

import pytest

from services.ingestion import model_lifecycle


@pytest.mark.asyncio
async def test_shutdown_model_lifecycle_posts_down_once_per_control_plane(monkeypatch):
    posts: list[tuple[str, dict[str, str]]] = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers):
            posts.append((url, dict(headers or {})))
            return FakeResponse()

    monkeypatch.setattr(model_lifecycle.httpx, "AsyncClient", FakeClient)

    pool = [
        {
            "lifecycle_base_url": "http://192.168.1.83:8085/",
            "lifecycle_down_path": "/down",
            "lifecycle_api_key": "manager-key",
            "lifecycle_auto_stop": True,
        },
        {
            "lifecycle_base_url": "http://192.168.1.83:8085",
            "lifecycle_down_path": "down",
            "lifecycle_api_key": "manager-key",
            "lifecycle_auto_stop": True,
        },
        {
            "lifecycle_base_url": "http://192.168.1.84:8085",
            "lifecycle_down_path": "/down",
            "lifecycle_api_key": "other-key",
            "lifecycle_auto_stop": False,
        },
    ]

    await model_lifecycle.shutdown_model_lifecycle(pool, purpose="test")

    assert posts == [
        (
            "http://192.168.1.83:8085/down",
            {"X-Api-Key": "manager-key"},
        )
    ]
