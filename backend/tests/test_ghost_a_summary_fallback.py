from __future__ import annotations

import pytest

from services import ghost_a
from services.ghost_a import SummaryTask, summarize_parents
from services.ingestion import model_lifecycle


class _BlankSummaryResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": ""}}]}


class _BlankSummaryClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, *args, **kwargs) -> _BlankSummaryResponse:
        return _BlankSummaryResponse()


class _ExhaustedSummaryClient(_BlankSummaryClient):
    async def post(self, *args, **kwargs) -> _BlankSummaryResponse:
        raise RuntimeError("Insufficient Balance")


class _CapturingBlankSummaryClient(_BlankSummaryClient):
    payloads: list[dict] = []

    async def post(self, *args, **kwargs) -> _BlankSummaryResponse:
        self.payloads.append(dict(kwargs.get("json") or {}))
        return _BlankSummaryResponse()


@pytest.mark.asyncio
async def test_blank_model_content_defers_instead_of_using_fallback(monkeypatch) -> None:
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _BlankSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_BACKOFF_SECONDS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text=(
                    "Shopify lets sellers create an online store. "
                    "The tutorial explains account setup, pricing, products, "
                    "themes, and publishing an ecommerce website."
                ),
            )
        ],
        max_summary_tokens=80,
        pool=[
            {
                "model": "unit/blank-model",
                "base_url": None,
                "api_key": None,
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        global_max_concurrent=1,
    )

    assert results == []


@pytest.mark.asyncio
async def test_fatal_provider_exhaustion_defers_instead_of_using_fallback(monkeypatch) -> None:
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _ExhaustedSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_BACKOFF_SECONDS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="A valid source passage must remain queued until provider capacity returns.",
            )
        ],
        max_summary_tokens=80,
        pool=[
            {
                "model": "unit/exhausted-model",
                "base_url": None,
                "api_key": None,
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        global_max_concurrent=1,
    )

    assert results == []


@pytest.mark.asyncio
async def test_summary_lane_honors_provider_disable_thinking(monkeypatch) -> None:
    _CapturingBlankSummaryClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _CapturingBlankSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)

    await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="A provider-backed summary must expose content, not hidden reasoning.",
            )
        ],
        pool=[
            {
                "model": "openai/tencent/Hy3",
                "base_url": "https://api.siliconflow.example/v1",
                "api_key": "test-key",
                "max_concurrent": 1,
                "extra_params": {"disable_thinking": True},
            }
        ],
    )

    assert _CapturingBlankSummaryClient.payloads
    assert _CapturingBlankSummaryClient.payloads[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_summary_lifecycle_shutdown_runs_when_worker_raises(monkeypatch) -> None:
    ensure_calls = []
    shutdown_calls = []

    async def fake_ensure(pool, *, purpose):
        ensure_calls.append((pool, purpose))

    async def fake_shutdown(pool, *, purpose):
        shutdown_calls.append((pool, purpose))

    monkeypatch.setattr(model_lifecycle, "ensure_model_lifecycle_ready", fake_ensure)
    monkeypatch.setattr(model_lifecycle, "shutdown_model_lifecycle", fake_shutdown)

    with pytest.raises(KeyError):
        await summarize_parents(
            [
                SummaryTask(
                    parent_id="parent-1",
                    doc_id="doc-1",
                    corpus_id="corpus-1",
                    source_tier="parent",
                    text="A managed summary lane should still idle-stop after failure.",
                )
            ],
            max_summary_tokens=80,
            pool=[
                {
                    "base_url": "https://api.example.test/v1",
                    "api_key": "test-key",
                    "max_concurrent": 1,
                    "lifecycle_base_url": "http://192.168.1.83:8085",
                    "lifecycle_auto_start": True,
                    "lifecycle_auto_stop": True,
                    "extra_params": {},
                }
            ],
            global_max_concurrent=1,
        )

    assert len(ensure_calls) == 1
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0][1] == "ghost_a"
