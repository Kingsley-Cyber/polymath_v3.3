"""Shared scoring for semantic extraction parity artifacts."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any


_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize_name(value: Any) -> str:
    return " ".join(_NON_WORD.sub(" ", str(value or "").lower()).split())


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _entity_key(item: dict[str, Any]) -> str:
    return normalize_name(
        item.get("canonical_name") or item.get("surface_form") or item.get("text")
    )


def _relation_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_name(item.get("subject") or item.get("sub")),
        normalize_name(item.get("predicate") or item.get("pred")),
        normalize_name(item.get("object") or item.get("obj")),
    )


def _counter_overlap(
    predicted: Counter[Any], gold: Counter[Any]
) -> tuple[int, int, int]:
    tp = sum((predicted & gold).values())
    return tp, sum(predicted.values()) - tp, sum(gold.values()) - tp


def score_extraction_lane(
    samples: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score entity/relation identity plus evidence and endpoint invariants."""

    by_id = {
        str(item.get("id") or item.get("chunk_id")): item for item in results
    }
    predicted_entities: Counter[str] = Counter()
    gold_entities: Counter[str] = Counter()
    predicted_relations: Counter[tuple[str, str, str]] = Counter()
    gold_relations: Counter[tuple[str, str, str]] = Counter()
    evidence_total = 0
    evidence_exact = 0
    endpoint_total = 0
    endpoint_valid = 0
    schema_valid_results = 0
    missing_results = 0
    entity_type_correct = 0
    entity_type_total = 0

    for sample in samples:
        sample_id = str(sample["id"])
        result = by_id.get(sample_id)
        gold = sample.get("gold") or {}
        for item in gold.get("entities") or []:
            gold_entities[(sample_id, _entity_key(item))] += 1
        for item in gold.get("relations") or []:
            gold_relations[(sample_id, *_relation_key(item))] += 1
        if result is None:
            missing_results += 1
            continue
        entities = [item for item in (result.get("entities") or []) if isinstance(item, dict)]
        relations = [item for item in (result.get("relations") or []) if isinstance(item, dict)]
        names = {_entity_key(item) for item in entities if _entity_key(item)}
        for item in entities:
            key = _entity_key(item)
            if key:
                predicted_entities[(sample_id, key)] += 1
        for item in relations:
            key = _relation_key(item)
            if all(key):
                predicted_relations[(sample_id, *key)] += 1
            evidence = str(item.get("evidence_phrase") or item.get("ev") or "")
            evidence_total += 1
            if evidence and evidence in str(sample.get("text") or ""):
                evidence_exact += 1
            endpoint_total += 1
            if key[0] in names and key[2] in names:
                endpoint_valid += 1
        gold_types = {
            _entity_key(item): str(item.get("entity_type") or "")
            for item in (gold.get("entities") or [])
        }
        for item in entities:
            key = _entity_key(item)
            if key not in gold_types:
                continue
            entity_type_total += 1
            if str(item.get("entity_type") or item.get("label") or "") == gold_types[key]:
                entity_type_correct += 1
        if isinstance(result.get("entities"), list) and isinstance(
            result.get("relations"), list
        ):
            schema_valid_results += 1

    e_tp, e_fp, e_fn = _counter_overlap(predicted_entities, gold_entities)
    r_tp, r_fp, r_fn = _counter_overlap(predicted_relations, gold_relations)
    return {
        "samples": len(samples),
        "results": len(results),
        "missing_results": missing_results,
        "schema_valid_results": schema_valid_results,
        "entities": _prf(e_tp, e_fp, e_fn),
        "relations": _prf(r_tp, r_fp, r_fn),
        "entity_type_accuracy_on_matched": round(
            entity_type_correct / entity_type_total, 4
        )
        if entity_type_total
        else None,
        "relation_evidence_exact_rate": round(evidence_exact / evidence_total, 4)
        if evidence_total
        else None,
        "relation_endpoint_valid_rate": round(endpoint_valid / endpoint_total, 4)
        if endpoint_total
        else None,
        "claim_qualifier_contract_available": False,
    }


def _cue_matches(expected: str, observed: str) -> bool:
    left = normalize_name(expected)
    right = normalize_name(observed)
    return bool(left and right and (left in right or right in left))


def score_claim_candidates(
    samples: list[dict[str, Any]],
    candidates_by_sample: dict[str, list[dict[str, Any]]],
    qualifiers_by_sample: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Score target-aware claim fields and qualifier cue capture."""

    fields = ("polarity", "modal_force", "assertion_mode", "claim_type")
    field_totals = Counter()
    field_correct = Counter()
    expected_qualifiers: list[tuple[str, str, str]] = []
    observed_qualifiers: list[tuple[str, str, str]] = []
    expected_claims = 0
    matched_claims = 0
    missing_predicates: list[str] = []
    condition_total = condition_correct = 0
    exception_total = exception_correct = 0

    for sample in samples:
        sample_id = str(sample["id"])
        candidates = candidates_by_sample.get(sample_id) or []
        by_lemma: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            by_lemma.setdefault(
                normalize_name(candidate.get("predicate_lemma")), []
            ).append(candidate)
        for observed in qualifiers_by_sample.get(sample_id) or []:
            observed_qualifiers.append(
                (
                    sample_id,
                    str(observed.get("kind") or ""),
                    str(observed.get("cue") or ""),
                )
            )
        for expected in (sample.get("gold") or {}).get("claims") or []:
            expected_claims += 1
            lemma = normalize_name(expected.get("predicate_lemma"))
            options = by_lemma.get(lemma) or []
            if not options:
                missing_predicates.append(f"{sample_id}:{lemma}")
                continue
            candidate = options.pop(0)
            matched_claims += 1
            for field in fields:
                field_totals[field] += 1
                if str(candidate.get(field) or "") == str(expected.get(field) or ""):
                    field_correct[field] += 1
            for expected_condition in expected.get("conditions_contain") or []:
                condition_total += 1
                if any(
                    _cue_matches(expected_condition, value)
                    for value in (candidate.get("conditions") or [])
                ):
                    condition_correct += 1
            for expected_exception in expected.get("exceptions_contain") or []:
                exception_total += 1
                if any(
                    _cue_matches(expected_exception, value)
                    for value in (candidate.get("exceptions") or [])
                ):
                    exception_correct += 1
            for qualifier in expected.get("qualifiers") or []:
                expected_qualifiers.append(
                    (
                        sample_id,
                        str(qualifier.get("kind") or ""),
                        str(qualifier.get("cue_contains") or ""),
                    )
                )

    matched_observed: set[int] = set()
    qualifier_tp = 0
    for sample_id, kind, cue in expected_qualifiers:
        for index, observed in enumerate(observed_qualifiers):
            if index in matched_observed:
                continue
            if observed[0] == sample_id and observed[1] == kind and _cue_matches(cue, observed[2]):
                qualifier_tp += 1
                matched_observed.add(index)
                break
    qualifier_fp = len(observed_qualifiers) - len(matched_observed)
    qualifier_fn = len(expected_qualifiers) - qualifier_tp
    field_accuracy = {
        field: round(field_correct[field] / field_totals[field], 4)
        if field_totals[field]
        else None
        for field in fields
    }
    total_fields = sum(field_totals.values())
    return {
        "expected_claims": expected_claims,
        "matched_claims": matched_claims,
        "claim_match_rate": round(matched_claims / expected_claims, 4)
        if expected_claims
        else None,
        "claim_field_accuracy": field_accuracy,
        "claim_field_accuracy_overall": round(
            sum(field_correct.values()) / total_fields, 4
        )
        if total_fields
        else None,
        "condition_recall": round(condition_correct / condition_total, 4)
        if condition_total
        else None,
        "exception_recall": round(exception_correct / exception_total, 4)
        if exception_total
        else None,
        "qualifiers": _prf(qualifier_tp, qualifier_fp, qualifier_fn),
        "missing_predicates": missing_predicates,
        "claim_qualifier_contract_available": True,
    }
