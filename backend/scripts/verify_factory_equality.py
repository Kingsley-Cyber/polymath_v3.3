"""Factory equality gate (owner-ratified 2026-08-08).

    serial Graphify output digest == parallel factory output digest

Parallelism may change WHEN work happens, never WHAT semantic output exists.
This harness reconstructs the exact eligible units a frozen extraction saw
(from its persisted propositions), runs the OpenIE stage once serially and
once through the N-process farm, and compares the stage identity digests.

Usage:
    verify_factory_equality.py --frozen-dir <dir> [--workers 4]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from replay_downstream import _load_frozen_from_dir  # noqa: E402
from pathlib import Path  # noqa: E402

from models.graphify_contracts import NormalizedDocumentV1, OpenIERawPropositionV1  # noqa: E402
from services.extraction.graphify_openie import (  # noqa: E402
    OpenIEUnit,
    get_triplet_extract_cpu_provider,
    run_openie_extraction,
)


def units_from_frozen(frozen: dict) -> tuple[NormalizedDocumentV1, list[OpenIEUnit]]:
    document = NormalizedDocumentV1.model_validate(frozen["document"])
    seen: dict[str, OpenIEUnit] = {}
    for item in frozen["propositions"]:
        proposition = OpenIERawPropositionV1.model_validate(item)
        if proposition.unit_id not in seen:
            seen[proposition.unit_id] = OpenIEUnit(
                unit_id=proposition.unit_id,
                document_id=proposition.document_id,
                start=proposition.evidence_start,
                end=proposition.evidence_end,
                text=proposition.evidence_text,
                eligible=True,
            )
    units = sorted(seen.values(), key=lambda unit: (unit.start, unit.unit_id))
    return document, units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-dir", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    frozen = _load_frozen_from_dir(Path(args.frozen_dir))
    document, units = units_from_frozen(frozen)
    provider = get_triplet_extract_cpu_provider()
    print(f"{len(units)} eligible units from {args.frozen_dir}")

    os.environ["GRAPHIFY_OPENIE_WORKERS"] = "1"
    started = time.perf_counter()
    serial = run_openie_extraction([document], units, provider)
    serial_seconds = time.perf_counter() - started
    print(f"serial: {len(serial.propositions)} propositions in {serial_seconds:.1f}s "
          f"(engine={serial.report['engine']})")

    os.environ["GRAPHIFY_OPENIE_WORKERS"] = str(args.workers)
    started = time.perf_counter()
    parallel = run_openie_extraction([document], units, provider)
    parallel_seconds = time.perf_counter() - started
    print(f"farm({args.workers}): {len(parallel.propositions)} propositions in "
          f"{parallel_seconds:.1f}s (engine={parallel.report['engine']})")

    equal = serial.report["identity_digest"] == parallel.report["identity_digest"]
    print(f"digest serial   : {serial.report['identity_digest'][:24]}")
    print(f"digest parallel : {parallel.report['identity_digest'][:24]}")
    print(f"failures serial/parallel: {serial.report['explicit_openie_failures']}"
          f"/{parallel.report['explicit_openie_failures']}")
    speedup = serial_seconds / parallel_seconds if parallel_seconds else float("inf")
    print(f"speedup: {speedup:.2f}x")
    print("EQUALITY GATE:", "PASS — identical semantic output" if equal else "FAIL")
    return 0 if equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
