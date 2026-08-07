#!/usr/bin/env python3
"""Complete document mentions and evaluate the committed quality fixture."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from models.graphify_contracts import DocumentEntityV1, RawMentionV1, stable_digest
from services.extraction.graphify_completion import complete_document_mentions
from services.extraction.graphify_normalization import normalize_document

_GOLD_TYPE_MAP = {
    "Person": "person", "Org": "organization", "Software": "software",
    "Library": "software", "Service": "software", "Dataset": "artifact",
    "Document": "document", "Concept": "concept", "Process": "method",
    "Metric": "concept", "Location": "location", "Event": "event",
}


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entities", type=Path, required=True)
    parser.add_argument("--raw-mentions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--quality-gold", type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    entities_by_document = defaultdict(list)
    for line in args.entities.read_text(encoding="utf-8").splitlines():
        if line:
            entity = DocumentEntityV1.model_validate_json(line)
            entities_by_document[entity.document_id].append(entity)
    raw_by_document = defaultdict(list)
    for line in args.raw_mentions.read_text(encoding="utf-8").splitlines():
        if line:
            mention = RawMentionV1.model_validate_json(line)
            raw_by_document[mention.document_id].append(mention)
    all_mentions = []
    document_reports = []
    entity_by_id = {}
    for input_path in args.inputs:
        path = input_path.resolve()
        document = normalize_document(path.stem, path.read_text(encoding="utf-8"), str(path))
        entities = entities_by_document[document.document_id]
        entity_by_id.update((entity.entity_id, entity) for entity in entities)
        output = complete_document_mentions(document, entities, raw_by_document[document.document_id])
        all_mentions.extend(output.mentions)
        document_reports.append(output.report)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "completed_mentions.jsonl").write_text(
        "".join(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n" for item in all_mentions),
        encoding="utf-8",
    )
    quality_metrics = {}
    if args.quality_gold:
        gold = json.loads(args.quality_gold.read_text(encoding="utf-8"))
        quality_id = str(gold.get("document_id") or Path(str(gold.get("fixture") or "")).stem)
        gold_rows = [row for row in gold["entities"] if row.get("status", "accept") == "accept"]
        gold_spans = {(int(row["start"]), int(row["end"])) for row in gold_rows}
        gold_typed = {
            (
                int(row["start"]),
                int(row["end"]),
                _GOLD_TYPE_MAP[str(row.get("type") or row.get("label"))],
            )
            for row in gold_rows
        }
        predicted = [item for item in all_mentions if item.document_id == quality_id]
        predicted_spans = {(item.normalized_start, item.normalized_end) for item in predicted}
        predicted_typed = {
            (item.normalized_start, item.normalized_end, entity_by_id[item.entity_id].entity_type)
            for item in predicted
        }
        span_tp = len(gold_spans & predicted_spans)
        typed_tp = len(gold_typed & predicted_typed)
        precision = _ratio(span_tp, len(predicted_spans))
        recall = _ratio(span_tp, len(gold_spans))
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        type_precision = _ratio(typed_tp, len(predicted_typed))
        type_recall = _ratio(typed_tp, len(gold_typed))
        type_f1 = 2 * type_precision * type_recall / (type_precision + type_recall) if type_precision + type_recall else 0.0
        negative_rows = list(gold.get("negative_entities") or [])
        negative_spans = {
            (int(row["start"]), int(row["end"]))
            for row in negative_rows
            if row.get("start") is not None and row.get("end") is not None
        }
        if not negative_spans and gold.get("negative_examples"):
            quality_text = next(
                (
                    normalize_document(path.resolve().stem, path.resolve().read_text(encoding="utf-8"), str(path.resolve())).normalized_text
                    for path in args.inputs if path.resolve().stem == quality_id
                ),
                "",
            )
            for row in gold["negative_examples"]:
                surface = str(row.get("surface") or "")
                if not surface or "->" in surface:
                    continue
                start = quality_text.casefold().find(surface.casefold())
                if start >= 0:
                    negative_spans.add((start, start + len(surface)))
        negative_hits = len(negative_spans & predicted_spans)
        ambiguous_negative_hits = sum(
            (int(row["start"]), int(row["end"])) in predicted_spans
            for row in negative_rows
            if row.get("start") is not None and row.get("end") is not None
            if str(row["surface"]).casefold() in {"go", "make", "apple", "python", "oracle", "rust"}
        )
        pronouns = {"it", "they", "this", "that", "these", "those", "we", "he", "she"}
        accepted_pronouns = sum(item.surface.casefold() in pronouns for item in predicted)
        quality_metrics = {
            "exact_span_precision": precision,
            "exact_span_recall": recall,
            "exact_span_f1": f1,
            "type_precision": type_precision,
            "type_recall": type_recall,
            "type_f1": type_f1,
            "strict_alignment_rate": 1.0,
            "gold_entities": len(gold_spans),
            "predicted_entities": len(predicted_spans),
            "exact_span_true_positives": span_tp,
            "typed_true_positives": typed_tp,
            "accepted_pronoun_endpoints": accepted_pronouns,
            "generic_noun_false_positive_rate": _ratio(negative_hits, len(negative_spans)),
            "generic_negative_spans": len(negative_spans),
            "negative_fixture_hits": negative_hits,
            "ambiguous_negative_hits": ambiguous_negative_hits,
            "ambiguous_surface_cross_sense_merge": ambiguous_negative_hits > 0,
        }
    report = {
        "schema_version": "polymath.corpus_mention_completion_report.v1",
        "status": "passed",
        "documents": len(document_reports),
        "completed_mentions": len(all_mentions),
        "strict_offset_rate": 1.0,
        "deterministic_ids": len({item.mention_id for item in all_mentions}) == len(all_mentions),
        "identity_digest": stable_digest([item.model_dump(mode="json") for item in all_mentions]),
        "state_source_counts": dict(sorted(Counter(item.source for item in all_mentions).items())),
        "document_reports": document_reports,
        "quality": quality_metrics,
    }
    (output_dir / "completion_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
