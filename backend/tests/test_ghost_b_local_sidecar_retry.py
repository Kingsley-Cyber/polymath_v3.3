"""Liveness-probe retry for the Ghost B extraction sidecar.

A transient blip (Docker Desktop dropping its host.docker.internal route after
sleep/wake, a sidecar still warming) must NOT fail a whole ingest batch off one
bad /health round-trip. A genuinely-absent sidecar must still fail — after
exhausting the retry budget — with the actionable message. A reachable-but-
unusable sidecar (e.g. ONNX-CPU with fallback off) is a stable verdict and must
fail FAST without burning the retry budget.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from services import ghost_b_local

_HEALTHY = {
    "status": "ok",
    "warm": True,
    "gliner": {"backend": "torch", "device": "mps", "loaded": True},
}
_ONNX_CPU = {
    "status": "ok",
    "warm": True,
    "gliner": {"backend": "onnx", "device": "cpu",
               "providers": ["CPUExecutionProvider"]},
}


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install(monkeypatch, health_script, urls=None):
    """Patch httpx.AsyncClient with a scriptable stand-in shared across every
    client instance (probe clients across retries + the POST client), so the
    `health_script` advances one entry per GET /health regardless of how many
    clients the function opens. Each entry is an Exception class (raises), an
    int (that HTTP status), or a dict (200 + that /health payload)."""
    state = {"script": list(health_script), "idx": 0, "get": 0, "post": 0}

    class _Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            state["get"] += 1
            i = state["idx"]
            state["idx"] += 1
            beh = state["script"][i] if i < len(state["script"]) else _HEALTHY
            if isinstance(beh, type) and issubclass(beh, Exception):
                raise beh("simulated transport error")
            if isinstance(beh, int):
                return _Resp(status_code=beh)
            return _Resp(status_code=200, payload=beh)

        async def post(self, url, json=None):
            state["post"] += 1
            return _Resp(status_code=200, payload={"results": [], "timings": None})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))
    # Make backoff instant so the suite stays fast.
    monkeypatch.setattr(ghost_b_local, "PROBE_BACKOFF_S", 0.0)
    monkeypatch.setattr(ghost_b_local, "PROBE_BACKOFF_MAX_S", 0.0)
    monkeypatch.setattr(ghost_b_local, "RUNTIME_ENDPOINT_URLS", None)
    monkeypatch.setattr(
        ghost_b_local, "SIDECAR_URLS", urls or ["http://host.docker.internal:8084"])
    return state


def _run(tasks):
    return asyncio.run(
        ghost_b_local._extract_via_sidecar(tasks, do_facts=False, lens_id=None))


def test_transient_blip_recovers_after_retry(monkeypatch):
    monkeypatch.setattr(ghost_b_local, "PROBE_ATTEMPTS", 5)
    # First two probes unreachable, third one healthy -> must recover, not raise.
    state = _install(monkeypatch, [httpx.ConnectError, httpx.ConnectError, _HEALTHY])

    out = _run([{"chunk_id": "c1", "text": "x"}])

    assert out == []            # proceeded past the probe to dispatch
    assert state["get"] == 3    # retried until the 3rd probe succeeded
    assert state["post"] == 1   # the doc's single slice was POSTed


def test_genuinely_down_fails_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(ghost_b_local, "PROBE_ATTEMPTS", 4)
    state = _install(monkeypatch, [httpx.ConnectError] * 20)  # never reachable

    with pytest.raises(RuntimeError) as ei:
        _run([{"chunk_id": "c1", "text": "x"}])

    assert state["get"] == 4    # exhausted the full retry budget
    assert state["post"] == 0   # never dispatched
    msg = str(ei.value)
    assert "no extraction sidecar reachable" in msg
    assert "4 probe attempt" in msg


def test_unusable_sidecar_fails_fast_without_retry(monkeypatch):
    monkeypatch.delenv("LOCAL_GHOST_B_ALLOW_ONNX_CPU_FALLBACK", raising=False)
    monkeypatch.setattr(ghost_b_local, "PROBE_ATTEMPTS", 5)
    # Reachable on every probe, but ONNX-CPU -> unusable. Retrying cannot help.
    state = _install(monkeypatch, [_ONNX_CPU] * 20)

    with pytest.raises(RuntimeError) as ei:
        _run([{"chunk_id": "c1", "text": "x"}])

    assert state["get"] == 1    # one probe only — verdict is stable, no retry
    assert state["post"] == 0
    msg = str(ei.value)
    assert "unusable" in msg.lower()
    assert "CUDAExecutionProvider" in msg
