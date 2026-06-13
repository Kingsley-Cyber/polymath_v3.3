#!/usr/bin/env python3
"""Generate concept-mapped ModernBERT predicate examples.

This is the pragmatic dataset expander for the predicate classifier:

    sentence [SEP] subject [SEP] object -> predicate | none

Entity typing is not the classifier's brain. The classifier learns the
relationship concept. Python/GLiNER/OntologyMapper still own final entity
types, type-pair legality, evidence gates, and graph writes.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
from pathlib import Path
from typing import Any

from build_modernbert_predicate_dataset import (
    DEFAULT_EVAL_DIRS,
    PATTERNS,
    PREDICATES,
    contaminated,
    load_eval_texts,
    normalize_name,
    normalize_text,
    pattern_hits,
    sentence_split,
    valid_length,
    write_jsonl,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

POSITIVE_PREDICATES = [label for label in PREDICATES if label != "none"]

SUBJECT_CONCEPTS = [
    "schema validation",
    "evidence ledger",
    "ontology mapper",
    "predicate classifier",
    "entity ranker",
    "candidate generator",
    "graph compiler",
    "local extractor",
    "chunk router",
    "relation verifier",
    "fact normalizer",
    "span detector",
    "JSONL converter",
    "Pydantic gate",
    "Neo4j writer",
    "Mongo artifact log",
    "Qdrant index",
    "retrieval pipeline",
    "model router",
    "inference lane",
    "training corpus",
    "validation split",
    "negative sampler",
    "evidence matcher",
    "dependency parser",
    "concept taxonomy",
    "context window",
    "batch scheduler",
    "mobile runtime",
    "adapter checkpoint",
    "quantization pass",
    "evaluation harness",
    "relation ontology",
    "entity mention",
    "confidence threshold",
    "deduplication rule",
    "structured output parser",
    "classification head",
    "ModernBERT encoder",
    "GLiNER span assist",
]

OBJECT_CONCEPTS = [
    "candidate spans",
    "evidence phrases",
    "entity pairs",
    "graph edges",
    "ontology predicates",
    "training labels",
    "JSON schema",
    "relation cues",
    "chunk text",
    "surface forms",
    "canonical names",
    "validation errors",
    "accepted entities",
    "accepted relations",
    "fact records",
    "negative examples",
    "type constraints",
    "sentence windows",
    "dependency paths",
    "predicate vocabulary",
    "model logits",
    "classification scores",
    "mobile inference",
    "structured artifacts",
    "semantic facets",
    "evidence substrings",
    "graph provenance",
    "training batches",
    "validation metrics",
    "token windows",
    "local extraction",
    "ontology triples",
    "schema contracts",
    "corpus chunks",
    "synthetic examples",
    "predicate concepts",
    "entity surfaces",
    "relation endpoints",
    "Python validators",
    "gold fixtures",
]

MODIFIERS = [
    "basic",
    "advanced",
    "field",
    "mobile",
    "regional",
    "nightly",
    "clinical",
    "urban",
    "emergency",
    "training",
    "analysis",
    "production",
    "research",
    "defense",
    "safety",
    "school",
    "community",
    "cloud",
    "edge",
    "simulation",
    "quality",
    "planning",
    "operations",
    "benchmark",
    "deployment",
    "logistics",
    "inspection",
    "review",
    "scoring",
    "automation",
]

SCOPES = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "north",
    "south",
    "east",
    "west",
    "primary",
    "secondary",
    "field",
    "lab",
    "pilot",
    "release",
    "review",
    "audit",
    "training",
    "operations",
    "planning",
    "response",
    "analysis",
    "simulation",
    "testing",
    "production",
    "mobile",
    "cloud",
    "edge",
    "classroom",
    "clinical",
    "tactical",
]


SEMANTIC_DOMAINS: dict[str, list[tuple[str, str, list[str]]]] = {
    "includes": [
        ("recipe", "ingredient", [
            "{s} includes {o} before the dish is baked.",
            "The chef explains that {s} includes {o}, herbs, and oil.",
        ]),
        ("software package", "dependency", [
            "{s} includes {o} so the application can start.",
            "The release bundle includes {o} inside {s}.",
        ]),
        ("training manual", "safety checklist", [
            "{s} includes {o} for every field exercise.",
            "The instructor notes that {s} includes {o}, maps, and scoring rubrics.",
        ]),
        ("medical kit", "sterile bandage", [
            "{s} includes {o} for emergency treatment.",
            "The supply list says {s} includes {o}.",
        ]),
    ],
    "quantizes": [
        ("compression method", "model weights", [
            "{s} quantizes {o} from floating point values into smaller integer buckets.",
            "During mobile optimization, {s} quantizes {o}.",
        ]),
        ("signal processor", "audio samples", [
            "{s} quantizes {o} before transmission.",
            "The encoder quantizes {o} using {s}.",
        ]),
        ("image pipeline", "color values", [
            "{s} quantizes {o} into a smaller palette.",
            "For storage efficiency, {s} quantizes {o}.",
        ]),
        ("statistics lesson", "continuous scores", [
            "{s} quantizes {o} into ordinal bands for analysis.",
            "The analyst uses {s} to quantize {o}.",
        ]),
    ],
    "deploys": [
        ("army command", "infantry brigade", [
            "{s} deploys {o} to secure the border region.",
            "During the operation, {s} deploys {o}, medical teams, and logistics units.",
        ]),
        ("cloud platform", "payment service", [
            "{s} deploys {o} after the release gate passes.",
            "The operations team uses {s} to deploy {o}.",
        ]),
        ("emergency agency", "rescue crew", [
            "{s} deploys {o} after the storm warning.",
            "The disaster plan says {s} deploys {o} to the affected area.",
        ]),
        ("mobile app pipeline", "on-device model", [
            "{s} deploys {o} to the phone runtime.",
            "The build system deploys {o} through {s}.",
        ]),
    ],
    "trains": [
        ("military academy", "cadet unit", [
            "{s} trains {o} for field navigation and response drills.",
            "The commander says {s} trains {o} before deployment.",
        ]),
        ("machine learning job", "language model", [
            "{s} trains {o} on labeled relation examples.",
            "The experiment uses {s} to train {o}.",
        ]),
        ("basketball coach", "junior team", [
            "{s} trains {o} every morning before the tournament.",
            "The season plan says {s} trains {o} on defense and passing.",
        ]),
        ("hospital program", "new nurses", [
            "{s} trains {o} on safety procedures.",
            "The onboarding course trains {o} through {s}.",
        ]),
    ],
    "runs": [
        ("Python script", "data pipeline", [
            "{s} runs {o} after the input file is prepared.",
            "The scheduler starts {s}, which runs {o}.",
        ]),
        ("server process", "background service", [
            "{s} runs {o} on the production host.",
            "The monitoring log shows that {s} runs {o}.",
        ]),
        ("athlete", "marathon route", [
            "{s} runs {o} before sunrise.",
            "During the race, {s} runs {o} at a steady pace.",
        ]),
        ("school district", "summer program", [
            "{s} runs {o} for students who need extra support.",
            "The administrator says {s} runs {o} every year.",
        ]),
    ],
    "evaluates": [
        ("benchmark suite", "model accuracy", [
            "{s} evaluates {o} using held-out examples.",
            "The report says {s} evaluates {o}, latency, and memory use.",
        ]),
        ("teacher", "student essay", [
            "{s} evaluates {o} with a scoring rubric.",
            "After the exam, {s} evaluates {o}.",
        ]),
        ("financial analyst", "quarterly performance", [
            "{s} evaluates {o} before the investment meeting.",
            "The review memo shows how {s} evaluates {o}.",
        ]),
        ("doctor", "patient symptoms", [
            "{s} evaluates {o} before recommending treatment.",
            "The clinic workflow says {s} evaluates {o}.",
        ]),
    ],
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        normalize_text(str(row.get("text") or "")),
        normalize_name(str(row.get("subject") or "")),
        normalize_name(str(row.get("object") or "")),
        str(row.get("label") or ""),
    )


def concept_name_present(row: dict[str, Any]) -> bool:
    text_key = normalize_name(str(row.get("text") or ""))
    subject_key = normalize_name(str(row.get("subject") or ""))
    object_key = normalize_name(str(row.get("object") or ""))
    return bool(subject_key and object_key and subject_key in text_key and object_key in text_key)


def relation_verb(predicate: str) -> str:
    return {
        "includes": "includes",
        "uses": "uses",
        "supports": "supports",
        "produces": "produces",
        "implements": "implements",
        "has_part": "contains",
        "instance_of": "is a type of",
        "references": "references",
        "depends_on": "depends on",
        "quantizes": "quantizes",
        "causes": "causes",
        "synonym_of": "is also called",
        "member_of": "belongs to",
        "example_of": "is an example of",
        "evaluates": "evaluates",
        "deploys": "deploys",
        "creates": "creates",
        "trains": "trains",
        "runs": "runs",
        "located_in": "is located in",
    }[predicate]


def sentence_for(predicate: str, subject: str, obj: str, template_id: int) -> str:
    verb = relation_verb(predicate)
    templates = [
        "{s} {v} {o} in the local extraction workflow.",
        "{s}, a concept in the pipeline, {v} {o} before graph validation.",
        "During ingestion, {s} {v} {o} so the classifier can learn the predicate concept.",
        "{o} is used by {s} when the extraction system validates ontology triples.",
        "{s} {v} {o}, calibration data, beta signals, gamma checks in one controlled example.",
        "{s} serves as the mechanism that {v} {o} inside the deterministic relation pipeline.",
        "The training note says that {s} {v} {o} for schema-aligned extraction.",
        "In the textbook-style chunk, {s} {v} {o} as an evidence-backed relation.",
        "{s} is a pipeline concept that {v} {o} during predicate classification.",
        "The ontology example defines {s} as the component that {v} {o}.",
        "{s} {v} {o} after the candidate pair is selected from the sentence window.",
        "{s}, a reusable extraction idea, {v} {o}, validation traces, and audit metrics.",
    ]
    if predicate == "synonym_of":
        templates = [
            "{s} is also called {o} in the ontology vocabulary.",
            "{s}, a label variant, is also called {o} in the schema notes.",
            "The alias table says {s} is also called {o}.",
            "{s} serves as the same concept as {o} in the local relation map.",
        ]
    elif predicate in {"instance_of", "example_of"}:
        templates = [
            "{s} is a type of {o} in the ontology vocabulary.",
            "{s}, a concrete example, is an example of {o}.",
            "The schema note classifies {s} as an example of {o}.",
            "{s} serves as an example of {o} in the predicate training corpus.",
        ]
    elif predicate == "member_of":
        templates = [
            "{s} belongs to {o} in the concept group.",
            "{s}, a grouped extraction concept, belongs to {o}.",
            "The ontology list places {s} as a member of {o}.",
            "{s} is a member of {o} in the training taxonomy.",
        ]
    elif predicate == "located_in":
        templates = [
            "{s} is located in {o} during local deployment.",
            "{s}, a runtime concept, is located in {o}.",
            "The deployment note places {s} in {o}.",
            "{s} is located in {o} for the extraction benchmark.",
        ]
    return templates[template_id % len(templates)].format(s=subject, v=verb, o=obj)


def make_example(predicate: str, index: int) -> dict[str, Any]:
    if predicate in SEMANTIC_DOMAINS:
        domains = SEMANTIC_DOMAINS[predicate]
        domain_name, object_name, templates = domains[index % len(domains)]
        modifier = MODIFIERS[(index // len(domains)) % len(MODIFIERS)]
        scope = SCOPES[(index // (len(domains) * len(MODIFIERS))) % len(SCOPES)]
        object_modifier = MODIFIERS[(index * 3 + len(predicate)) % len(MODIFIERS)]
        object_scope = SCOPES[(index * 5 + len(predicate)) % len(SCOPES)]
        subject = f"{modifier} {scope} {domain_name}"
        obj = f"{object_modifier} {object_scope} {object_name}"
        text = templates[(index // len(domains)) % len(templates)].format(s=subject, o=obj)
        return {
            "text": text,
            "subject": subject,
            "subject_type": "Concept",
            "object": obj,
            "object_type": "Concept",
            "label": predicate,
        }
    subject_base = SUBJECT_CONCEPTS[index % len(SUBJECT_CONCEPTS)]
    object_base = OBJECT_CONCEPTS[(index * 7 + len(predicate)) % len(OBJECT_CONCEPTS)]
    modifier = MODIFIERS[(index // len(SUBJECT_CONCEPTS)) % len(MODIFIERS)]
    scope = SCOPES[(index // (len(SUBJECT_CONCEPTS) * len(MODIFIERS))) % len(SCOPES)]
    object_modifier = MODIFIERS[(index * 3 + len(predicate)) % len(MODIFIERS)]
    object_scope = SCOPES[(index * 5 + len(predicate)) % len(SCOPES)]
    subject = f"{modifier} {scope} {subject_base}"
    obj = f"{object_modifier} {object_scope} {object_base}"
    text = sentence_for(predicate, subject, obj, index)
    return {
        "text": text,
        "subject": subject,
        "subject_type": "Concept",
        "object": obj,
        "object_type": "Concept",
        "label": predicate,
    }


def as_example(row: dict[str, Any]):
    from build_modernbert_predicate_dataset import Example

    return Example(
        text=str(row["text"]),
        subject=str(row["subject"]),
        subject_type=str(row.get("subject_type") or "Concept"),
        object=str(row["object"]),
        object_type=str(row.get("object_type") or "Concept"),
        label=str(row["label"]),
        source=str(row.get("source") or "generated_or_existing"),
        subject_surface=str(row.get("subject_surface") or row.get("subject") or ""),
        object_surface=str(row.get("object_surface") or row.get("object") or ""),
    )


def audit_rows(rows: list[dict[str, Any]], eval_texts: set[str]) -> dict[str, Any]:
    counts = collections.Counter(str(row.get("label")) for row in rows)
    keys = [row_key(row) for row in rows]
    length_failures = sum(
        not valid_length(as_example(row))
        for row in rows
    )
    name_failures = sum(
        not concept_name_present(row)
        for row in rows
    )
    contamination = sum(contaminated(as_example(row), eval_texts) for row in rows)
    pattern_gaps: dict[str, dict[str, float]] = {}
    for pred in POSITIVE_PREDICATES:
        pred_rows = [row for row in rows if row.get("label") == pred]
        if not pred_rows:
            pattern_gaps[pred] = {name: 0.0 for name in PATTERNS}
            continue
        hit_counts = collections.Counter()
        for row in pred_rows:
            hit_counts.update(pattern_hits(str(row.get("text") or "")))
        gaps = {
            name: hit_counts.get(name, 0) / len(pred_rows)
            for name in PATTERNS
            if hit_counts.get(name, 0) / len(pred_rows) < 0.05
        }
        if gaps:
            pattern_gaps[pred] = gaps
    return {
        "total_examples": len(rows),
        "label_counts": dict(counts),
        "none_ratio": counts.get("none", 0) / max(1, len(rows)),
        "duplicate_count": len(keys) - len(set(keys)),
        "length_failures": length_failures,
        "name_presence_failures": name_failures,
        "eval_contamination_count": contamination,
        "pattern_gaps_below_5_percent": pattern_gaps,
    }


def stratified_split_rows(rows: list[dict[str, Any]], seed: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    by_label: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_label[str(row["label"])].append(row)
    train: list[dict[str, Any]] = []
    val: list[dict[str, Any]] = []
    for label_rows in by_label.values():
        rng.shuffle(label_rows)
        val_count = max(1, round(len(label_rows) * 0.10))
        val.extend(label_rows[:val_count])
        train.extend(label_rows[val_count:])
    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--target-positive-per-label", type=int, default=700)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-prefix", default="")
    args = parser.parse_args()

    rows = read_jsonl(args.data_dir / "train.jsonl") + read_jsonl(args.data_dir / "validation.jsonl")
    eval_texts = load_eval_texts(DEFAULT_EVAL_DIRS)

    clean: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if row.get("label") not in PREDICATES:
            continue
        key = row_key(row)
        if key in seen:
            continue
        ex = as_example(row)
        if not valid_length(ex) or not concept_name_present(row) or contaminated(ex, eval_texts):
            continue
        seen.add(key)
        clean.append({k: row[k] for k in ("text", "subject", "subject_type", "object", "object_type", "label")})

    generated = 0
    counts = collections.Counter(row["label"] for row in clean)
    for predicate in POSITIVE_PREDICATES:
        index = 0
        while counts[predicate] < args.target_positive_per_label:
            row = make_example(predicate, index)
            index += 1
            key = row_key(row)
            if key in seen:
                continue
            ex = as_example(row)
            if not valid_length(ex) or not concept_name_present(row):
                continue
            seen.add(key)
            clean.append(row)
            counts[predicate] += 1
            generated += 1

    train, val = stratified_split_rows(clean, args.seed)
    train_path = args.data_dir / f"{args.output_prefix}train.jsonl"
    val_path = args.data_dir / f"{args.output_prefix}validation.jsonl"
    report_path = args.data_dir / f"{args.output_prefix}audit_report.md"
    json_path = args.data_dir / f"{args.output_prefix}audit_report.json"
    write_rows(train_path, train)
    write_rows(val_path, val)
    audit = audit_rows(clean, eval_texts)
    final_checks = {
        "total_examples_ge_3000": audit["total_examples"] >= 3000,
        "every_positive_label_ge_target": all(audit["label_counts"].get(pred, 0) >= args.target_positive_per_label for pred in POSITIVE_PREDICATES),
        "none_ge_30_percent": audit["none_ratio"] >= 0.30,
        "duplicates_zero": audit["duplicate_count"] == 0,
        "length_failures_zero": audit["length_failures"] == 0,
        "name_presence_failures_zero": audit["name_presence_failures"] == 0,
        "eval_contamination_zero": audit["eval_contamination_count"] == 0,
    }
    lines = [
        "# ModernBERT Concept-Mapped Predicate Dataset Audit",
        "",
        "## BLUF",
        "",
        f"- Total examples: `{audit['total_examples']}`",
        f"- Train/validation: `{len(train)}` / `{len(val)}`",
        f"- Generated concept examples added: `{generated}`",
        f"- Target positive examples per label: `{args.target_positive_per_label}`",
        f"- None ratio: `{audit['none_ratio']:.2%}`",
        "",
        "## Final Checks",
        "",
    ]
    lines.extend(f"- `{key}`: `{value}`" for key, value in final_checks.items())
    lines.extend(["", "## Label Counts", ""])
    for label in PREDICATES:
        lines.append(f"- `{label}`: `{audit['label_counts'].get(label, 0)}`")
    lines.extend(["", "## Pattern Gaps Below 5%", ""])
    if audit["pattern_gaps_below_5_percent"]:
        for label, gaps in audit["pattern_gaps_below_5_percent"].items():
            gap_text = ", ".join(f"{name}={ratio:.2%}" for name, ratio in gaps.items())
            lines.append(f"- `{label}`: {gap_text}")
    else:
        lines.append("- none")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps({"audit": audit, "final_checks": final_checks, "generated": generated}, indent=2),
        encoding="utf-8",
    )
    print(f"generated={generated} total={len(clean)} train={len(train)} validation={len(val)}")
    print(f"wrote {train_path}")
    print(f"wrote {val_path}")
    print(f"wrote {report_path}")


if __name__ == "__main__":
    main()
