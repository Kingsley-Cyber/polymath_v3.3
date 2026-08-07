#!/usr/bin/env python3
"""Verify immutable control-pack file checksums."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    manifest_path = ROOT / "MANIFEST.json"
    if not manifest_path.is_file():
        print("MANIFEST.json is missing", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = []
    for rel, expected in manifest["files"].items():
        path = ROOT / rel
        if not path.is_file():
            failures.append({"path": rel, "reason": "missing"})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected["sha256"]:
            failures.append({"path": rel, "reason": "sha256", "expected": expected["sha256"], "actual": digest})
    if failures:
        print(json.dumps(failures, indent=2), file=sys.stderr)
        return 1
    print(f"Verified {len(manifest['files'])} immutable files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
