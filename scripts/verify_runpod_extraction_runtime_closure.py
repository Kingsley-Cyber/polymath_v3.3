#!/usr/bin/env python3
"""Verify the credential-free RunPod extraction source and pin closure."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "runpod_flash_extractor"
BACKEND = ROOT / "backend"

VENDORED_MAP = {
    "models/extraction_registry.py": "models/extraction_registry.py",
    "models/hash_taxonomy.py": "models/hash_taxonomy.py",
    "models/local_extraction.py": "models/local_extraction.py",
    "models/semantic_artifacts.py": "models/semantic_artifacts.py",
    "services/ingestion/gliner_mentions.py": "services/ingestion/gliner_mentions.py",
    "services/ingestion/semantic_observations.py": "services/ingestion/semantic_observations.py",
    "registries/extraction_vocabularies.v1.json": "registries/extraction_vocabularies.v1.json",
    "registries/predicate_normalization.v1.json": "registries/predicate_normalization.v1.json",
}
EXPECTED_WORKER_FILES = {
    "app.py",
    "runtime.py",
    "models/__init__.py",
    "services/__init__.py",
    "services/ingestion/__init__.py",
    *VENDORED_MAP,
}
FORBIDDEN_PATTERNS = {
    "credential_literal": re.compile(
        r"\b(?:api[_-]?key|secret|password)\s*=\s*['\"][^'\"]+['\"]",
        re.I,
    ),
    "bearer_literal": re.compile(r"bearer\s+[A-Za-z0-9._-]{16,}", re.I),
    "mongo_uri": re.compile(r"mongodb(?:\+srv)?://", re.I),
    "private_key": re.compile(r"BEGIN (?:RSA |OPENSSH )?PRIVATE KEY"),
    "provider_policy": re.compile(
        r"semantic_gateway_(?:provider_prices|route_parameters)"
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tracked_worker_files() -> set[str]:
    roots = ("models", "services", "registries")
    return {
        path.relative_to(WORKER).as_posix()
        for root in roots
        for path in (WORKER / root).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    } | {"app.py", "runtime.py"}


def _local_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots & {"models", "services", "registries"}


def verify() -> dict[str, Any]:
    mismatches = []
    vendored_hashes = {}
    for worker_relative, backend_relative in sorted(VENDORED_MAP.items()):
        worker_path = WORKER / worker_relative
        backend_path = BACKEND / backend_relative
        worker_hash = sha256(worker_path) if worker_path.is_file() else None
        backend_hash = sha256(backend_path) if backend_path.is_file() else None
        vendored_hashes[worker_relative] = worker_hash
        if worker_hash != backend_hash:
            mismatches.append(
                {
                    "path": worker_relative,
                    "worker_sha256": worker_hash,
                    "backend_sha256": backend_hash,
                }
            )

    observed_files = _tracked_worker_files()
    unexpected_files = sorted(observed_files - EXPECTED_WORKER_FILES)
    missing_files = sorted(EXPECTED_WORKER_FILES - observed_files)
    source_hashes = {
        relative: sha256(WORKER / relative)
        for relative in sorted(observed_files)
        if (WORKER / relative).is_file()
    }
    source_closure_sha256 = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    secret_findings = []
    for relative in sorted(observed_files):
        path = WORKER / relative
        if path.suffix not in {".py", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in FORBIDDEN_PATTERNS.items():
            if pattern.search(text):
                secret_findings.append({"path": relative, "pattern": name})

    import_roots = sorted(
        set().union(
            *(
                _local_import_roots(WORKER / relative)
                for relative in observed_files
                if relative.endswith(".py")
            )
        )
    )
    return {
        "schema_version": "polymath.runpod_extraction_runtime_closure.v1",
        "python_target": "3.11.15",
        "worker_file_count": len(observed_files),
        "worker_files": sorted(observed_files),
        "source_hashes": source_hashes,
        "source_closure_sha256": source_closure_sha256,
        "vendored_hashes": vendored_hashes,
        "vendored_mismatch_count": len(mismatches),
        "vendored_mismatches": mismatches,
        "unexpected_files": unexpected_files,
        "missing_files": missing_files,
        "local_import_roots": import_roots,
        "secret_finding_count": len(secret_findings),
        "secret_findings": secret_findings,
        "all_green": not (
            mismatches
            or unexpected_files
            or missing_files
            or secret_findings
            or import_roots != ["models", "services"]
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
