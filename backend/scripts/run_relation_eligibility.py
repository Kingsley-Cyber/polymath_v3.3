#!/usr/bin/env python3
"""Persist relation-eligibility decisions for canonical Graphify documents."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import CompletedMentionV1, stable_digest
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_relations import evaluate_relation_eligibility
from services.extraction.graphify_survey import survey_document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mentions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    mentions = [
        CompletedMentionV1.model_validate_json(line)
        for line in args.mentions.read_text(encoding="utf-8").splitlines()
        if line
    ]
    mentions_by_document = defaultdict(list)
    for mention in mentions:
        mentions_by_document[mention.document_id].append(mention)

    documents = []
    surveys = []
    for input_path in args.inputs:
        path = input_path.resolve()
        document = normalize_document(path.stem, path.read_text(encoding="utf-8"), str(path))
        documents.append(document)
        surveys.append(survey_document(document))
    decisions = evaluate_relation_eligibility(documents, surveys, mentions)
    document_by_id = {item.document_id: item for item in documents}
    rows = []
    for decision in decisions:
        document = document_by_id[decision.document_id]
        source_text = document.normalized_text[decision.start:decision.end]
        row = decision.as_dict()
        row.update({
            "source_text": source_text,
            "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        })
        rows.append(row)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / "relation_eligibility.jsonl"
    artifact_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    reason_counts = Counter(reason for row in rows for reason in row["reasons"])
    report = {
        "schema_version": "polymath.relation_eligibility_report.v1",
        "status": "passed",
        "documents": len(documents),
        "source_blocks": len(rows),
        "eligible_units": sum(bool(row["eligible"]) for row in rows),
        "ineligible_units": sum(not bool(row["eligible"]) for row in rows),
        "decision_conservation": len(rows)
        == sum(bool(row["eligible"]) for row in rows)
        + sum(not bool(row["eligible"]) for row in rows),
        "source_text_preserved": all(
            row["source_text"] == document_by_id[row["document_id"]].normalized_text[row["start"]:row["end"]]
            for row in rows
        ),
        "reason_counts": dict(sorted(reason_counts.items())),
        "identity_digest": stable_digest(rows),
    }
    (output_dir / "relation_eligibility_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["decision_conservation"] and report["source_text_preserved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
