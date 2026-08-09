#!/usr/bin/env python3
"""Reduce persisted raw mentions into document-local entity clusters."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import RawMentionV1, stable_digest
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_reducer import reduce_document_entities
from services.extraction.graphify_survey import survey_document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-mentions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    mentions_by_document = defaultdict(list)
    for line in args.raw_mentions.read_text(encoding="utf-8").splitlines():
        if line:
            mention = RawMentionV1.model_validate_json(line)
            mentions_by_document[mention.document_id].append(mention)
    all_entities = []
    all_assignments = []
    document_reports = []
    for input_path in args.inputs:
        path = input_path.resolve()
        document = normalize_document(path.stem, path.read_text(encoding="utf-8"), str(path))
        output = reduce_document_entities(
            document, mentions_by_document[document.document_id], survey_document(document),
        )
        all_entities.extend(output.entities)
        all_assignments.extend(output.assignments)
        document_reports.append(output.report)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "document_entities.jsonl").write_text(
        "".join(json.dumps(entity.model_dump(mode="json"), sort_keys=True) + "\n" for entity in all_entities),
        encoding="utf-8",
    )
    (output_dir / "mention_assignments.jsonl").write_text(
        "".join(json.dumps(item.as_dict(), sort_keys=True) + "\n" for item in all_assignments),
        encoding="utf-8",
    )
    states = Counter(entity.state.value for entity in all_entities)
    report = {
        "schema_version": "polymath.corpus_entity_reducer_report.v1",
        "status": "passed",
        "documents": len(document_reports),
        "aligned_raw_mentions": sum(int(item["aligned_raw_mentions"]) for item in document_reports),
        "terminal_assignments": len(all_assignments),
        "conservation": all(bool(item["conservation"]) for item in document_reports),
        "clusters": len(all_entities),
        "state_counts": dict(sorted(states.items())),
        "strong_singletons": sum(int(item["strong_singletons"]) for item in document_reports),
        "survey_only_clusters": sum(int(item["survey_only_clusters"]) for item in document_reports),
        "hard_entity_cap": None,
        "identity_digest": stable_digest({
            "entities": [entity.model_dump(mode="json") for entity in all_entities],
            "assignments": [item.as_dict() for item in all_assignments],
        }),
        "document_reports": document_reports,
    }
    (output_dir / "reducer_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["conservation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
