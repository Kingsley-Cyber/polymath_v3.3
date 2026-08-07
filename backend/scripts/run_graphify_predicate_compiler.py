#!/usr/bin/env python3
"""Compile persisted OpenIE proposition families into bounded predicates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1, OpenIEPropositionFamilyV1, OpenIERawPropositionV1,
)
from services.extraction.graphify_predicate_compiler import compile_openie_predicates


def _load(path: Path, model):
    return [model.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--families", type=Path, required=True)
    parser.add_argument("--propositions", type=Path, required=True)
    parser.add_argument("--arguments", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = compile_openie_predicates(
        _load(args.families, OpenIEPropositionFamilyV1),
        _load(args.propositions, OpenIERawPropositionV1),
        _load(args.arguments, AdaptedOpenIEArgumentV1),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "predicate_candidates.jsonl").write_text(
        "".join(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n" for item in result.candidates),
        encoding="utf-8",
    )
    (args.output_dir / "predicate_compiler_report.json").write_text(
        json.dumps(result.report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0 if result.report["decision_conservation"] and not result.report["forced_related_to_fallbacks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
