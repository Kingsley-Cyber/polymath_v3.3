#!/usr/bin/env python3
"""Independently verify the exact isolation backup manifest and gzip rows."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path


ROOT = Path(
    "/data/ingest-files/runpod-job-journals/"
    "e2e-isolation-backup-20260716T0046Z"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def gzip_rows(path: Path) -> int:
    rows = 0
    with gzip.open(path, "rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                raise RuntimeError(f"unterminated JSONL row: {path.name}")
            json.loads(line)
            rows += 1
    return rows


def main() -> None:
    manifest_path = ROOT / "manifest.json"
    declared_manifest_sha = (ROOT / "MANIFEST.sha256").read_text().split()[0]
    actual_manifest_sha = sha256(manifest_path)
    if actual_manifest_sha != declared_manifest_sha:
        raise RuntimeError("manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text())

    receipts = {
        "mongo.jsonl.gz": manifest["mongo"]["receipt"],
        "qdrant.jsonl.gz": manifest["qdrant"]["receipt"],
        "neo4j_nodes.jsonl.gz": manifest["neo4j"]["nodes"],
        "neo4j_relationships.jsonl.gz": manifest["neo4j"]["relationships"],
        manifest["baseline"]["path"]: manifest["baseline"],
    }
    verified: dict[str, dict[str, int | str]] = {}
    for name, receipt in receipts.items():
        path = ROOT / name
        actual_sha = sha256(path)
        if actual_sha != receipt["sha256"]:
            raise RuntimeError(f"file SHA-256 mismatch: {name}")
        if path.stat().st_size != int(receipt["bytes"]):
            raise RuntimeError(f"file size mismatch: {name}")
        rows = gzip_rows(path) if name.endswith(".gz") else int(receipt["rows"])
        if rows != int(receipt["rows"]):
            raise RuntimeError(f"row-count mismatch: {name}")
        verified[name] = {
            "rows": rows,
            "bytes": path.stat().st_size,
            "sha256": actual_sha,
        }

    print(
        json.dumps(
            {
                "backup_root": str(ROOT),
                "manifest_sha256": actual_manifest_sha,
                "verified": verified,
                "secret_disposition": manifest["secret_field_scan"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
