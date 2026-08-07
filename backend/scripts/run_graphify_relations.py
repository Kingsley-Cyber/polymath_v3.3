#!/usr/bin/env python3
"""Run the parse-once relation lane and score the committed quality fixture."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import CompletedMentionV1, DocumentEntityV1, stable_digest
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_relations import run_relation_fast_path
from services.extraction.graphify_survey import survey_document


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _write_jsonl(path: Path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entities", type=Path, required=True)
    parser.add_argument("--mentions", type=Path, required=True)
    parser.add_argument("--quality-gold", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    entities = [
        DocumentEntityV1.model_validate_json(line)
        for line in args.entities.read_text(encoding="utf-8").splitlines() if line
    ]
    mentions = [
        CompletedMentionV1.model_validate_json(line)
        for line in args.mentions.read_text(encoding="utf-8").splitlines() if line
    ]
    documents = []
    surveys = []
    text_by_document = {}
    for input_path in args.inputs:
        path = input_path.resolve()
        document = normalize_document(path.stem, path.read_text(encoding="utf-8"), str(path))
        documents.append(document)
        surveys.append(survey_document(document))
        text_by_document[document.document_id] = document.normalized_text

    output = run_relation_fast_path(documents, surveys, mentions, entities)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "relation_eligibility.jsonl", [item.as_dict() for item in output.eligibility])
    _write_jsonl(
        output_dir / "relation_endpoint_entities.jsonl",
        [item.model_dump(mode="json") for item in output.endpoint_entities],
    )
    _write_jsonl(
        output_dir / "relation_endpoint_mentions.jsonl",
        [item.model_dump(mode="json") for item in output.endpoint_mentions],
    )
    _write_jsonl(
        output_dir / "surface_relations.jsonl",
        [item.model_dump(mode="json") for item in output.surface_relations],
    )
    _write_jsonl(
        output_dir / "mapped_relations.jsonl",
        [item.model_dump(mode="json") for item in output.mapped_relations],
    )
    _write_jsonl(
        output_dir / "assertion_decisions.jsonl",
        [item.model_dump(mode="json") for item in output.assertions],
    )

    mention_by_id = {
        item.mention_id: item for item in [*mentions, *output.endpoint_mentions]
    }
    quality_gold = json.loads(args.quality_gold.read_text(encoding="utf-8"))
    quality_id = str(quality_gold["document_id"])

    def gold_key(row: dict) -> tuple[int, int, int, int, str | None]:
        return (
            int(row["subject_start"]), int(row["subject_end"]),
            int(row["object_start"]), int(row["object_end"]), row.get("predicate"),
        )

    def prediction_key(row) -> tuple[int, int, int, int, str | None]:
        subject = mention_by_id[row.subject_mention_id]
        object_mention = mention_by_id[row.object_mention_id]
        return (
            subject.normalized_start, subject.normalized_end,
            object_mention.normalized_start, object_mention.normalized_end,
            row.canonical_candidate,
        )

    quality_predictions = [row for row in output.mapped_relations if row.document_id == quality_id]
    accepted_gold = {gold_key(row) for row in quality_gold["relations"] if row["lane"] == "accept"}
    accepted_pairs = {key[:4] for key in accepted_gold}
    qualified_gold = {gold_key(row) for row in quality_gold["relations"] if row["lane"] == "qualified"}
    open_gold = {gold_key(row)[:4] for row in quality_gold["relations"] if row["lane"] == "open"}
    passive_gold = {
        gold_key(row) for row in quality_gold["relations"]
        if row["lane"] == "accept" and row.get("voice") == "passive"
    }
    predicted_accepted = {
        prediction_key(row) for row in quality_predictions if row.terminal_state.value == "accepted"
    }
    predicted_qualified = {
        prediction_key(row) for row in quality_predictions if row.terminal_state.value == "qualified"
    }
    predicted_open = {
        prediction_key(row)[:4] for row in quality_predictions if row.terminal_state.value == "open"
    }
    accepted_tp = accepted_gold & predicted_accepted
    precision = _ratio(len(accepted_tp), len(predicted_accepted))
    recall = _ratio(len(accepted_tp), len(accepted_gold))
    f1 = _ratio(2 * precision * recall, precision + recall)
    evidence_alignment = all(
        text_by_document[row.document_id][row.evidence_start:row.evidence_end] == row.evidence_text
        for row in output.mapped_relations
    )
    unsupported = sum(
        row.terminal_state.value == "accepted"
        and (row.canonical_candidate is None or not row.mapping_rule.startswith("mapped:"))
        for row in output.mapped_relations
    )
    forced_related = sum(
        row.canonical_candidate == "related_to" and "related" not in row.surface_predicate.casefold()
        for row in output.mapped_relations
    )
    metrics = {
        "final_pair_recall": _ratio(
            len(accepted_pairs & {key[:4] for key in predicted_accepted}), len(accepted_pairs)
        ),
        "directed_triple_precision": precision,
        "directed_triple_recall": recall,
        "directed_triple_f1": f1,
        "direction_fixture_accuracy": _ratio(len(passive_gold & predicted_accepted), len(passive_gold)),
        "qualification_fixture_accuracy": _ratio(
            len(qualified_gold & predicted_qualified), len(qualified_gold)
        ),
        "open_relation_recall": _ratio(len(open_gold & predicted_open), len(open_gold)),
        "exact_evidence_alignment": 1.0 if evidence_alignment else 0.0,
        "unsupported_canonical_edges": unsupported,
        "forced_related_to_fallbacks": forced_related,
        "accepted_gold": len(accepted_gold),
        "accepted_predictions": len(predicted_accepted),
        "accepted_true_positives": len(accepted_tp),
        "qualified_gold": len(qualified_gold),
        "qualified_true_positives": len(qualified_gold & predicted_qualified),
        "passive_gold": len(passive_gold),
        "passive_true_positives": len(passive_gold & predicted_accepted),
        "open_gold": len(open_gold),
        "open_true_positives": len(open_gold & predicted_open),
    }
    report = dict(output.report)
    report["quality"] = metrics
    report["identity_digest"] = stable_digest({
        "lane_identity": output.report["identity_digest"],
        "quality": metrics,
    })
    (output_dir / "relation_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
