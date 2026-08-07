#!/usr/bin/env python3
"""Run the isolated Graphify entity census over one or more Markdown files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from services.extraction.graphify_census import JsonlRawMentionSink, run_entity_census
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_provider_registry import canonical_entity_provider
from services.extraction.graphify_survey import survey_document


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    documents = []
    surveys = []
    for input_path in args.inputs:
        path = input_path.resolve()
        document = normalize_document(path.stem, path.read_text(encoding="utf-8"), str(path))
        documents.append(document)
        surveys.append(survey_document(document))
    sink = JsonlRawMentionSink(output_dir / "raw_mentions.jsonl")
    output = run_entity_census(documents, surveys, canonical_entity_provider(), sink)
    _write_jsonl(output_dir / "windows.jsonl", output.windows)
    (output_dir / "census_report.json").write_text(
        json.dumps(output.report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    offset_report = {
        "schema_version": "polymath.census_offset_report.v1",
        "status": "passed" if output.report["strict_alignment_rate"] == 1.0 else "failed",
        "emitted_predictions": output.report["emitted_predictions"],
        "aligned_mentions": output.report["aligned_mentions"],
        "alignment_failures": output.report["alignment_failures"],
        "strict_alignment_rate": output.report["strict_alignment_rate"],
        "local_offsets_checked": output.report["emitted_predictions"],
        "normalized_document_offsets_checked": output.report["emitted_predictions"],
        "original_source_offsets_checked": output.report["emitted_predictions"],
    }
    (output_dir / "offset_validation.json").write_text(
        json.dumps(offset_report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps({**output.report, "output_dir": str(output_dir)}, indent=2, sort_keys=True))
    return 0 if output.report["conservation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
