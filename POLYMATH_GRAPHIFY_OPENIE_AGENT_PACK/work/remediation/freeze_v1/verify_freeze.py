#!/usr/bin/env python3
"""Fail if any frozen Graphify development evaluation component changed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def main() -> int:
    manifest = json.loads((HERE / "FREEZE_MANIFEST.json").read_text(encoding="utf-8"))
    checks: list[tuple[str, str, str]] = []
    for fixture_name in ("technical_book_66", "meridian_adversarial_44"):
        fixture = manifest[fixture_name]
        for component_name in ("fixture", "answer_key", "matching_policy"):
            component = fixture[component_name]
            checks.append((f"{fixture_name}.{component_name}", component["path"], component["sha256"]))
    checks.append((
        "technical_book_66.scorer",
        manifest["technical_book_66"]["scorer"]["path"],
        manifest["technical_book_66"]["scorer"]["sha256"],
    ))
    checks.append((
        "meridian_adversarial_44.baseline_scorer_harness",
        manifest["meridian_adversarial_44"]["baseline_scorer_harness"]["path"],
        manifest["meridian_adversarial_44"]["baseline_scorer_harness"]["sha256"],
    ))
    checks.append((
        "meridian_adversarial_44.stage_scorer",
        manifest["meridian_adversarial_44"]["stage_scorer"]["path"],
        manifest["meridian_adversarial_44"]["stage_scorer"]["sha256"],
    ))

    failures = []
    rows = []
    for name, raw_path, expected in checks:
        path = resolve(raw_path)
        actual = digest(path) if path.is_file() else "MISSING"
        passed = actual == expected
        rows.append({"component": name, "path": str(path), "sha256": actual, "passed": passed})
        if not passed:
            failures.append({"component": name, "expected": expected, "actual": actual})
    print(json.dumps({"status": "passed" if not failures else "failed", "checks": rows, "failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
