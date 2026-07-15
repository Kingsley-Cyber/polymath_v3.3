#!/usr/bin/env python3
"""Probe one preregistered task through the exact custom-image runtime.

Run this script inside the built image with the repository mounted read-only.
It reads only fixture ID/text, verifies immutable source identities, and
executes the real offline spaCy + GLiNER path without provider or data writes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(repo: Path, worker_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(worker_root))
    from runtime import DETERMINISM_PROFILE, extract_local_batch

    spec_path = repo / "backend/evals/runpod_same_chunk_lockdown_v1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source = repo / spec["source_fixture"]["path"]
    if sha256(source) != spec["source_fixture"]["sha256"]:
        raise RuntimeError("source fixture identity mismatch")
    fixture = json.loads(source.read_text(encoding="utf-8"))
    sample_id = str(spec["source_fixture"]["sample_ids"][0])
    sample = next(row for row in fixture["samples"] if str(row["id"]) == sample_id)
    baseline_path = (
        repo / "docs/baselines/RUNPOD_SAME_CHUNK_LOCAL_REFERENCE_2026-07-15.json"
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("spec_sha256") != sha256(spec_path):
        raise RuntimeError("baseline/spec identity mismatch")
    identity = baseline["runtime_identity"]
    task = {
        "document_id": f"doc:runpod-lockdown:{sample_id}",
        "child_id": f"child:runpod-lockdown:{sample_id}",
        "source_version_id": f"srcv:runpod-lockdown:{sample_id}",
        "text": str(sample["text"]),
    }
    payload = {
        "contract_version": "polymath.runpod_local_extraction.v1",
        "batch_id": "runpod-lockdown:exact-image-probe",
        "model_id": identity["gliner_model_id"],
        "model_revision": identity["gliner_model_revision"],
        "spacy_pipeline": identity["spacy_model"],
        "asset_contract": identity["asset_contract"],
        "determinism_profile": DETERMINISM_PROFILE,
        "tasks": [task],
    }
    output = extract_local_batch(payload)
    if output.get("contract_version") != payload["contract_version"]:
        raise RuntimeError("runtime probe contract mismatch")
    results = output.get("results") or []
    if len(results) != 1:
        raise RuntimeError("runtime probe result cardinality mismatch")
    result = results[0]
    if any(result.get(key) != task[key] for key in ("document_id", "child_id")):
        raise RuntimeError("runtime probe durable identity mismatch")
    extraction = result.get("extraction") or {}
    for entity in extraction.get("entities") or []:
        start, end = int(entity["start_char"]), int(entity["end_char"])
        if task["text"][start:end] != entity["text"]:
            raise RuntimeError("runtime probe entity span failed round trip")
    if extraction.get("relations"):
        raise RuntimeError("runtime probe relations must remain empty")
    baseline_result = next(
        row
        for row in baseline["output"]["results"]
        if row["child_id"] == task["child_id"]
    )
    expected_result = copy.deepcopy(baseline_result)
    observed_result = copy.deepcopy(result)
    expected_confidences = [
        float(entity.pop("confidence"))
        for entity in expected_result["extraction"]["entities"]
    ]
    observed_confidences = [
        float(entity.pop("confidence"))
        for entity in observed_result["extraction"]["entities"]
    ]
    if expected_result != observed_result:
        raise RuntimeError("runtime probe semantic result differs from reference")
    if len(expected_confidences) != len(observed_confidences):
        raise RuntimeError("runtime probe confidence cardinality mismatch")
    confidence_deltas = [
        abs(left - right)
        for left, right in zip(expected_confidences, observed_confidences, strict=True)
    ]
    tolerance = float(spec["comparison"]["confidence_absolute_tolerance"])
    maximum_delta = max(confidence_deltas, default=0.0)
    if maximum_delta > tolerance:
        raise RuntimeError(
            f"runtime probe confidence delta {maximum_delta} exceeds {tolerance}"
        )
    observed_determinism = output.get("runtime_identity", {}).get("determinism")
    if not isinstance(observed_determinism, dict):
        raise RuntimeError("runtime probe determinism attestation missing")
    if observed_determinism.get("profile") != DETERMINISM_PROFILE:
        raise RuntimeError("runtime probe determinism profile mismatch")
    return {
        "schema_version": "polymath.runpod_custom_image_runtime_probe.v1",
        "success": True,
        "spec_sha256": sha256(spec_path),
        "baseline_sha256": sha256(baseline_path),
        "sample_id": sample_id,
        "metrics": output.get("metrics"),
        "result_count": 1,
        "runtime_identity": output.get("runtime_identity"),
        "reference_comparison": {
            "selection_identity": True,
            "confidence_count": len(confidence_deltas),
            "confidence_max_abs_delta": maximum_delta,
            "confidence_tolerance": tolerance,
        },
        "provider_calls": 0,
        "durable_writes": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("/repo"))
    parser.add_argument("--worker-root", type=Path, default=Path("/app"))
    args = parser.parse_args()
    print(json.dumps(run(args.repo, args.worker_root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
