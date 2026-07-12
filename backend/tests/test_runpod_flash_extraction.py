from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from models.schemas import RunpodFlashExtractionSettings
from services.ghost_b import ExtractionTask, SchemaContext
from services import runpod_flash_extraction as runpod_flash


def _config(**overrides) -> RunpodFlashExtractionSettings:
    values = {
        "enabled": True,
        "endpoint_id": "endpoint-test",
        "request_batch_size": 2,
        "request_concurrency": 2,
        "poll_interval_seconds": 0.25,
    }
    values.update(overrides)
    return RunpodFlashExtractionSettings(**values)


def _task(chunk_id: str, text: str) -> ExtractionTask:
    return ExtractionTask(
        chunk_id=chunk_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
    )


@pytest.mark.asyncio
async def test_extract_entities_validates_wire_and_adds_deterministic_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "Facial Action Coding System (FACS) is a method that represents "
        "facial muscle movements."
    )

    async def fake_submit(*_args, request, **_kwargs):
        assert request["entity_labels"] == ["Method", "Concept"]
        assert request["relation_labels"] == ["represents"]
        assert request["model_revision"] == _config().model_revision
        task = request["tasks"][0]
        return {
            "contract_version": runpod_flash.RUNPOD_CONTRACT_VERSION,
            "results": [
                {
                    "chunk_id": task["chunk_id"],
                    "entities": [
                        {
                            "canonical_name": "facial action coding system",
                            "surface_form": "Facial Action Coding System",
                            "entity_type": "Method",
                            "confidence": 0.96,
                            "char_start": 0,
                            "char_end": 27,
                        },
                        {
                            "canonical_name": "facial muscle movements",
                            "surface_form": "facial muscle movements",
                            "entity_type": "Concept",
                            "confidence": 0.91,
                            "char_start": text.index("facial muscle movements"),
                            "char_end": text.index("facial muscle movements")
                            + len("facial muscle movements"),
                        },
                    ],
                    "relations": [
                        {
                            "subject": "facial action coding system",
                            "predicate": "represents",
                            "object": "facial muscle movements",
                            "confidence": 0.88,
                            "evidence_phrase": text,
                        }
                    ],
                }
            ],
            "metrics": {
                "entities_emitted": 2,
                "relations_emitted": 1,
                "model_source": "runpod_cached_model",
            },
            "_runpod_job": {
                "job_id": "job-1",
                "execution_time_ms": 1000,
                "delay_time_ms": 50,
            },
        }

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-1", text)],
        schema=SchemaContext(
            entity_schema=["Method", "Concept"],
            relation_schema=["represents"],
            strict="soft",
        ),
        runpod_config=_config(),
        runpod_api_key="test-secret",
        return_report=True,
    )

    assert not report.failures
    assert len(report.results) == 1
    result = report.results[0]
    assert result.provider == "runpod_flash"
    assert result.output_mode == "joint_relex"
    assert result.relations[0].predicate == "represents"
    assert "FACS" in result.entities[0].query_aliases
    assert report.metrics["schema_evidence_pass_rate"] == 1.0
    assert report.metrics["estimated_compute_cost_usd"] > 0
    assert report.metrics["remote"]["batches"] == 1
    assert report.metrics["remote"]["model_sources"] == ["runpod_cached_model"]
    assert "runpod_job" not in report.metrics["remote"]


@pytest.mark.asyncio
async def test_missing_remote_artifact_is_a_durable_chunk_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(*_args, request, **_kwargs):
        first = request["tasks"][0]
        return {
            "contract_version": runpod_flash.RUNPOD_CONTRACT_VERSION,
            "results": [{"chunk_id": first["chunk_id"], "entities": [], "relations": []}],
            "metrics": {"entities_emitted": 0, "relations_emitted": 0},
            "_runpod_job": {},
        }

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-1", "One."), _task("chunk-2", "Two.")],
        runpod_config=_config(),
        runpod_api_key="test-secret",
        return_report=True,
    )

    assert [item.chunk_id for item in report.results] == ["chunk-1"]
    assert len(report.failures) == 1
    assert report.failures[0].chunk_id == "chunk-2"
    assert report.failures[0].error_type == "missing_remote_artifact"


@pytest.mark.asyncio
async def test_stale_worker_contract_is_rejected_as_a_durable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(*_args, request, **_kwargs):
        return {
            "contract_version": "polymath.runpod_gliner_relex.v1",
            "results": [
                {
                    "chunk_id": request["tasks"][0]["chunk_id"],
                    "entities": [],
                    "relations": [],
                }
            ],
            "metrics": {},
            "_runpod_job": {},
        }

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-stale", "A source-backed sentence.")],
        runpod_config=_config(),
        runpod_api_key="test-secret",
        return_report=True,
    )

    assert not report.results
    assert len(report.failures) == 1
    assert report.failures[0].error_type == "RuntimeError"
    assert "contract revision mismatch" in report.failures[0].error_message


@pytest.mark.asyncio
async def test_runpod_configuration_fails_closed_before_network() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        await runpod_flash.extract_entities(
            [_task("chunk-1", "Text")],
            runpod_config=_config(enabled=False),
            runpod_api_key="test-secret",
        )
    with pytest.raises(RuntimeError, match="endpoint_id"):
        await runpod_flash.extract_entities(
            [_task("chunk-1", "Text")],
            runpod_config=_config(endpoint_id=""),
            runpod_api_key="test-secret",
        )
    with pytest.raises(RuntimeError, match="API key"):
        await runpod_flash.extract_entities(
            [_task("chunk-1", "Text")],
            runpod_config=_config(),
            runpod_api_key="",
        )


def test_runpod_worker_bounds_are_validated() -> None:
    with pytest.raises(ValidationError, match="min_workers"):
        RunpodFlashExtractionSettings(min_workers=4, max_workers=2)


def test_completed_worker_error_is_not_misclassified_as_missing_artifacts() -> None:
    with pytest.raises(RuntimeError, match="worker rejected"):
        runpod_flash._extract_output(
            {"output": {"success": False, "error": "bad payload"}}
        )


@pytest.mark.asyncio
async def test_submit_and_poll_retries_transient_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"submit": 0, "status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/run"):
            calls["submit"] += 1
            assert json.loads(request.content) == {
                "input": {"payload": {"tasks": []}}
            }
            if calls["submit"] == 1:
                return httpx.Response(429, headers={"retry-after": "0.25"})
            return httpx.Response(200, json={"id": "job-test"})
        if "/status/" in request.url.path:
            calls["status"] += 1
            if calls["status"] == 1:
                return httpx.Response(503)
            if calls["status"] == 2:
                return httpx.Response(200, json={"status": "IN_QUEUE"})
            return httpx.Response(
                200,
                json={
                    "status": "COMPLETED",
                    "output": {"results": []},
                    "delayTime": 12,
                    "executionTime": 34,
                },
            )
        return httpx.Response(404)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runpod_flash.asyncio, "sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await runpod_flash._submit_and_wait(
            client,
            endpoint_id="endpoint-test",
            api_key="test-secret",
            request={"tasks": []},
            timeout_seconds=30,
            poll_interval_seconds=0.25,
        )

    assert calls == {"submit": 2, "status": 3}
    assert output["results"] == []
    assert output["_runpod_job"]["execution_time_ms"] == 34
