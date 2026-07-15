from __future__ import annotations

import copy
import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from models.extraction_registry import extraction_registry_hashes
from models.schemas import (
    IngestionConfig,
    RunpodFlashAccount,
    RunpodFlashExtractionSettings,
    RunpodLocalExtractionRoute,
)
from services import runpod_flash_extraction, runpod_local_extraction
from services.ghost_b import ExtractionTask
from services.ingestion import worker
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
)


def _config() -> RunpodFlashExtractionSettings:
    return RunpodFlashExtractionSettings(
        enabled=True,
        request_batch_size=8,
        request_concurrency=2,
        timeout_seconds=30,
        poll_interval_seconds=0.25,
    )


def _account(name: str, endpoint: str) -> RunpodFlashAccount:
    return RunpodFlashAccount(
        name=name,
        endpoint_id=endpoint,
        request_concurrency=2,
    )


def _task(
    text: str = "Prices increase.",
    *,
    suffix: str = "test",
) -> ExtractionTask:
    return ExtractionTask(
        chunk_id=f"child:{suffix}",
        doc_id=f"doc:{suffix}",
        corpus_id="corpus:test",
        text=text,
        metadata={"source_version_id": f"srcv:{suffix}"},
    )


def _runtime_identity() -> dict:
    return {
        "python": "3.11.15",
        "platform": "fixture",
        "distributions": dict(runpod_local_extraction.EXPECTED_DISTRIBUTIONS),
        "spacy_model": runpod_local_extraction.SPACY_MODEL,
        "spacy_model_version": runpod_local_extraction.SPACY_MODEL_VERSION,
        "parser_version": runpod_local_extraction.PARSER_VERSION,
        "gliner_model_id": runpod_local_extraction.GLINER_MODEL_ID,
        "gliner_model_revision": runpod_local_extraction.GLINER_MODEL_REVISION,
        "asset_contract": dict(runpod_local_extraction.EXPECTED_ASSET_CONTRACT),
        "registry_namespace_hashes": extraction_registry_hashes(),
        "source_closure": {
            "closure_sha256": (runpod_local_extraction.EXPECTED_SOURCE_CLOSURE_SHA256)
        },
        "determinism": dict(runpod_local_extraction.EXPECTED_DETERMINISM),
        "model_snapshot": {
            "config_sha256": runpod_local_extraction.EXPECTED_ASSET_CONTRACT[
                "gliner_config_sha256"
            ],
            "weights_sha256": runpod_local_extraction.EXPECTED_ASSET_CONTRACT[
                "gliner_weights_sha256"
            ],
        },
    }


def _remote_result(
    text: str = "Prices increase.",
    *,
    document_id: str = "doc:test",
    child_id: str = "child:test",
    source_version_id: str = "srcv:test",
) -> dict:
    bundle = build_spacy_observation_bundle(
        text=text,
        nlp=runpod_local_extraction._load_nlp(),
        source_version_id=source_version_id,
        hierarchy_node_id=child_id,
        parser_id=runpod_local_extraction.SPACY_MODEL,
        parser_version=runpod_local_extraction.PARSER_VERSION,
    )
    compiled = compile_local_extraction_v1(
        bundle,
        document_id=document_id,
        child_id=child_id,
    )
    extraction = compiled.extraction.model_dump(mode="json")
    extraction["entities"] = [
        {
            "mention_id": "mention:test",
            "text": "Prices",
            "entity_type": "CONCEPT",
            "start_char": 0,
            "end_char": 6,
            "canonical_label": "prices",
            "confidence": 0.9,
        }
    ]
    return {
        "document_id": document_id,
        "child_id": child_id,
        "source_version_id": source_version_id,
        "extraction": extraction,
        "temporal_captures": [],
        "temporal_captures_truncated": False,
        "mention_selection_counts": {"accepted": 1},
        "compilation_receipt": compiled.receipt(),
    }


def _remote_output(request: dict, text: str = "Prices increase.") -> dict:
    results = [
        _remote_result(
            str(row["text"]),
            document_id=str(row["document_id"]),
            child_id=str(row["child_id"]),
            source_version_id=str(row["source_version_id"]),
        )
        for row in request["tasks"]
    ]
    return {
        "contract_version": runpod_local_extraction.CONTRACT_VERSION,
        "batch_id": request["batch_id"],
        "results": results,
        "runtime_identity": _runtime_identity(),
        "metrics": {
            "chunks": len(results),
            "windows": 1,
            "entities": 1,
            "predicates": 1,
            "relations": 0,
            "duration_seconds": 0.1,
        },
        "_runpod_job": {
            "job_id": "job:test",
            "delay_time_ms": 1,
            "execution_time_ms": 2,
        },
    }


def test_default_config_preserves_legacy_v2_v3_adapter() -> None:
    config = IngestionConfig(extraction_engine="runpod_flash")
    assert config.runpod_wire_contract == "legacy_v2_v3"
    assert (
        worker._runpod_extractor_for_config(config)
        is runpod_flash_extraction.extract_entities
    )
    local = IngestionConfig(
        extraction_engine="runpod_flash",
        runpod_wire_contract="local_extraction_v1",
        runpod_endpoint_id_override="greenendpoint",
        runpod_account_name_override="primary",
    )
    assert (
        worker._runpod_extractor_for_config(local)
        is runpod_local_extraction.extract_entities
    )

    burst = IngestionConfig(
        extraction_engine="runpod_flash",
        runpod_wire_contract="local_extraction_v1",
        runpod_local_extraction_routes=[
            {"account_name": "primary", "endpoint_id": "greenprimary"},
            {"account_name": "secondary", "endpoint_id": "greensecondary"},
        ],
    )
    assert burst.runpod_endpoint_id_override is None
    assert [row.account_name for row in burst.runpod_local_extraction_routes] == [
        "primary",
        "secondary",
    ]


def test_local_artifacts_round_trip_through_resume_staging() -> None:
    row = {
        "schema_version": "polymath.extract.local_extraction.v1",
        "chunk_id": "child:test",
        "doc_id": "doc:test",
        "corpus_id": "corpus:test",
        "entities": [],
        "relations": [],
        "facts": [],
        "source_version_id": "srcv:test",
        "local_extraction": {"schema_version": "local_extraction.v1"},
        "claim_compilation": {"schema_version": "claim_compilation.v1"},
    }
    result = worker._rehydrate_ghost_b_staging([row])[0]
    assert result.source_version_id == "srcv:test"
    assert result.local_extraction == {"schema_version": "local_extraction.v1"}
    assert result.claim_compilation == {"schema_version": "claim_compilation.v1"}


@pytest.mark.parametrize(
    "values",
    [
        {"runpod_endpoint_id_override": "greenendpoint"},
        {"runpod_account_name_override": "primary"},
        {"runpod_wire_contract": "local_extraction_v1"},
        {
            "extraction_engine": "runpod_flash",
            "runpod_wire_contract": "local_extraction_v1",
            "runpod_endpoint_id_override": "greenendpoint",
        },
        {
            "extraction_engine": "runpod_flash",
            "runpod_wire_contract": "local_extraction_v1",
            "runpod_local_extraction_routes": [
                {"account_name": "primary", "endpoint_id": "greenprimary"}
            ],
        },
        {
            "extraction_engine": "runpod_flash",
            "runpod_wire_contract": "local_extraction_v1",
            "runpod_endpoint_id_override": "greenendpoint",
            "runpod_account_name_override": "primary",
            "runpod_local_extraction_routes": [
                {"account_name": "primary", "endpoint_id": "greenprimary"},
                {"account_name": "secondary", "endpoint_id": "greensecondary"},
            ],
        },
        {
            "extraction_engine": "runpod_flash",
            "runpod_wire_contract": "local_extraction_v1",
            "runpod_local_extraction_routes": [
                {"account_name": "primary", "endpoint_id": "greenprimary"},
                {"account_name": "primary", "endpoint_id": "greensecondary"},
            ],
        },
    ],
)
def test_corpus_contract_override_fails_closed(values: dict) -> None:
    with pytest.raises(ValidationError):
        IngestionConfig(**values)


@pytest.mark.asyncio
async def test_named_account_pinned_endpoint_and_local_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []
    settings_reads: list[str] = []

    async def get_config():
        settings_reads.append("config")
        return _config(), None

    async def get_accounts():
        settings_reads.append("accounts")
        return [
            (_account("primary", "blue-primary"), "key-primary"),
            (_account("secondary", "blue-secondary"), "key-secondary"),
        ]

    async def forbidden_write(*_args, **_kwargs):
        raise AssertionError("adapter attempted to mutate Settings")

    async def fake_submit(
        _client,
        *,
        endpoint_id,
        api_key,
        request,
        **_kwargs,
    ):
        assert api_key == "key-primary"
        calls.append((endpoint_id, copy.deepcopy(request)))
        return _remote_output(request)

    from services.settings import settings_service

    monkeypatch.setattr(settings_service, "get_system_runpod_flash", get_config)
    monkeypatch.setattr(
        settings_service,
        "get_system_runpod_flash_accounts",
        get_accounts,
    )
    monkeypatch.setattr(
        settings_service,
        "update_ingestion_settings",
        forbidden_write,
    )
    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", fake_submit)

    report = await runpod_local_extraction.extract_entities(
        [_task()],
        endpoint_id="greenendpoint",
        account_name="primary",
        user_id="corpus-owner-does-not-select-settings-scope",
        return_report=True,
    )

    assert settings_reads == ["config", "accounts"]
    assert [endpoint for endpoint, _ in calls] == ["greenendpoint"]
    request = calls[0][1]
    assert request["contract_version"] == ("polymath.runpod_local_extraction.v1")
    assert set(request["tasks"][0]) == {
        "document_id",
        "child_id",
        "source_version_id",
        "text",
    }
    assert report.failures == []
    assert report.metrics["failed_chunks"] == 0
    result = report.results[0]
    assert result.source_version_id == "srcv:test"
    assert result.local_extraction["schema_version"] == "local_extraction.v1"
    assert result.claim_compilation["schema_version"] == "claim_compilation.v1"
    assert result.claim_compilation["claims"]
    assert result.relations == []
    assert result.provider_card["endpoint"] == "greenendpoint"
    assert result.provider_card["account"] == "primary"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["source_closure", "entity_span"])
async def test_remote_identity_or_span_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    async def fake_submit(_client, *, request, **_kwargs):
        output = _remote_output(request)
        if mutation == "source_closure":
            output["runtime_identity"]["source_closure"]["closure_sha256"] = "0" * 64
        else:
            output["results"][0]["extraction"]["entities"][0]["text"] = "Wrong!"
        return output

    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", fake_submit)
    with pytest.raises(RuntimeError, match="LocalExtractionV1"):
        await runpod_local_extraction.extract_entities(
            [_task()],
            endpoint_id="greenendpoint",
            account_name="primary",
            runpod_config=_config(),
            accounts=[(_account("primary", "blue-primary"), "key-primary")],
            return_report=True,
        )


@pytest.mark.asyncio
async def test_normalization_empty_noise_is_counted_and_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(_client, *, request, **_kwargs):
        output = _remote_output(request)
        output["results"][0]["extraction"]["entities"] = [
            {
                "mention_id": "mention:punctuation-noise",
                "text": ".",
                "entity_type": "CONCEPT",
                "start_char": 15,
                "end_char": 16,
                "canonical_label": "",
                "confidence": 0.9,
            }
        ]
        output["results"][0]["mention_selection_counts"] = {
            "raw": 1,
            "selected": 1,
        }
        return output

    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", fake_submit)
    report = await runpod_local_extraction.extract_entities(
        [_task()],
        endpoint_id="greenendpoint",
        account_name="primary",
        runpod_config=_config(),
        accounts=[(_account("primary", "blue-primary"), "key-primary")],
        return_report=True,
    )

    assert report.results[0].entities == []
    assert report.results[0].local_extraction["entities"] == []
    assert report.results[0].provider_card["mention_selection_counts"] == {
        "empty_canonical_label": 1,
        "raw": 1,
        "selected": 0,
    }
    assert report.metrics["mention_exclusion_counts"] == {
        "empty_canonical_label": 1
    }


@pytest.mark.asyncio
async def test_empty_canonical_non_noise_surface_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(_client, *, request, **_kwargs):
        output = _remote_output(request)
        output["results"][0]["extraction"]["entities"][0]["canonical_label"] = ""
        return output

    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", fake_submit)
    with pytest.raises(RuntimeError, match="non-noise surface"):
        await runpod_local_extraction.extract_entities(
            [_task()],
            endpoint_id="greenendpoint",
            account_name="primary",
            runpod_config=_config(),
            accounts=[(_account("primary", "blue-primary"), "key-primary")],
            return_report=True,
        )


@pytest.mark.asyncio
async def test_retry_reuses_retained_completed_output_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    task = _task()
    task_row = runpod_local_extraction._task_dict(task)
    request = runpod_local_extraction._request([task_row])
    journal_path = tmp_path / (
        "corpus-" + hashlib.sha256(task.corpus_id.encode()).hexdigest() + ".jsonl"
    )
    journal_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "event": "submitted",
                    "batch_id": request["batch_id"],
                    "job_id": "job-retained",
                    "endpoint_id": "greenendpoint",
                    "account_name": "primary",
                },
                {
                    "event": "terminal",
                    "status": "COMPLETED",
                    "batch_id": request["batch_id"],
                    "job_id": "job-retained",
                    "endpoint_id": "greenendpoint",
                    "account_name": "primary",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    dispatched = False

    async def forbidden_submit(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("retry created a new provider job")

    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", forbidden_submit)
    retained_output = _remote_output(request)
    retained_output.pop("_runpod_job")

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url.path.endswith("/status/job-retained")
        return httpx.Response(
            200,
            json={
                "status": "COMPLETED",
                "output": retained_output,
                "delayTime": 11,
                "executionTime": 22,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await runpod_local_extraction.extract_entities(
            [task],
            endpoint_id="greenendpoint",
            account_name="primary",
            runpod_config=_config(),
            accounts=[(_account("primary", "blue-primary"), "key-primary")],
            http_client=client,
            job_journal_dir=str(tmp_path),
            return_report=True,
        )

    assert dispatched is False
    assert report.metrics["reused_request_batches"] == 1
    assert report.metrics["new_request_batches"] == 0
    assert report.metrics["remote_jobs"][0]["job_id"] == "job-retained"
    assert report.metrics["remote_jobs"][0]["reused"] is True
    rows = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert rows[-1]["event"] == "reused_terminal_output"


@pytest.mark.asyncio
async def test_partial_reusable_closure_refuses_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    tasks = [_task(suffix="0"), _task(suffix="1")]
    config = _config().model_copy(update={"request_batch_size": 1})
    first_request = runpod_local_extraction._request(
        [runpod_local_extraction._task_dict(tasks[0])]
    )
    journal_path = tmp_path / (
        "corpus-" + hashlib.sha256(tasks[0].corpus_id.encode()).hexdigest() + ".jsonl"
    )
    journal_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "event": "submitted",
                    "batch_id": first_request["batch_id"],
                    "job_id": "job-retained",
                    "endpoint_id": "greenendpoint",
                    "account_name": "primary",
                },
                {
                    "event": "terminal",
                    "status": "COMPLETED",
                    "batch_id": first_request["batch_id"],
                    "job_id": "job-retained",
                    "endpoint_id": "greenendpoint",
                    "account_name": "primary",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    dispatched = False

    async def forbidden_submit(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("partial replay reached provider dispatch")

    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", forbidden_submit)
    with pytest.raises(RuntimeError, match="closure is partial"):
        await runpod_local_extraction.extract_entities(
            tasks,
            endpoint_id="greenendpoint",
            account_name="primary",
            runpod_config=config,
            accounts=[(_account("primary", "blue-primary"), "key-primary")],
            job_journal_dir=str(tmp_path),
            return_report=True,
        )
    assert dispatched is False


@pytest.mark.asyncio
async def test_missing_source_version_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched = False

    async def fake_submit(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("missing identity reached RunPod")

    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", fake_submit)
    task = _task()
    task.metadata = {}
    with pytest.raises(ValueError, match="identity is incomplete"):
        await runpod_local_extraction.extract_entities(
            [task],
            endpoint_id="greenendpoint",
            account_name="primary",
            runpod_config=_config(),
            accounts=[(_account("primary", "blue-primary"), "key-primary")],
        )
    assert dispatched is False


@pytest.mark.asyncio
async def test_explicit_routes_split_deterministically_without_failover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: dict[str, tuple[str, str]] = {}

    async def fake_submit(
        _client,
        *,
        endpoint_id,
        api_key,
        request,
        job_event_sink,
        **_kwargs,
    ):
        child_id = request["tasks"][0]["child_id"]
        calls[child_id] = (endpoint_id, api_key)
        await job_event_sink(
            {
                "event": "submitted",
                "batch_id": request["batch_id"],
                "job_id": f"job-{child_id}",
            }
        )
        output = _remote_output(request)
        output["_runpod_job"]["job_id"] = f"job-{child_id}"
        return output

    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", fake_submit)
    config = _config().model_copy(
        update={"request_batch_size": 1, "request_concurrency": 2}
    )
    tasks = [_task(suffix=str(index)) for index in range(4)]
    report = await runpod_local_extraction.extract_entities(
        tasks,
        routes=[
            RunpodLocalExtractionRoute(
                account_name="primary",
                endpoint_id="greenprimary",
            ),
            RunpodLocalExtractionRoute(
                account_name="secondary",
                endpoint_id="greensecondary",
            ),
        ],
        runpod_config=config,
        accounts=[
            (_account("primary", "blueprimary"), "key-primary"),
            (_account("secondary", "bluesecondary"), "key-secondary"),
        ],
        job_journal_dir=str(tmp_path),
        return_report=True,
    )

    assert calls == {
        "child:0": ("greenprimary", "key-primary"),
        "child:1": ("greensecondary", "key-secondary"),
        "child:2": ("greenprimary", "key-primary"),
        "child:3": ("greensecondary", "key-secondary"),
    }
    assert report.metrics["dispatch_policy"] == (
        "explicit_pinned_routes_round_robin_no_failover"
    )
    assert [row["request_batches"] for row in report.metrics["routes"]] == [2, 2]
    assert {
        (row["account"], row["endpoint"]) for row in report.metrics["remote_jobs"]
    } == {
        ("primary", "greenprimary"),
        ("secondary", "greensecondary"),
    }
    assert [row.provider_card["account"] for row in report.results] == [
        "primary",
        "secondary",
        "primary",
        "secondary",
    ]
    journal_rows = [
        json.loads(line)
        for line in next(tmp_path.glob("corpus-*.jsonl")).read_text().splitlines()
    ]
    assert journal_rows[0]["event"] == "journal_preflight"
    assert {row["job_id"] for row in journal_rows[1:]} == {
        "job-child:0",
        "job-child:1",
        "job-child:2",
        "job-child:3",
    }
    assert all("text" not in row and "api_key" not in row for row in journal_rows)


@pytest.mark.asyncio
async def test_unwritable_journal_refuses_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    dispatched = False

    async def fake_submit(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("provider reached with unwritable journal")

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked")
    monkeypatch.setattr(runpod_local_extraction, "_submit_and_wait", fake_submit)
    with pytest.raises((FileExistsError, NotADirectoryError)):
        await runpod_local_extraction.extract_entities(
            [_task()],
            endpoint_id="greenendpoint",
            account_name="primary",
            runpod_config=_config(),
            accounts=[(_account("primary", "blueprimary"), "key-primary")],
            job_journal_dir=str(blocked),
        )
    assert dispatched is False


@pytest.mark.asyncio
async def test_transport_journals_submitted_id_before_first_poll() -> None:
    events: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/run"):
            return httpx.Response(200, json={"id": "job-immediate"})
        if "/status/" in request.url.path:
            assert [row["event"] for row in events] == ["submitted"]
            return httpx.Response(
                200,
                json={
                    "status": "COMPLETED",
                    "output": {"results": []},
                    "delayTime": 11,
                    "executionTime": 22,
                },
            )
        return httpx.Response(404)

    async def sink(event: dict) -> None:
        events.append(copy.deepcopy(event))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await runpod_flash_extraction._submit_and_wait(
            client,
            endpoint_id="greenendpoint",
            api_key="test-secret",
            request={"batch_id": "batch:test", "tasks": []},
            timeout_seconds=30,
            poll_interval_seconds=0.25,
            job_event_sink=sink,
        )

    assert [row["event"] for row in events] == ["submitted", "terminal"]
    assert events[0]["job_id"] == "job-immediate"
    assert events[1]["status"] == "COMPLETED"
    assert output["_runpod_job"] == {
        "job_id": "job-immediate",
        "delay_time_ms": 11,
        "execution_time_ms": 22,
    }
