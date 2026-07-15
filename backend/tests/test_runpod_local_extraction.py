from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from models.extraction_registry import extraction_registry_hashes
from models.schemas import (
    IngestionConfig,
    RunpodFlashAccount,
    RunpodFlashExtractionSettings,
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


def _task(text: str = "Prices increase.") -> ExtractionTask:
    return ExtractionTask(
        chunk_id="child:test",
        doc_id="doc:test",
        corpus_id="corpus:test",
        text=text,
        metadata={"source_version_id": "srcv:test"},
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


def _remote_result(text: str = "Prices increase.") -> dict:
    bundle = build_spacy_observation_bundle(
        text=text,
        nlp=runpod_local_extraction._load_nlp(),
        source_version_id="srcv:test",
        hierarchy_node_id="child:test",
        parser_id=runpod_local_extraction.SPACY_MODEL,
        parser_version=runpod_local_extraction.PARSER_VERSION,
    )
    compiled = compile_local_extraction_v1(
        bundle,
        document_id="doc:test",
        child_id="child:test",
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
        "document_id": "doc:test",
        "child_id": "child:test",
        "source_version_id": "srcv:test",
        "extraction": extraction,
        "temporal_captures": [],
        "temporal_captures_truncated": False,
        "mention_selection_counts": {"accepted": 1},
        "compilation_receipt": compiled.receipt(),
    }


def _remote_output(request: dict, text: str = "Prices increase.") -> dict:
    return {
        "contract_version": runpod_local_extraction.CONTRACT_VERSION,
        "batch_id": request["batch_id"],
        "results": [_remote_result(text)],
        "runtime_identity": _runtime_identity(),
        "metrics": {
            "chunks": 1,
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
