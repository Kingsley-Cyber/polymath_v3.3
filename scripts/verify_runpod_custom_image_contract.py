#!/usr/bin/env python3
"""Verify the locked custom RunPod image inputs before an external build."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "runpod_flash_extractor"
DOCKERFILE = WORKER / "Dockerfile.locked"
LOCK = WORKER / "requirements.custom-image.lock"
APP = WORKER / "app.py"

BASE_IMAGE = (
    "python:3.11.15-slim-bookworm@"
    "sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba"
)
BASE_AMD64_DIGEST = (
    "sha256:28255a3ace7eb4c48bc1b57b90af29e1bc82b4fd6c60614a8e3dce61b87ff941"
)
EXPECTED = {
    "torch": "2.12.0",
    "transformers": "4.57.6",
    "tokenizers": "0.22.2",
    "numpy": "2.2.6",
    "safetensors": "0.7.0",
    "sentencepiece": "0.2.1",
    "huggingface-hub": "0.36.2",
    "pydantic": "2.13.4",
    "gliner": "0.2.26",
    "spacy": "3.8.14",
    "en-core-web-sm": "3.8.0",
    "runpod": "1.10.1",
    "runpod-flash": "1.18.0",
}
FORBIDDEN = {
    "credential_literal": re.compile(
        r"\b(?:api[_-]?key|secret|password)\s*=\s*['\"][^'\"]+['\"]",
        re.I,
    ),
    "bearer_literal": re.compile(r"bearer\s+[A-Za-z0-9._-]{16,}", re.I),
    "mongo_uri": re.compile(r"mongodb(?:\+srv)?://", re.I),
    "private_key": re.compile(r"BEGIN (?:RSA |OPENSSH )?PRIVATE KEY"),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locked_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        if not raw or raw[0].isspace() or raw.startswith("#"):
            continue
        requirement = raw.removesuffix(" \\")
        if " @ " in requirement:
            name, url = requirement.split(" @ ", 1)
            version_match = re.search(r"-(\d+\.\d+\.\d+)(?:-|/)", url)
            if version_match:
                result[name.casefold().replace("_", "-")] = version_match.group(1)
        elif "==" in requirement:
            name, version = requirement.split("==", 1)
            result[name.casefold().replace("_", "-")] = version
    return result


def function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def verify() -> dict[str, Any]:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    versions = locked_versions()
    mismatches = {
        name: {"expected": expected, "observed": versions.get(name)}
        for name, expected in EXPECTED.items()
        if versions.get(name) != expected
    }
    secret_findings = []
    for path in (
        DOCKERFILE,
        LOCK,
        APP,
        ROOT / "scripts/bake_runpod_extraction_assets.py",
    ):
        text = path.read_text(encoding="utf-8")
        for name, pattern in FORBIDDEN.items():
            if pattern.search(text):
                secret_findings.append(
                    {"path": str(path.relative_to(ROOT)), "pattern": name}
                )
    from_line = next(
        line for line in dockerfile.splitlines() if line.startswith("FROM ")
    )
    required_docker_tokens = {
        "require_hashes": "--require-hashes",
        "non_root": "USER 10001:10001",
        "entrypoint": 'CMD ["python", "app.py"]',
        "baked_model_cache_runtime": ("POLYMATH_HF_CACHE_ROOT=/opt/polymath/hf-cache"),
        "offline_model_runtime": "POLYMATH_LOCAL_FILES_ONLY=1",
        "cublas_deterministic_workspace": "CUBLAS_WORKSPACE_CONFIG=:4096:8",
        "nvidia_tf32_disabled": "NVIDIA_TF32_OVERRIDE=0",
        "omp_threads_fixed": "OMP_NUM_THREADS=1",
        "mkl_threads_fixed": "MKL_NUM_THREADS=1",
        "openblas_threads_fixed": "OPENBLAS_NUM_THREADS=1",
        "numexpr_threads_fixed": "NUMEXPR_NUM_THREADS=1",
        "source_commit_label": "org.opencontainers.image.revision",
        "source_closure_label": "io.polymath.source-closure",
    }
    docker_checks = {
        "base_image_exact": from_line == f"FROM {BASE_IMAGE}",
        "amd64_child_digest_labeled": (
            f'io.polymath.base-amd64-digest="{BASE_AMD64_DIGEST}"' in dockerfile
        ),
        **{name: token in dockerfile for name, token in required_docker_tokens.items()},
    }
    required_functions = {"extract_batch", "handle_serverless_job", "main"}
    functions = function_names(APP)
    return {
        "schema_version": "polymath.runpod_custom_image_contract_verify.v1",
        "base_image": BASE_IMAGE,
        "dockerfile_sha256": sha256(DOCKERFILE),
        "requirements_lock_sha256": sha256(LOCK),
        "locked_distribution_count": len(versions),
        "critical_versions": {name: versions.get(name) for name in sorted(EXPECTED)},
        "critical_mismatches": mismatches,
        "docker_checks": docker_checks,
        "handler_functions": sorted(functions & required_functions),
        "secret_findings": secret_findings,
        "all_green": (
            not mismatches
            and all(docker_checks.values())
            and required_functions <= functions
            and not secret_findings
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = verify()
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
