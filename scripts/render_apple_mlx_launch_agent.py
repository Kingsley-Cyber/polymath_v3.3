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
        "EMBEDDER_MODEL_NAME": args.embedder_model_name,
        "EMBED_BATCH_SIZE": str(args.embed_batch_size),
        "EMBED_MAX_LENGTH": str(args.embed_max_length),
        "EMBEDDER_REQUEST_TIMEOUT_SECONDS": str(args.embedder_request_timeout_seconds),
        "EMBEDDER_QUEUE_TIMEOUT_SECONDS": str(args.embedder_queue_timeout_seconds),
        "EMBEDDER_WARMUP_TIMEOUT_SECONDS": str(args.embedder_warmup_timeout_seconds),
        "HF_HOME": str(Path(args.runtime_root) / "volumes" / "hf-cache"),
        "HF_HUB_CACHE": str(Path(args.runtime_root) / "volumes" / "hf-cache" / "hub"),
        "POLYMATH_DOCKER_DATA_ROOT": args.runtime_root,
        "MLX_CACHE_LIMIT_GB": str(args.mlx_cache_limit_gb),
        "RERANKER_CAL_MU": str(args.reranker_cal_mu),
        "RERANKER_CAL_T": str(args.reranker_cal_t),
        "RERANKER_CAL_VERSION": args.reranker_cal_version,
        "RERANKER_BATCH_SIZE": str(args.reranker_batch_size),
        "RERANKER_MAX_DOC_CHARS": str(args.reranker_max_doc_chars),
        "RERANKER_MAX_QUERY_CHARS": str(args.reranker_max_query_chars),
        "RERANKER_REQUEST_TIMEOUT_SECONDS": str(args.reranker_request_timeout_seconds),
        "RERANKER_QUEUE_TIMEOUT_SECONDS": str(args.reranker_queue_timeout_seconds),
        "RERANKER_SCORE_SCALE": args.reranker_score_scale,
        "RERANKER_WARM_ON_STARTUP": args.reranker_warm_on_startup,
        "RERANKER_WARMUP_CANDIDATE_SHAPES": args.reranker_warmup_candidate_shapes,
        "RERANKER_WARMUP_CANDIDATES": str(args.reranker_warmup_candidates),
        "RERANKER_WARMUP_DOC_CHARS": str(args.reranker_warmup_doc_chars),
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
    parser.add_argument("--embedder-model-name", required=True)
    parser.add_argument("--embed-batch-size", required=True, type=int)
    parser.add_argument("--embed-max-length", required=True, type=int)
    parser.add_argument("--embedder-request-timeout-seconds", required=True, type=float)
    parser.add_argument("--embedder-queue-timeout-seconds", required=True, type=float)
    parser.add_argument("--embedder-warmup-timeout-seconds", required=True, type=float)
    parser.add_argument("--mlx-cache-limit-gb", required=True, type=float)
    parser.add_argument("--reranker-cal-mu", required=True, type=float)
    parser.add_argument("--reranker-cal-t", required=True, type=float)
    parser.add_argument("--reranker-cal-version", required=True)
    parser.add_argument("--reranker-batch-size", required=True, type=int)
    parser.add_argument("--reranker-max-doc-chars", required=True, type=int)
    parser.add_argument("--reranker-max-query-chars", required=True, type=int)
    parser.add_argument("--reranker-request-timeout-seconds", required=True, type=float)
    parser.add_argument("--reranker-queue-timeout-seconds", required=True, type=float)
    parser.add_argument("--reranker-warm-on-startup", required=True)
    parser.add_argument("--reranker-warmup-candidate-shapes", required=True)
    parser.add_argument("--reranker-warmup-candidates", required=True, type=int)
    parser.add_argument("--reranker-warmup-doc-chars", required=True, type=int)
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
