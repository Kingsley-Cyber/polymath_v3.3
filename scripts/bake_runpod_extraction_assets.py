#!/usr/bin/env python3
"""Bake and verify the locked GLiNER snapshot inside the custom image."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "runpod_flash_extractor"
if not WORKER.is_dir() and Path("/app/runtime.py").is_file():
    WORKER = Path("/app")
sys.path.insert(0, str(WORKER))

import runtime


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bake() -> dict[str, Any]:
    from huggingface_hub import snapshot_download

    snapshot = Path(
        snapshot_download(
            repo_id=runtime.GLINER_MODEL_ID,
            revision=runtime.GLINER_MODEL_REVISION,
            cache_dir=os.environ.get("HF_HOME") or None,
        )
    )
    observed_assets = {
        "config_sha256": sha256(snapshot / "gliner_config.json"),
        "weights_sha256": sha256(snapshot / "model.safetensors"),
    }
    expected_assets = {
        "config_sha256": runtime.GLINER_CONFIG_SHA256,
        "weights_sha256": runtime.GLINER_WEIGHTS_SHA256,
    }
    if observed_assets != expected_assets:
        raise RuntimeError("downloaded GLiNER snapshot differs from locked hashes")

    identity = runtime.runtime_identity(model_snapshot=snapshot)
    import spacy

    nlp = spacy.load(runtime.SPACY_MODEL)
    if str(nlp.meta.get("version") or "") != runtime.SPACY_MODEL_VERSION:
        raise RuntimeError("spaCy model version differs from locked contract")
    return {
        "schema_version": "polymath.runpod_custom_image_bake.v1",
        "python": platform.python_version(),
        "runpod": metadata.version("runpod"),
        "runpod_flash": metadata.version("runpod-flash"),
        "snapshot_path": str(snapshot),
        "runtime_identity": identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = bake()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(args.report),
                "python": report["python"],
                "closure_sha256": report["runtime_identity"]["source_closure"][
                    "closure_sha256"
                ],
                "model_snapshot": report["runtime_identity"]["model_snapshot"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
