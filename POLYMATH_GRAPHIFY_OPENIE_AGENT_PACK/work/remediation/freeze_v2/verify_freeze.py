#!/usr/bin/env python3
"""Verify the graphify-qualification-freeze-v2 manifest.

Run BEFORE and AFTER the sealed qualification run. Any mismatch (code hash,
config hash, environment version, or dirty worktree) invalidates the
qualification claim. Exit 0 = frozen state intact.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/king/polymath_v3.3")
MANIFEST = Path(__file__).with_name("FREEZE_MANIFEST.json")


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    failures: list[str] = []

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    if head != manifest["frozen_at_commit"]:
        failures.append(f"HEAD {head} != frozen commit {manifest['frozen_at_commit']}")
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).decode().strip()
    if dirty:
        failures.append(f"worktree dirty ({len(dirty.splitlines())} paths)")

    for rel, expected in manifest["code"].items():
        path = ROOT / rel
        if not path.exists():
            failures.append(f"missing: {rel}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"hash mismatch: {rel}")

    try:
        import importlib.metadata as im
        for package, expected in manifest["environment"]["packages"].items():
            actual = im.version(package)
            if actual != expected:
                failures.append(f"package drift: {package} {actual} != {expected}")
    except Exception as exc:  # environment not importable = failure
        failures.append(f"environment check failed: {exc}")

    if failures:
        print("FREEZE VIOLATED:")
        for failure in failures:
            print("  -", failure)
        return 1
    print("FREEZE INTACT:", manifest["freeze_id"], "@", manifest["frozen_at_commit"][:12])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
