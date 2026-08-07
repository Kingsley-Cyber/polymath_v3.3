#!/usr/bin/env python3
"""Assemble persisted OpenIE predicate candidates into authority lanes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import AdaptedOpenIEArgumentV1, OpenIEPredicateCandidateV1
from services.extraction.graphify_assertion_assembler import assemble_openie_assertions


def _load(path: Path, model):
    return [model.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--arguments", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = assemble_openie_assertions(
        _load(args.candidates, OpenIEPredicateCandidateV1),
        _load(args.arguments, AdaptedOpenIEArgumentV1),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "assertion_decisions.jsonl").write_text(
        "".join(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n" for item in result.assertions),
        encoding="utf-8",
    )
    (args.output_dir / "assertion_report.json").write_text(
        json.dumps(result.report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0 if result.report["decision_conservation"] and not result.report["wildcard_endpoints"] and not result.report["qualified_promoted_to_fact"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
