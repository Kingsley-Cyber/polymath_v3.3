#!/usr/bin/env python3
"""Summarize the measurable serial worker stages from the final container log."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from statistics import mean, median
from typing import Any


PHASE_RE = re.compile(
    r"phase=(parse|chunk|ghosts|mongo|embed|qdrant|neo4j) "
    r"duration=([0-9.]+)s doc=([0-9a-f]+)"
)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "total_seconds": round(sum(values), 3),
        "mean_seconds_per_book": round(mean(values), 3) if values else 0.0,
        "p50_seconds": round(median(values), 3) if values else 0.0,
        "p95_seconds": round(_percentile(values, 0.95), 3),
        "max_seconds": round(max(values, default=0.0), 3),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
    names = {
        str(row["doc_id"])[:12]: str(row["filename"])
        for row in metrics["documents"]
    }
    measured: dict[str, dict[str, float]] = {}
    for line in args.log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = PHASE_RE.search(line)
        if not match:
            continue
        phase, duration, doc_prefix = match.groups()
        measured.setdefault(doc_prefix[:12], {})[phase] = float(duration)
    unknown = sorted(prefix for prefix in measured if prefix not in names)
    if unknown:
        raise RuntimeError(f"worker log contains unknown document prefixes: {unknown}")
    if len(measured) != 12:
        raise RuntimeError(f"expected 12 post-rebuild document logs; observed={len(measured)}")
    per_document = []
    for prefix, phases in sorted(measured.items(), key=lambda item: names[item[0]]):
        per_document.append(
            {
                "doc_id_prefix": prefix,
                "filename": names[prefix],
                "phase_seconds": dict(sorted(phases.items())),
                "measured_serial_seconds": round(
                    sum(
                        value
                        for phase, value in phases.items()
                        if phase in {"chunk", "mongo", "embed", "qdrant", "neo4j"}
                    ),
                    3,
                ),
            }
        )
    phase_names = sorted({phase for phases in measured.values() for phase in phases})
    result = {
        "schema_version": "runpod_e2e_local_phase_metrics.v1",
        "measurement_scope": {
            "documents_in_preregistered_corpus": 15,
            "documents_in_current_container_log": len(measured),
            "missing_due_to_sealed_rebuilds": 3,
            "fresh_and_resume_paths_mixed": True,
            "classification": "VERIFIED partial-sample measurement",
        },
        "phase_distributions": {
            phase: _distribution(
                [phases[phase] for phases in measured.values() if phase in phases]
            )
            for phase in phase_names
        },
        "per_document": per_document,
        "interpretation": {
            "embed": "Mac MLX embedding compute",
            "qdrant": "Qdrant writes/promotion and tier-0 summary indexing",
            "neo4j": "bounded Neo4j graph write/promotion",
            "ghosts": (
                "combined concurrent Ghost A API-summary and Ghost B extraction wall; "
                "not a summary-only timing"
            ),
            "summary_only_timing": "not identifiable from worker phase logs",
        },
    }
    _atomic_write(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
