"""Build exact, durable-ID-resolved final metrics for the 15-doc RunPod burst."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings


STATE = Path("/data/ingest-files/runpod-job-journals/e2e-launch-state.json")
JOURNAL_ROOT = Path("/data/ingest-files/runpod-job-journals")
SCHEMA = "runpod_e2e_burst_metrics.v1"
PREREGISTERED_REQUEST_ESTIMATE = 709
FLEET = 20
RATE_USD_PER_SECOND = 0.00031
OVERHEAD = 1.5


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "min": round(min(values, default=0.0), 3),
        "p50": round(median(values), 3) if values else 0.0,
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values, default=0.0), 3),
    }


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(STATE.read_text(encoding="utf-8"))
    corpus_id = str(state["corpus_id"])
    corpus_hash = hashlib.sha256(corpus_id.encode("utf-8")).hexdigest()
    journal_path = JOURNAL_ROOT / f"corpus-{corpus_hash}.jsonl"
    rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    submitted = [row for row in rows if row.get("event") == "submitted"]
    terminal = [row for row in rows if row.get("event") == "terminal"]
    submitted_by_id = {str(row.get("job_id") or ""): row for row in submitted}
    terminal_by_id = {str(row.get("job_id") or ""): row for row in terminal}
    if (
        not submitted
        or len(submitted) != len(terminal)
        or len(submitted_by_id) != len(submitted)
        or len(terminal_by_id) != len(terminal)
        or set(submitted_by_id) != set(terminal_by_id)
    ):
        raise RuntimeError(
            "RunPod journal does not close at unique submitted/terminal jobs"
        )
    if any(str(row.get("status") or "") != "COMPLETED" for row in terminal):
        raise RuntimeError("RunPod journal has a non-COMPLETED terminal")

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        database = client[settings.MONGODB_DATABASE]
        documents = await database["documents"].find(
            {"corpus_id": corpus_id},
            {
                "_id": 0,
                "doc_id": 1,
                "filename": 1,
                "original_filename": 1,
                "ghost_b_metrics": 1,
                "write_state.verified": 1,
            },
        ).to_list(length=20)
        if len(documents) != 15:
            raise RuntimeError(f"document closure is not 15: {len(documents)}")
        job_to_doc: dict[str, str] = {}
        per_doc: list[dict[str, Any]] = []
        active_wall_seconds = 0.0
        steady_jobs = 0
        steady_seconds = 0.0
        first_wave_delays: list[float] = []
        steady_delays: list[float] = []
        for document in sorted(documents, key=lambda row: str(row.get("doc_id") or "")):
            if (document.get("write_state") or {}).get("verified") is not True:
                raise RuntimeError("document is not verified at burst finalization")
            metrics = document.get("ghost_b_metrics") or {}
            remote_jobs = metrics.get("remote_jobs") or []
            job_ids = [str(row.get("job_id") or "") for row in remote_jobs]
            if not job_ids or "" in job_ids or len(job_ids) != len(set(job_ids)):
                raise RuntimeError("document remote-job identity closure failed")
            for job_id in job_ids:
                if job_id in job_to_doc:
                    raise RuntimeError("RunPod job mapped to multiple documents")
                if job_id not in terminal_by_id:
                    raise RuntimeError("document remote job is absent from journal")
                job_to_doc[job_id] = str(document["doc_id"])
            terminals = sorted(
                (terminal_by_id[job_id] for job_id in job_ids),
                key=lambda row: _timestamp(str(row["timestamp_utc"])),
            )
            starts = [_timestamp(str(submitted_by_id[job_id]["timestamp_utc"])) for job_id in job_ids]
            ends = [_timestamp(str(terminal_by_id[job_id]["timestamp_utc"])) for job_id in job_ids]
            wall = max(ends) - min(starts)
            active_wall_seconds += wall
            first_wave_count = min(FLEET, len(terminals))
            first_wave = terminals[:first_wave_count]
            steady = terminals[first_wave_count:]
            first_wave_delays.extend(
                float(row.get("delay_time_ms") or 0.0) / 1000.0 for row in first_wave
            )
            steady_delays.extend(
                float(row.get("delay_time_ms") or 0.0) / 1000.0 for row in steady
            )
            doc_steady_seconds = 0.0
            if steady:
                boundary = _timestamp(str(first_wave[-1]["timestamp_utc"]))
                doc_steady_seconds = max(
                    0.0,
                    _timestamp(str(steady[-1]["timestamp_utc"])) - boundary,
                )
                steady_jobs += len(steady)
                steady_seconds += doc_steady_seconds
            per_doc.append(
                {
                    "doc_id": str(document["doc_id"]),
                    "filename": str(
                        document.get("original_filename")
                        or document.get("filename")
                        or ""
                    ),
                    "requested_chunks": int(metrics.get("requested_chunks") or 0),
                    "request_jobs": len(job_ids),
                    "worker_seconds": round(
                        sum(
                            float(terminal_by_id[job_id].get("execution_time_ms") or 0.0)
                            for job_id in job_ids
                        )
                        / 1000.0,
                        3,
                    ),
                    "active_wall_seconds": round(wall, 3),
                    "steady_tail_jobs": len(steady),
                    "steady_tail_seconds": round(doc_steady_seconds, 3),
                    "jobs_by_account": dict(
                        sorted(
                            Counter(
                                str(terminal_by_id[job_id].get("account_name") or "")
                                for job_id in job_ids
                            ).items()
                        )
                    ),
                }
            )
        if set(job_to_doc) != set(terminal_by_id):
            raise RuntimeError("durable document IDs do not resolve every journal job")
        durable_request_jobs = sum(
            int((document.get("ghost_b_metrics") or {}).get("request_batches") or 0)
            for document in documents
        )
        if durable_request_jobs != len(terminal):
            raise RuntimeError(
                "durable document request-batch total does not match journal closure"
            )
        worker_seconds = sum(
            float(row.get("execution_time_ms") or 0.0) for row in terminal
        ) / 1000.0
        delays = [float(row.get("delay_time_ms") or 0.0) / 1000.0 for row in terminal]
        first_dispatch = min(
            _timestamp(str(row["timestamp_utc"])) for row in submitted
        )
        last_terminal = max(
            _timestamp(str(row["timestamp_utc"])) for row in terminal
        )
        overall_wall = last_terminal - first_dispatch
        observed_jobs = len(terminal)
        active_requests_per_minute = observed_jobs / active_wall_seconds * 60.0
        steady_requests_per_minute = (
            steady_jobs / steady_seconds * 60.0 if steady_seconds else 0.0
        )
        required_rpm = observed_jobs / 3.0
        observed_rpm_for_scale = active_requests_per_minute
        extrapolated_fleet = math.ceil(
            FLEET * required_rpm / observed_rpm_for_scale
        )
        result = {
            "schema_version": SCHEMA,
            "corpus_id": corpus_id,
            "durable_id_resolution": {
                "method": "documents.ghost_b_metrics.remote_jobs.job_id_to_fsynced_journal_job_id",
                "resolved_jobs": len(job_to_doc),
                "unresolved_jobs": 0,
                "ordinal_coincidence_used": False,
            },
            "fleet": {
                "configured_workers": FLEET,
                "primary_max": 10,
                "secondary_max": 10,
            },
            "requests": {
                "preregistered_no_write_estimate": PREREGISTERED_REQUEST_ESTIMATE,
                "submitted": len(submitted),
                "terminal_completed": len(terminal),
                "failed": 0,
                "by_account": dict(
                    sorted(
                        Counter(str(row.get("account_name") or "") for row in terminal).items()
                    )
                ),
            },
            "timing": {
                "overall_wall_seconds_first_dispatch_to_last_terminal": round(
                    overall_wall, 3
                ),
                "active_extraction_wall_seconds_sum": round(active_wall_seconds, 3),
                "active_requests_per_minute": round(active_requests_per_minute, 3),
                "steady_state_definition": (
                    "per-document terminal tail after the first 20 terminal jobs; "
                    "durations summed without inter-document graph/embed gaps"
                ),
                "steady_tail_jobs": steady_jobs,
                "steady_tail_seconds": round(steady_seconds, 3),
                "steady_state_requests_per_minute": round(
                    steady_requests_per_minute, 3
                ),
            },
            "delay_seconds": {
                "all": _distribution(delays),
                "first_wave_per_document": _distribution(first_wave_delays),
                "steady_tail": _distribution(steady_delays),
            },
            "worker_seconds": round(worker_seconds, 3),
            "estimated_compute_cost_usd": round(
                worker_seconds * RATE_USD_PER_SECOND * OVERHEAD, 9
            ),
            "cost_basis": {
                "estimated_only": True,
                "rate_usd_per_execution_second": RATE_USD_PER_SECOND,
                "overhead_multiplier": OVERHEAD,
            },
            "three_minute_extrapolation": {
                "target_seconds": 180,
                "required_requests_per_minute": round(required_rpm, 3),
                "observed_active_requests_per_minute": round(
                    observed_rpm_for_scale, 3
                ),
                "linearly_extrapolated_fleet": extrapolated_fleet,
                "perfect_utilization_worker_second_lower_bound": math.ceil(
                    worker_seconds / 180.0
                ),
                "caveat": (
                    "linear fleet estimate uses measured active per-document burst rate; "
                    "cold pulls and the sequential durable document pipeline make it optimistic"
                ),
            },
            "documents": per_doc,
        }
        _atomic_write(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        client.close()


asyncio.run(main())
