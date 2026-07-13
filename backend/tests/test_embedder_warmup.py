"""P1.8: embedder real-inference warmup + tri-state readiness.

The sidecar module is loaded from the ``SIDECAR_PATH`` env var (repo-relative
default) and the encode function is monkeypatched — there is no Apple GPU /
MLX runtime inside the backend container. Tests assert the shared invariants,
not one wording or ranking:

* warmup runs the fixed neutral batch through the serving-path admission gate
  at the lowest (backfill) workload class and always releases the gate;
* success flips ``inference_ready`` and records duration + vector dim;
* any failure records the error and leaves ``inference_ready`` false;
* /health carries the additive tri-state + warmup diagnostics keys while the
  legacy keys survive untouched;
* the backend health checker prefers ``inference_ready`` when present and
  falls back to legacy HTTP-200-means-ok when absent.
"""

import importlib.util
import os
from pathlib import Path

import pytest


def _load_sidecar_module():
    root = Path(
        os.environ.get(
            "SIDECAR_PATH",
            str(Path(__file__).parents[2] / "scripts" / "apple_ml_services"),
        )
    )
    path = root / "embedder_mlx" / "main.py"
    spec = importlib.util.spec_from_file_location("embedder_mlx_warmup_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _FakeVectors:
    """Stands in for the numpy array _encode_batch returns; shape only."""

    def __init__(self, rows: int, dim: int) -> None:
        self.shape = (rows, dim)


# ── Sidecar warmup behaviour ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warmup_success_sets_flags_duration_and_dim(monkeypatch):
    module = _load_sidecar_module()
    seen: dict = {}

    def fake_encode(inputs):
        seen["inputs"] = list(inputs)
        return _FakeVectors(len(inputs), module.EMBED_DIM)

    real_acquire = module._request_gate.acquire

    async def recording_acquire(workload_class, timeout):
        seen["workload_class"] = workload_class
        await real_acquire(workload_class, timeout)

    monkeypatch.setattr(module, "_encode_batch", fake_encode)
    monkeypatch.setattr(module._request_gate, "acquire", recording_acquire)

    await module._run_startup_warmup()

    assert module._warmup_complete is True
    assert module._warmup_error is None
    assert isinstance(module._warmup_seconds, float)
    assert module._warmup_seconds >= 0.0
    assert module._warmup_vector_dim == module.EMBED_DIM
    # Fixed neutral batch through the exact serving encode function.
    assert seen["inputs"] == ["warmup", "warmup"]
    # Lowest/backfill admission class: warmup can never delay interactive.
    assert seen["workload_class"] == "backfill_repair"
    # Gate fully released afterwards: an interactive request admits instantly.
    await module._request_gate.acquire("interactive_query", timeout=0.2)
    await module._request_gate.release()


@pytest.mark.asyncio
async def test_warmup_failure_records_error_and_stays_not_ready(monkeypatch):
    module = _load_sidecar_module()

    def broken_encode(inputs):
        raise RuntimeError("metal exploded")

    monkeypatch.setattr(module, "_encode_batch", broken_encode)

    await module._run_startup_warmup()

    assert module._warmup_complete is False
    assert "metal exploded" in (module._warmup_error or "")
    assert isinstance(module._warmup_seconds, float)
    assert module._warmup_vector_dim is None
    # Gate released even on failure.
    await module._request_gate.acquire("interactive_query", timeout=0.2)
    await module._request_gate.release()

    monkeypatch.setattr(module, "_model", object())
    body = await module.health()
    assert body["inference_ready"] is False
    assert "metal exploded" in body["warmup"]["error"]


@pytest.mark.asyncio
async def test_warmup_dimension_mismatch_is_a_failure(monkeypatch):
    module = _load_sidecar_module()
    monkeypatch.setattr(
        module, "_encode_batch", lambda inputs: _FakeVectors(len(inputs), 4)
    )

    await module._run_startup_warmup()

    assert module._warmup_complete is False
    assert "dimension" in (module._warmup_error or "").lower()
    assert module._warmup_vector_dim is None


# ── Sidecar /health tri-state ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_carries_tri_state_before_and_after_warmup(monkeypatch):
    module = _load_sidecar_module()
    monkeypatch.setattr(module, "_model", object())  # model loaded, no GPU

    before = await module.health()
    assert before["status"] == "ok"  # legacy contract untouched
    assert before["liveness"] is True
    assert before["model_loaded"] is True
    assert before["inference_ready"] is False
    assert before["warmup"] == {
        "complete": False,
        "duration_s": None,
        "vector_dim": None,
        "model": module.MODEL_ID,
        "error": None,
    }

    monkeypatch.setattr(
        module,
        "_encode_batch",
        lambda inputs: _FakeVectors(len(inputs), module.EMBED_DIM),
    )
    await module._run_startup_warmup()

    after = await module.health()
    assert after["liveness"] is True
    assert after["model_loaded"] is True
    assert after["inference_ready"] is True
    assert after["warmup"]["complete"] is True
    assert after["warmup"]["duration_s"] is not None
    assert after["warmup"]["vector_dim"] == module.EMBED_DIM
    assert after["warmup"]["model"] == module.MODEL_ID
    assert after["warmup"]["error"] is None
    # Additive-only: every legacy health key survives.
    for key in (
        "status",
        "model",
        "device",
        "in_flight",
        "active_seconds",
        "last_request_seconds",
        "last_error",
        "queue_depth",
    ):
        assert key in after


# ── Backend checker: prefer inference_ready, legacy fallback ────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("response body is not JSON")
        return self._payload


def _fake_async_client(response):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, timeout=None):
            return response

    return _Client


async def _checked_status(monkeypatch, response):
    import services.health_service as hs

    monkeypatch.setattr(hs.httpx, "AsyncClient", _fake_async_client(response))
    return await hs.HealthService().check_embedder()


@pytest.mark.asyncio
async def test_checker_ok_when_inference_ready_true(monkeypatch):
    status = await _checked_status(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "status": "ok",
                "inference_ready": True,
                "warmup": {"complete": True, "error": None},
            },
        ),
    )
    assert status.status == "ok"
    assert status.error is None


@pytest.mark.asyncio
async def test_checker_error_when_inference_ready_false(monkeypatch):
    status = await _checked_status(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "status": "ok",
                "inference_ready": False,
                "warmup": {"complete": False, "error": "RuntimeError: boom"},
            },
        ),
    )
    assert status.status == "error"
    assert "inference-ready" in (status.error or "")
    assert "boom" in (status.error or "")


@pytest.mark.asyncio
async def test_checker_legacy_fallback_without_tri_state_key(monkeypatch):
    status = await _checked_status(monkeypatch, _FakeResponse(200, {"status": "ok"}))
    assert status.status == "ok"


@pytest.mark.asyncio
async def test_checker_legacy_fallback_on_non_json_200(monkeypatch):
    status = await _checked_status(monkeypatch, _FakeResponse(200, None))
    assert status.status == "ok"


@pytest.mark.asyncio
async def test_checker_still_errors_on_http_503(monkeypatch):
    status = await _checked_status(monkeypatch, _FakeResponse(503, {"detail": "down"}))
    assert status.status == "error"
    assert "HTTP 503" in (status.error or "")
