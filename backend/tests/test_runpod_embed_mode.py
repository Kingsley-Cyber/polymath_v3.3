"""Embed mode 'runpod' — offline dispatcher tests (P1.8 burst embedding).

All HTTP is monkeypatched at the ``embedder._runpod_flash_submit`` seam (the
lazy wrapper around the extraction lane's ``_submit_and_wait``); no live
Runpod calls. Verifies:
  - least-in-flight routing spreads batches across two accounts
  - dimension mismatch raises before any vector could land in Qdrant
  - over-cap inputs split into <=256-text requests, order preserved
  - accounts lacking embed_endpoint_id are skipped (all lacking: fail closed)
  - one-hop failover to a second account; single-account failure raises
  - max_concurrent caps total in-flight requests
  - mode='local' never touches the Runpod path
"""

from __future__ import annotations

import asyncio
from collections import Counter
from unittest.mock import AsyncMock, patch

import pytest

from config import get_settings
from models.schemas import RunpodFlashAccount, RunpodFlashExtractionSettings
from services import embedder
from services.settings import settings_service

DIM = 8


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-auth-secret")
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _account(name: str, embed_endpoint_id: str) -> RunpodFlashAccount:
    return RunpodFlashAccount(
        name=name,
        endpoint_id=f"extract-{name}",
        embed_endpoint_id=embed_endpoint_id,
        request_concurrency=8,
    )


def _wire_accounts(monkeypatch, accounts):
    monkeypatch.setattr(
        settings_service,
        "get_system_runpod_flash_accounts",
        AsyncMock(return_value=accounts),
    )
    monkeypatch.setattr(
        settings_service,
        "get_system_runpod_flash",
        AsyncMock(
            return_value=(
                RunpodFlashExtractionSettings(
                    enabled=True, timeout_seconds=30, poll_interval_seconds=0.25
                ),
                None,
            )
        ),
    )


def _ok_output(texts: list[str]) -> dict:
    """Contract-conformant worker output; vectors encode the input text so
    order reassembly is observable."""
    return {
        "contract_version": embedder.RUNPOD_EMBED_CONTRACT_VERSION,
        "vectors": [[float(text)] * DIM for text in texts],
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "dim": DIM,
    }


@pytest.mark.asyncio
async def test_batches_route_across_two_accounts_least_in_flight(monkeypatch):
    _wire_accounts(
        monkeypatch,
        [(_account("acct-a", "ep-a"), "key-a"), (_account("acct-b", "ep-b"), "key-b")],
    )
    calls: list[str] = []

    async def fake_submit(client, *, endpoint_id, api_key, request, **kwargs):
        calls.append(endpoint_id)
        await asyncio.sleep(0)  # keep batches in flight concurrently
        return _ok_output(request["texts"])

    monkeypatch.setattr(embedder, "_runpod_flash_submit", fake_submit)

    texts = [str(i) for i in range(600)]  # 3 slices: 256 + 256 + 88
    vectors = await embedder.embed_batch(texts, mode="runpod", expected_dim=DIM)

    assert Counter(calls) == {"ep-a": 2, "ep-b": 1}
    assert [int(v[0]) for v in vectors] == list(range(600))
    assert all(len(v) == DIM for v in vectors)


@pytest.mark.asyncio
async def test_dimension_mismatch_raises_and_never_truncates(monkeypatch):
    _wire_accounts(monkeypatch, [(_account("acct-a", "ep-a"), "key-a")])

    async def fake_submit(client, *, request, **kwargs):
        return {
            "contract_version": embedder.RUNPOD_EMBED_CONTRACT_VERSION,
            "vectors": [[0.1] * (DIM - 4) for _ in request["texts"]],
            "model": "Qwen/Qwen3-Embedding-0.6B",
            "dim": DIM - 4,
        }

    monkeypatch.setattr(embedder, "_runpod_flash_submit", fake_submit)

    with patch.object(
        embedder, "_embed_batch_local", new_callable=AsyncMock
    ) as local_mock:
        with pytest.raises(RuntimeError, match="dimension mismatch"):
            await embedder.embed_batch(["x"], mode="runpod", expected_dim=DIM)
    # Fail closed: no silent local fallback, nothing usable returned.
    local_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_cap_input_splits_into_256_text_requests(monkeypatch):
    _wire_accounts(monkeypatch, [(_account("acct-a", "ep-a"), "key-a")])
    seen_batches: list[list[str]] = []

    async def fake_submit(client, *, request, **kwargs):
        assert request["contract_version"] == "polymath.runpod_embed.v1"
        assert set(request) == {"contract_version", "texts"}
        seen_batches.append(list(request["texts"]))
        return _ok_output(request["texts"])

    monkeypatch.setattr(embedder, "_runpod_flash_submit", fake_submit)

    texts = [str(i) for i in range(300)]
    vectors = await embedder.embed_batch(texts, mode="runpod", expected_dim=DIM)

    assert [len(batch) for batch in seen_batches] == [256, 44]
    assert all(
        len(batch) <= embedder.RUNPOD_EMBED_MAX_TEXTS_PER_REQUEST
        for batch in seen_batches
    )
    assert [int(v[0]) for v in vectors] == list(range(300))


@pytest.mark.asyncio
async def test_accounts_without_embed_endpoint_id_are_skipped(monkeypatch):
    _wire_accounts(
        monkeypatch,
        [
            (_account("acct-extract-only", ""), "key-a"),  # no embed endpoint
            (_account("acct-b", "ep-b"), "key-b"),
        ],
    )
    calls: list[str] = []

    async def fake_submit(client, *, endpoint_id, request, **kwargs):
        calls.append(endpoint_id)
        return _ok_output(request["texts"])

    monkeypatch.setattr(embedder, "_runpod_flash_submit", fake_submit)

    vectors = await embedder.embed_batch(["1", "2"], mode="runpod", expected_dim=DIM)

    assert calls == ["ep-b"]
    assert len(vectors) == 2


@pytest.mark.asyncio
async def test_no_account_with_embed_endpoint_id_fails_closed(monkeypatch):
    _wire_accounts(monkeypatch, [(_account("acct-extract-only", ""), "key-a")])
    submit_mock = AsyncMock()
    monkeypatch.setattr(embedder, "_runpod_flash_submit", submit_mock)

    with patch.object(
        embedder, "_embed_batch_local", new_callable=AsyncMock
    ) as local_mock:
        with pytest.raises(RuntimeError, match="embed_endpoint_id"):
            await embedder.embed_batch(["x"], mode="runpod", expected_dim=DIM)
    submit_mock.assert_not_awaited()
    local_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_batch_fails_over_once_to_the_other_account(monkeypatch):
    # Alpha tiebreak makes a-bad the deterministic primary.
    _wire_accounts(
        monkeypatch,
        [(_account("a-bad", "ep-bad"), "key-a"), (_account("b-good", "ep-good"), "key-b")],
    )
    calls: list[str] = []

    async def fake_submit(client, *, endpoint_id, request, **kwargs):
        calls.append(endpoint_id)
        if endpoint_id == "ep-bad":
            raise RuntimeError("Runpod job failed: worker exploded")
        return _ok_output(request["texts"])

    monkeypatch.setattr(embedder, "_runpod_flash_submit", fake_submit)

    vectors = await embedder.embed_batch(["7"], mode="runpod", expected_dim=DIM)

    assert calls == ["ep-bad", "ep-good"]  # exactly one hop, no retry storm
    assert vectors == [[7.0] * DIM]


@pytest.mark.asyncio
async def test_single_account_failure_raises_without_local_fallback(monkeypatch):
    _wire_accounts(monkeypatch, [(_account("acct-a", "ep-a"), "key-a")])
    calls: list[str] = []

    async def fake_submit(client, *, endpoint_id, **kwargs):
        calls.append(endpoint_id)
        raise RuntimeError("Runpod job timed_out: boom")

    monkeypatch.setattr(embedder, "_runpod_flash_submit", fake_submit)

    with patch.object(
        embedder, "_embed_batch_local", new_callable=AsyncMock
    ) as local_mock:
        with pytest.raises(RuntimeError, match="boom"):
            await embedder.embed_batch(["x"], mode="runpod", expected_dim=DIM)
    assert calls == ["ep-a"]  # no second account to fail over to
    local_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_concurrent_caps_total_in_flight_requests(monkeypatch):
    _wire_accounts(monkeypatch, [(_account("acct-a", "ep-a"), "key-a")])
    state = {"current": 0, "max": 0}

    async def fake_submit(client, *, request, **kwargs):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1
        return _ok_output(request["texts"])

    monkeypatch.setattr(embedder, "_runpod_flash_submit", fake_submit)

    texts = [str(i) for i in range(600)]  # 3 slices
    vectors = await embedder.embed_batch(
        texts, mode="runpod", expected_dim=DIM, max_concurrent=1
    )

    assert state["max"] == 1
    assert [int(v[0]) for v in vectors] == list(range(600))


@pytest.mark.asyncio
async def test_local_mode_is_untouched_and_attempts_no_runpod_http(monkeypatch):
    accounts_mock = AsyncMock(side_effect=AssertionError("accounts resolved for local mode"))
    monkeypatch.setattr(
        settings_service, "get_system_runpod_flash_accounts", accounts_mock
    )
    submit_mock = AsyncMock(side_effect=AssertionError("runpod HTTP attempted"))
    monkeypatch.setattr(embedder, "_runpod_flash_submit", submit_mock)

    with patch.object(
        embedder, "_embed_batch_local", new_callable=AsyncMock
    ) as local_mock:
        local_mock.return_value = [[0.0] * DIM]
        vectors = await embedder.embed_batch(["hello"], mode="local", expected_dim=DIM)

    local_mock.assert_awaited_once()
    submit_mock.assert_not_awaited()
    accounts_mock.assert_not_awaited()
    assert vectors == [[0.0] * DIM]
