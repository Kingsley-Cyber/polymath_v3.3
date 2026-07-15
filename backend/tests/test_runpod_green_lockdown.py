from __future__ import annotations

import copy
import json

import httpx
import pytest

from scripts.run_runpod_green_lockdown import (
    CONTROL_TIMEOUT_SECONDS,
    REMAINING_CONTROL_NAMES,
    _persist_case_receipt,
    _submit_and_wait,
    compare_to_reference,
    invalid_requests,
    validate_refusal,
)


def _output(confidence: float = 0.5) -> dict:
    return {
        "contract_version": "polymath.runpod_local_extraction.v1",
        "batch_id": "b",
        "metrics": {"chunks": 1, "duration_seconds": 1.2},
        "runtime_identity": {
            "platform": "different-by-device",
            "python": "3.11.15",
            "source_closure": {
                "closure_sha256": (
                    "2e47c86fe41db25b3a0fc81408ff775a829be59871a5479a1bfd1a4dad0e8010"
                )
            },
            "determinism": {
                "profile": "polymath.torch_cuda_deterministic.v1",
                "torch_deterministic_algorithms": True,
                "torch_deterministic_warn_only": False,
                "torch_float32_matmul_precision": "highest",
                "cuda_matmul_allow_tf32": False,
                "cudnn_allow_tf32": False,
                "cudnn_benchmark": False,
                "cudnn_deterministic": True,
                "cuda_matmul_allow_fp16_reduced_precision_reduction": False,
                "cuda_matmul_allow_bf16_reduced_precision_reduction": False,
                "torch_num_threads": 1,
                "torch_num_interop_threads": 1,
            },
        },
        "results": [
            {
                "document_id": "d",
                "child_id": "c",
                "source_version_id": "s",
                "extraction": {
                    "entities": [
                        {
                            "mention_id": "m",
                            "text": "x",
                            "entity_type": "PROCESS",
                            "start_char": 0,
                            "end_char": 1,
                            "canonical_label": "x",
                            "confidence": confidence,
                        }
                    ],
                    "predicates": [],
                    "relations": [],
                    "sentence_ids": [],
                    "unresolved_spans": [],
                    "document_id": "d",
                    "child_id": "c",
                    "schema_version": "local_extraction.v1",
                },
                "temporal_captures": [],
                "temporal_captures_truncated": False,
            }
        ],
    }


def test_compare_accepts_only_preregistered_device_differences() -> None:
    receipt = compare_to_reference(_output(), _output(0.500009), 1e-5)
    assert receipt["exact_semantic_mismatches"] == 0
    assert receipt["confidence_max_abs_delta"] == pytest.approx(9e-6)


def test_compare_rejects_semantic_or_confidence_drift() -> None:
    changed = _output()
    changed["results"][0]["extraction"]["entities"][0]["text"] = "y"
    with pytest.raises(AssertionError, match="semantic mismatch"):
        compare_to_reference(_output(), changed, 1e-5)
    with pytest.raises(AssertionError, match="exceeds tolerance"):
        compare_to_reference(_output(), _output(0.50002), 1e-5)

    unattested = _output()
    unattested["runtime_identity"].pop("determinism")
    with pytest.raises(AssertionError, match="attestation is missing"):
        compare_to_reference(_output(), unattested, 1e-5)

    wrong_source = _output()
    wrong_source["runtime_identity"]["source_closure"]["closure_sha256"] = "0" * 64
    with pytest.raises(AssertionError, match="source closure"):
        compare_to_reference(_output(), wrong_source, 1e-5)


def test_invalid_cases_are_general_contract_mutations() -> None:
    request = {
        "contract_version": "polymath.runpod_local_extraction.v1",
        "tasks": [{"source_version_id": "s"}],
    }
    cases = invalid_requests(copy.deepcopy(request))
    assert cases["malformed_contract"]["contract_version"] == "polymath.invalid"
    assert cases["out_of_registry_label_injection"]["entity_types"] == [
        "OUT_OF_REGISTRY_LABEL"
    ]
    assert cases["bad_source_identity"]["tasks"][0]["source_version_id"] == ""
    assert request == {
        "contract_version": "polymath.runpod_local_extraction.v1",
        "tasks": [{"source_version_id": "s"}],
    }
    assert REMAINING_CONTROL_NAMES == (
        "out_of_registry_label_injection",
        "bad_source_identity",
    )
    assert CONTROL_TIMEOUT_SECONDS == 900


def test_refusal_requires_named_fail_closed_code() -> None:
    assert (
        validate_refusal(
            "bad", {"success": False, "error_code": "extraction_contract_rejected"}
        )["success"]
        is False
    )
    with pytest.raises(AssertionError, match="did not fail closed"):
        validate_refusal("bad", {"success": True})


@pytest.mark.asyncio
async def test_job_id_is_fsynced_before_terminal_failure(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.method == "POST" and request.url.path.endswith("/run"):
            return httpx.Response(200, json={"id": "provider-job-1"})
        return httpx.Response(
            200,
            json={
                "id": "provider-job-1",
                "status": "FAILED",
                "delayTime": 123,
            },
        )

    journal = tmp_path / "jobs.jsonl"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="job_id=provider-job-1"):
            await _submit_and_wait(
                client,
                endpoint_id="endpoint-1",
                api_key="unit-secret",
                payload={"contract_version": "unit"},
                timeout_seconds=10,
                case_name="valid_same_chunk",
                job_journal=journal,
            )

    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert calls == 2
    assert [row["event"] for row in rows] == [
        "journal_preflight",
        "submitted",
        "terminal",
    ]
    assert {row["job_id"] for row in rows[1:]} == {"provider-job-1"}
    assert rows[2]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_unwritable_journal_refuses_before_provider_submission(
    tmp_path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": "must-not-submit"})

    journal_directory = tmp_path / "journal-is-a-directory"
    journal_directory.mkdir()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IsADirectoryError):
            await _submit_and_wait(
                client,
                endpoint_id="endpoint-1",
                api_key="unit-secret",
                payload={"contract_version": "unit"},
                timeout_seconds=10,
                case_name="valid_same_chunk",
                job_journal=journal_directory,
            )
    assert calls == 0


@pytest.mark.asyncio
async def test_failed_control_requires_failed_status_and_named_refusal(
    tmp_path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/run"):
            return httpx.Response(200, json={"id": "control-job-1"})
        return httpx.Response(
            200,
            json={
                "id": "control-job-1",
                "status": "FAILED",
                "output": {
                    "success": False,
                    "error_code": "extraction_contract_rejected",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output, receipt = await _submit_and_wait(
            client,
            endpoint_id="endpoint-1",
            api_key="unit-secret",
            payload={"contract_version": "invalid"},
            timeout_seconds=10,
            case_name="malformed_contract",
            job_journal=tmp_path / "jobs.jsonl",
            expected_terminal_status="FAILED",
        )

    assert validate_refusal("malformed_contract", output)["success"] is False
    assert receipt["provider_status"] == "FAILED"


@pytest.mark.asyncio
async def test_control_warmth_is_fsynced_before_submission(tmp_path) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "GET" and request.url.path.endswith("/health"):
            return httpx.Response(
                200,
                json={
                    "workers": {
                        "idle": 0,
                        "initializing": 0,
                        "ready": 0,
                        "running": 0,
                        "throttled": 1,
                        "unhealthy": 0,
                    },
                    "jobs": {"inQueue": 0, "inProgress": 0},
                },
            )
        if request.method == "POST" and request.url.path.endswith("/run"):
            return httpx.Response(200, json={"id": "control-job-warmth"})
        return httpx.Response(
            200,
            json={
                "id": "control-job-warmth",
                "status": "FAILED",
                "output": {
                    "success": False,
                    "error_code": "extraction_contract_rejected",
                },
            },
        )

    journal = tmp_path / "jobs.jsonl"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _submit_and_wait(
            client,
            endpoint_id="endpoint-1",
            api_key="unit-secret",
            payload={"contract_version": "invalid"},
            timeout_seconds=CONTROL_TIMEOUT_SECONDS,
            case_name="out_of_registry_label_injection",
            job_journal=journal,
            expected_terminal_status="FAILED",
            journal_warmth=True,
        )

    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert paths == [
        "/v2/endpoint-1/health",
        "/v2/endpoint-1/run",
        "/v2/endpoint-1/status/control-job-warmth",
    ]
    assert [row["event"] for row in rows] == [
        "journal_preflight",
        "warmth_probe",
        "submitted",
        "terminal",
    ]
    assert rows[1]["workers"]["throttled"] == 1
    assert rows[1]["jobs"] == {"inProgress": 0, "inQueue": 0}


@pytest.mark.asyncio
async def test_failed_control_rejects_missing_or_wrong_refusal_output(
    tmp_path,
) -> None:
    output_by_job = {
        "missing-output": None,
        "wrong-output": {"success": False, "error_code": "wrong_code"},
    }

    async def submit(job_id: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/run"):
                return httpx.Response(200, json={"id": job_id})
            return httpx.Response(
                200,
                json={
                    "id": job_id,
                    "status": "FAILED",
                    "output": output_by_job[job_id],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _submit_and_wait(
                client,
                endpoint_id="endpoint-1",
                api_key="unit-secret",
                payload={"contract_version": "invalid"},
                timeout_seconds=10,
                case_name=job_id,
                job_journal=tmp_path / f"{job_id}.jsonl",
                expected_terminal_status="FAILED",
            )

    with pytest.raises(RuntimeError, match="non-object output"):
        await submit("missing-output")
    wrong, _ = await submit("wrong-output")
    with pytest.raises(AssertionError, match="wrong refusal code"):
        validate_refusal("wrong-output", wrong)


def test_case_receipt_is_parseable_and_complete(tmp_path) -> None:
    output = {"success": False, "error_code": "extraction_contract_rejected"}
    job = {"job_id": "control-job-1", "provider_status": "FAILED"}
    path = _persist_case_receipt(
        tmp_path,
        case_name="bad_source_identity",
        output=output,
        job=job,
    )
    assert json.loads(path.read_text()) == {
        "case": "bad_source_identity",
        "job": job,
        "output": output,
    }
