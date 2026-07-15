#!/usr/bin/env python3
"""Freeze and verify the pinned-local half of RunPod same-chunk parity."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "runpod_flash_extractor"
DEFAULT_SPEC = ROOT / "backend/evals/runpod_same_chunk_lockdown_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_tasks(spec_path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("schema_version") != "polymath.runpod_same_chunk_lockdown.v1":
        raise ValueError("unsupported same-chunk spec")
    source = ROOT / spec["source_fixture"]["path"]
    if sha256(source) != spec["source_fixture"]["sha256"]:
        raise ValueError("same-chunk source fixture hash drifted")
    fixture = json.loads(source.read_text(encoding="utf-8"))
    # Text and stable sample ID are the only fields read from the gold fixture.
    text_by_id = {str(row["id"]): str(row["text"]) for row in fixture["samples"]}
    samples = [
        {"id": sample_id, "text": text_by_id[sample_id]}
        for sample_id in spec["source_fixture"]["sample_ids"]
    ]
    for row in spec["synthetic_samples"]:
        if "text" in row:
            text = str(row["text"])
        else:
            text = str(row["repeat_text"]) * int(row["repeat_count"]) + str(
                row["suffix"]
            )
        samples.append({"id": str(row["id"]), "text": text})
    tasks = [
        {
            "document_id": f"doc:runpod-lockdown:{row['id']}",
            "child_id": f"child:runpod-lockdown:{row['id']}",
            "source_version_id": f"srcv:runpod-lockdown:{row['id']}",
            "text": row["text"],
        }
        for row in samples
    ]
    if len(tasks) != 12 or len({row["child_id"] for row in tasks}) != len(tasks):
        raise ValueError("same-chunk task cardinality/identity drifted")
    return spec, tasks


def stable_output(output: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(output))
    value["metrics"].pop("duration_seconds", None)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats != 2:
        raise ValueError("B1 requires exactly two deterministic local runs")

    sys.path.insert(0, str(WORKER))
    import runtime

    spec, tasks = load_tasks(args.spec)
    request = {
        "contract_version": runtime.CONTRACT_VERSION,
        "batch_id": "runpod-lockdown:same-chunk-v1",
        "model_id": runtime.GLINER_MODEL_ID,
        "model_revision": runtime.GLINER_MODEL_REVISION,
        "spacy_pipeline": runtime.SPACY_MODEL,
        "asset_contract": dict(runtime.EXPECTED_ASSET_CONTRACT),
        "determinism_profile": runtime.DETERMINISM_PROFILE,
        "tasks": tasks,
    }
    outputs = [stable_output(runtime.extract_local_batch(request)) for _ in range(2)]
    hashes = [canonical_hash(value) for value in outputs]
    if len(set(hashes)) != 1:
        raise RuntimeError("pinned-local same-chunk output is not deterministic")
    result = {
        "schema_version": "polymath.runpod_same_chunk_local_reference.v1",
        "spec_path": str(args.spec.relative_to(ROOT)),
        "spec_sha256": sha256(args.spec),
        "task_count": len(tasks),
        "task_input_sha256": canonical_hash(tasks),
        "run_hashes": hashes,
        "byte_deterministic": True,
        "runtime_identity": outputs[0]["runtime_identity"],
        "output": outputs[0],
        "run_mode": {
            "provider_calls": 0,
            "database_writes": 0,
            "graph_writes": 0,
            "vector_writes": 0,
        },
        "comparison_contract": spec["comparison"],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "task_count": len(tasks),
                "task_input_sha256": result["task_input_sha256"],
                "output_sha256": hashes[0],
                "byte_deterministic": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
