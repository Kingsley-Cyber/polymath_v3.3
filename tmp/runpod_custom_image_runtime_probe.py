#!/usr/bin/env python3
"""Operational scratch: run one preregistered extraction inside the exact image."""

from __future__ import annotations

import hashlib
import json
import sys
import traceback
from pathlib import Path


REPO = Path("/repo")
sys.path.insert(0, "/app")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    spec_path = REPO / "backend/evals/runpod_same_chunk_lockdown_v1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source = REPO / spec["source_fixture"]["path"]
    if sha256(source) != spec["source_fixture"]["sha256"]:
        raise RuntimeError("source fixture identity mismatch")
    fixture = json.loads(source.read_text(encoding="utf-8"))
    sample_id = spec["source_fixture"]["sample_ids"][0]
    sample = next(row for row in fixture["samples"] if row["id"] == sample_id)
    baseline = json.loads(
        (
            REPO
            / "docs/baselines/RUNPOD_SAME_CHUNK_LOCAL_REFERENCE_2026-07-15.json"
        ).read_text(encoding="utf-8")
    )
    identity = baseline["runtime_identity"]
    payload = {
        "contract_version": "polymath.runpod_local_extraction.v1",
        "batch_id": "runpod-lockdown:diagnostic-one-task",
        "model_id": identity["gliner_model_id"],
        "model_revision": identity["gliner_model_revision"],
        "spacy_pipeline": identity["spacy_model"],
        "asset_contract": identity["asset_contract"],
        "tasks": [
            {
                "document_id": f"doc:runpod-lockdown:{sample_id}",
                "child_id": f"child:runpod-lockdown:{sample_id}",
                "source_version_id": f"srcv:runpod-lockdown:{sample_id}",
                "text": sample["text"],
            }
        ],
    }
    from runtime import extract_local_batch

    output = extract_local_batch(payload)
    print(
        json.dumps(
            {
                "success": True,
                "contract_version": output.get("contract_version"),
                "metrics": output.get("metrics"),
                "result_count": len(output.get("results") or []),
                "runtime_identity": output.get("runtime_identity"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise
