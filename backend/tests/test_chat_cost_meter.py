from __future__ import annotations

import json
from decimal import Decimal

import pytest

from models.schemas import ChatChunk
from services import llm as llm_module
from services.chat_cost_meter import (
    CHAT_COST_TRACE_TITLE,
    aggregate_chat_cost_ledgers,
    chat_cost_scope,
    meter_chat_sse_stream,
    record_chat_provider_call,
)
from services.llm import LLMService
from utils.streaming import build_sse_chunk


MODEL = "anthropic/minimax-m2.7"
API_BASE = "https://opencode.ai/zen/go"


def _telemetry(input_tokens: int, output_tokens: int) -> dict:
    return {
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "actual_cost_usd": None,
        "cost_source": None,
    }


def _payload(frame: str) -> dict:
    line = next(line for line in frame.splitlines() if line.startswith("data:"))
    return json.loads(line[5:].strip())


def test_one_trace_reproduces_exact_arithmetic_without_secrets() -> None:
    with chat_cost_scope() as ledger:
        record_chat_provider_call(
            call_kind="stream_chat",
            model=MODEL,
            api_base=API_BASE + "?api_key=must-not-leak",
            provider_telemetry=_telemetry(1000, 500),
        )
        snapshot = ledger.snapshot()

    assert snapshot["accounting_state"] == "CLOSED"
    assert snapshot["computed_cost_usd"] == "0.0009"
    call = snapshot["calls"][0]
    price = call["price"]
    reproduced = (
        Decimal(call["input_tokens"]) * Decimal(price["input_usd_per_unit"])
        + Decimal(call["output_tokens"]) * Decimal(price["output_usd_per_unit"])
    ) / Decimal(price["price_unit_tokens"])
    assert reproduced == Decimal(call["computed_cost_usd"])
    rendered = json.dumps(snapshot)
    assert "must-not-leak" not in rendered
    assert "api_key" not in rendered


def test_missing_price_fails_open_instead_of_estimating() -> None:
    with chat_cost_scope() as ledger:
        record_chat_provider_call(
            call_kind="complete_sync",
            model="unknown/provider-model",
            api_base="https://unknown.invalid/v1",
            provider_telemetry=_telemetry(10, 2),
        )
        snapshot = ledger.snapshot()

    assert snapshot["accounting_state"] == "OPEN"
    assert snapshot["computed_cost_usd"] is None
    assert snapshot["unmetered_synthesis_call_count"] == 1
    assert snapshot["calls"][0]["failure_reason"] == "price_route_missing"


def test_transport_retry_is_surfaced_as_unmetered_exposure() -> None:
    with chat_cost_scope() as ledger:
        record_chat_provider_call(
            call_kind="stream_chat",
            model=MODEL,
            api_base=API_BASE,
            provider_telemetry=_telemetry(10, 2),
            transport_attempts=2,
        )
        snapshot = ledger.snapshot()

    assert snapshot["synthesis_call_count"] == 2
    assert snapshot["metered_synthesis_call_count"] == 1
    assert snapshot["unmetered_synthesis_call_count"] == 1
    assert snapshot["accounting_state"] == "OPEN"


def test_run_aggregator_sums_closed_request_ledgers() -> None:
    request_ledgers = []
    for usage in ((100, 20), (200, 40)):
        with chat_cost_scope() as ledger:
            record_chat_provider_call(
                call_kind="stream_chat",
                model=MODEL,
                api_base=API_BASE,
                provider_telemetry=_telemetry(*usage),
            )
            request_ledgers.append(ledger.snapshot())

    run = aggregate_chat_cost_ledgers(request_ledgers)
    assert run["accounting_state"] == "CLOSED"
    assert run["request_ledger_count"] == 2
    assert run["synthesis_call_count"] == 2
    assert run["input_tokens"] == 300
    assert run["output_tokens"] == 60
    assert run["computed_cost_usd"] == "0.000162"


def test_run_aggregator_requires_at_least_one_request_ledger() -> None:
    run = aggregate_chat_cost_ledgers([])
    assert run["accounting_state"] == "OPEN"
    assert run["zero_unmetered_synthesis_calls"] is False


@pytest.mark.asyncio
async def test_sse_wrapper_inserts_one_reproducible_trace_before_done() -> None:
    async def source():
        record_chat_provider_call(
            call_kind="stream_chat",
            model=MODEL,
            api_base=API_BASE,
            provider_telemetry=_telemetry(1000, 500),
        )
        yield build_sse_chunk(
            ChatChunk(type="token", content="answer", conversation_id="conv-1")
        )
        yield build_sse_chunk(ChatChunk(type="done", conversation_id="conv-1"))

    frames = [
        frame
        async for frame in meter_chat_sse_stream(
            source(),
            enabled=True,
        )
    ]
    payloads = [_payload(frame) for frame in frames]
    assert [row["type"] for row in payloads] == ["token", "trace_event", "done"]
    trace = payloads[1]["trace_event"]
    assert trace["title"] == CHAT_COST_TRACE_TITLE
    ledger = trace["metadata"]["chat_cost_ledger"]
    assert ledger["accounting_state"] == "CLOSED"
    assert ledger["computed_cost_usd"] == "0.0009"


@pytest.mark.asyncio
async def test_sse_wrapper_disabled_is_byte_exact_passthrough() -> None:
    expected = build_sse_chunk(ChatChunk(type="done", conversation_id="conv-1"))

    async def source():
        yield expected

    frames = [
        frame
        async for frame in meter_chat_sse_stream(
            source(),
            enabled=False,
        )
    ]
    assert frames == [expected]


@pytest.mark.asyncio
async def test_model_skipped_request_emits_closed_zero_cost_trace() -> None:
    async def source():
        yield build_sse_chunk(ChatChunk(type="done", conversation_id="conv-1"))

    frames = [
        frame
        async for frame in meter_chat_sse_stream(
            source(),
            enabled=True,
        )
    ]
    ledger = _payload(frames[0])["trace_event"]["metadata"]["chat_cost_ledger"]
    assert ledger["accounting_state"] == "CLOSED"
    assert ledger["synthesis_call_count"] == 0
    assert ledger["computed_cost_usd"] == "0"


class _FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, *, payload: dict | None = None) -> None:
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}'
        yield (
            'data: {"choices":[],"usage":{"prompt_tokens":100,'
            '"completion_tokens":20,"total_tokens":120}}'
        )
        yield "data: [DONE]"

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    is_closed = False

    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = payload
        self.stream_body: dict | None = None
        self.post_body: dict | None = None

    def stream(self, _method, _url, *, json, **_kwargs):
        self.stream_body = json
        return _FakeResponse()

    async def post(self, _url, *, json, **_kwargs):
        self.post_body = json
        return _FakeResponse(payload=self.payload)


class _FailingClient(_FakeClient):
    async def post(self, _url, *, json, **_kwargs):
        self.post_body = json
        raise TimeoutError("synthetic timeout")


@pytest.mark.asyncio
async def test_stream_usage_request_and_terminal_metering(monkeypatch) -> None:
    monkeypatch.setattr(llm_module.settings, "CHAT_COST_TELEMETRY_ENABLED", True)
    client = _FakeClient()
    service = LLMService()
    service._client = client

    with chat_cost_scope() as ledger:
        chunks = [
            chunk
            async for chunk in service.stream_chat(
                messages=[{"role": "user", "content": "hi"}],
                model=MODEL,
                api_base=API_BASE,
                api_key="test-secret",
            )
        ]
        snapshot = ledger.snapshot()

    assert chunks == [{"content": "hello"}]
    assert client.stream_body["stream_options"] == {"include_usage": True}
    assert snapshot["accounting_state"] == "CLOSED"
    assert snapshot["input_tokens"] == 100
    assert snapshot["output_tokens"] == 20
    assert "test-secret" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_stream_request_is_unchanged_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(llm_module.settings, "CHAT_COST_TELEMETRY_ENABLED", False)
    client = _FakeClient()
    service = LLMService()
    service._client = client

    chunks = [
        chunk
        async for chunk in service._stream_chat_once(
            messages=[{"role": "user", "content": "hi"}],
            model=MODEL,
            api_base=API_BASE,
            api_key="test-secret",
        )
    ]

    assert chunks == [{"content": "hello"}]
    assert "stream_options" not in client.stream_body


@pytest.mark.asyncio
async def test_nonstream_helper_is_included_in_request_ledger() -> None:
    payload = {
        "choices": [{"message": {"content": "helper output"}}],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "total_tokens": 60,
        },
    }
    client = _FakeClient(payload=payload)
    service = LLMService()
    service._client = client

    with chat_cost_scope() as ledger:
        output = await service.complete_sync(
            messages=[{"role": "user", "content": "helper"}],
            model=MODEL,
            api_base=API_BASE,
            api_key="test-secret",
        )
        snapshot = ledger.snapshot()

    assert output == "helper output"
    assert snapshot["accounting_state"] == "CLOSED"
    assert snapshot["calls"][0]["call_kind"] == "complete_sync"
    assert snapshot["input_tokens"] == 50
    assert snapshot["output_tokens"] == 10


@pytest.mark.asyncio
async def test_nonstream_helper_failure_is_not_silently_dropped() -> None:
    service = LLMService()
    service._client = _FailingClient()

    with chat_cost_scope() as ledger:
        with pytest.raises(TimeoutError, match="synthetic timeout"):
            await service.complete_sync(
                messages=[{"role": "user", "content": "helper"}],
                model=MODEL,
                api_base=API_BASE,
                api_key="test-secret",
            )
        snapshot = ledger.snapshot()

    assert snapshot["accounting_state"] == "OPEN"
    assert snapshot["synthesis_call_count"] == 1
    assert snapshot["unmetered_synthesis_call_count"] == 1
    assert snapshot["calls"][0]["failure_reason"] == "complete_sync_error:TimeoutError"


def test_feature_flag_ships_default_off() -> None:
    from config import Settings

    assert Settings().CHAT_COST_TELEMETRY_ENABLED is False
