from __future__ import annotations

import pytest

from services import ghost_a
from services.ghost_a import (
    SummaryTask,
    parse_summary_microbatch_response,
    parse_tagged_summary_response,
    provider_summary_microbatch_size,
    provider_summary_token_budget,
    summary_compiler_token_budget,
    summarize_parents,
)
from services.ingestion import model_lifecycle
from services.ingestion import summary_provider_pool
from services.ingestion.summary_provider_pool import (
    prepare_summary_provider_pool,
    resolve_summary_provider_pool,
)


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


class _KeyAwareSummaryClient(_BlankSummaryClient):
    payloads: list[dict] = []

    async def post(self, *args, **kwargs) -> _EnvelopeIgnoringResponse:
        payload = dict(kwargs.get("json") or {})
        self.payloads.append(payload)
        if payload.get("api_key") == "dead-key":
            request = ghost_a.httpx.Request("POST", "https://provider.test/chat")
            response = ghost_a.httpx.Response(
                402,
                request=request,
                json={"error": {"message": "Insufficient Balance"}},
            )
            raise ghost_a.httpx.HTTPStatusError(
                "402 Payment Required",
                request=request,
                response=response,
            )
        return _EnvelopeIgnoringResponse(
            '{"summary":"A funded sibling lane compiles a durable semantic summary after one key fails.",'
            '"central_claim":"A failed key does not disable funded siblings.",'
            '"key_points":[{"point":"The healthy key keeps serving the queued source.",'
            '"supporting_child_ids":["child-1"]}],'
            '"concept_tags":["summary lanes","circuit breaker"]}'
        )


class _CapturingBlankSummaryClient(_BlankSummaryClient):
    payloads: list[dict] = []

    async def post(self, *args, **kwargs) -> _BlankSummaryResponse:
        self.payloads.append(dict(kwargs.get("json") or {}))
        return _BlankSummaryResponse()


def test_summary_compiler_budget_is_separate_from_semantic_length() -> None:
    assert summary_compiler_token_budget(175) == 1024
    assert summary_compiler_token_budget(175, 4) == 4096
    assert summary_compiler_token_budget(1024, 8) == 8192


def test_hy3_summary_budget_reserves_reasoning_overhead() -> None:
    hy3 = {
        "provider_preset": "siliconflow",
        "model": "openai/tencent/Hy3",
        "base_url": "https://api.siliconflow.com/v1",
    }
    longcat = {
        "provider_preset": "longcat",
        "model": "openai/LongCat-2.0",
        "base_url": "https://api.longcat.chat/openai/v1",
    }

    assert provider_summary_token_budget(hy3, 175) == 4096
    assert provider_summary_token_budget(longcat, 175) == 1024
    assert provider_summary_token_budget(hy3, 175, 8) == 8192
    assert provider_summary_microbatch_size(hy3, 4) == 1
    assert provider_summary_microbatch_size(longcat, 4) == 4


@pytest.mark.asyncio
async def test_longcat_summary_uses_xml_schema_control_wrapper(monkeypatch) -> None:
    _EnvelopeIgnoringClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _EnvelopeIgnoringClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="The source child supports a durable semantic claim.",
                source_child_ids=["child-1"],
            )
        ],
        pool=[
            {
                "provider_preset": "longcat",
                "model": "openai/LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": "longcat-test-key",
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        global_max_concurrent=1,
    )

    assert [result.parent_id for result in results] == ["parent-1"]
    payload = _EnvelopeIgnoringClient.payloads[0]
    user_prompt = payload["messages"][1]["content"]
    assert '<schema_control contract="parent_summary.v1"' in user_prompt
    assert "<json_payload>" in user_prompt
    assert "response_format" not in payload
    assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_explicit_longcat_summary_bypasses_litellm_route_collapse(
    monkeypatch,
) -> None:
    _DirectLongCatClient.urls.clear()
    _DirectLongCatClient.headers.clear()
    _DirectLongCatClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _DirectLongCatClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="The source child supports direct LongCat schema-control dispatch.",
                source_child_ids=["child-1"],
            )
        ],
        pool=[
            {
                "provider_preset": "longcat",
                "model": "openai/LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": "longcat-direct-key",
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        global_max_concurrent=1,
    )

    assert [result.parent_id for result in results] == ["parent-1"]
    assert _DirectLongCatClient.urls == [
        "https://api.longcat.chat/openai/v1/chat/completions"
    ]
    payload = _DirectLongCatClient.payloads[0]
    assert "api_base" not in payload
    assert "api_key" not in payload
    assert "cache" not in payload
    assert "response_format" not in payload
    assert payload["thinking"] == {"type": "disabled"}
    assert _DirectLongCatClient.headers[0]["Authorization"] == (
        "Bearer longcat-direct-key"
    )


@pytest.mark.asyncio
async def test_deepseek_summary_uses_native_json_object_response_format(monkeypatch) -> None:
    _EnvelopeIgnoringClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _EnvelopeIgnoringClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="The source child supports a durable semantic claim.",
                source_child_ids=["child-1"],
            )
        ],
        pool=[
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com",
                "api_key": "deepseek-test-key",
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        global_max_concurrent=1,
    )

    assert [result.parent_id for result in results] == ["parent-1"]
    payload = _EnvelopeIgnoringClient.payloads[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert "<schema_control" not in payload["messages"][1]["content"]


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


class _DirectLongCatClient(_BlankSummaryClient):
    urls: list[str] = []
    headers: list[dict] = []
    payloads: list[dict] = []

    async def post(self, *args, **kwargs) -> _EnvelopeIgnoringResponse:
        self.urls.append(str(args[0]))
        self.headers.append(dict(kwargs.get("headers") or {}))
        self.payloads.append(dict(kwargs.get("json") or {}))
        return _EnvelopeIgnoringResponse(
            '{"summary":"A direct LongCat provider call compiles a durable semantic summary without passing through LiteLLM routing.",'
            '"central_claim":"Direct provider dispatch preserves account-lane identity.",'
            '"key_points":[{"point":"The summary is grounded in the supplied source child.",'
            '"supporting_child_ids":["child-1"]}],'
            '"concept_tags":["direct dispatch","schema control"]}'
        )


class _TaggedRescueClient(_BlankSummaryClient):
    payloads: list[dict] = []

    async def post(self, *args, **kwargs) -> _EnvelopeIgnoringResponse:
        payload = dict(kwargs.get("json") or {})
        self.payloads.append(payload)
        user = payload["messages"][1]["content"]
        if "ITEMS:" in user:
            return _EnvelopeIgnoringResponse(
                '{"items":['
                '{"target_id":"parent-0","artifact":{"source":"copied-json"}},'
                '{"target_id":"parent-1","artifact":{"source":"copied-json"}},'
                '{"target_id":"parent-2","artifact":{"source":"copied-json"}}'
                "]}"
            )
        return _EnvelopeIgnoringResponse(
            "SUMMARY: Durable queues preserve validated retrieval artifacts while preventing repeated provider work. "
            "The process separates generation from promotion and records failures for explicit operator review.\n"
            "CLAIM: Durable queues prevent duplicate provider work.\n"
            "POINT: Validated artifacts remain reusable across retries.\n"
            "POINT: Invalid generations are not promoted into retrieval storage.\n"
            "POINT: Operators explicitly control retries after a terminal failure.\n"
            "TAGS: durable queues | retrieval artifacts | validation | retries\n"
            "MECHANISM: validation before durable promotion\n"
            "ABSTRACTION: medium"
        )


class _CrossProviderComplianceClient(_BlankSummaryClient):
    payloads: list[dict] = []

    async def post(self, *args, **kwargs) -> _EnvelopeIgnoringResponse:
        payload = dict(kwargs.get("json") or {})
        self.payloads.append(payload)
        if "longcat" not in str(payload.get("model") or "").lower():
            return _EnvelopeIgnoringResponse("")
        user = payload["messages"][1]["content"]
        target_ids = [f"parent-{index}" for index in range(3) if f"parent-{index}" in user]
        items = [
            {
                "target_id": target_id,
                "artifact": {
                    "summary": (
                        "A second provider compiles the source into a durable semantic "
                        "summary after the first provider rejects the output contract."
                    ),
                    "central_claim": "Cross-provider routing recovers contract compliance.",
                    "key_points": [
                        {
                            "point": "The validated artifact remains grounded in the source child.",
                            "supporting_child_ids": ["child-1"],
                        }
                    ],
                    "concept_tags": ["provider routing", "validation"],
                },
            }
            for target_id in target_ids
        ]
        return _EnvelopeIgnoringResponse(__import__("json").dumps({"items": items}))


class _InvalidSharedKeyProbeClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, *args, **kwargs):
        return ghost_a.httpx.Response(401, request=ghost_a.httpx.Request("GET", args[0]))


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


def test_tagged_summary_compiler_attaches_known_child_evidence() -> None:
    parsed = parse_tagged_summary_response(
        "SUMMARY: A bounded provider rescue produces factual retrieval prose without requiring native JSON support. "
        "The deterministic compiler attaches known evidence identifiers and validates the resulting artifact.\n"
        "CLAIM: The compiler converts tagged prose into a validated artifact.\n"
        "POINT: Tagged prose avoids malformed JSON responses.\n"
        "TAGS: tagged prose | validation | retrieval\n"
        "ABSTRACTION: medium",
        source_child_ids=["child-1"],
    )

    assert parsed["summary"].startswith("A bounded provider rescue")
    assert parsed["validation_status"] == "valid"
    assert parsed["key_points"][0]["supporting_child_ids"] == ["child-1"]


@pytest.mark.asyncio
async def test_invalid_json_microbatch_uses_bounded_tagged_rescue(monkeypatch) -> None:
    _TaggedRescueClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _TaggedRescueClient)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id=f"parent-{index}",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="Durable queues preserve validated artifacts and prevent duplicate work.",
                source_child_ids=["child-1"],
            )
            for index in range(3)
        ],
        pool=[
            {
                "model": "unit/tagged-rescue-model",
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
    assert len(_TaggedRescueClient.payloads) == 4
    assert sum(
        "Produce this exact line-oriented contract" in payload["messages"][1]["content"]
        for payload in _TaggedRescueClient.payloads
    ) == 3


@pytest.mark.asyncio
async def test_validation_rejection_routes_to_untried_provider_signature(monkeypatch) -> None:
    _CrossProviderComplianceClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _CrossProviderComplianceClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_BACKOFF_SECONDS", 0)

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id=f"parent-{index}",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="Cross-provider routing must preserve evidence and validation.",
                source_child_ids=["child-1"],
            )
            for index in range(3)
        ],
        pool=[
            {
                "model": "openai/tencent/Hy3",
                "base_url": "https://siliconflow.invalid/v1",
                "api_key": "hy3-test-key",
                "max_concurrent": 3,
                "extra_params": {"microbatch_size": 3},
            },
            {
                "model": "openai/LongCat-2.0",
                "base_url": "https://longcat.invalid/v1",
                "api_key": "longcat-test-key",
                "max_concurrent": 1,
                "extra_params": {"microbatch_size": 3},
            },
        ],
        global_max_concurrent=4,
    )

    assert {result.parent_id for result in results} == {
        "parent-0",
        "parent-1",
        "parent-2",
    }
    hy3_calls = [
        payload
        for payload in _CrossProviderComplianceClient.payloads
        if "hy3" in str(payload.get("model") or "").lower()
    ]
    longcat_calls = [
        payload
        for payload in _CrossProviderComplianceClient.payloads
        if "longcat" in str(payload.get("model") or "").lower()
    ]
    # Hy3 is intentionally unbatched: each target receives one normal request
    # and one bounded tagged rescue before routing to another provider family.
    assert len(hy3_calls) == 6
    assert sum(
        "Produce this exact line-oriented contract"
        in payload["messages"][1]["content"]
        for payload in hy3_calls
    ) == 3
    assert len(longcat_calls) == 1


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
    assert len(_CapturingBlankSummaryClient.payloads) == 2
    assert "Produce this exact line-oriented contract" in (
        _CapturingBlankSummaryClient.payloads[1]["messages"][1]["content"]
    )


@pytest.mark.asyncio
async def test_summary_pool_drops_provider_after_consecutive_empty_artifacts(
    monkeypatch,
) -> None:
    _CapturingBlankSummaryClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _CapturingBlankSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 0)
    pool_status = {"drop_threshold": 3}

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id=f"parent-{index}",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="A provider must return a valid durable summary artifact.",
            )
            for index in range(4)
        ],
        pool=[
            {
                "model": "unit/always-empty",
                "base_url": None,
                "api_key": None,
                "max_concurrent": 1,
                "extra_params": {"microbatch_size": 1},
            }
        ],
        pool_status=pool_status,
    )

    assert results == []
    assert pool_status["dropped_provider_count"] == 1
    assert pool_status["dropped_providers"][0]["reason"] == (
        "consecutive_empty_or_rejected"
    )
    assert pool_status["dropped_providers"][0]["consecutive_rejections"] == 3
    assert len(_CapturingBlankSummaryClient.payloads) == 6


def test_summary_pool_pins_flash_and_demotes_uncanaried_hy3() -> None:
    pool, report = prepare_summary_provider_pool(
        [
            {"model": "openai/tencent/Hy3", "extra_params": {}},
            {"model": "openai/LongCat-2.0", "extra_params": {}},
            {"model": "deepseek/deepseek-v4-flash", "extra_params": {}},
        ]
    )

    assert [entry["model"] for entry in pool] == [
        "deepseek/deepseek-v4-flash",
        "openai/LongCat-2.0",
    ]
    assert report["primary_model"] == "deepseek/deepseek-v4-flash"
    assert report["demoted_provider_count"] == 1


def test_summary_pool_honors_disabled_lanes() -> None:
    pool, report = prepare_summary_provider_pool(
        [
            {
                "enabled": False,
                "model": "deepseek/deepseek-v4-flash",
                "provider_preset": "deepseek",
            },
            {
                "model": "deepseek/deepseek-v4-flash",
                "provider_preset": "deepseek-2",
            },
        ]
    )

    assert [entry["provider_preset"] for entry in pool] == ["deepseek-2"]
    assert report["disabled_provider_count"] == 1
    assert report["disabled_models"] == ["deepseek/deepseek-v4-flash"]


def test_summary_pool_preserves_distinct_same_provider_credentials() -> None:
    pool, report = prepare_summary_provider_pool(
        [
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "deepseek-account-a",
                "max_concurrent": 45,
            },
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "deepseek-account-b",
                "max_concurrent": 45,
            },
            {
                "provider_preset": "longcat",
                "model": "openai/LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": "longcat-account-a",
                "max_concurrent": 3,
            },
            {
                "provider_preset": "longcat",
                "model": "openai/LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": "longcat-account-b",
                "max_concurrent": 3,
            },
            {
                "provider_preset": "longcat",
                "model": "openai/LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": "longcat-account-c",
                "max_concurrent": 3,
            },
        ]
    )

    assert [entry["provider_preset"] for entry in pool] == [
        "deepseek",
        "deepseek",
        "longcat",
        "longcat",
        "longcat",
    ]
    assert [entry["max_concurrent"] for entry in pool] == [45, 45, 3, 3, 3]
    assert report["admitted_provider_count"] == 5
    assert report["admitted_provider_capacity"] == 99
    assert "deepseek-account-a" not in str(report)
    assert "longcat-account-c" not in str(report)


def test_summary_pool_collapses_same_credential_duplicate() -> None:
    pool, report = prepare_summary_provider_pool(
        [
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "same-deepseek-account",
                "max_concurrent": 45,
            },
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "same-deepseek-account",
                "max_concurrent": 45,
            },
        ]
    )

    assert len(pool) == 1
    assert report["admitted_provider_count"] == 1
    assert report["admitted_provider_capacity"] == 45


@pytest.mark.asyncio
async def test_invalid_shared_flash_key_is_not_auto_inserted(monkeypatch) -> None:
    async def fake_any_user(name: str):
        return "invalid-shared-key" if name == "deepseek" else None

    monkeypatch.setattr(
        "services.settings.settings_service.get_plaintext_key_any_user",
        fake_any_user,
    )
    monkeypatch.setattr(
        summary_provider_pool.httpx,
        "AsyncClient",
        _InvalidSharedKeyProbeClient,
    )

    pool, report = await resolve_summary_provider_pool(
        configured_refs=[],
        runtime_refs=[],
    )

    assert pool == []
    assert report["flash_key_available"] is False
    assert report["shared_flash_key_probe_ok"] is False
    assert report["shared_flash_key_error"] == "http_401"


@pytest.mark.asyncio
async def test_explicit_longcat_pool_does_not_auto_insert_shared_deepseek(
    monkeypatch,
) -> None:
    async def fake_any_user(name: str):
        return "valid-looking-shared-key" if name == "deepseek" else None

    class _ProbeShouldNotRun:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("explicit non-empty pool must not probe DeepSeek")

    monkeypatch.setattr(
        "services.settings.settings_service.get_plaintext_key_any_user",
        fake_any_user,
    )
    monkeypatch.setattr(
        summary_provider_pool.httpx,
        "AsyncClient",
        _ProbeShouldNotRun,
    )

    pool, report = await resolve_summary_provider_pool(
        configured_refs=[
            {
                "provider_preset": "longcat",
                "model": "openai/LongCat-2.0",
                "base_url": "https://api.longcat.chat/openai/v1",
                "api_key": "longcat-key",
                "max_concurrent": 45,
            }
        ],
        runtime_refs=[],
    )

    assert [entry["provider_preset"] for entry in pool] == ["longcat"]
    assert report["flash_primary"] is False
    assert report["admitted_models"] == ["openai/LongCat-2.0"]


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
async def test_fatal_deepseek_key_does_not_disable_funded_sibling(monkeypatch) -> None:
    _KeyAwareSummaryClient.payloads.clear()
    monkeypatch.setattr(ghost_a.httpx, "AsyncClient", _KeyAwareSummaryClient)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(ghost_a, "_SUMMARY_RETRY_BACKOFF_SECONDS", 0)
    pool_status = {"drop_threshold": 1}

    results = await summarize_parents(
        [
            SummaryTask(
                parent_id="parent-1",
                doc_id="doc-1",
                corpus_id="corpus-1",
                source_tier="parent",
                text="Funded sibling summary lanes must keep serving when one key returns 402.",
                source_child_ids=["child-1"],
            )
        ],
        max_summary_tokens=80,
        pool=[
            {
                "provider_preset": "deepseek",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "dead-key",
                "max_concurrent": 1,
                "extra_params": {},
            },
            {
                "provider_preset": "deepseek-2",
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "funded-key",
                "max_concurrent": 1,
                "extra_params": {},
            },
        ],
        global_max_concurrent=2,
        pool_status=pool_status,
    )

    assert [result.parent_id for result in results] == ["parent-1"]
    assert pool_status["active_provider_count"] == 1
    assert pool_status["dropped_provider_count"] == 1
    assert len(pool_status["dropped_signatures"]) == 1
    assert _KeyAwareSummaryClient.payloads[0]["api_key"] == "dead-key"
    assert any(
        payload.get("api_key") == "funded-key"
        for payload in _KeyAwareSummaryClient.payloads
    )


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
    assert _CapturingBlankSummaryClient.payloads[0]["enable_thinking"] is False
    assert "thinking" not in _CapturingBlankSummaryClient.payloads[0]
    assert _CapturingBlankSummaryClient.payloads[0]["cache"] == {
        "no-cache": True,
        "no-store": True,
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
