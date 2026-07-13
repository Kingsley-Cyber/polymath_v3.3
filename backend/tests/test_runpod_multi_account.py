"""P2.7c multi-account RunPod routing — offline unit coverage.

Fakes only: the module-level HTTP submit/poll function is monkeypatched, so
no network or live RunPod endpoint is ever touched. Idioms follow
``tests/test_runpod_flash_extraction.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from models.schemas import RunpodFlashAccount, RunpodFlashExtractionSettings
from services import runpod_flash_extraction as runpod_flash
from services.ghost_b import ExtractionTask, SchemaContext
from services.secrets import encrypt
from services.settings import SettingsService


def _config(**overrides) -> RunpodFlashExtractionSettings:
    values = {
        "enabled": True,
        "endpoint_id": "endpoint-test",
        "request_batch_size": 1,
        "request_concurrency": 2,
        "poll_interval_seconds": 0.25,
    }
    values.update(overrides)
    return RunpodFlashExtractionSettings(**values)


def _account(name: str, *, weight: float = 1.0, concurrency: int = 4) -> RunpodFlashAccount:
    return RunpodFlashAccount(
        name=name,
        endpoint_id=f"endpoint-{name}",
        request_concurrency=concurrency,
        weight=weight,
    )


def _task(chunk_id: str, text: str = "A source-backed sentence.") -> ExtractionTask:
    return ExtractionTask(
        chunk_id=chunk_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
    )


def _schema() -> SchemaContext:
    return SchemaContext(
        entity_schema=["Concept"],
        relation_schema=["related_to"],
        strict="soft",
    )


def _success_output(request: dict) -> dict:
    return {
        "contract_version": runpod_flash.RUNPOD_CONTRACT_VERSION,
        "results": [
            {"chunk_id": task["chunk_id"], "entities": [], "relations": []}
            for task in request["tasks"]
        ],
        "metrics": {"entities_emitted": 0, "relations_emitted": 0},
        "_runpod_job": {"job_id": "job", "execution_time_ms": 10, "delay_time_ms": 1},
    }


@pytest.mark.asyncio
async def test_least_in_flight_routing_alternates_across_two_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: list[str] = []

    async def fake_submit(*_args, endpoint_id, request, **_kwargs):
        call_order.append(endpoint_id)
        await asyncio.sleep(0)
        return _success_output(request)

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task(f"chunk-{i}") for i in range(4)],
        schema=_schema(),
        runpod_config=_config(),
        accounts=[(_account("alpha"), "key-alpha"), (_account("beta"), "key-beta")],
        return_report=True,
    )

    assert not report.failures
    assert len(report.results) == 4
    # in_flight counts assigned-and-unfinished batches, so concurrent slices
    # interleave deterministically: alpha, beta, alpha, beta.
    assert call_order == [
        "endpoint-alpha",
        "endpoint-beta",
        "endpoint-alpha",
        "endpoint-beta",
    ]
    assert report.metrics["account_dispatch"] == {
        "alpha": {"batches": 2, "failures": 0, "failovers": 0},
        "beta": {"batches": 2, "failures": 0, "failovers": 0},
    }
    served = {result.provider_card["endpoint"] for result in report.results}
    assert served == {"endpoint-alpha", "endpoint-beta"}


@pytest.mark.asyncio
async def test_higher_weight_wins_in_flight_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: list[str] = []

    async def fake_submit(*_args, endpoint_id, request, **_kwargs):
        call_order.append(endpoint_id)
        return _success_output(request)

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-1")],
        schema=_schema(),
        runpod_config=_config(),
        accounts=[
            (_account("alpha", weight=1.0), "key-alpha"),
            (_account("beta", weight=2.0), "key-beta"),
        ],
        return_report=True,
    )

    assert call_order == ["endpoint-beta"]
    assert report.metrics["account_dispatch"] == {
        "alpha": {"batches": 0, "failures": 0, "failovers": 0},
        "beta": {"batches": 1, "failures": 0, "failovers": 0},
    }


@pytest.mark.asyncio
async def test_per_account_request_concurrency_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active: dict[str, int] = {}
    peak: dict[str, int] = {}

    async def fake_submit(*_args, endpoint_id, request, **_kwargs):
        active[endpoint_id] = active.get(endpoint_id, 0) + 1
        peak[endpoint_id] = max(peak.get(endpoint_id, 0), active[endpoint_id])
        await asyncio.sleep(0.01)
        active[endpoint_id] -= 1
        return _success_output(request)

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task(f"chunk-{i}") for i in range(4)],
        schema=_schema(),
        runpod_config=_config(),
        accounts=[
            (_account("alpha", concurrency=1), "key-alpha"),
            (_account("beta", concurrency=1), "key-beta"),
        ],
        return_report=True,
    )

    assert not report.failures
    assert len(report.results) == 4
    assert peak == {"endpoint-alpha": 1, "endpoint-beta": 1}
    dispatch = report.metrics["account_dispatch"]
    assert dispatch["alpha"]["batches"] + dispatch["beta"]["batches"] == 4


@pytest.mark.asyncio
async def test_failed_batch_fails_over_once_to_another_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(*_args, endpoint_id, request, **_kwargs):
        if endpoint_id == "endpoint-alpha":
            raise RuntimeError("alpha down")
        return _success_output(request)

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-1")],
        schema=_schema(),
        runpod_config=_config(),
        accounts=[(_account("alpha"), "key-alpha"), (_account("beta"), "key-beta")],
        return_report=True,
    )

    assert not report.failures
    assert len(report.results) == 1
    assert report.results[0].provider_card["endpoint"] == "endpoint-beta"
    assert report.metrics["account_dispatch"] == {
        "alpha": {"batches": 1, "failures": 1, "failovers": 0},
        "beta": {"batches": 1, "failures": 0, "failovers": 1},
    }


@pytest.mark.asyncio
async def test_failover_is_bounded_then_error_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_submit(*_args, endpoint_id, request, **_kwargs):
        calls.append(endpoint_id)
        raise RuntimeError(f"{endpoint_id} down")

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-1")],
        schema=_schema(),
        runpod_config=_config(),
        accounts=[(_account("alpha"), "key-alpha"), (_account("beta"), "key-beta")],
        return_report=True,
    )

    # Exactly two attempts: primary plus ONE requeue, then durable failure.
    assert calls == ["endpoint-alpha", "endpoint-beta"]
    assert not report.results
    assert len(report.failures) == 1
    assert report.failures[0].error_type == "RuntimeError"
    assert "endpoint-beta down" in report.failures[0].error_message
    assert report.metrics["account_dispatch"] == {
        "alpha": {"batches": 1, "failures": 1, "failovers": 0},
        "beta": {"batches": 1, "failures": 1, "failovers": 1},
    }


@pytest.mark.asyncio
async def test_legacy_single_account_params_keep_metrics_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(*_args, endpoint_id, request, **_kwargs):
        assert endpoint_id == "endpoint-test"
        return _success_output(request)

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-1")],
        schema=_schema(),
        runpod_config=_config(),
        runpod_api_key="test-secret",
        return_report=True,
    )

    assert not report.failures
    assert len(report.results) == 1
    assert report.results[0].provider_card["endpoint"] == "endpoint-test"
    # Legacy call path emits no account_dispatch: diagnostics byte-identical.
    assert "account_dispatch" not in report.metrics


@pytest.mark.asyncio
async def test_disabled_and_keyless_accounts_fail_closed_before_network() -> None:
    with pytest.raises(RuntimeError, match="enabled"):
        await runpod_flash.extract_entities(
            [_task("chunk-1")],
            runpod_config=_config(),
            accounts=[
                (
                    RunpodFlashAccount(
                        name="alpha", endpoint_id="endpoint-alpha", enabled=False
                    ),
                    "key-alpha",
                )
            ],
        )
    with pytest.raises(RuntimeError, match="API key"):
        await runpod_flash.extract_entities(
            [_task("chunk-1")],
            runpod_config=_config(),
            accounts=[(_account("alpha"), "")],
        )


@pytest.mark.asyncio
async def test_settings_empty_accounts_fall_back_to_legacy_default_pair() -> None:
    runpod = RunpodFlashExtractionSettings(
        enabled=True,
        endpoint_id="unit-endpoint",
        request_concurrency=5,
        max_workers=12,
    )

    class SettingsCollection:
        async def find_one(self, query):
            return {
                "user_id": "user-1",
                "ingestion": {"runpod_flash": runpod.model_dump()},
                "api_keys": {"runpod": encrypt("unit-runpod-secret")},
            }

    class FakeDatabase:
        def __getitem__(self, name):
            assert name == "settings"
            return SettingsCollection()

    service = SettingsService()
    service._db = FakeDatabase()

    accounts = await service.get_system_runpod_flash_accounts("user-1")

    assert len(accounts) == 1
    account, api_key = accounts[0]
    assert account.name == "default"
    assert account.endpoint_id == "unit-endpoint"
    assert account.enabled is True
    assert account.request_concurrency == 5
    assert account.max_workers == 12
    assert api_key == "unit-runpod-secret"


@pytest.mark.asyncio
async def test_settings_accounts_decrypt_and_skip_disabled_or_keyless() -> None:
    runpod = RunpodFlashExtractionSettings(
        enabled=True,
        endpoint_id="legacy-endpoint",
        accounts=[
            RunpodFlashAccount(name="alpha", endpoint_id="endpoint-alpha"),
            RunpodFlashAccount(
                name="bravo", endpoint_id="endpoint-bravo", enabled=False
            ),
            RunpodFlashAccount(name="charlie", endpoint_id="endpoint-charlie"),
        ],
    )

    class SettingsCollection:
        async def find_one(self, query):
            return {
                "user_id": "user-1",
                "ingestion": {"runpod_flash": runpod.model_dump()},
                "api_keys": {
                    "runpod": encrypt("legacy-secret"),
                    "runpod_accounts": {
                        "alpha": encrypt("alpha-secret"),
                        "bravo": encrypt("bravo-secret"),
                        # charlie deliberately keyless
                    },
                },
            }

    class FakeDatabase:
        def __getitem__(self, name):
            assert name == "settings"
            return SettingsCollection()

    service = SettingsService()
    service._db = FakeDatabase()

    accounts = await service.get_system_runpod_flash_accounts("user-1")

    assert [(account.name, key) for account, key in accounts] == [
        ("alpha", "alpha-secret")
    ]
