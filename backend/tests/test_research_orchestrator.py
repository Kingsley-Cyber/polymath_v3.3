from __future__ import annotations

import asyncio

import pytest

from services.research.orchestrator import ResearchLaneSpec, run_research_lanes


async def _empty_list():
    return []


@pytest.mark.asyncio
async def test_research_lanes_retry_and_fallback_without_collapsing_bundle():
    calls = {"retrieval": 0}

    async def flaky_retrieval():
        calls["retrieval"] += 1
        if calls["retrieval"] == 1:
            raise RuntimeError("temporary retrieval pressure")
        return ["evidence"]

    async def failing_graph():
        raise TimeoutError("graph lane timed out")

    outcomes = await run_research_lanes(
        [
            ResearchLaneSpec(
                name="retrieval",
                run=flaky_retrieval,
                max_attempts=2,
                required=True,
            ),
            ResearchLaneSpec(
                name="graph",
                run=failing_graph,
                fallback=_empty_list,
                max_attempts=1,
            ),
        ],
        max_concurrency=2,
    )

    assert outcomes[0].name == "retrieval"
    assert outcomes[0].status == "success"
    assert outcomes[0].attempts == 2
    assert outcomes[0].result == ["evidence"]
    assert outcomes[1].name == "graph"
    assert outcomes[1].status == "fallback"
    assert outcomes[1].error_type == "TimeoutError"
    assert outcomes[1].receipt()["item_count"] == 0


@pytest.mark.asyncio
async def test_research_lanes_respect_max_concurrency():
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def lane():
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        return ["ok"]

    outcomes = await run_research_lanes(
        [ResearchLaneSpec(name=f"lane-{index}", run=lane) for index in range(6)],
        max_concurrency=2,
    )

    assert all(outcome.status == "success" for outcome in outcomes)
    assert max_active <= 2
