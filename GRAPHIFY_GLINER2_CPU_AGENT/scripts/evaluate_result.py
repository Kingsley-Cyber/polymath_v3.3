#!/usr/bin/env python3
"""Evaluate a Graphify benchmark result against the committed acceptance gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PACK_ROOT = Path(__file__).resolve().parents[1]


def get_path(value: dict[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted)
        current = current[part]
    return current


def compare(actual: Any, op: str, expected: Any) -> bool:
    if op == "eq": return actual == expected
    if op == "ne": return actual != expected
    if op == "ge": return actual >= expected
    if op == "gt": return actual > expected
    if op == "le": return actual <= expected
    if op == "lt": return actual < expected
    raise ValueError(f"Unsupported operator: {op}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result")
    parser.add_argument("--gates", default=str(PACK_ROOT / "acceptance_gates.json"))
    parser.add_argument("--output", default=str(PACK_ROOT / "artifacts" / "reports" / "gate_evaluation.json"))
    args = parser.parse_args()

    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    gates = json.loads(Path(args.gates).read_text(encoding="utf-8"))
    evaluations = []
    required_pass = True
    optional_failures = 0
    for group, checks in gates["groups"].items():
        for check in checks:
            missing = False
            try:
                actual = get_path(result, check["path"])
                passed = compare(actual, check["op"], check["value"])
            except KeyError:
                actual = None
                missing = True
                passed = False
            item = {"group": group, **check, "actual": actual, "missing": missing, "passed": passed}
            evaluations.append(item)
            if check.get("required", True) and not passed:
                required_pass = False
            elif not check.get("required", True) and not passed:
                optional_failures += 1

    report = {
        "passed": required_pass,
        "optional_failures": optional_failures,
        "evaluations": evaluations,
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for item in evaluations:
        mark = "PASS" if item["passed"] else ("MISS" if item["missing"] else "FAIL")
        req = "required" if item.get("required", True) else "optional"
        print(f"{mark:4} {req:8} {item['group']}.{item['path']}: {item['actual']} {item['op']} {item['value']}")
    print(output)
    return 0 if required_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
