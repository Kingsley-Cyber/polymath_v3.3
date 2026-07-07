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
from dataclasses import dataclass, field
import sys
import types

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
    ghost_b_local._PROBE_CACHE.clear()
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


def test_sidecar_microbatch_timeout_preserves_successes(monkeypatch):
    """A slow slice must not discard earlier successful chunk results."""
    state = {"get": 0, "post": 0}

    class _Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            state["get"] += 1
            return _Resp(status_code=200, payload=_HEALTHY)

        async def post(self, url, json=None):
            state["post"] += 1
            tasks = list((json or {}).get("tasks") or [])
            if state["post"] == 2:
                raise httpx.ReadTimeout("simulated slow GLiREL slice")
            return _Resp(
                status_code=200,
                payload={
                    "results": [
                        {
                            "schema_version": ghost_b_local.SCHEMA_VERSION,
                            "chunk_id": t["chunk_id"],
                            "doc_id": t["doc_id"],
                            "corpus_id": t["corpus_id"],
                            "entities": [],
                            "relations": [],
                            "facts": [],
                            "text": t.get("text") or "",
                        }
                        for t in tasks
                    ],
                    "timings": {"chunks": len(tasks), "total_s": 0.1},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))
    monkeypatch.setattr(ghost_b_local, "PROBE_ATTEMPTS", 1)
    monkeypatch.setattr(ghost_b_local, "RUNTIME_ENDPOINT_URLS", None)
    monkeypatch.setattr(ghost_b_local, "SIDECAR_URLS", ["http://sidecar.local"])
    monkeypatch.setattr(ghost_b_local, "SIDECAR_SLICE", 2048)
    monkeypatch.setattr(ghost_b_local, "SIDECAR_MICRO_BATCH", 2)
    monkeypatch.setattr(ghost_b_local, "SIDECAR_MAX_IN_FLIGHT", 1)
    monkeypatch.setattr(ghost_b_local, "SIDECAR_SLICE_TIMEOUT_S", 30.0)
    ghost_b_local._PROBE_CACHE.clear()
    tasks = [
        {"chunk_id": f"c{i}", "doc_id": "d", "corpus_id": "k", "text": "x"}
        for i in range(5)
    ]

    report = asyncio.run(
        ghost_b_local._extract_via_sidecar_report(
            tasks,
            do_facts=False,
            lens_id=None,
        )
    )

    assert [r["chunk_id"] for r in report.raw] == ["c0", "c1"]
    assert [f["chunk_id"] for f in report.failures] == ["c2", "c3", "c4"]
    assert report.failures[0]["error_type"] == "ReadTimeout"
    assert report.failures[-1]["error_type"] == "not_processed_after_slice_failure"
    assert report.metrics["requested_chunks"] == 5
    assert report.metrics["extracted_chunks"] == 2
    assert report.metrics["failed_chunks"] == 3
    assert state["post"] == 2


def test_extract_entities_report_carries_sidecar_failures(monkeypatch):
    fake_ghost_b = types.ModuleType("services.ghost_b")

    @dataclass
    class _EntityItem:
        canonical_name: str
        surface_form: str = ""
        entity_type: str = "Concept"
        confidence: float = 1.0
        query_aliases: list[str] = field(default_factory=list)
        definitional_phrase: str = ""
        object_kind: str = ""

    @dataclass
    class _RelationItem:
        subject: str
        predicate: str
        object: str
        object_kind: str = "literal"
        confidence: float = 1.0
        evidence_phrase: str = ""
        relation_cue: str = ""

    @dataclass
    class _FactItem:
        subject: str
        fact_type: str
        property_name: str
        value: str
        unit: str | None = None
        condition: str | None = None
        confidence: float = 1.0
        evidence_phrase: str = ""

    @dataclass
    class _ExtractionResult:
        schema_version: str
        chunk_id: str
        doc_id: str
        corpus_id: str
        entities: list = field(default_factory=list)
        relations: list = field(default_factory=list)
        facts: list = field(default_factory=list)
        text: str = ""
        entity_drop_count: int = 0
        relation_drop_count: int = 0
        evidence_drop_count: int = 0
        fact_drop_count: int = 0
        schema_lens_id: str | None = None

    @dataclass
    class _ExtractionFailureItem:
        chunk_id: str
        doc_id: str
        corpus_id: str
        model: str
        lane: int
        attempts: int
        error_type: str
        error_message: str

    @dataclass
    class _ExtractionBatchReport:
        results: list
        failures: list
        metrics: dict

    fake_ghost_b.EntityItem = _EntityItem
    fake_ghost_b.RelationItem = _RelationItem
    fake_ghost_b.FactItem = _FactItem
    fake_ghost_b.ExtractionResult = _ExtractionResult
    fake_ghost_b.ExtractionFailureItem = _ExtractionFailureItem
    fake_ghost_b.ExtractionBatchReport = _ExtractionBatchReport
    monkeypatch.setitem(sys.modules, "services.ghost_b", fake_ghost_b)

    raw = [
        {
            "schema_version": ghost_b_local.SCHEMA_VERSION,
            "chunk_id": "c-ok",
            "doc_id": "d",
            "corpus_id": "k",
            "entities": [],
            "relations": [],
            "facts": [],
            "text": "ok",
        }
    ]
    failures = [
        {
            "chunk_id": "c-fail",
            "doc_id": "d",
            "corpus_id": "k",
            "model": "http://sidecar.local",
            "lane": 0,
            "attempts": 1,
            "error_type": "ReadTimeout",
            "error_message": "slice timed out",
        }
    ]

    async def _fake_report(*_args, **_kwargs):
        return ghost_b_local._SidecarReport(
            raw=raw,
            failures=failures,
            metrics={
                "requested_chunks": 2,
                "extracted_chunks": 1,
                "failed_chunks": 1,
                "success_rate": 0.5,
            },
        )

    monkeypatch.setattr(ghost_b_local, "EXTRACT_MODE", "http")
    monkeypatch.setattr(ghost_b_local, "_extract_via_sidecar_report", _fake_report)

    report = asyncio.run(
        ghost_b_local.extract_entities(
            [
                {"chunk_id": "c-ok", "doc_id": "d", "corpus_id": "k", "text": "ok"},
                {"chunk_id": "c-fail", "doc_id": "d", "corpus_id": "k", "text": "bad"},
            ],
            return_report=True,
        )
    )

    assert [r.chunk_id for r in report.results] == ["c-ok"]
    assert [f.chunk_id for f in report.failures] == ["c-fail"]
    assert report.failures[0].error_type == "ReadTimeout"
    assert report.metrics["failed_chunks"] == 1
