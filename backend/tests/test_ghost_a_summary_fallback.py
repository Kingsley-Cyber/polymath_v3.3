from __future__ import annotations

import pytest

from services import ghost_a
from services.ghost_a import (
    SummaryTask,
    parse_summary_microbatch_response,
    summary_compiler_token_budget,
    summarize_parents,
)
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


def test_summary_compiler_budget_is_separate_from_semantic_length() -> None:
    assert summary_compiler_token_budget(175) == 1024
    assert summary_compiler_token_budget(175, 4) == 4096
    assert summary_compiler_token_budget(1024, 8) == 8192


class _EnvelopeIgnoringResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        }


class _EnvelopeIgnoringClient(_BlankSummaryClient):
    payloads: list[dict] = []

    async def post(self, *args, **kwargs) -> _EnvelopeIgnoringResponse:
        payload = dict(kwargs.get("json") or {})
        self.payloads.append(payload)
        user = payload["messages"][1]["content"]
        if "ITEMS:" in user:
            return _EnvelopeIgnoringResponse(
                '{"summary":"A provider ignored the batch envelope but returned prose."}'
            )
        return _EnvelopeIgnoringResponse(
            '{"summary":"The passage explains a durable semantic claim with supporting evidence.",'
            '"central_claim":"The passage explains a durable semantic claim.",'
            '"key_points":[{"point":"The claim is supported by the source child.",'
            '"supporting_child_ids":["child-1"]}]}'
        )


@pytest.mark.asyncio
async def test_summary_microbatch_uses_one_provider_call_for_four_targets(monkeypatch) -> None:
    _CapturingBlankSummaryClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _CapturingBlankSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id=f"parent-{index}",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text=f"Independent source passage {index} with enough semantic content.",
            )
            for index in range(4)
        ],
        pool=[
            {
                "model": "unit/batch-model",
                "base_url": None,
                "api_key": None,
                "max_concurrent": 1,
                "extra_params": {"microbatch_size": 4},
            }
        ],
        global_max_concurrent=1,
    )

    assert results == []
    assert len(_CapturingBlankSummaryClient.payloads) == 1
    assert "parent-0" in _CapturingBlankSummaryClient.payloads[0]["messages"][1]["content"]


def test_summary_microbatch_parser_salvages_valid_siblings_only() -> None:
    parsed = parse_summary_microbatch_response(
        '{"items":['
        '{"target_id":"parent-1","artifact":{"summary":"valid"}},'
        '{"target_id":"unknown","artifact":{"summary":"ignore"}},'
        '{"target_id":"parent-2","artifact":"malformed"}'
        ']}',
        allowed_target_ids={"parent-1", "parent-2"},
    )

    assert set(parsed) == {"parent-1"}
    assert "valid" in parsed["parent-1"]


def test_summary_microbatch_parser_skips_reasoning_object_prefix() -> None:
    parsed = parse_summary_microbatch_response(
        '<think>{"plan":"compile each item"}</think>\n'
        '{"items":[{"target_id":"parent-1","artifact":{"summary":"valid"}}]}',
        allowed_target_ids={"parent-1"},
    )

    assert set(parsed) == {"parent-1"}


@pytest.mark.asyncio
async def test_summary_microbatch_falls_back_only_missing_targets(monkeypatch) -> None:
    _EnvelopeIgnoringClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _EnvelopeIgnoringClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id=f"parent-{index}",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="The source child supports a durable semantic claim.",
                source_child_ids=["child-1"],
            )
            for index in range(3)
        ],
        pool=[
            {
                "model": "unit/envelope-ignoring-model",
                "base_url": None,
                "api_key": None,
                "max_concurrent": 4,
                "extra_params": {"microbatch_size": 4},
            }
        ],
        global_max_concurrent=4,
    )

    assert {result.parent_id for result in results} == {
        "parent-0",
        "parent-1",
        "parent-2",
    }
    assert len(_EnvelopeIgnoringClient.payloads) == 4
    assert sum("ITEMS:" in payload["messages"][1]["content"] for payload in _EnvelopeIgnoringClient.payloads) == 1


@pytest.mark.asyncio
async def test_blank_model_content_defers_instead_of_using_fallback(monkeypatch) -> None:
    _CapturingBlankSummaryClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _CapturingBlankSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 2)
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
    assert len(_CapturingBlankSummaryClient.payloads) == 1


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
    assert _CapturingBlankSummaryClient.payloads[0]["cache"] == {
        "no-cache": True,
        "no-store": True,
    }
    assert _CapturingBlankSummaryClient.payloads[0]["response_format"] == {
        "type": "json_object"
    }


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
