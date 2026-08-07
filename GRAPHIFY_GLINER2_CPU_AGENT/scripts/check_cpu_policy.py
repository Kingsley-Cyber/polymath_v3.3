#!/usr/bin/env python3
"""Enforce CPU-only extraction wiring within the explicitly discovered candidate scope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

PACK_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = {
    "mps": re.compile(r"\bmps\b", re.I),
    "mlx": re.compile(r"\bmlx\b", re.I),
    "cuda": re.compile(r"\bcuda\b", re.I),
    "device_auto": re.compile(r"device_map\s*=\s*[\"']auto[\"']", re.I),
    "glirel": re.compile(r"\bglirel\b", re.I),
    "relex": re.compile(r"gliner[-_ ]?relex|\brelex\b", re.I),
    "liquid": re.compile(r"LiquidAI|LFM2", re.I),
}
REQUIRED_MODEL = "fastino/gliner2-base-v1"
CPU_HINTS = [re.compile(pattern, re.I) for pattern in [r"device\s*=\s*[\"']cpu[\"']", r"\.to\(\s*[\"']cpu[\"']", r"map_location\s*=\s*[\"']cpu[\"']"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--scope", default=str(PACK_ROOT / ".agent_state" / "extraction_scope.json"))
    parser.add_argument("--output", default=str(PACK_ROOT / "artifacts" / "reports" / "cpu_policy.json"))
    args = parser.parse_args()

    repo_root = Path(args.repo_root).expanduser().resolve()
    scope_path = Path(args.scope).expanduser().resolve()
    if not scope_path.is_file():
        raise SystemExit(f"Scope file does not exist: {scope_path}")
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    files = list(dict.fromkeys(scope.get("runtime_provider_files", []) + scope.get("provider_registry_files", [])))
    if not files:
        raise SystemExit("Scope contains no runtime_provider_files/provider_registry_files")

    violations = []
    missing_files = []
    model_found = False
    cpu_hint_found = False
    scanned = []
    for rel in files:
        path = (repo_root / rel).resolve()
        if not path.is_file():
            missing_files.append(rel)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        scanned.append(rel)
        model_found = model_found or REQUIRED_MODEL in text
        cpu_hint_found = cpu_hint_found or any(pattern.search(text) for pattern in CPU_HINTS)
        for line_no, line in enumerate(text.splitlines(), 1):
            for name, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    violations.append({"file": rel, "line": line_no, "rule": name, "text": line.strip()[:500]})

    report = {
        "passed": not violations and not missing_files and model_found and cpu_hint_found,
        "model_required": REQUIRED_MODEL,
        "model_found": model_found,
        "cpu_assertion_or_mapping_found": cpu_hint_found,
        "files_scanned": scanned,
        "missing_files": missing_files,
        "violations": violations,
        "note": "Offline baseline files are intentionally excluded from the candidate runtime scope.",
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(output)
    if not report["passed"]:
        print(json.dumps(report, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
