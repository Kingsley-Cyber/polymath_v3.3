import asyncio
import importlib.util
from pathlib import Path

import pytest


def _load_sidecar_module():
    path = (
        Path(__file__).parents[2]
        / "scripts"
        / "apple_ml_services"
        / "embedder_mlx"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location("embedder_mlx_priority_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_priority_gate_admits_interactive_before_waiting_backfill():
    module = _load_sidecar_module()
    gate = module.PriorityRequestGate()
    order: list[str] = []

    await gate.acquire("document_ingestion", timeout=1.0)

    async def waiter(name: str, workload_class: str):
        await gate.acquire(workload_class, timeout=1.0)
        order.append(name)
        await gate.release()

    backfill = asyncio.create_task(waiter("backfill", "backfill_repair"))
    interactive = asyncio.create_task(waiter("interactive", "interactive_query"))
    await asyncio.sleep(0)
    assert gate.queue_depth == 2

    await gate.release()
    await asyncio.gather(backfill, interactive)

    assert order == ["interactive", "backfill"]
