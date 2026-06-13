#!/usr/bin/env python3
"""Score a local extraction report against a deterministic gold answer sheet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from autoresearch_polymath_local_extraction import (  # noqa: E402
    bad_surface,
    entity_candidates,
    find_surface_for_label,
    gold_entity_labels,
    infer_entity_type,
    names_match,
    norm_key,
    score_results_against_gold,
)


def select_results(report: dict[str, Any], report_index: int) -> list[dict[str, Any]]:
    if isinstance(report.get("reports"), list):
        return list(report["reports"][report_index].get("results") or [])
    payload = report.get("payload")
    if isinstance(payload, dict):
        return list(payload.get("results") or [])
    if isinstance(report.get("results"), list):
        return list(report.get("results") or [])
    raise ValueError("report does not contain reports[].results, payload.results, or results")


def max_counts(gold: dict[str, Any]) -> dict[str, int]:
    entity_total = 0
    relation_total = 0
    for entry in gold.values():
        entity_total += len(gold_entity_labels(entry))
        relation_total += len(entry.get("relations") or [])
    return {
        "max_entity_tp": entity_total,
        "max_relation_tp": relation_total,
        "max_total_tp": entity_total + relation_total,
    }


def load_samples_by_id(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_id = str(row.get("id") or row.get("fixture_id") or row.get("chunk_id") or "")
        if sample_id:
            out[sample_id] = row
    return out


def result_by_id(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for result in results:
        sample_id = str(result.get("id") or "")
        if sample_id:
            out[sample_id] = result
    return out


def counter_add(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def surface_features(label: str) -> list[str]:
    value = str(label or "").strip()
    key = norm_key(value)
    tokens = key.split()
    features: list[str] = []
    if len(tokens) == 1:
        features.append("single_token")
    elif len(tokens) >= 2:
        features.append("multi_word")
    if value.isupper() and 2 <= len(value) <= 8:
        features.append("acronym")
    if value.islower():
        features.append("lowercase")
    if "-" in value:
        features.append("hyphenated")
    if any(ch.isdigit() for ch in value):
        features.append("numeric")
    if any(ch.isupper() for ch in value[1:]) and not value.isupper():
        features.append("mixed_or_title_case")
    if len(tokens) >= 4:
        features.append("long_phrase")
    return features or ["plain"]


def context_features(label: str, text: str) -> list[str]:
    surface = find_surface_for_label(label, text) if text else None
    if not surface:
        return ["not_found_in_text"]
    lower_surface = surface.lower()
    out: list[str] = []
    for line in text.splitlines():
        if lower_surface not in line.lower():
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            out.append("heading")
        if stripped.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.")):
            out.append("list_item")
        if "`" in line:
            out.append("code_or_inline_code")
        if "(" in line and ")" in line:
            out.append("parenthetical")
        if len(stripped) < 80 and not stripped.endswith((".", "?", "!")):
            out.append("fragment_or_title")
        break
    return sorted(set(out)) or ["sentence_body"]


def report_candidate_surfaces(result: dict[str, Any]) -> list[str]:
    candidates = result.get("entity_candidates") or []
    out: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("surface") or "")
        else:
            text = str(getattr(item, "text", "") or "")
        if text:
            out.append(text)
    return out


def clean_object_entity_names(result: dict[str, Any]) -> list[str]:
    clean = result.get("clean_object") or {}
    return [
        str(item.get("canonical_name") or item.get("surface_form") or "")
        for item in clean.get("entities") or []
    ]


def clean_object_relations(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    clean = result.get("clean_object") or {}
    return [
        (
            str(item.get("subject") or ""),
            str(item.get("predicate") or ""),
            str(item.get("object") or ""),
        )
        for item in clean.get("relations") or []
    ]


def any_name_match(label: str, choices: list[str]) -> bool:
    return any(names_match(label, choice) for choice in choices)


def build_gap_diagnostics(
    *,
    score: dict[str, Any],
    results: list[dict[str, Any]],
    gold: dict[str, Any],
    samples_by_id: dict[str, dict[str, Any]],
    max_generated_candidates: int,
) -> dict[str, Any]:
    del gold
    by_id = result_by_id(results)
    summary = {
        "missed_entity_gap_category_counts": {},
        "missed_entity_type_counts": {},
        "missed_entity_surface_feature_counts": {},
        "missed_entity_context_feature_counts": {},
        "extra_entity_surface_feature_counts": {},
        "extra_entity_dirty_surface_count": 0,
    }
    per_chunk: list[dict[str, Any]] = []

    for chunk_score in score.get("per_chunk") or []:
        sample_id = str(chunk_score.get("id") or "")
        result = by_id.get(sample_id) or {}
        sample = samples_by_id.get(sample_id) or {}
        text = str(sample.get("text") or "")
        predicted = clean_object_entity_names(result)
        report_candidates = report_candidate_surfaces(result)
        generated_candidates = [
            item.text for item in entity_candidates(text, max_generated_candidates)
        ] if text else []
        relations = clean_object_relations(result)
        relation_endpoint_names = {sub for sub, _, _ in relations} | {obj for _, _, obj in relations}

        missed_details: list[dict[str, Any]] = []
        for label in chunk_score.get("missed_entities") or []:
            entity_type = infer_entity_type(str(label))
            surface = find_surface_for_label(str(label), text) if text else None
            in_report_candidates = any_name_match(str(label), report_candidates)
            in_generated_candidates = any_name_match(str(label), generated_candidates)
            if not surface:
                gap_category = "not_found_in_chunk_text"
            elif in_report_candidates:
                gap_category = "candidate_not_selected_or_pruned"
            elif in_generated_candidates:
                gap_category = "candidate_generator_can_find_but_report_did_not_include"
            else:
                gap_category = "missing_from_candidate_generator"

            counter_add(summary["missed_entity_gap_category_counts"], gap_category)
            counter_add(summary["missed_entity_type_counts"], entity_type)
            features = surface_features(str(label))
            contexts = context_features(str(label), text)
            for feature in features:
                counter_add(summary["missed_entity_surface_feature_counts"], feature)
            for context in contexts:
                counter_add(summary["missed_entity_context_feature_counts"], context)
            missed_details.append(
                {
                    "label": label,
                    "inferred_type": entity_type,
                    "gap_category": gap_category,
                    "surface_found": surface,
                    "surface_features": features,
                    "context_features": contexts,
                    "in_report_candidates": in_report_candidates,
                    "in_generated_candidates": in_generated_candidates,
                }
            )

        extra_details: list[dict[str, Any]] = []
        for label in chunk_score.get("extra_entities") or []:
            features = surface_features(str(label))
            for feature in features:
                counter_add(summary["extra_entity_surface_feature_counts"], feature)
            dirty = bad_surface(str(label))
            summary["extra_entity_dirty_surface_count"] += int(dirty)
            extra_details.append(
                {
                    "label": label,
                    "surface_features": features,
                    "dirty_surface": dirty,
                    "used_as_relation_endpoint": any_name_match(str(label), list(relation_endpoint_names)),
                }
            )

        per_chunk.append(
            {
                "id": sample_id,
                "entity_expected": chunk_score.get("entity_expected"),
                "entity_predicted": chunk_score.get("entity_predicted"),
                "entity_correct": chunk_score.get("entity_correct"),
                "entity_recall": chunk_score.get("entity_recall"),
                "relation_recall": chunk_score.get("relation_recall"),
                "missed_entities": missed_details,
                "extra_entities": extra_details,
                "predicted_entities": predicted,
                "predicted_relations": relations,
            }
        )

    return {"summary": summary, "per_chunk": per_chunk}


def grade(score: float) -> str:
    if score >= 0.95:
        return "production_candidate"
    if score >= 0.90:
        return "near_cloud_target"
    if score >= 0.80:
        return "research_promising"
    if score >= 0.65:
        return "prototype_only"
    return "not_ready"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report-index", type=int, default=0)
    parser.add_argument("--samples", type=Path, default=None)
    parser.add_argument("--diagnostics-out", type=Path, default=None)
    parser.add_argument("--max-generated-candidates", type=int, default=240)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    results = select_results(report, args.report_index)
    samples_by_id = load_samples_by_id(args.samples)
    score = score_results_against_gold(results, gold)
    maxima = max_counts(gold)
    score["max_counts"] = maxima
    score["grade"] = {
        "entity": grade(float(score.get("entity_f1") or 0)),
        "relation": grade(float(score.get("relation_f1") or 0)),
        "graph": grade(float(score.get("graph_f1") or 0)),
    }
    diagnostics = build_gap_diagnostics(
        score=score,
        results=results,
        gold=gold,
        samples_by_id=samples_by_id,
        max_generated_candidates=args.max_generated_candidates,
    ) if samples_by_id else {}
    output = {
        "schema": "local_extraction_gold_score_v1",
        "report_path": str(args.report),
        "gold_path": str(args.gold),
        "samples_path": str(args.samples) if args.samples else None,
        "report_index": args.report_index,
        "score": score,
        "diagnostics": diagnostics,
    }
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.diagnostics_out:
        args.diagnostics_out.write_text(
            json.dumps(diagnostics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print("LOCAL EXTRACTION GOLD SCORE")
    print(f"gold chunks: {len(gold)}")
    print(
        "max TP E/R/total: "
        f"{maxima['max_entity_tp']}/{maxima['max_relation_tp']}/{maxima['max_total_tp']}"
    )
    print(
        "entity P/R/F1: "
        f"{score['entity_precision']*100:.1f}% / "
        f"{score['entity_recall']*100:.1f}% / "
        f"{score['entity_f1']*100:.1f}%"
    )
    print(
        "relation P/R/F1: "
        f"{score['relation_precision']*100:.1f}% / "
        f"{score['relation_recall']*100:.1f}% / "
        f"{score['relation_f1']*100:.1f}%"
    )
    print(f"graph F1: {score['graph_f1']*100:.1f}% ({score['grade']['graph']})")
    print(
        "TP/FP/FN entities: "
        f"{score['entity_tp']}/{score['entity_fp']}/{score['entity_fn']}"
    )
    print(
        "TP/FP/FN relations: "
        f"{score['relation_tp']}/{score['relation_fp']}/{score['relation_fn']}"
    )
    if diagnostics:
        diag = diagnostics.get("summary") or {}
        print("missed entity gap categories:")
        for key, value in sorted((diag.get("missed_entity_gap_category_counts") or {}).items()):
            print(f"  {key}: {value}")
        print("missed entity inferred types:")
        for key, value in sorted((diag.get("missed_entity_type_counts") or {}).items()):
            print(f"  {key}: {value}")
        print("missed entity surface features:")
        for key, value in sorted((diag.get("missed_entity_surface_feature_counts") or {}).items()):
            print(f"  {key}: {value}")
    print(f"wrote {args.out}")
    if args.diagnostics_out:
        print(f"wrote diagnostics {args.diagnostics_out}")


if __name__ == "__main__":
    main()
