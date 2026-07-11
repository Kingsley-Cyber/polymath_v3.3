from __future__ import annotations

import asyncio

import pytest

from services.ingestion import model_lifecycle


@pytest.fixture(autouse=True)
def _clear_lifecycle_failure_cooldowns():
    model_lifecycle._failure_cooldowns.clear()
    yield
    model_lifecycle._failure_cooldowns.clear()


@pytest.mark.asyncio
async def test_ready_quarantines_failed_managed_lane_and_keeps_cloud_lane(
    monkeypatch,
):
    attempts: list[str] = []

    async def fake_ready(entry, *, purpose):
        attempts.append(str(entry["model"]))
        raise OSError("controller offline")

    monkeypatch.setattr(model_lifecycle, "_ensure_one_ready", fake_ready)
    pool = [
        {
            "model": "openai/polymath-extract",
            "lifecycle_base_url": "http://controller.test",
            "lifecycle_auto_start": True,
        },
        {
            "model": "openai/tencent/Hy3",
            "base_url": "https://cloud.test/v1",
        },
    ]

    ready = await model_lifecycle.ensure_model_lifecycle_ready(
        pool,
        purpose="ghost_b",
    )

    assert attempts == ["openai/polymath-extract"]
    assert [entry["model"] for entry in ready] == ["openai/tencent/Hy3"]


@pytest.mark.asyncio
async def test_ready_raises_when_failed_lifecycle_lane_is_only_capacity(monkeypatch):
    async def fake_ready(entry, *, purpose):
        raise OSError("controller offline")

    monkeypatch.setattr(model_lifecycle, "_ensure_one_ready", fake_ready)

    with pytest.raises(RuntimeError, match="all model lanes unavailable"):
        await model_lifecycle.ensure_model_lifecycle_ready(
            [
                {
                    "model": "openai/polymath-extract",
                    "lifecycle_base_url": "http://controller.test",
                    "lifecycle_auto_start": True,
                }
            ],
            purpose="ghost_b",
        )


@pytest.mark.asyncio
async def test_ready_cooldown_avoids_repeated_controller_attempts(monkeypatch):
    attempts = 0

    async def fake_ready(entry, *, purpose):
        nonlocal attempts
        attempts += 1
        raise OSError("controller offline")

    monkeypatch.setattr(model_lifecycle, "_ensure_one_ready", fake_ready)
    pool = [
        {
            "model": "openai/polymath-extract",
            "lifecycle_base_url": "http://controller.test",
            "lifecycle_auto_start": True,
            "lifecycle_failure_cooldown_seconds": 300,
        },
        {"model": "openai/tencent/Hy3"},
    ]

    first = await model_lifecycle.ensure_model_lifecycle_ready(
        pool,
        purpose="ghost_b",
    )
    second = await model_lifecycle.ensure_model_lifecycle_ready(
        pool,
        purpose="ghost_b",
    )

    assert attempts == 1
    assert first == second == [{"model": "openai/tencent/Hy3"}]


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
            "extra_params": {"lifecycle_idle_shutdown_seconds": 0},
        },
        {
            "lifecycle_base_url": "http://192.168.1.83:8085",
            "lifecycle_down_path": "down",
            "lifecycle_api_key": "manager-key",
            "lifecycle_auto_stop": True,
            "extra_params": {"lifecycle_idle_shutdown_seconds": 0},
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


@pytest.mark.asyncio
async def test_shutdown_model_lifecycle_positive_idle_schedules_without_down(
    monkeypatch,
):
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
    model_lifecycle._shutdown_tasks.clear()
    model_lifecycle._shutdown_generations.clear()

    pool = [
        {
            "lifecycle_base_url": "http://192.168.1.83:8085",
            "lifecycle_down_path": "/down",
            "lifecycle_api_key": "manager-key",
            "lifecycle_auto_stop": True,
            "extra_params": {"lifecycle_idle_shutdown_seconds": 600},
        }
    ]

    await model_lifecycle.shutdown_model_lifecycle(pool, purpose="ghost_b")

    assert posts == []
    assert len(model_lifecycle._shutdown_tasks) == 1

    tasks = list(model_lifecycle._shutdown_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    model_lifecycle._shutdown_tasks.clear()
    model_lifecycle._shutdown_generations.clear()


@pytest.mark.asyncio
async def test_lifecycle_hold_defers_shutdown_until_release(monkeypatch):
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
    model_lifecycle._shutdown_tasks.clear()
    model_lifecycle._shutdown_generations.clear()
    model_lifecycle._lifecycle_holds.clear()

    pool = [
        {
            "lifecycle_base_url": "http://192.168.1.83:8085",
            "lifecycle_down_path": "/down",
            "lifecycle_api_key": "manager-key",
            "lifecycle_auto_start": False,
            "lifecycle_auto_stop": True,
            "extra_params": {"lifecycle_idle_shutdown_seconds": 0},
        }
    ]

    await model_lifecycle.acquire_model_lifecycle_hold(
        pool,
        purpose="batch:a1",
        hold_id="batch:a1-full",
    )
    await model_lifecycle.shutdown_model_lifecycle(pool, purpose="ghost_b")
    assert posts == []

    await model_lifecycle.release_model_lifecycle_hold(
        pool,
        purpose="batch:a1",
        hold_id="batch:a1-full",
    )

    assert posts == [
        (
            "http://192.168.1.83:8085/down",
            {"X-Api-Key": "manager-key"},
        )
    ]
    assert model_lifecycle._lifecycle_holds == {}
