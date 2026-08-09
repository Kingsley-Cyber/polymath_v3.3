#!/usr/bin/env python3
"""Load the canonical entity provider twice and emit a runtime proof."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from services.extraction.graphify_provider_registry import canonical_entity_provider


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    first = canonical_entity_provider()
    first_health = first.health()
    second = canonical_entity_provider()
    predictions = second.predict_entities([
        "Polymath uses MongoDB to store canonical evidence.",
        "OpenAI employs Sam Altman.",
    ])
    second_health = second.health()
    retired_prefixes = (
        "glirel",
        "gliner_relex",
        "mlx",
        "mlx_lm",
        "services.ingestion.relex_local",
        "services.runpod_flash_extraction",
    )
    retired_loaded = sorted(
        name for name in sys.modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in retired_prefixes)
    )
    payload = {
        "schema_version": "polymath.gliner2_cpu_proof.v1",
        "status": "passed",
        "same_provider_object": first is second,
        "single_warm_model": first_health["model_load_count"] == second_health["model_load_count"] == 1,
        "cpu_only": first_health["device"] == second_health["device"] == "cpu",
        "retired_provider_modules_loaded": retired_loaded,
        "retired_providers_not_loaded": not retired_loaded,
        "health": second_health,
        "package_pins": {
            name: importlib.metadata.version(name)
            for name in ("gliner2", "torch", "transformers", "peft", "accelerate", "safetensors")
        },
        "predictions": [
            [
                {
                    "text": item.text,
                    "entity_type": item.entity_type,
                    "start": item.start,
                    "end": item.end,
                    "confidence": item.confidence,
                }
                for item in row
            ]
            for row in predictions
        ],
        "elapsed_seconds": time.perf_counter() - started,
    }
    if (
        not payload["single_warm_model"]
        or not payload["cpu_only"]
        or not payload["same_provider_object"]
        or not payload["retired_providers_not_loaded"]
    ):
        payload["status"] = "failed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
