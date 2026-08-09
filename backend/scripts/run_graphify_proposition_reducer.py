#!/usr/bin/env python3
"""Collapse equivalent OpenIE renderings into provenance-preserving families."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import AdaptedOpenIEArgumentV1, OpenIERawPropositionV1
from services.extraction.graphify_proposition_reducer import reduce_openie_propositions


def _load(path: Path, model):
    return [model.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--propositions", type=Path, required=True)
    parser.add_argument("--arguments", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = reduce_openie_propositions(
        _load(args.propositions, OpenIERawPropositionV1),
        _load(args.arguments, AdaptedOpenIEArgumentV1),
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "proposition_families.jsonl").write_text(
        "".join(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n" for item in output.families),
        encoding="utf-8",
    )
    (output_dir / "proposition_reducer_report.json").write_text(
        json.dumps(output.report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output.report, indent=2, sort_keys=True))
    return 0 if output.report["conservation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
