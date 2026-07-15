from __future__ import annotations

import copy
import json

import httpx
import pytest

from scripts.run_runpod_green_lockdown import (
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
        "runtime_identity": {"platform": "different-by-device", "python": "3.11.15"},
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
    assert [row["event"] for row in rows] == ["submitted", "terminal"]
    assert {row["job_id"] for row in rows} == {"provider-job-1"}
    assert rows[1]["status"] == "FAILED"
