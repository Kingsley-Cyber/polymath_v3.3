"""Regression: relex_local must microbatch sidecar POSTs at the wire max.

Fundamentals of Data Engineering (q9) has 1695 children; the sidecar
ExtractRequest caps tasks at 512. A single POST of the whole document
returned HTTP 422 and marked the item failed — that is a client contract
bug, not a source defect.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.ingestion.relex_local import RELEX_SIDECAR_TASK_MAX, _extract_predictions


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def post(self, path: str, json=None, timeout=None):  # noqa: A002
        assert path == "/extract"
        tasks = list((json or {}).get("tasks") or [])
        assert len(tasks) <= RELEX_SIDECAR_TASK_MAX
        ids = [t["chunk_id"] for t in tasks]
        self.calls.append(ids)
        return _FakeResp(
            200,
            {
                "results": [
                    {"chunk_id": cid, "entities": [], "relations": [], "raw_pair_scores": []}
                    for cid in ids
                ],
                "failures": [],
            },
        )


@pytest.mark.asyncio
async def test_extract_predictions_microbatches_above_sidecar_max() -> None:
    n = RELEX_SIDECAR_TASK_MAX * 3 + 7  # 1543 — under Fundamentals scale
    tasks = [SimpleNamespace(chunk_id=f"c{i}", text=f"chunk {i}") for i in range(n)]
    client = _FakeClient()
    preds, fails = await _extract_predictions(client, tasks)
    assert fails == {}
    assert len(preds) == n
    assert len(client.calls) == 4
    assert [len(c) for c in client.calls] == [
        RELEX_SIDECAR_TASK_MAX,
        RELEX_SIDECAR_TASK_MAX,
        RELEX_SIDECAR_TASK_MAX,
        7,
    ]


@pytest.mark.asyncio
async def test_extract_predictions_single_post_when_under_max() -> None:
    tasks = [SimpleNamespace(chunk_id="a", text="one"), SimpleNamespace(chunk_id="b", text="two")]
    client = _FakeClient()
    preds, fails = await _extract_predictions(client, tasks)
    assert fails == {}
    assert set(preds) == {"a", "b"}
    assert len(client.calls) == 1
    assert len(client.calls[0]) == 2


@pytest.mark.asyncio
async def test_extract_entities_pipelined_keeps_task_order_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice pipelining must not change outputs: results stay in task order,
    per-chunk sidecar failures still become ExtractionFailureItem rows."""
    from services.ingestion import relex_local

    n = RELEX_SIDECAR_TASK_MAX + 5  # force 2 slices
    tasks = [
        SimpleNamespace(
            chunk_id=f"c{i}", text=f"chunk {i}", doc_id="d1", corpus_id="k1",
        )
        for i in range(n)
    ]
    failed_ids = {"c3", f"c{RELEX_SIDECAR_TASK_MAX + 1}"}  # one per slice

    class _PipelinedFakeClient:
        async def get(self, path: str, timeout=None):
            assert path == "/health"
            return _FakeResp(200, {
                "extractor": "relex_local",
                "service_schema_version": relex_local.SERVICE_SCHEMA_VERSION,
                "ready": True,
                "model_hash_verified": True,
                "model_id": "test-model",
                "model_hash": "deadbeef",
            })

        async def post(self, path: str, json=None, timeout=None):  # noqa: A002
            rows = list((json or {}).get("tasks") or [])
            assert len(rows) <= RELEX_SIDECAR_TASK_MAX
            return _FakeResp(200, {
                "results": [
                    {"chunk_id": r["chunk_id"], "entities": [],
                     "relations": [], "raw_pair_scores": []}
                    for r in rows if r["chunk_id"] not in failed_ids
                ],
                "failures": [
                    {"chunk_id": r["chunk_id"], "error": "boom"}
                    for r in rows if r["chunk_id"] in failed_ids
                ],
            })

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        relex_local.httpx, "AsyncClient",
        lambda *a, **kw: _PipelinedFakeClient(),
    )

    def _fake_process(task, pred_row, policy, health):
        return SimpleNamespace(
            chunk_id=task.chunk_id, relations=[], entities=[],
            local_extraction={"gate_decision_counts": {}},
        )

    monkeypatch.setattr(relex_local, "_process_chunk_sync", _fake_process)
    monkeypatch.setattr(relex_local, "load_policy", lambda: None)
    monkeypatch.setattr(relex_local, "ontology_hash", lambda: "oh")
    monkeypatch.setattr(relex_local, "acceptance_policy_hash", lambda: "ah")

    report = await relex_local.extract_entities(tasks, return_report=True)

    ok_ids = [t.chunk_id for t in tasks if t.chunk_id not in failed_ids]
    assert [r.chunk_id for r in report.results] == ok_ids
    assert sorted(f.chunk_id for f in report.failures) == sorted(failed_ids)
    assert report.metrics["requested_chunks"] == n
    assert report.metrics["extracted_chunks"] == n - len(failed_ids)
    assert report.metrics["failed_chunks"] == len(failed_ids)
