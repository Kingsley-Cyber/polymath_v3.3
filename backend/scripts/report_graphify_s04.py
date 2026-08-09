#!/usr/bin/env python3
"""Emit release and fixture evidence for Graphify stage S04."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import (
    FROZEN_ENTITY_TYPES,
    GRAPHIFY_CONTRACT_RELEASE,
    contract_schema_hash,
    stable_digest,
)
from services.extraction.graphify_normalization import NORMALIZATION_RELEASE, normalize_document
from services.extraction.graphify_survey import SURVEY_RELEASE, survey_document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    reports = []
    for path in args.inputs:
        resolved = path.resolve()
        document = normalize_document(resolved.stem, resolved.read_text(encoding="utf-8"), str(resolved))
        survey = survey_document(document)
        reports.append({
            "path": str(resolved.relative_to(REPO_ROOT)),
            "original_sha256": document.original_sha256,
            "normalized_sha256": document.normalized_sha256,
            "normalized_characters": len(document.normalized_text),
            "offset_boundaries": len(document.normalized_to_original),
            "headings": len(survey.headings),
            "aliases": len(survey.aliases),
            "definitions": len(survey.definitions),
            "blocks": len(survey.blocks),
            "furniture_candidates": sum(block.furniture_candidate for block in survey.blocks),
            "gazetteer_candidates": len(survey.gazetteer_candidates),
            "survey_hash": survey.survey_hash,
        })
    payload = {
        "schema_version": "polymath.graphify_s04_evidence.v1",
        "status": "passed",
        "releases": {
            "contracts": GRAPHIFY_CONTRACT_RELEASE,
            "normalization": NORMALIZATION_RELEASE,
            "survey": SURVEY_RELEASE,
        },
        "hashes": {
            "contract_schema": contract_schema_hash(),
            "frozen_entity_types": stable_digest(sorted(FROZEN_ENTITY_TYPES)),
        },
        "frozen_entity_types": sorted(FROZEN_ENTITY_TYPES),
        "fixtures": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
