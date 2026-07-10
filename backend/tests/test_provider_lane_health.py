import pytest

from services.ingestion.provider_lane_health import (
    adapt_extraction_pool_concurrency,
    filter_extraction_pool_by_provider_health,
    summarize_provider_lane_health,
)
from services.ingestion.worker import _build_ghost_b_error_event_sink


class _InsertCollection:
    def __init__(self):
        self.rows = []

    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        return type("Result", (), {"inserted_id": "fake"})()


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


def _rate_event(provider: str, model: str, lane: int) -> dict:
    return {
        "event": "ghost_b_attempt_rate_limited",
        "provider_card": {"provider": provider, "model": model},
        "model": model,
        "lane": lane,
        "error_type": "rate_limited",
        "error_message": "HTTP 429",
    }


def test_provider_lane_health_cools_only_rate_limited_provider_lane():
    events = [
        *[_rate_event("longcat", "openai/LongCat-2.0", 1) for _ in range(5)],
        {
            "event": "ghost_b_attempt_succeeded",
            "provider_card": {"provider": "longcat", "model": "openai/LongCat-2.0"},
            "model": "openai/LongCat-2.0",
            "lane": 2,
        },
        {
            "event": "ghost_b_attempt_succeeded",
            "provider_card": {
                "provider": "local_private_vllm",
                "model": "openai/polymath-extract",
            },
            "model": "openai/polymath-extract",
            "lane": 0,
        },
    ]

    health = summarize_provider_lane_health(
        events,
        min_rate_limit_events=5,
        rate_limit_ratio=0.50,
    )
    assert health["status"] == "degraded"
    assert "longcat|openai/longcat-2.0" not in health["cooldown_keys"]
    assert "longcat|openai/longcat-2.0|lane:1" in health["cooldown_keys"]
    assert "local_private_vllm|openai/polymath-extract" not in health["cooldown_keys"]

    pool = [
        {
            "model": "openai/polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
            "max_concurrent": 60,
        },
        {
            "provider": "longcat",
            "model": "openai/LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
            "max_concurrent": 45,
        },
        {
            "provider": "longcat",
            "model": "openai/LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
            "max_concurrent": 45,
        },
        {
            "provider": "siliconflow",
            "model": "openai/tencent/Hy3",
            "base_url": "https://api.siliconflow.com/v1",
            "max_concurrent": 8,
        },
    ]
    filtered, skipped = filter_extraction_pool_by_provider_health(pool, health)

    assert [entry["model"] for entry in filtered] == [
        "openai/polymath-extract",
        "openai/LongCat-2.0",
        "openai/tencent/Hy3",
    ]
    assert skipped == [
        {
            "lane": 1,
            "provider": "longcat",
            "model": "openai/LongCat-2.0",
            "reason": "provider_rate_limited",
        }
    ]


def test_provider_lane_health_uses_provider_wide_cooldown_without_lane_identity():
    events = [
        {
            "event": "ghost_b_attempt_rate_limited",
            "provider_card": {"provider": "longcat", "model": "openai/LongCat-2.0"},
            "model": "openai/LongCat-2.0",
            "error_type": "rate_limited",
            "error_message": "HTTP 429",
        }
        for _ in range(5)
    ]

    health = summarize_provider_lane_health(events, min_rate_limit_events=5)
    assert health["status"] == "degraded"
    assert health["cooldown_keys"] == ["longcat|openai/longcat-2.0"]


def test_provider_lane_health_never_empties_extraction_pool():
    health = summarize_provider_lane_health(
        [_rate_event("longcat", "openai/LongCat-2.0", 0) for _ in range(5)],
        min_rate_limit_events=5,
    )
    pool = [
        {
            "provider": "longcat",
            "model": "openai/LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
            "max_concurrent": 45,
        }
    ]

    filtered, skipped = filter_extraction_pool_by_provider_health(pool, health)

    assert filtered == pool
    assert skipped == []


def test_longcat_concurrency_starts_as_canary_and_earns_bounded_ramp():
    pool = [
        {
            "provider": "longcat",
            "model": "openai/LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
            "max_concurrent": 45,
            "extra_params": {},
        }
    ]

    cold, cold_adjustments = adapt_extraction_pool_concurrency(pool, {"lanes": []})
    assert cold[0]["max_concurrent"] == 2
    assert cold_adjustments[0]["reasons"] == ["provider_canary_ramp"]

    health = {
        "lanes": [
            {
                "key": "longcat|openai/longcat-2.0",
                "succeeded": 30,
                "rate_limited": 0,
                "rate_limit_ratio": 0.0,
            }
        ]
    }
    ramped, _ = adapt_extraction_pool_concurrency(pool, health)
    assert ramped[0]["max_concurrent"] == 4

    trusted_pool = [
        {
            **pool[0],
            "extra_params": {"provider_canary_passed": True},
        }
    ]
    trusted, adjustments = adapt_extraction_pool_concurrency(trusted_pool, health)
    assert trusted[0]["max_concurrent"] == 45
    assert adjustments == []


@pytest.mark.asyncio
async def test_ghost_b_audit_sink_persists_rate_limit_events():
    coll = _InsertCollection()
    db = _FakeDb({"ghost_b_error_events": coll})
    sink = _build_ghost_b_error_event_sink(db, run_id="run-1")

    assert sink is not None
    await sink(
        {
            "event": "ghost_b_attempt_rate_limited",
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "provider_card": {"provider": "longcat", "model": "LongCat-2.0"},
        }
    )

    assert len(coll.rows) == 1
    assert coll.rows[0]["event"] == "ghost_b_attempt_rate_limited"
    assert coll.rows[0]["run_id"] == "run-1"
    assert coll.rows[0]["sample_index"] == 1
