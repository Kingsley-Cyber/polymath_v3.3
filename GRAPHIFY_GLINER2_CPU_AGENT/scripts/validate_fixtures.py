#!/usr/bin/env python3
"""Validate fixture checksums, exact offsets, structural counts, and coverage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def fail(message: str) -> None:
    raise AssertionError(message)


def check_span(text: str, item: dict, start_key: str, end_key: str, surface_key: str) -> None:
    start = item[start_key]
    end = item[end_key]
    surface = item[surface_key]
    if not (0 <= start < end <= len(text)):
        fail(f"Invalid span {start}:{end} for {surface!r}")
    actual = text[start:end]
    if actual != surface:
        fail(f"Span mismatch at {start}:{end}: expected {surface!r}, found {actual!r}")


def validate_quality() -> dict:
    md = FIXTURES / "graphify_quality_fixture.md"
    gold_path = FIXTURES / "graphify_quality_gold.json"
    text = md.read_text(encoding="utf-8")
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest != gold["sha256"]:
        fail("Quality fixture SHA-256 mismatch")
    if not 50 <= gold["sentence_count"] <= 120:
        fail(f"Unexpected quality sentence count: {gold['sentence_count']}")
    for item in gold["entities"]:
        check_span(text, item, "start", "end", "surface")
        if text[item["evidence_start"]:item["evidence_end"]] != item["evidence"]:
            fail("Quality entity evidence mismatch")
    for item in gold["negative_entities"]:
        check_span(text, item, "start", "end", "surface")
    for item in gold["relations"]:
        check_span(text, item, "subject_start", "subject_end", "subject")
        check_span(text, item, "object_start", "object_end", "object")
        if text[item["evidence_start"]:item["evidence_end"]] != item["evidence"]:
            fail("Quality relation evidence mismatch")
        if not (item["evidence_start"] <= item["subject_start"] < item["subject_end"] <= item["evidence_end"]):
            fail("Subject is outside evidence span")
        if not (item["evidence_start"] <= item["object_start"] < item["object_end"] <= item["evidence_end"]):
            fail("Object is outside evidence span")
    for item in gold["aliases"]:
        check_span(text, item, "canonical_start", "canonical_end", "canonical")
        check_span(text, item, "alias_start", "alias_end", "alias")
    entity_types = {item["type"] for item in gold["entities"]}
    missing_types = set(gold["canonical_entity_types"]) - entity_types
    if missing_types:
        fail(f"Quality fixture does not exercise entity types: {sorted(missing_types)}")
    predicates = {item["predicate"] for item in gold["relations"] if item["predicate"]}
    missing_predicates = set(gold["canonical_predicates"]) - predicates
    if missing_predicates:
        fail(f"Quality fixture does not exercise predicates: {sorted(missing_predicates)}")
    lanes = {item["lane"] for item in gold["relations"]}
    for required in {"accept", "qualified", "open", "reject"}:
        if required not in lanes:
            fail(f"Quality fixture missing relation lane: {required}")
    return {
        "sha256": digest,
        "bytes": len(text.encode()),
        "sentences": gold["sentence_count"],
        "entities": len(gold["entities"]),
        "negative_entities": len(gold["negative_entities"]),
        "relations": len(gold["relations"]),
        "aliases": len(gold["aliases"]),
    }


def validate_throughput() -> dict:
    md = FIXTURES / "graphify_throughput_fixture.md"
    gold_path = FIXTURES / "graphify_throughput_gold.json"
    text = md.read_text(encoding="utf-8")
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest != gold["sha256"]:
        fail("Throughput fixture SHA-256 mismatch")
    byte_count = len(text.encode())
    if byte_count != gold["bytes"]:
        fail("Throughput byte count mismatch")
    if not 100_000 <= byte_count <= 250_000:
        fail(f"Throughput fixture must be 100–250 KB, found {byte_count}")
    if text.count(gold["repeated_furniture_text"]) != gold["repeated_furniture_count"]:
        fail("Repeated furniture count mismatch")
    if text.count("```python") != gold["code_block_count"]:
        fail("Code block count mismatch")
    if text.count("| Component | Role |") != gold["module_table_count"]:
        fail("Table count mismatch")
    if text.count("### References") != gold["local_reference_section_count"]:
        fail("Local references count mismatch")
    if text.count("- Bibliography item ") != gold["global_bibliography_items"]:
        fail("Global bibliography count mismatch")
    for item in gold["sampled_entities"]:
        check_span(text, item, "start", "end", "surface")
    for item in gold["sampled_relations"]:
        check_span(text, item, "subject_start", "subject_end", "subject")
        check_span(text, item, "object_start", "object_end", "object")
        if text[item["evidence_start"]:item["evidence_end"]] != item["evidence"]:
            fail("Throughput sampled relation evidence mismatch")
    return {
        "sha256": digest,
        "bytes": byte_count,
        "modules": gold["module_count"],
        "sampled_entities": len(gold["sampled_entities"]),
        "sampled_relations": len(gold["sampled_relations"]),
        "furniture_occurrences": gold["repeated_furniture_count"],
        "code_blocks": gold["code_block_count"],
        "tables": gold["module_table_count"],
    }


def main() -> int:
    try:
        result = {"quality": validate_quality(), "throughput": validate_throughput()}
    except AssertionError as exc:
        print(f"Fixture validation failed: {exc}", file=sys.stderr)
        return 1
    output = ROOT / "artifacts" / "reports" / "fixture_validation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"passed": True, **result}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
