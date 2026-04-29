"""
Tests for the Phase 22 Modal deployer + router.

All Modal SDK calls are mocked — we never hit modal.com. Two layers:
  1. Unit tests on services.modal_deployer — deploy_app, destroy_app,
     get_app_status, warm_up.
  2. Smoke tests on modal_embedder.build_app — confirm that kwargs
     actually flow through to the registered function specs (catches
     decorator-capture regressions that the factory refactor exists
     specifically to prevent).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import modal_deployer
from services.modal_deployer import (
    ModalDeployError,
    deploy_app,
    destroy_app,
    get_app_status,
    warm_up,
)


# ── build_app smoke ─────────────────────────────────────────────────────────


def test_build_app_returns_modal_app_with_expected_name():
    """build_app constructs a Modal App whose name matches the kwarg —
    proves we're not accidentally reusing a stale global."""
    from modal_embedder import build_app

    app = build_app(app_name="polymath-test-one", gpu_tier="T4")
    assert app is not None
    # modal.App exposes .name in 0.64
    assert getattr(app, "name", None) == "polymath-test-one"


def test_build_app_is_reentrant_with_different_params():
    """Two consecutive build_app calls must produce two independent App
    objects with different names — decorator-capture regression guard."""
    from modal_embedder import build_app

    app_a = build_app(app_name="polymath-test-a", gpu_tier="T4")
    app_b = build_app(app_name="polymath-test-b", gpu_tier="L4")
    assert app_a is not app_b
    assert getattr(app_a, "name", None) == "polymath-test-a"
    assert getattr(app_b, "name", None) == "polymath-test-b"


# ── deploy_app ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_happy_path_streams_phases_and_returns_url():
    events: list[dict] = []

    async def capture(evt):
        events.append(evt)

    # Build a fake app object with a registered serve function carrying a URL
    fake_serve = MagicMock(web_url="https://ws--polymath-embedder-serve.modal.run")
    fake_app = MagicMock()
    fake_app.registered_functions = {"serve": fake_serve}

    fake_handle = MagicMock(app_id="ap-deadbeef", web_urls=[fake_serve.web_url])

    with patch.object(modal_deployer, "_verify_creds", new_callable=AsyncMock), \
         patch("modal_embedder.build_app", return_value=fake_app), \
         patch("modal.runner.deploy_app", return_value=fake_handle) as sdk_mock:
        result = await deploy_app(
            token_id="id-x", token_secret="secret-y",
            gpu_tier="T4", max_containers=4, min_containers=0,
            app_name="polymath-embedder",
            progress_callback=capture,
        )

    sdk_mock.assert_called_once()
    assert result["url"].endswith(".modal.run")
    assert result["app_id"] == "ap-deadbeef"
    assert isinstance(result["duration_ms"], int)
    phases = [e["phase"] for e in events]
    assert phases[:3] == ["verifying_tokens", "building_app", "deploying"]
    assert phases[-1] == "ready"


@pytest.mark.asyncio
async def test_deploy_missing_tokens_raises_before_any_sdk_call():
    with pytest.raises(ModalDeployError) as exc_info:
        await deploy_app(
            token_id="", token_secret="",
            app_name="polymath-embedder",
        )
    assert exc_info.value.at_phase == "verifying_tokens"


@pytest.mark.asyncio
async def test_deploy_verify_failure_never_reaches_build():
    """If token verify raises, build_app must NOT be called — cheap early
    exit is the whole point of the verify phase."""
    build_mock = MagicMock()
    with patch.object(
        modal_deployer, "_verify_creds",
        new_callable=AsyncMock,
        side_effect=ModalDeployError("bad creds", at_phase="verifying_tokens"),
    ), patch("modal_embedder.build_app", build_mock):
        with pytest.raises(ModalDeployError):
            await deploy_app(
                token_id="id-x", token_secret="secret-y",
                app_name="polymath-embedder",
            )
    build_mock.assert_not_called()


@pytest.mark.asyncio
async def test_deploy_sdk_exception_tagged_at_deploying_phase():
    fake_app = MagicMock()
    fake_app.registered_functions = {}
    with patch.object(modal_deployer, "_verify_creds", new_callable=AsyncMock), \
         patch("modal_embedder.build_app", return_value=fake_app), \
         patch("modal.runner.deploy_app", side_effect=RuntimeError("quota exceeded")):
        with pytest.raises(ModalDeployError) as exc_info:
            await deploy_app(
                token_id="id-x", token_secret="secret-y",
                app_name="polymath-embedder",
            )
    assert exc_info.value.at_phase == "deploying"


# ── destroy_app ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_destroy_idempotent_when_app_absent():
    """modal.App.lookup raising NotFoundError is NOT an error — destroy is
    idempotent. The endpoint returns ok=True so the UI can 'destroy' an
    already-dead app without showing a scary 500."""
    class _NotFound(Exception):
        pass

    fake_modal = MagicMock()
    fake_modal.App.lookup.side_effect = _NotFound("gone")

    with patch.object(modal_deployer, "_verify_creds", new_callable=AsyncMock), \
         patch.dict("sys.modules", {"modal": fake_modal}):
        result = await destroy_app(
            token_id="id-x", token_secret="secret-y",
            app_name="polymath-embedder",
        )
    assert result["ok"] is True
    assert result["was_deployed"] is False


# ── get_app_status ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_deployed_false_on_missing_tokens():
    res = await get_app_status(token_id="", token_secret="")
    assert res["deployed"] is False
    assert "tokens missing" in res.get("error", "").lower()


@pytest.mark.asyncio
async def test_status_returns_deployed_true_with_url_and_app_id():
    handle = MagicMock(app_id="ap-xyz", web_urls=["https://ws--p-serve.modal.run"])
    fake_modal = MagicMock()
    fake_modal.App.lookup.return_value = handle
    with patch.object(modal_deployer, "_verify_creds", new_callable=AsyncMock), \
         patch.dict("sys.modules", {"modal": fake_modal}):
        res = await get_app_status(
            token_id="id-x", token_secret="secret-y",
            app_name="polymath-embedder",
        )
    assert res["deployed"] is True
    assert res["url"].endswith(".modal.run")
    assert res["app_id"] == "ap-xyz"


# ── warm_up ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warm_up_hits_health_endpoint():
    called: dict = {}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            called["url"] = url
            resp = MagicMock()
            resp.status_code = 200
            return resp

    with patch("services.modal_deployer.httpx.AsyncClient", return_value=_Client()):
        await warm_up("https://ws--p-serve.modal.run")
    assert called["url"].endswith("/health")


@pytest.mark.asyncio
async def test_warm_up_swallows_exceptions_silently():
    """Warm-up must be fire-and-forget — a failed ping cannot break the
    deploy flow."""
    class _Broken:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            raise RuntimeError("connection refused")

    with patch("services.modal_deployer.httpx.AsyncClient", return_value=_Broken()):
        # No exception should propagate
        await warm_up("https://ws--p-serve.modal.run")


@pytest.mark.asyncio
async def test_warm_up_noop_on_empty_url():
    # Just assert no exception; no HTTP client should be constructed.
    with patch("services.modal_deployer.httpx.AsyncClient") as client_mock:
        await warm_up("")
    client_mock.assert_not_called()
