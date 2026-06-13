#!/usr/bin/env python3
"""Grade local extraction quality with a real failure rubric.

F1 alone is too blunt for Ghost B work. This rubric separates:

- contract safety: can the stack accept it?
- entity quality: did it find useful ontology nodes without noise?
- relation quality: did it connect the right endpoints with the right predicate?
- graph usefulness: is the accepted output worth writing?
- deployment readiness: speed after quality gates

The model is graded against a gold answer sheet, but the report also explains
what failed: missing endpoints, wrong predicate, wrong direction, over-linking,
evidence drops, truncation, and noisy entities.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from autoresearch_polymath_local_extraction import (  # noqa: E402
    bad_surface,
    gold_entity_labels,
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


def select_summary(report: dict[str, Any], report_index: int) -> dict[str, Any]:
    if isinstance(report.get("reports"), list):
        return dict(report["reports"][report_index].get("summary") or {})
    payload = report.get("payload")
    if isinstance(payload, dict):
        return dict(payload.get("summary") or {})
    return dict(report.get("summary") or {})


def load_samples_by_id(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sample_id = str(row.get("id") or row.get("fixture_id") or row.get("chunk_id") or "")
        if sample_id:
            out[sample_id] = row
    return out


def clean_entities(result: dict[str, Any]) -> list[dict[str, Any]]:
    return list((result.get("clean_object") or {}).get("entities") or [])


def clean_relations(result: dict[str, Any]) -> list[dict[str, Any]]:
    return list((result.get("clean_object") or {}).get("relations") or [])


def predicted_entity_names(result: dict[str, Any]) -> list[str]:
    return [str(item.get("canonical_name") or item.get("surface_form") or "") for item in clean_entities(result)]


def predicted_relation_tuples(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (
            str(item.get("subject") or ""),
            str(item.get("predicate") or "").lower(),
            str(item.get("object") or ""),
        )
        for item in clean_relations(result)
    ]


def expected_relation_tuples(gold_entry: dict[str, Any]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for rel in gold_entry.get("relations") or []:
        if isinstance(rel, list | tuple) and len(rel) == 3:
            out.append((str(rel[0]), str(rel[1]).lower(), str(rel[2])))
    return out


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def pct(value: float) -> float:
    return round(value * 100.0, 2)


def relation_exact(pred: tuple[str, str, str], gold: tuple[str, str, str]) -> bool:
    return pred[1] == gold[1] and names_match(pred[0], gold[0]) and names_match(pred[2], gold[2])


def relation_direct_endpoints(pred: tuple[str, str, str], gold: tuple[str, str, str]) -> bool:
    return names_match(pred[0], gold[0]) and names_match(pred[2], gold[2])


def relation_reversed_endpoints(pred: tuple[str, str, str], gold: tuple[str, str, str]) -> bool:
    return names_match(pred[0], gold[2]) and names_match(pred[2], gold[0])


def relation_any_endpoint_pair(pred: tuple[str, str, str], gold: tuple[str, str, str]) -> bool:
    return relation_direct_endpoints(pred, gold) or relation_reversed_endpoints(pred, gold)


def classify_extra_relation(
    pred: tuple[str, str, str],
    expected: list[tuple[str, str, str]],
) -> str:
    if pred[1] == "related_to":
        return "generic_related_to_extra"
    for gold in expected:
        if relation_direct_endpoints(pred, gold) and pred[1] != gold[1]:
            return "wrong_predicate_right_direction"
        if relation_reversed_endpoints(pred, gold) and pred[1] == gold[1]:
            return "wrong_direction_right_predicate"
        if relation_reversed_endpoints(pred, gold):
            return "wrong_direction_and_predicate"
    for gold in expected:
        subject_matches = names_match(pred[0], gold[0]) or names_match(pred[0], gold[2])
        object_matches = names_match(pred[2], gold[0]) or names_match(pred[2], gold[2])
        predicate_matches = pred[1] == gold[1]
        if predicate_matches and (subject_matches or object_matches):
            return "wrong_endpoint_with_right_predicate"
        if subject_matches or object_matches:
            return "unsupported_or_overbroad_endpoint"
    return "unsupported_extra_relation"


def classify_missed_relation(
    gold: tuple[str, str, str],
    predicted_entities: list[str],
    predicted: list[tuple[str, str, str]],
) -> str:
    subject_present = any(names_match(gold[0], entity) for entity in predicted_entities)
    object_present = any(names_match(gold[2], entity) for entity in predicted_entities)
    if not subject_present and not object_present:
        return "missed_relation_both_endpoints_missing"
    if not subject_present:
        return "missed_relation_subject_missing"
    if not object_present:
        return "missed_relation_object_missing"
    for pred in predicted:
        if relation_direct_endpoints(pred, gold) and pred[1] != gold[1]:
            return "missed_relation_wrong_predicate"
        if relation_reversed_endpoints(pred, gold):
            return "missed_relation_wrong_direction"
    return "missed_relation_endpoints_present_no_edge"


def greedy_exact_relation_matches(
    predicted: list[tuple[str, str, str]],
    expected: list[tuple[str, str, str]],
) -> tuple[set[int], set[int]]:
    used_gold: set[int] = set()
    used_pred: set[int] = set()
    for pred_idx, pred in enumerate(predicted):
        for gold_idx, gold in enumerate(expected):
            if gold_idx in used_gold:
                continue
            if relation_exact(pred, gold):
                used_gold.add(gold_idx)
                used_pred.add(pred_idx)
                break
    return used_pred, used_gold


def relation_diagnostics_for_chunk(
    result: dict[str, Any],
    gold_entry: dict[str, Any],
) -> dict[str, Any]:
    predicted = predicted_relation_tuples(result)
    expected = expected_relation_tuples(gold_entry)
    pred_entities = predicted_entity_names(result)
    exact_pred_idx, exact_gold_idx = greedy_exact_relation_matches(predicted, expected)

    extra_categories: dict[str, int] = {}
    extras: list[dict[str, Any]] = []
    for idx, pred in enumerate(predicted):
        if idx in exact_pred_idx:
            continue
        category = classify_extra_relation(pred, expected)
        extra_categories[category] = extra_categories.get(category, 0) + 1
        extras.append({"relation": list(pred), "category": category})

    missed_categories: dict[str, int] = {}
    missed: list[dict[str, Any]] = []
    for idx, gold in enumerate(expected):
        if idx in exact_gold_idx:
            continue
        category = classify_missed_relation(gold, pred_entities, predicted)
        missed_categories[category] = missed_categories.get(category, 0) + 1
        missed.append({"relation": list(gold), "category": category})

    endpoint_pair_hits = 0
    endpoint_pair_gold_hits: set[int] = set()
    right_direction_wrong_predicate = 0
    reversed_endpoint_hits = 0
    for pred in predicted:
        if any(relation_any_endpoint_pair(pred, gold) for gold in expected):
            endpoint_pair_hits += 1
        for gold_idx, gold in enumerate(expected):
            if relation_any_endpoint_pair(pred, gold):
                endpoint_pair_gold_hits.add(gold_idx)
            if relation_direct_endpoints(pred, gold) and pred[1] != gold[1]:
                right_direction_wrong_predicate += 1
            if relation_reversed_endpoints(pred, gold):
                reversed_endpoint_hits += 1

    return {
        "predicted_count": len(predicted),
        "expected_count": len(expected),
        "exact_count": len(exact_pred_idx),
        "extra_categories": extra_categories,
        "missed_categories": missed_categories,
        "extras": extras,
        "missed": missed,
        "endpoint_precision": safe_div(endpoint_pair_hits, len(predicted)),
        "endpoint_recall": safe_div(len(endpoint_pair_gold_hits), len(expected)),
        "right_direction_wrong_predicate": right_direction_wrong_predicate,
        "reversed_endpoint_hits": reversed_endpoint_hits,
    }


def entity_noise_stats(score: dict[str, Any], results_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    dirty_extra = 0
    generic_extra = 0
    extras_total = 0
    generic_terms = {
        "ai",
        "api",
        "app",
        "data",
        "image",
        "source",
        "model",
        "probably",
        "next app",
    }
    for chunk in score.get("per_chunk") or []:
        for extra in chunk.get("extra_entities") or []:
            extras_total += 1
            key = norm_key(str(extra))
            if bad_surface(str(extra)):
                dirty_extra += 1
            if key in generic_terms or len(key) <= 2:
                generic_extra += 1
    raw_bad_clean = 0
    for result in results_by_id.values():
        for entity in clean_entities(result):
            surface = str(entity.get("surface_form") or entity.get("canonical_name") or "")
            if bad_surface(surface):
                raw_bad_clean += 1
    return {
        "extra_entities_total": extras_total,
        "dirty_extra_entities": dirty_extra,
        "generic_extra_entities": generic_extra,
        "bad_clean_surfaces": raw_bad_clean,
        "noise_rate": safe_div(dirty_extra + generic_extra, extras_total),
    }


def result_by_id(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id") or ""): item for item in results if item.get("id")}


def report_contract_stats(results: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    samples = len(results)
    schema_pass = int(summary.get("schema_pass") or sum(1 for item in results if item.get("schema_ok")))
    truncations = int(summary.get("truncation_count") or sum(1 for item in results if item.get("truncated")))
    reasoning = int(summary.get("reasoning_responses") or sum(1 for item in results if item.get("reasoning_tokens_seen")))
    error_count = int(summary.get("error_count") or sum(len(item.get("errors") or []) for item in results))

    raw_entities = raw_relations = raw_facts = 0
    dropped_entities = dropped_relations = dropped_facts = 0
    for result in results:
        gate_counts = ((result.get("diagnostics") or {}).get("gate_counts") or {})
        raw_entities += int(gate_counts.get("raw_entities") or 0)
        raw_relations += int(gate_counts.get("raw_relations") or 0)
        raw_facts += int(gate_counts.get("raw_facts") or 0)
        dropped_entities += int(gate_counts.get("dropped_entities") or 0)
        dropped_relations += int(gate_counts.get("dropped_relations") or 0)
        dropped_facts += int(gate_counts.get("dropped_facts") or 0)

    raw_total = raw_entities + raw_relations + raw_facts
    dropped_total = dropped_entities + dropped_relations + dropped_facts
    return {
        "samples": samples,
        "schema_pass": schema_pass,
        "schema_pass_rate": safe_div(schema_pass, samples),
        "truncations": truncations,
        "truncation_rate": safe_div(truncations, samples),
        "reasoning_responses": reasoning,
        "reasoning_rate": safe_div(reasoning, samples),
        "error_count": error_count,
        "errors_per_chunk": safe_div(error_count, samples),
        "raw_entities": raw_entities,
        "raw_relations": raw_relations,
        "raw_facts": raw_facts,
        "dropped_entities": dropped_entities,
        "dropped_relations": dropped_relations,
        "dropped_facts": dropped_facts,
        "drop_rate": safe_div(dropped_total, raw_total),
    }


def aggregate_relation_diagnostics(
    results: list[dict[str, Any]],
    gold: dict[str, Any],
) -> dict[str, Any]:
    by_category_extra: dict[str, int] = {}
    by_category_missed: dict[str, int] = {}
    per_chunk: list[dict[str, Any]] = []
    endpoint_precision_values: list[float] = []
    endpoint_recall_values: list[float] = []
    reversed_hits = 0
    wrong_predicate_right_direction = 0
    for result in results:
        sample_id = str(result.get("id") or "")
        gold_entry = gold.get(sample_id) or {}
        diag = relation_diagnostics_for_chunk(result, gold_entry)
        for key, value in diag["extra_categories"].items():
            by_category_extra[key] = by_category_extra.get(key, 0) + value
        for key, value in diag["missed_categories"].items():
            by_category_missed[key] = by_category_missed.get(key, 0) + value
        endpoint_precision_values.append(float(diag["endpoint_precision"]))
        endpoint_recall_values.append(float(diag["endpoint_recall"]))
        reversed_hits += int(diag["reversed_endpoint_hits"])
        wrong_predicate_right_direction += int(diag["right_direction_wrong_predicate"])
        per_chunk.append({"id": sample_id, **diag})
    return {
        "extra_relation_categories": by_category_extra,
        "missed_relation_categories": by_category_missed,
        "endpoint_precision_avg": sum(endpoint_precision_values) / len(endpoint_precision_values) if endpoint_precision_values else 0.0,
        "endpoint_recall_avg": sum(endpoint_recall_values) / len(endpoint_recall_values) if endpoint_recall_values else 0.0,
        "reversed_endpoint_hits": reversed_hits,
        "right_direction_wrong_predicate": wrong_predicate_right_direction,
        "per_chunk": per_chunk,
    }


def readiness_label(score: float) -> str:
    if score >= 90:
        return "production_candidate"
    if score >= 80:
        return "near_cloud_candidate"
    if score >= 65:
        return "research_promising"
    if score >= 45:
        return "prototype_only"
    return "not_ready"


def compute_rubric(
    *,
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    gold: dict[str, Any],
) -> dict[str, Any]:
    score = score_results_against_gold(results, gold)
    by_id = result_by_id(results)
    contract = report_contract_stats(results, summary)
    relation_diag = aggregate_relation_diagnostics(results, gold)
    noise = entity_noise_stats(score, by_id)

    entity_precision = float(score.get("entity_precision") or 0)
    entity_recall = float(score.get("entity_recall") or 0)
    entity_f1 = float(score.get("entity_f1") or 0)
    relation_precision = float(score.get("relation_precision") or 0)
    relation_recall = float(score.get("relation_recall") or 0)
    relation_f1 = float(score.get("relation_f1") or 0)
    graph_f1 = float(score.get("graph_f1") or 0)

    # Contract score: a model can only be useful if it reliably emits
    # parseable, non-truncated, non-reasoning, low-drop outputs.
    contract_score = (
        6.0 * contract["schema_pass_rate"]
        + 5.0 * (1.0 - clamp01(contract["truncation_rate"]))
        + 2.0 * (1.0 - clamp01(contract["reasoning_rate"]))
        + 4.0 * (1.0 - clamp01(contract["drop_rate"]))
        + 3.0 * (1.0 - clamp01(contract["errors_per_chunk"] / 3.0))
    )

    noise_control = 1.0 - clamp01(noise["noise_rate"])
    entity_score = (
        8.0 * entity_precision
        + 8.0 * entity_recall
        + 5.0 * entity_f1
        + 4.0 * noise_control
    )

    endpoint_fit = (
        0.5 * float(relation_diag["endpoint_precision_avg"])
        + 0.5 * float(relation_diag["endpoint_recall_avg"])
    )
    predicate_direction_fit = safe_div(
        float(score.get("relation_tp") or 0),
        float(score.get("relation_tp") or 0)
        + float(relation_diag["right_direction_wrong_predicate"])
        + float(relation_diag["reversed_endpoint_hits"]),
    )
    relation_score = (
        12.0 * relation_precision
        + 12.0 * relation_recall
        + 8.0 * relation_f1
        + 4.0 * endpoint_fit
        + 4.0 * predicate_direction_fit
    )

    accepted_relations = int(summary.get("accepted_relations") or sum(len(clean_relations(item)) for item in results))
    relation_nonzero = 1.0 if accepted_relations > 0 else 0.0
    generic_extras = relation_diag["extra_relation_categories"].get("generic_related_to_extra", 0)
    relation_fp = int(score.get("relation_fp") or 0)
    specificity = 1.0 - clamp01(safe_div(generic_extras, relation_fp))
    graph_score = 10.0 * graph_f1 + 3.0 * relation_nonzero + 2.0 * specificity

    quality_score = contract_score + entity_score + relation_score + graph_score

    chunks_per_hour = float(summary.get("chunks_per_hour_wall") or 0.0)
    tok_s = float(summary.get("completion_tok_s_median") or 0.0)
    throughput_score = 6.0 * clamp01(math.log1p(chunks_per_hour) / math.log1p(15000.0))
    tok_score = 4.0 * clamp01(tok_s / 120.0)
    deployment_score = 0.90 * quality_score + throughput_score + tok_score

    return {
        "schema": "polymath_extraction_quality_rubric_v1",
        "score_100": {
            "quality_score": round(quality_score, 2),
            "deployment_score": round(deployment_score, 2),
            "label": readiness_label(quality_score),
        },
        "components": {
            "contract_safety_20": round(contract_score, 2),
            "entity_quality_25": round(entity_score, 2),
            "relation_quality_40": round(relation_score, 2),
            "graph_usefulness_15": round(graph_score, 2),
            "throughput_bonus_10": round(throughput_score + tok_score, 2),
        },
        "raw_metrics": {
            "entity_precision": pct(entity_precision),
            "entity_recall": pct(entity_recall),
            "entity_f1": pct(entity_f1),
            "relation_precision": pct(relation_precision),
            "relation_recall": pct(relation_recall),
            "relation_f1": pct(relation_f1),
            "graph_f1": pct(graph_f1),
            "entity_tp_fp_fn": [score.get("entity_tp"), score.get("entity_fp"), score.get("entity_fn")],
            "relation_tp_fp_fn": [score.get("relation_tp"), score.get("relation_fp"), score.get("relation_fn")],
            "chunks_per_hour": round(chunks_per_hour, 2),
            "completion_tok_s_median": round(tok_s, 2),
        },
        "contract": contract,
        "entity_noise": noise,
        "relation_diagnostics": relation_diag,
        "gold_score": score,
        "verdict": verdict(quality_score, score, contract, relation_diag),
    }


def verdict(
    quality_score: float,
    score: dict[str, Any],
    contract: dict[str, Any],
    relation_diag: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    if contract["schema_pass_rate"] < 1:
        out.append("Reject: schema/Pydantic pass is not 100%.")
    if contract["truncations"]:
        out.append("Reject: output truncated; increase max_tokens or shorten prompt/chunk.")
    if float(score.get("relation_precision") or 0) < 0.8:
        out.append("Reject for graph writes: relation precision below 80%.")
    if float(score.get("relation_recall") or 0) < 0.5:
        out.append("Weak relation recall: model is missing too many expected edges.")
    if relation_diag["extra_relation_categories"]:
        top_extra = sorted(relation_diag["extra_relation_categories"].items(), key=lambda item: (-item[1], item[0]))[:3]
        out.append("Top extra-relation failures: " + ", ".join(f"{k}={v}" for k, v in top_extra))
    if relation_diag["missed_relation_categories"]:
        top_missed = sorted(relation_diag["missed_relation_categories"].items(), key=lambda item: (-item[1], item[0]))[:3]
        out.append("Top missed-relation failures: " + ", ".join(f"{k}={v}" for k, v in top_missed))
    if quality_score >= 80:
        out.append("Candidate: quality is high enough for broader fixture testing.")
    elif quality_score >= 65:
        out.append("Research promising: keep testing, but do not write Neo4j edges by default.")
    else:
        out.append("Not ready: use only as a proposal/helper lane behind Python gates.")
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report-index", type=int, default=0)
    parser.add_argument("--samples", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    results = select_results(report, args.report_index)
    summary = select_summary(report, args.report_index)
    rubric = compute_rubric(results=results, summary=summary, gold=gold)
    output = {
        "report_path": str(args.report),
        "gold_path": str(args.gold),
        "samples_path": str(args.samples) if args.samples else None,
        "rubric": rubric,
    }
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    score = rubric["score_100"]
    components = rubric["components"]
    raw = rubric["raw_metrics"]
    print("POLYMATH EXTRACTION QUALITY RUBRIC")
    print(f"quality_score: {score['quality_score']}/100 ({score['label']})")
    print(f"deployment_score: {score['deployment_score']}/100")
    print(
        "components: "
        f"contract={components['contract_safety_20']}/20, "
        f"entity={components['entity_quality_25']}/25, "
        f"relation={components['relation_quality_40']}/40, "
        f"graph={components['graph_usefulness_15']}/15, "
        f"speed_bonus={components['throughput_bonus_10']}/10"
    )
    print(
        "E P/R/F1: "
        f"{raw['entity_precision']}% / {raw['entity_recall']}% / {raw['entity_f1']}%"
    )
    print(
        "R P/R/F1: "
        f"{raw['relation_precision']}% / {raw['relation_recall']}% / {raw['relation_f1']}%"
    )
    print(f"Graph F1: {raw['graph_f1']}%")
    print(f"TP/FP/FN E: {raw['entity_tp_fp_fn']} R: {raw['relation_tp_fp_fn']}")
    print("verdict:")
    for item in rubric["verdict"]:
        print(f"- {item}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
