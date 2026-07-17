#!/usr/bin/env python3
"""Render the Apple ML LaunchAgent deterministically.

The installer and drift checker both call this file so deployment cannot
silently compare against a second, hand-maintained plist representation.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import plistlib


def build_plist(args: argparse.Namespace) -> dict:
    environment = {
        "APPLE_MLX_EMBED_MODEL_ID": args.embed_model,
        "APPLE_MLX_RERANKER_MODEL_ID": args.reranker_model,
        "APPLE_RERANKER_BACKEND": args.reranker_backend,
        "APPLE_TORCH_RERANKER_MODEL_ID": args.torch_reranker_model,
        "ARBITER_ACQUIRE_TIMEOUT_SECONDS": str(args.arbiter_acquire_timeout_seconds),
        "ARBITER_EMBED_HOLD_TARGET_MS": str(args.arbiter_embed_hold_target_ms),
        "ARBITER_ENABLED": args.arbiter_enabled,
        "ARBITER_HOST": args.arbiter_host,
        "ARBITER_MAX_EMBED_BURST": str(args.arbiter_max_embed_burst),
        "ARBITER_PORT": str(args.arbiter_port),
        "ARBITER_RERANK_HOLD_TARGET_MS": str(args.arbiter_rerank_hold_target_ms),
        "ARBITER_RERANK_STARVATION_SECONDS": str(
            args.arbiter_rerank_starvation_seconds
        ),
        "ARBITER_STALE_LEASE_SECONDS": str(args.arbiter_stale_lease_seconds),
        "EMBED_BATCH_SIZE": str(args.embed_batch_size),
        "HF_HOME": str(Path(args.runtime_root) / "volumes" / "hf-cache"),
        "HF_HUB_CACHE": str(Path(args.runtime_root) / "volumes" / "hf-cache" / "hub"),
        "POLYMATH_DOCKER_DATA_ROOT": args.runtime_root,
        "RERANKER_SCORE_SCALE": args.reranker_score_scale,
        "RERANKER_WARM_ON_STARTUP": "true",
        "RERANKER_WARMUP_CANDIDATE_SHAPES": "16,24",
        "START_DOCLING": args.start_docling,
        "START_EMBEDDER": args.start_embedder,
        "START_RERANKER": args.start_reranker,
    }
    return {
        "EnvironmentVariables": environment,
        "KeepAlive": True,
        "Label": args.label,
        "ProgramArguments": ["/bin/bash", str(Path(args.services_dir) / "start.sh")],
        "RunAtLoad": True,
        "StandardErrorPath": str(Path(args.log_dir) / "apple_ml_services.err.log"),
        "StandardOutPath": str(Path(args.log_dir) / "apple_ml_services.log"),
        "WorkingDirectory": args.services_dir,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--services-dir", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--embed-model", required=True)
    parser.add_argument("--reranker-model", required=True)
    parser.add_argument("--reranker-backend", required=True)
    parser.add_argument("--torch-reranker-model", required=True)
    parser.add_argument("--embed-batch-size", required=True, type=int)
    parser.add_argument("--start-embedder", required=True)
    parser.add_argument("--start-reranker", required=True)
    parser.add_argument("--start-docling", required=True)
    parser.add_argument("--reranker-score-scale", required=True)
    parser.add_argument("--arbiter-enabled", required=True)
    parser.add_argument("--arbiter-host", required=True)
    parser.add_argument("--arbiter-port", required=True, type=int)
    parser.add_argument("--arbiter-acquire-timeout-seconds", required=True, type=float)
    parser.add_argument("--arbiter-embed-hold-target-ms", required=True, type=int)
    parser.add_argument("--arbiter-rerank-hold-target-ms", required=True, type=int)
    parser.add_argument("--arbiter-max-embed-burst", required=True, type=int)
    parser.add_argument(
        "--arbiter-rerank-starvation-seconds", required=True, type=float
    )
    parser.add_argument("--arbiter-stale-lease-seconds", required=True, type=float)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        plistlib.dump(build_plist(args), handle, fmt=plistlib.FMT_XML, sort_keys=True)


if __name__ == "__main__":
    main()
