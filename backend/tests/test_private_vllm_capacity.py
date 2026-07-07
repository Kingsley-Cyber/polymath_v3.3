from __future__ import annotations

from services.private_vllm_capacity import (
    parse_private_vllm_status,
    plan_private_vllm_concurrency,
)


def test_parse_status_accepts_vram_aliases_and_derives_free():
    capacity = parse_private_vllm_status(
        {
            "ready": True,
            "vram_total_gb": 94.5,
            "vram_used_gb": 43.2,
            "running_requests": 2,
            "waiting_requests": 1,
        }
    )

    assert capacity.ready is True
    assert capacity.gpu_vram_total_gb == 94.5
    assert round(capacity.gpu_vram_free_gb or 0, 1) == 51.3
    assert capacity.running_requests == 2
    assert capacity.waiting_requests == 1


def test_plan_prefers_server_recommended_concurrency():
    capacity = parse_private_vllm_status(
        {"ready": True, "gpu_vram_free_gb": 20, "recommended_concurrency": 37}
    )

    effective, meta = plan_private_vllm_concurrency(60, capacity)

    assert effective == 37
    assert meta["reason"] == "server_recommended"


def test_plan_uses_85_percent_free_vram_budget_when_estimate_available():
    capacity = parse_private_vllm_status({"ready": True, "gpu_vram_free_gb": 10})

    effective, meta = plan_private_vllm_concurrency(
        60,
        capacity,
        safety_ratio=0.85,
        per_request_vram_gb=0.5,
    )

    assert effective == 17
    assert meta["reason"] == "vram_budget"


def test_plan_holds_to_minimum_when_not_ready():
    capacity = parse_private_vllm_status({"ready": False, "recommended_concurrency": 60})

    effective, meta = plan_private_vllm_concurrency(60, capacity, min_concurrency=1)

    assert effective == 1
    assert meta["reason"] == "not_ready"
