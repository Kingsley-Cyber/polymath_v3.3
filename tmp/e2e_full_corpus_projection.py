"""Project measured E2E extraction economics to 100/300/500 books."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any


BOOK_COUNTS = (100, 300, 500)
FLEETS = (10, 20, 50, 100)
BATCH_SIZES = (32, 64, 128)
RATE_USD_PER_SECOND = 0.00031
OVERHEAD_MULTIPLIER = 1.5


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _fit_nonnegative(rows: list[dict[str, Any]]) -> dict[str, float]:
    x = [
        (
            float(row["request_jobs"]),
            float(row["requested_chunks"]),
            float(row["worker_seconds"]),
        )
        for row in rows
    ]
    jj = sum(jobs * jobs for jobs, _, _ in x)
    tt = sum(tasks * tasks for _, tasks, _ in x)
    jt = sum(jobs * tasks for jobs, tasks, _ in x)
    jy = sum(jobs * seconds for jobs, _, seconds in x)
    ty = sum(tasks * seconds for _, tasks, seconds in x)
    determinant = jj * tt - jt * jt
    candidates: list[tuple[float, float]] = []
    if determinant > 0:
        per_request = (jy * tt - ty * jt) / determinant
        per_task = (ty * jj - jy * jt) / determinant
        if per_request >= 0 and per_task >= 0:
            candidates.append((per_request, per_task))
    candidates.extend(
        [
            (max(0.0, jy / jj) if jj else 0.0, 0.0),
            (0.0, max(0.0, ty / tt) if tt else 0.0),
        ]
    )

    def sse(candidate: tuple[float, float]) -> float:
        per_request, per_task = candidate
        return sum(
            (seconds - (per_request * jobs + per_task * tasks)) ** 2
            for jobs, tasks, seconds in x
        )

    per_request, per_task = min(candidates, key=sse)
    residual = sse((per_request, per_task))
    mean = sum(seconds for _, _, seconds in x) / len(x)
    total = sum((seconds - mean) ** 2 for _, _, seconds in x)
    return {
        "fixed_worker_seconds_per_request": per_request,
        "worker_seconds_per_task": per_task,
        "r_squared": 1.0 - residual / total if total else 1.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
    documents = list(metrics["documents"])
    if len(documents) != 15:
        raise RuntimeError("projection requires the closed 15-document metric set")
    fit = _fit_nonnegative(documents)
    tasks_per_book = sum(float(row["requested_chunks"]) for row in documents) / 15.0
    cold_p95 = float(metrics["delay_seconds"]["first_wave_per_document"]["p95"])
    scenarios: list[dict[str, Any]] = []
    for books in BOOK_COUNTS:
        projected_tasks = int(math.ceil(tasks_per_book * books))
        for batch_size in BATCH_SIZES:
            requests_per_book = sum(
                math.ceil(float(row["requested_chunks"]) / batch_size)
                for row in documents
            ) / 15.0
            projected_requests = int(math.ceil(requests_per_book * books))
            worker_seconds = (
                fit["fixed_worker_seconds_per_request"] * projected_requests
                + fit["worker_seconds_per_task"] * projected_tasks
            )
            cost = worker_seconds * RATE_USD_PER_SECOND * OVERHEAD_MULTIPLIER
            for fleet in FLEETS:
                ideal_seconds = worker_seconds / fleet
                cold_aware_seconds = ideal_seconds + cold_p95
                scenarios.append(
                    {
                        "books": books,
                        "batch_size": batch_size,
                        "fleet": fleet,
                        "projected_tasks": projected_tasks,
                        "projected_requests": projected_requests,
                        "projected_worker_seconds": round(worker_seconds, 3),
                        "projected_compute_cost_usd": round(cost, 3),
                        "ideal_steady_wall_seconds": round(ideal_seconds, 1),
                        "cold_p95_added_seconds": round(cold_p95, 1),
                        "cold_aware_wall_seconds": round(cold_aware_seconds, 1),
                        "cold_aware_wall_minutes": round(cold_aware_seconds / 60.0, 2),
                    }
                )
    fifteen_tasks = int(math.ceil(tasks_per_book * 15))
    fifteen_requests_64 = int(
        math.ceil(
            sum(
                math.ceil(float(row["requested_chunks"]) / 64)
                for row in documents
            )
        )
    )
    fifteen_worker_seconds_64 = (
        fit["fixed_worker_seconds_per_request"] * fifteen_requests_64
        + fit["worker_seconds_per_task"] * fifteen_tasks
    )
    steady_fleet_for_three_minutes = math.ceil(fifteen_worker_seconds_64 / 180.0)
    cold_aware_denominator = 180.0 - cold_p95
    cold_aware_fleet_for_three_minutes = (
        math.ceil(fifteen_worker_seconds_64 / cold_aware_denominator)
        if cold_aware_denominator > 0
        else None
    )
    result = {
        "schema_version": "runpod_e2e_full_corpus_projection.v1",
        "measurement_basis": {
            "books": 15,
            "configured_batch_size": 32,
            "configured_fleet": int(metrics["fleet"]["configured_workers"]),
            "measured_tasks": int(
                sum(int(row["requested_chunks"]) for row in documents)
            ),
            "measured_requests": int(metrics["requests"]["terminal_completed"]),
            "measured_worker_seconds": float(metrics["worker_seconds"]),
            "tasks_per_book": round(tasks_per_book, 3),
            "first_wave_delay_p95_seconds": cold_p95,
        },
        "nonnegative_document_level_fit": {
            key: round(value, 9) for key, value in fit.items()
        },
        "projection_law": (
            "worker_seconds = fitted_request_overhead * projected_requests + "
            "fitted_task_seconds * projected_tasks; wall = worker_seconds/fleet + "
            "measured first-wave delay p95"
        ),
        "scenario_classification": {
            "batch_32": "VERIFIED measurement-aligned projection",
            "batch_64": "INFERRED; adapter permits up to 64 but requires a production memory canary",
            "batch_128": "ASSUMED planning sensitivity only; current adapter caps at 64",
            "wall_clock": "INFERRED extraction-only wave utilization; excludes serial local stages",
        },
        "scenarios": scenarios,
        "recommendation": {
            "fleet": 100,
            "batch_size": 64,
            "reason": (
                "64 is the current adapter ceiling and fleet 100 is the largest requested "
                "planning row; validate with a bounded memory canary before cutover"
            ),
            "exact_total_worker_quota_ask": 100,
            "exact_per_account_quota_ask_for_two_equal_accounts": 50,
        },
        "owner_three_minute_15_book_target": {
            "batch_size": 64,
            "projected_tasks": fifteen_tasks,
            "projected_requests": fifteen_requests_64,
            "projected_worker_seconds": round(fifteen_worker_seconds_64, 3),
            "steady_state_fleet_required": steady_fleet_for_three_minutes,
            "cold_p95_aware_fleet_required": cold_aware_fleet_for_three_minutes,
            "prewarm_required": cold_p95 >= 60.0,
        },
    }
    _atomic_write(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
