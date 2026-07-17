#!/usr/bin/env python3
"""Sustained, read-only query-embedding soak using the production client."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections.abc import Sequence

from services import embedder


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    return ordered[max(0, index)]


async def run_soak(*, calls: int, concurrency: int) -> dict:
    if calls < 1 or concurrency < 1:
        raise ValueError("calls and concurrency must both be positive")

    preflight = await embedder.preflight_local_embedder_for_eval_batch()
    semaphore = asyncio.Semaphore(concurrency)
    latencies_ms: list[float] = []
    failures: list[dict[str, str | int]] = []
    dimensions: set[int] = set()

    async def one(index: int) -> None:
        text = (
            "Sustained retrieval embedding stability probe "
            f"{index:04d}; unique cache-bypass token mlx-soak-{index:04d}."
        )
        started = time.perf_counter()
        try:
            async with semaphore:
                vector = await embedder.embed_query(text)
            latency_ms = (time.perf_counter() - started) * 1000
            latencies_ms.append(latency_ms)
            dimensions.add(len(vector))
            if len(vector) != int(embedder.get_settings().EMBEDDING_DIMENSION):
                raise ValueError(f"unexpected dimension {len(vector)}")
            if not all(math.isfinite(float(value)) for value in vector):
                raise ValueError("non-finite vector value")
        except Exception as exc:
            failures.append(
                {
                    "index": index,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )

    started = time.perf_counter()
    await asyncio.gather(*(one(index) for index in range(calls)))
    wall_seconds = time.perf_counter() - started
    result = {
        "schema_version": "polymath.mlx_embedder_soak.v1",
        "preflight": preflight,
        "requested_calls": calls,
        "successful_calls": len(latencies_ms),
        "failed_calls": len(failures),
        "concurrency": concurrency,
        "wall_seconds": round(wall_seconds, 3),
        "requests_per_minute": round(
            (len(latencies_ms) / wall_seconds) * 60 if wall_seconds else 0.0,
            3,
        ),
        "latency_ms": {
            "min": round(min(latencies_ms), 3) if latencies_ms else None,
            "p50": round(statistics.median(latencies_ms), 3) if latencies_ms else None,
            "p95": round(_percentile(latencies_ms, 0.95), 3) if latencies_ms else None,
            "max": round(max(latencies_ms), 3) if latencies_ms else None,
        },
        "dimensions": sorted(dimensions),
        "failures": failures,
        "status": "pass" if not failures and len(latencies_ms) == calls else "fail",
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=3)
    return parser


async def _main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = await run_soak(calls=args.calls, concurrency=args.concurrency)
    except Exception as exc:
        result = {
            "schema_version": "polymath.mlx_embedder_soak.v1",
            "status": "preflight_abort",
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
