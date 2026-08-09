#!/usr/bin/env python3
"""Validate ModernBERT predicate-classifier JSONL training data.

This checks the local predicate-classifier dataset as a training artifact,
while also verifying that its labels can compile back into the repo's
Ghost B ExtractionResponse predicate schema.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent

CLASSIFIER_LABELS = [
    "includes",
    "uses",
    "supports",
    "produces",
    "implements",
    "has_part",
    "instance_of",
    "references",
    "depends_on",
    "quantizes",
    "causes",
    "synonym_of",
    "member_of",
    "example_of",
    "evaluates",
    "deploys",
    "creates",
    "trains",
    "runs",
    "located_in",
    "none",
]

REQUIRED_KEYS = {
    "text",
    "subject",
    "subject_type",
    "object",
    "object_type",
    "label",
}

# Local classifier labels that are not native Ghost B predicates compile into
# one or more legal repo predicates in the deterministic OntologyMapper.
LOCAL_TO_REPO_PREDICATE = {
    "includes": ["part_of", "supports"],
    "uses": ["uses"],
    "supports": ["supports"],
    "produces": ["produces"],
    "implements": ["implements"],
    "has_part": ["part_of"],
    "instance_of": ["instance_of"],
    "references": ["references"],
    "depends_on": ["depends_on"],
    "quantizes": ["uses", "implements"],
    "causes": ["causes"],
    "synonym_of": ["synonym_of"],
    "member_of": ["member_of"],
    "example_of": ["example_of"],
    "evaluates": ["supports"],
    "deploys": ["uses", "implements"],
    "creates": ["created_by", "produces"],
    "trains": ["uses"],
    "runs": ["uses", "implements"],
    "located_in": ["located_in"],
}

FALLBACK_ENTITY_TYPES = {
    "Person",
    "Organization",
    "Location",
    "Event",
    "Concept",
    "Method",
    "Product",
    "Software",
    "Document",
    "Standard",
    "Rule",
    "Law",
    "Artifact",
    "TimeReference",
    "other",
}

FALLBACK_REPO_PREDICATES = {
    "part_of",
    "member_of",
    "located_in",
    "works_for",
    "created_by",
    "owns",
    "affiliated_with",
    "synonym_of",
    "instance_of",
    "example_of",
    "uses",
    "references",
    "implements",
    "depends_on",
    "produces",
    "stores",
    "detects",
    "supports",
    "defines",
    "represents",
    "maps_to",
    "preceded_by",
    "causes",
    "overlaps",
    "during",
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
    "related_to",
}

FALLBACK_FACT_TYPES = {
    "property",
    "status",
    "timestamp",
    "quantity",
    "threshold",
    "category",
    "tag",
    "rule_condition",
    "rule_action",
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class DatasetFile:
    path: Path
    rows: list[dict[str, Any]]
    invalid_json: list[str]


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def normalize_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def light_stem_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def name_appears_in_text(name: Any, text: Any) -> bool:
    name_norm = normalize_name(name)
    text_norm = normalize_name(text)
    if not name_norm:
        return False

    padded_text = f" {text_norm} "
    if f" {name_norm} " in padded_text:
        return True

    name_stemmed = " ".join(light_stem_token(token) for token in name_norm.split())
    text_stemmed = " ".join(light_stem_token(token) for token in text_norm.split())
    if f" {name_stemmed} " in f" {text_stemmed} ":
        return True

    # Handles compact code/model names such as HelloWorld vs "hello world".
    compact_name = "".join(name_norm.split())
    compact_text = "".join(text_norm.split())
    return len(compact_name) >= 5 and compact_name in compact_text


def load_jsonl(path: Path) -> DatasetFile:
    rows: list[dict[str, Any]] = []
    invalid: list[str] = []
    if not path.exists():
        return DatasetFile(path=path, rows=[], invalid_json=[f"missing file: {path}"])

    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            raw = line.strip()
            if not raw:
                invalid.append(f"{path}:{line_no}: blank line")
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                invalid.append(f"{path}:{line_no}: {exc.msg}")
                continue
            if not isinstance(item, dict):
                invalid.append(f"{path}:{line_no}: line is not a JSON object")
                continue
            item["_line_no"] = line_no
            item["_path"] = str(path)
            rows.append(item)
    return DatasetFile(path=path, rows=rows, invalid_json=invalid)


def literal_values_from_schema(path: Path, alias: str) -> set[str]:
    if not path.exists():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == alias for target in node.targets):
            continue
        value = node.value
        if not isinstance(value, ast.Subscript):
            continue
        if not isinstance(value.value, ast.Name) or value.value.id != "Literal":
            continue
        slice_node = value.slice
        elements = slice_node.elts if isinstance(slice_node, ast.Tuple) else [slice_node]
        return {
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    return set()


def load_repo_schema(schema_path: Path) -> tuple[set[str], set[str], set[str], str]:
    entity_types = literal_values_from_schema(schema_path, "EntityType") or FALLBACK_ENTITY_TYPES
    predicates = literal_values_from_schema(schema_path, "Predicate") or FALLBACK_REPO_PREDICATES
    fact_types = literal_values_from_schema(schema_path, "FactType") or FALLBACK_FACT_TYPES
    source = str(schema_path) if schema_path.exists() else "fallback constants"
    return entity_types, predicates, fact_types, source


def entity_name_present(row: dict[str, Any]) -> tuple[bool, str]:
    subject_options = [row.get("subject"), row.get("subject_surface")]
    object_options = [row.get("object"), row.get("object_surface")]

    subject_ok = any(name_appears_in_text(option, row.get("text")) for option in subject_options)
    object_ok = any(name_appears_in_text(option, row.get("text")) for option in object_options)

    if subject_ok and object_ok:
        return True, ""
    missing = []
    if not subject_ok:
        missing.append(f"subject={row.get('subject')!r}")
    if not object_ok:
        missing.append(f"object={row.get('object')!r}")
    return False, ", ".join(missing)


def row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        normalize_ws(row.get("text")),
        normalize_name(row.get("subject")),
        normalize_name(row.get("object")),
        normalize_ws(row.get("label")),
    )


def default_eval_fixture(repo_root: Path) -> Path | None:
    candidates = [
        repo_root / "eval/fixtures/10_chunks.jsonl",
        repo_root / "scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl",
    ]
    candidates.extend(sorted((repo_root / "scripts/local_extraction_fixtures").glob("*10_chunks*.jsonl")))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_eval_texts(path: Path | None) -> tuple[list[str], str]:
    if path is None:
        return [], "no eval fixture found"
    if not path.exists():
        return [], f"eval fixture missing: {path}"

    texts: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            text = item.get("text") or item.get("chunk_text") or item.get("content")
            if text:
                texts.append(normalize_ws(text))
    return texts, str(path)


def check_valid_json(files: list[DatasetFile]) -> CheckResult:
    errors = [err for dataset in files for err in dataset.invalid_json]
    sample = "; ".join(errors[:5])
    return CheckResult(
        "Valid JSONL",
        not errors,
        f"{sum(len(dataset.rows) for dataset in files)} objects loaded"
        if not errors
        else f"{len(errors)} invalid lines. {sample}",
    )


def check_required_keys(rows: list[dict[str, Any]]) -> CheckResult:
    missing: list[str] = []
    for row in rows:
        diff = REQUIRED_KEYS - set(row)
        if diff:
            missing.append(f"{row['_path']}:{row['_line_no']} missing {sorted(diff)}")
    return CheckResult(
        "Required keys",
        not missing,
        "all rows contain text, subject, subject_type, object, object_type, label"
        if not missing
        else "; ".join(missing[:5]),
    )


def check_label_vocabulary(rows: list[dict[str, Any]]) -> CheckResult:
    allowed = set(CLASSIFIER_LABELS)
    bad = [
        f"{row['_path']}:{row['_line_no']} label={row.get('label')!r}"
        for row in rows
        if row.get("label") not in allowed
    ]
    observed = Counter(row.get("label") for row in rows)
    missing = sorted(allowed - set(observed))
    passed = not bad and not missing
    detail = f"{len(observed)} labels observed; all 21 represented"
    if bad:
        detail = f"{len(bad)} off-vocab labels. " + "; ".join(bad[:5])
    elif missing:
        detail = f"missing classifier labels: {', '.join(missing)}"
    return CheckResult("Allowed label vocabulary", passed, detail)


def check_predicate_distribution(train_rows: list[dict[str, Any]], min_per_label: int) -> CheckResult:
    counts = Counter(row.get("label") for row in train_rows)
    low = {
        label: counts.get(label, 0)
        for label in CLASSIFIER_LABELS
        if label != "none" and counts.get(label, 0) < min_per_label
    }
    if low:
        detail = ", ".join(f"{label}={count}" for label, count in sorted(low.items()))
        return CheckResult("Predicate distribution", False, f"below {min_per_label}: {detail}")
    min_count = min(counts.get(label, 0) for label in CLASSIFIER_LABELS if label != "none")
    return CheckResult(
        "Predicate distribution",
        True,
        f"all positive predicates >= {min_per_label}; minimum train count={min_count}",
    )


def check_none_ratio(train_rows: list[dict[str, Any]], min_pct: float) -> CheckResult:
    total = len(train_rows)
    none_count = sum(1 for row in train_rows if row.get("label") == "none")
    pct = (none_count / total * 100.0) if total else 0.0
    return CheckResult(
        "None ratio",
        pct >= min_pct,
        f"none={none_count}/{total} ({pct:.2f}%), required >= {min_pct:.2f}%",
    )


def check_entity_presence(rows: list[dict[str, Any]]) -> CheckResult:
    missing: list[str] = []
    for row in rows:
        ok, detail = entity_name_present(row)
        if not ok:
            missing.append(f"{row['_path']}:{row['_line_no']} {detail}")
    return CheckResult(
        "Entity names in evidence",
        not missing,
        "subject/object names or surfaces appear in every evidence sentence"
        if not missing
        else f"{len(missing)} missing entity references. " + "; ".join(missing[:5]),
    )


def check_duplicates(train_rows: list[dict[str, Any]], validation_rows: list[dict[str, Any]]) -> CheckResult:
    errors: list[str] = []
    for label, rows in [("train", train_rows), ("validation", validation_rows)]:
        seen: set[tuple[str, str, str, str]] = set()
        for row in rows:
            key = row_key(row)
            if key in seen:
                errors.append(f"duplicate inside {label}: {row['_path']}:{row['_line_no']} {key[1]}->{key[3]}->{key[2]}")
            seen.add(key)

    train_keys = {row_key(row) for row in train_rows}
    for row in validation_rows:
        key = row_key(row)
        if key in train_keys:
            errors.append(f"train/validation overlap: {row['_path']}:{row['_line_no']} {key[1]}->{key[3]}->{key[2]}")

    return CheckResult(
        "Duplicates",
        not errors,
        "no duplicate tuples inside splits and no train/validation overlap"
        if not errors
        else f"{len(errors)} duplicate/overlap issues. " + "; ".join(errors[:5]),
    )


def check_eval_overlap(rows: list[dict[str, Any]], eval_texts: list[str], eval_source: str) -> CheckResult:
    if not eval_texts:
        return CheckResult("Eval contamination", False, eval_source)

    overlaps: list[str] = []
    for row in rows:
        text = normalize_ws(row.get("text"))
        if not text:
            continue
        if any(text == eval_text or text in eval_text for eval_text in eval_texts):
            overlaps.append(f"{row['_path']}:{row['_line_no']} {row.get('subject')}->{row.get('label')}->{row.get('object')}")

    return CheckResult(
        "Eval contamination",
        not overlaps,
        f"checked against {eval_source}; no evidence sentence overlaps"
        if not overlaps
        else f"{len(overlaps)} overlaps against {eval_source}. " + "; ".join(overlaps[:5]),
    )


def check_split_ratio(train_count: int, validation_count: int, target: float, tolerance: float) -> CheckResult:
    total = train_count + validation_count
    train_pct = (train_count / total * 100.0) if total else 0.0
    target_pct = target * 100.0
    delta = abs(train_pct - target_pct)
    return CheckResult(
        "Train/validation split",
        delta <= tolerance,
        f"train={train_count}, validation={validation_count}, train_pct={train_pct:.2f}% "
        f"(target {target_pct:.2f}% +/- {tolerance:.2f}%)",
    )


def check_text_lengths(rows: list[dict[str, Any]], min_chars: int, max_chars: int) -> CheckResult:
    bad = [
        f"{row['_path']}:{row['_line_no']} len={len(str(row.get('text') or ''))}"
        for row in rows
        if not min_chars <= len(str(row.get("text") or "")) <= max_chars
    ]
    return CheckResult(
        "Text length",
        not bad,
        f"all evidence sentences are {min_chars}-{max_chars} characters"
        if not bad
        else f"{len(bad)} rows outside length bounds. " + "; ".join(bad[:5]),
    )


def check_repo_schema(rows: list[dict[str, Any]], schema_path: Path) -> CheckResult:
    entity_types, repo_predicates, fact_types, source = load_repo_schema(schema_path)

    bad_types: list[str] = []
    for row in rows:
        for key in ("subject_type", "object_type"):
            if row.get(key) not in entity_types:
                bad_types.append(f"{row['_path']}:{row['_line_no']} {key}={row.get(key)!r}")

    bad_label_routes = {
        label: mapped
        for label, mapped in LOCAL_TO_REPO_PREDICATE.items()
        if any(predicate not in repo_predicates for predicate in mapped)
    }
    positive_labels = set(CLASSIFIER_LABELS) - {"none"}
    missing_routes = sorted(positive_labels - set(LOCAL_TO_REPO_PREDICATE))
    fact_schema_ok = bool(fact_types)

    passed = not bad_types and not bad_label_routes and not missing_routes and fact_schema_ok
    if passed:
        classifier_scope = sorted(set().union(*[set(v) for v in LOCAL_TO_REPO_PREDICATE.values()]))
        out_of_scope = sorted(repo_predicates - set(classifier_scope))
        return CheckResult(
            "Repo schema compatibility",
            True,
            f"entity types valid; 20 positive classifier labels compile to Ghost B predicates; "
            f"schema source={source}; repo predicates outside classifier scope={len(out_of_scope)}",
        )

    problems: list[str] = []
    if bad_types:
        problems.append(f"{len(bad_types)} off-schema entity types: " + "; ".join(bad_types[:5]))
    if bad_label_routes:
        problems.append(f"bad local->repo routes: {bad_label_routes}")
    if missing_routes:
        problems.append(f"missing local->repo routes: {missing_routes}")
    if not fact_schema_ok:
        problems.append("FactType schema not found")
    return CheckResult("Repo schema compatibility", False, " | ".join(problems))


def print_distribution(title: str, rows: list[dict[str, Any]]) -> None:
    counts = Counter(row.get("label") for row in rows)
    total = len(rows)
    print(f"\n{title}")
    print("-" * len(title))
    for label in CLASSIFIER_LABELS:
        count = counts.get(label, 0)
        pct = (count / total * 100.0) if total else 0.0
        print(f"{label:12s} {count:6d} {pct:6.2f}%")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, default=REPO_ROOT / "data/train.jsonl")
    parser.add_argument("--validation", type=Path, default=REPO_ROOT / "data/validation.jsonl")
    parser.add_argument("--schema", type=Path, default=REPO_ROOT / "backend/services/ghost_b_schemas.py")
    parser.add_argument("--eval-fixture", type=Path, default=None)
    parser.add_argument("--min-per-predicate", type=int, default=100)
    parser.add_argument("--none-min-pct", type=float, default=30.0)
    parser.add_argument("--split-target", type=float, default=0.90)
    parser.add_argument("--split-tolerance-pct", type=float, default=2.0)
    parser.add_argument("--min-text-chars", type=int, default=20)
    parser.add_argument("--max-text-chars", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    train = load_jsonl(args.train)
    validation = load_jsonl(args.validation)
    rows = train.rows + validation.rows
    eval_path = args.eval_fixture or default_eval_fixture(REPO_ROOT)
    eval_texts, eval_source = load_eval_texts(eval_path)

    checks = [
        check_valid_json([train, validation]),
        check_required_keys(rows),
        check_label_vocabulary(rows),
        check_predicate_distribution(train.rows, args.min_per_predicate),
        check_none_ratio(train.rows, args.none_min_pct),
        check_entity_presence(rows),
        check_duplicates(train.rows, validation.rows),
        check_eval_overlap(rows, eval_texts, eval_source),
        check_split_ratio(len(train.rows), len(validation.rows), args.split_target, args.split_tolerance_pct),
        check_text_lengths(rows, args.min_text_chars, args.max_text_chars),
        check_repo_schema(rows, args.schema),
    ]

    print("ModernBERT Predicate Dataset Validation")
    print("======================================")
    print(f"Train:      {args.train} ({len(train.rows)} rows)")
    print(f"Validation: {args.validation} ({len(validation.rows)} rows)")
    print(f"Eval:       {eval_source}")
    print(f"Schema:     {args.schema if args.schema.exists() else 'fallback constants'}")

    print("\nSummary")
    print("-------")
    for result in checks:
        icon = "✅" if result.passed else "❌"
        print(f"{icon} {result.name:28s} {result.detail}")

    print_distribution("Train label distribution", train.rows)
    print_distribution("Validation label distribution", validation.rows)

    failed = [result for result in checks if not result.passed]
    if failed:
        print(f"\nFAILED: {len(failed)} check(s) failed.")
        return 1

    print("\nPASSED: all training-data checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
