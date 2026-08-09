#!/usr/bin/env python3
"""Run Balanced CPU triplet-extract on persisted eligible units."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_openie import (
    OpenIEUnit,
    get_triplet_extract_cpu_provider,
    run_openie_extraction,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eligibility", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    documents = []
    for input_path in args.inputs:
        path = input_path.resolve()
        documents.append(normalize_document(path.stem, path.read_text(encoding="utf-8"), str(path)))
    units = [
        OpenIEUnit(
            unit_id=str(row["unit_id"]),
            document_id=str(row["document_id"]),
            start=int(row["start"]),
            end=int(row["end"]),
            text=str(row["source_text"]),
            eligible=bool(row["eligible"]),
        )
        for line in args.eligibility.read_text(encoding="utf-8").splitlines()
        if line
        for row in (json.loads(line),)
    ]
    output = run_openie_extraction(documents, units, get_triplet_extract_cpu_provider())
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "openie_raw_propositions.jsonl").write_text(
        "".join(
            json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n"
            for item in output.propositions
        ),
        encoding="utf-8",
    )
    (output_dir / "openie_report.json").write_text(
        json.dumps(output.report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output.report, indent=2, sort_keys=True))
    return 0 if output.report["conservation"] and output.report["exact_evidence_alignment"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
