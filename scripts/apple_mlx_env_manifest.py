#!/usr/bin/env python3
"""Validate the complete, explicit environment for an Apple ML deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

SCHEMA_VERSION = "polymath.apple_mlx_environment.v2"
REQUIRED_KEYS = (
    "POLYMATH_DOCKER_DATA_ROOT",
    "APPLE_MLX_EMBED_MODEL_ID",
    "APPLE_MLX_RERANKER_MODEL_ID",
    "APPLE_RERANKER_BACKEND",
    "APPLE_TORCH_RERANKER_MODEL_ID",
    "RERANKER_SCORE_SCALE",
    "EMBEDDER_MODEL_NAME",
    "EMBED_BATCH_SIZE",
    "EMBED_MAX_LENGTH",
    "EMBEDDER_REQUEST_TIMEOUT_SECONDS",
    "EMBEDDER_QUEUE_TIMEOUT_SECONDS",
    "EMBEDDER_WARMUP_TIMEOUT_SECONDS",
    "MLX_CACHE_LIMIT_GB",
    "RERANKER_CAL_MU",
    "RERANKER_CAL_T",
    "RERANKER_CAL_VERSION",
    "RERANKER_BATCH_SIZE",
    "RERANKER_MAX_DOC_CHARS",
    "RERANKER_MAX_QUERY_CHARS",
    "RERANKER_REQUEST_TIMEOUT_SECONDS",
    "RERANKER_QUEUE_TIMEOUT_SECONDS",
    "RERANKER_WARM_ON_STARTUP",
    "RERANKER_WARMUP_CANDIDATE_SHAPES",
    "RERANKER_WARMUP_CANDIDATES",
    "RERANKER_WARMUP_DOC_CHARS",
    "START_EMBEDDER",
    "START_RERANKER",
    "START_DOCLING",
    "ARBITER_ENABLED",
    "ARBITER_HOST",
    "ARBITER_PORT",
    "ARBITER_ACQUIRE_TIMEOUT_SECONDS",
    "ARBITER_EMBED_HOLD_TARGET_MS",
    "ARBITER_RERANK_HOLD_TARGET_MS",
    "ARBITER_MAX_EMBED_BURST",
    "ARBITER_RERANK_STARVATION_SECONDS",
    "ARBITER_STALE_LEASE_SECONDS",
)


class ManifestError(ValueError):
    """The deployment manifest is incomplete, malformed, or mismatched."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_manifest(path: Path, process_environment: Mapping[str, str]) -> None:
    missing = [
        key
        for key in REQUIRED_KEYS
        if not str(process_environment.get(key, "")).strip()
    ]
    if missing:
        raise ManifestError(f"cannot write incomplete manifest; missing={missing}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "environment": {key: process_environment[key] for key in REQUIRED_KEYS},
    }
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_manifest(
    path: Path,
    *,
    process_environment: Mapping[str, str] | None = None,
    expected_arbiter_enabled: bool | None = None,
) -> dict[str, str]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("Apple ML environment manifest schema is invalid")
    environment = payload.get("environment")
    if not isinstance(environment, dict):
        raise ManifestError("Apple ML environment manifest has no environment object")
    missing = [key for key in REQUIRED_KEYS if key not in environment]
    blank = [
        key
        for key in REQUIRED_KEYS
        if key in environment and not str(environment[key]).strip()
    ]
    non_strings = [
        key
        for key in REQUIRED_KEYS
        if key in environment and not isinstance(environment[key], str)
    ]
    if missing or blank or non_strings:
        raise ManifestError(
            f"manifest incomplete: missing={missing} blank={blank} "
            f"non_strings={non_strings}"
        )
    normalized = {key: str(environment[key]) for key in REQUIRED_KEYS}
    enabled = normalized["ARBITER_ENABLED"].strip().lower()
    if enabled not in {"true", "false"}:
        raise ManifestError("ARBITER_ENABLED must be exactly true or false")
    if (
        expected_arbiter_enabled is not None
        and (enabled == "true") is not expected_arbiter_enabled
    ):
        raise ManifestError("ARBITER_ENABLED differs from the requested phase")
    if process_environment is not None:
        mismatched = [
            key
            for key, expected in normalized.items()
            if process_environment.get(key) != expected
        ]
        if mismatched:
            raise ManifestError(
                f"process environment differs from manifest: {mismatched}"
            )
    normalized["_manifest_sha256"] = _sha256(raw)
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest", type=Path)
    mode.add_argument("--write-manifest", type=Path)
    parser.add_argument(
        "--expect-arbiter-enabled", choices=("true", "false"), required=True
    )
    parser.add_argument("--check-process-environment", action="store_true")
    args = parser.parse_args()
    if args.write_manifest is not None:
        write_manifest(args.write_manifest, os.environ)
        manifest_path = args.write_manifest
    else:
        manifest_path = args.manifest
    environment = load_manifest(
        manifest_path,
        process_environment=os.environ if args.check_process_environment else None,
        expected_arbiter_enabled=args.expect_arbiter_enabled == "true",
    )
    print(
        json.dumps(
            {
                "passed": True,
                "schema_version": SCHEMA_VERSION,
                "manifest_sha256": environment["_manifest_sha256"],
                "required_key_count": len(REQUIRED_KEYS),
                "arbiter_enabled": environment["ARBITER_ENABLED"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
