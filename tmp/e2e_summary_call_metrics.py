#!/usr/bin/env python3
"""Secret-free timing and cost census for the E2E summary call ledger."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from statistics import mean, median
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")


def _seconds(start: Any, end: Any) -> float:
    if isinstance(start, datetime) and isinstance(end, datetime):
        return max(0.0, (end - start).total_seconds())
    return 0.0


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


async def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    run_id = str(state["batch_id"])
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        receipts = await database["summary_cost_call_receipts"].find(
            {"run_id": run_id},
            {
                "_id": 0,
                "provider": 1,
                "model": 1,
                "route_id": 1,
                "item_count": 1,
                "actual_nanos": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "status": 1,
                "created_at": 1,
                "settled_at": 1,
            },
        ).to_list(length=None)
        if not receipts:
            raise RuntimeError("summary receipt ledger is empty")
        bad = [row for row in receipts if str(row.get("status") or "") != "settled"]
        if bad:
            raise RuntimeError(f"summary receipt ledger has non-settled rows: {len(bad)}")
        latencies = [
            _seconds(row.get("created_at"), row.get("settled_at"))
            for row in receipts
        ]
        missing_latency = sum(1 for value in latencies if value <= 0)
        positive = [value for value in latencies if value > 0]
        cost_usd = sum(int(row.get("actual_nanos") or 0) for row in receipts) / 1e9
        result = {
            "schema_version": "runpod_e2e_summary_call_metrics.v1",
            "run_id": run_id,
            "calls": len(receipts),
            "items": sum(int(row.get("item_count") or 0) for row in receipts),
            "providers": sorted({str(row.get("provider") or "") for row in receipts}),
            "models": sorted({str(row.get("model") or "") for row in receipts}),
            "routes": sorted({str(row.get("route_id") or "") for row in receipts}),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in receipts),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in receipts),
            "accounted_cost_usd": round(cost_usd, 9),
            "average_cost_usd_per_book": round(cost_usd / 15.0, 9),
            "average_calls_per_book": round(len(receipts) / 15.0, 3),
            "provider_call_latency_seconds": {
                "count": len(positive),
                "missing_or_zero": missing_latency,
                "total_call_seconds": round(sum(positive), 3),
                "mean": round(mean(positive), 3) if positive else 0.0,
                "p50": round(median(positive), 3) if positive else 0.0,
                "p95": round(_percentile(positive, 0.95), 3),
                "max": round(max(positive, default=0.0), 3),
                "average_call_seconds_per_book": round(sum(positive) / 15.0, 3),
            },
            "timing_caveat": (
                "call-seconds are provider request latencies and can overlap; they are not "
                "serial wall-clock. The worker log does not isolate Ghost A from concurrent "
                "Ghost B extraction."
            ),
            "secret_values_emitted": 0,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        client.close()


asyncio.run(main())
