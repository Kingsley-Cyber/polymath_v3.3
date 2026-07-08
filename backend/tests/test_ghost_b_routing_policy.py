import asyncio
from types import SimpleNamespace

import pytest

import services.ghost_b as ghost_b
from services.ghost_b import ExtractionTask
from services.ingestion import model_lifecycle


def test_mixed_private_vllm_and_cloud_defaults_to_balanced_routing():
    pool = [
        {
            "provider_preset": "vllm-rtx",
            "model": "polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
            "max_concurrent": 60,
            "extra_params": {"managed_vllm": True, "resource_class": "rtx"},
        },
        {
            "provider_preset": "siliconflow",
            "model": "tencent/Hy3",
            "base_url": "https://api.siliconflow.cn/v1",
            "max_concurrent": 8,
            "extra_params": {},
        },
    ]

    assert ghost_b._resolve_extraction_routing_policy(pool) == "balanced"


def test_explicit_primary_fallback_policy_wins_over_mixed_default():
    pool = [
        {
            "provider_preset": "vllm-rtx",
            "model": "polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
            "max_concurrent": 60,
            "extra_params": {
                "managed_vllm": True,
                "resource_class": "rtx",
                "routing_policy": "primary_fallback",
            },
        },
        {
            "provider_preset": "longcat",
            "model": "LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
            "max_concurrent": 45,
            "extra_params": {},
        },
    ]

    assert ghost_b._resolve_extraction_routing_policy(pool) == "primary_fallback"


def test_balanced_spawn_order_gives_cloud_lanes_early_work():
    order = ghost_b._worker_lane_spawn_order(
        [60, 8, 45],
        disabled_lanes=set(),
        routing_policy="balanced",
    )

    assert order[:9] == [0, 1, 2, 0, 1, 2, 0, 1, 2]
    assert order.count(0) == 60
    assert order.count(1) == 8
    assert order.count(2) == 45


def test_balanced_spawn_order_can_rotate_first_lane():
    order = ghost_b._worker_lane_spawn_order(
        [60, 8, 45],
        disabled_lanes=set(),
        routing_policy="balanced",
        start_offset=1,
    )

    assert order[:9] == [1, 2, 0, 1, 2, 0, 1, 2, 0]
    assert order.count(0) == 60
    assert order.count(1) == 8
    assert order.count(2) == 45


def test_balanced_route_offset_rotates_across_documents():
    pool = [
        {"provider_preset": "vllm-rtx", "base_url": "http://rtx/v1", "model": "a"},
        {"provider_preset": "siliconflow", "base_url": "https://sf/v1", "model": "b"},
        {"provider_preset": "longcat", "base_url": "https://lc/v1", "model": "c"},
    ]

    ghost_b._BALANCED_ROUTE_OFFSETS.clear()
    first_lanes = [
        ghost_b._worker_lane_spawn_order(
            [60, 8, 45],
            disabled_lanes=set(),
            routing_policy="balanced",
            start_offset=ghost_b._next_balanced_route_offset(pool, "balanced"),
        )[0]
        for _ in range(4)
    ]

    assert first_lanes == [0, 1, 2, 0]


def test_primary_fallback_spawn_order_uses_only_first_healthy_lane():
    order = ghost_b._worker_lane_spawn_order(
        [60, 8, 45],
        disabled_lanes=set(),
        routing_policy="primary_fallback",
    )
    failover_order = ghost_b._worker_lane_spawn_order(
        [60, 8, 45],
        disabled_lanes={0},
        routing_policy="primary_fallback",
    )

    assert set(order) == {0}
    assert len(order) == 60
    assert set(failover_order) == {1}
    assert len(failover_order) == 8


@pytest.mark.asyncio
async def test_balanced_routing_records_calls_on_multiple_lanes(monkeypatch):
    calls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {
                    "total_tokens": 20,
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                '{"t":"e","cn":"alpha","sf":"Alpha",'
                                '"et":"concept","cf":0.95}\n{"t":"x"}'
                            )
                        },
                    }
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            calls.append(json["model"])
            await asyncio.sleep(0.001)
            return FakeResponse()

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=2,
        EXTRACTION_FOREGROUND_MAX_CALLS=2,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=2,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=4,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    report = await ghost_b.extract_entities(
        [
            ExtractionTask(
                chunk_id=f"c{i}",
                doc_id="d1",
                corpus_id="corp1",
                text="Alpha works.",
            )
            for i in range(4)
        ],
        pool=[
            {
                "provider_preset": "openai",
                "model": "lane-a",
                "base_url": "https://api.example-a.test/v1",
                "api_key": "test-a",
                "max_concurrent": 2,
                "extra_params": {
                    "routing_policy": "balanced",
                    "schema_mode": "jsonl",
                },
            },
            {
                "provider_preset": "deepseek",
                "model": "lane-b",
                "base_url": "https://api.example-b.test/v1",
                "api_key": "test-b",
                "max_concurrent": 2,
                "extra_params": {"schema_mode": "jsonl"},
            },
        ],
        return_report=True,
        enable_facts=False,
    )

    assert len(report.results) == 4
    assert report.metrics["routing_policy"] == "balanced"
    assert report.metrics["lane_call_counts"]["0"] > 0
    assert report.metrics["lane_call_counts"]["1"] > 0
    assert set(calls) == {"lane-a", "lane-b"}


@pytest.mark.asyncio
async def test_balanced_routing_rotates_single_chunk_documents(monkeypatch):
    calls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {
                    "total_tokens": 20,
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                '{"t":"e","cn":"alpha","sf":"Alpha",'
                                '"et":"concept","cf":0.95}\n{"t":"x"}'
                            )
                        },
                    }
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            calls.append(json["model"])
            return FakeResponse()

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=2,
        EXTRACTION_FOREGROUND_MAX_CALLS=2,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=2,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=2,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )
    pool = [
        {
            "provider_preset": "openai",
            "model": "lane-a",
            "base_url": "https://api.example-a.test/v1",
            "api_key": "test-a",
            "max_concurrent": 1,
            "extra_params": {
                "routing_policy": "balanced",
                "schema_mode": "jsonl",
            },
        },
        {
            "provider_preset": "deepseek",
            "model": "lane-b",
            "base_url": "https://api.example-b.test/v1",
            "api_key": "test-b",
            "max_concurrent": 1,
            "extra_params": {"schema_mode": "jsonl"},
        },
    ]
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    ghost_b._BALANCED_ROUTE_OFFSETS.clear()
    try:
        for idx in range(2):
            report = await ghost_b.extract_entities(
                [
                    ExtractionTask(
                        chunk_id=f"c{idx}",
                        doc_id=f"d{idx}",
                        corpus_id="corp1",
                        text="Alpha works.",
                    )
                ],
                pool=pool,
                return_report=True,
                enable_facts=False,
            )
            assert len(report.results) == 1
    finally:
        ghost_b._BALANCED_ROUTE_OFFSETS.clear()

    assert calls == ["lane-a", "lane-b"]


@pytest.mark.asyncio
async def test_lifecycle_shutdown_runs_when_worker_drain_raises(monkeypatch):
    ensure_calls = []
    shutdown_calls = []

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=2,
        EXTRACTION_FOREGROUND_MAX_CALLS=2,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=1,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=1,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )

    async def fake_ensure(pool, *, purpose):
        ensure_calls.append((pool, purpose))

    async def fake_shutdown(pool, *, purpose):
        shutdown_calls.append((pool, purpose))

    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(model_lifecycle, "ensure_model_lifecycle_ready", fake_ensure)
    monkeypatch.setattr(model_lifecycle, "shutdown_model_lifecycle", fake_shutdown)
    monkeypatch.setattr(
        ghost_b,
        "_worker_lane_spawn_order",
        lambda lane_limits, disabled_lanes, routing_policy, **_: [99],
    )

    with pytest.raises(IndexError):
        await ghost_b.extract_entities(
            [
                ExtractionTask(
                    chunk_id="c1",
                    doc_id="d1",
                    corpus_id="corp1",
                    text="Alpha works.",
                )
            ],
            pool=[
                {
                    "provider_preset": "openai",
                    "model": "managed-test",
                    "base_url": "https://api.example.test/v1",
                    "api_key": "test-key",
                    "max_concurrent": 1,
                    "lifecycle_base_url": "http://192.168.1.83:8085",
                    "lifecycle_auto_start": True,
                    "lifecycle_auto_stop": True,
                    "extra_params": {"schema_mode": "jsonl"},
                }
            ],
            return_report=True,
            enable_facts=False,
        )

    assert len(ensure_calls) == 1
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0][1] == "ghost_b"
