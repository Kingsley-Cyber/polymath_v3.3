#!/usr/bin/env python3
"""Build a checksum manifest for immutable control-pack files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_TOP = {"artifacts", ".agent_state"}
EXCLUDED_FILES = {"MANIFEST.json"}
EXCLUDED_DIRS = {"__pycache__"}


def main() -> int:
    files = {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if rel.parts[0] in EXCLUDED_TOP or rel.name in EXCLUDED_FILES or any(part in EXCLUDED_DIRS for part in rel.parts) or path.suffix == ".pyc":
            continue
        files[str(rel)] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
    output = ROOT / "MANIFEST.json"
    output.write_text(json.dumps({"version": "1.0.0", "files": files}, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
