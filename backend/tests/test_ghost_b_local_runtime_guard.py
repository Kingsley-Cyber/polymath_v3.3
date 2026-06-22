"""Runtime safety guards for local Ghost B extraction sidecars."""

from __future__ import annotations

from services import ghost_b_local


def _health(**gliner_overrides):
    return {
        "status": "ok",
        "warm": True,
        "gliner": {
            "backend": "onnx",
            "loaded": True,
            "device": "cuda",
            "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            **gliner_overrides,
        },
    }


def test_sidecar_health_accepts_onnx_cuda(monkeypatch):
    monkeypatch.delenv("LOCAL_GHOST_B_ALLOW_ONNX_CPU_FALLBACK", raising=False)

    usable, reason = ghost_b_local._sidecar_health_usable(_health())

    assert usable is True
    assert reason == "ok"


def test_sidecar_health_rejects_onnx_cpu_fallback_by_default(monkeypatch):
    monkeypatch.delenv("LOCAL_GHOST_B_ALLOW_ONNX_CPU_FALLBACK", raising=False)

    usable, reason = ghost_b_local._sidecar_health_usable(
        _health(device="cpu", providers=["CPUExecutionProvider"]),
    )

    assert usable is False
    assert "CUDAExecutionProvider" in reason


def test_sidecar_health_allows_onnx_cpu_fallback_when_explicit(monkeypatch):
    monkeypatch.setenv("LOCAL_GHOST_B_ALLOW_ONNX_CPU_FALLBACK", "true")

    usable, reason = ghost_b_local._sidecar_health_usable(
        _health(device="cpu", providers=["CPUExecutionProvider"]),
    )

    assert usable is True
    assert reason == "ok"


def test_sidecar_health_accepts_local_torch_mps(monkeypatch):
    monkeypatch.delenv("LOCAL_GHOST_B_ALLOW_ONNX_CPU_FALLBACK", raising=False)

    usable, reason = ghost_b_local._sidecar_health_usable(
        _health(backend="torch", device="mps", providers=[]),
    )

    assert usable is True
    assert reason == "ok"

