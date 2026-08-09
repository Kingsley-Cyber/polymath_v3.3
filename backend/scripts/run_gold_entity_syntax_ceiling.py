#!/usr/bin/env python3
"""Measure the deterministic relation ceiling with exact gold entity spans.

The evaluator is intentionally model-free. It groups the committed quality
fixture by evidence span, parses every group exactly once with the production
FrameExtractor pipeline, and calls the production syntax lane and gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from services.extraction.corroboration_gate import evaluate_relation, load_policy
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.relation_evidence import GateStatus
from services.extraction.syntax_lane import build_union_evidence, generate_syntax_records


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _span_pair(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(row["subject_start"]),
        int(row["subject_end"]),
        int(row["object_start"]),
        int(row["object_end"]),
    )


def _local_pair(row: dict[str, Any], base: int) -> tuple[int, int, int, int]:
    pair = _span_pair(row)
    return tuple(value - base for value in pair)  # type: ignore[return-value]


def _undirected(pair: tuple[int, int, int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    endpoints = ((pair[0], pair[1]), (pair[2], pair[3]))
    return tuple(sorted(endpoints))  # type: ignore[return-value]


def _record_pair(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(row.get("subject_start", -1)),
        int(row.get("subject_end", -1)),
        int(row.get("object_start", -1)),
        int(row.get("object_end", -1)),
    )


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    metrics = report["metrics"]
    lines = [
        "# Gold-Entity Deterministic Syntax Ceiling",
        "",
        f"Status: **{report['status']}**",
        "",
        "## Kill switch",
        "",
        f"- Gold pair recall: `{metrics['gold_pair_recall']:.6f}`",
        f"- Required next path: `{report['decision']['required_path']}`",
        f"- Semantic rescue required: `{str(report['decision']['semantic_rescue_required']).lower()}`",
        "",
        "## Ceiling metrics",
        "",
        f"- Undirected pair recall: `{metrics['gold_pair_recall']:.6f}` ({metrics['undirected_pair_true_positives']}/{metrics['gold_relation_pairs']})",
        f"- Directed pair recall: `{metrics['directed_pair_recall']:.6f}` ({metrics['directed_pair_true_positives']}/{metrics['gold_relation_pairs']})",
        f"- Canonical predicate accuracy on recovered directed pairs: `{metrics['canonical_predicate_accuracy']:.6f}`",
        f"- Directed triple recall: `{metrics['directed_triple_recall']:.6f}` ({metrics['directed_triple_true_positives']}/{metrics['gold_canonical_relations']})",
        f"- Open-relation recall: `{metrics['open_relation_recall']:.6f}` ({metrics['open_relation_true_positives']}/{metrics['gold_open_relations']})",
        "",
        "## Execution invariants",
        "",
        f"- Evidence groups: `{metrics['evidence_groups']}`",
        f"- spaCy parses: `{metrics['spacy_parses']}`",
        f"- Parse-once invariant: `{str(metrics['parse_once']).lower()}`",
        f"- Model calls: `{metrics['model_calls']}`",
        f"- Elapsed seconds: `{metrics['elapsed_seconds']:.6f}`",
        f"- Determinism digest: `{report['determinism_digest']}`",
        "",
        "## Interpretation",
        "",
        "The kill switch uses undirected gold span-pair recall from all resolved and open syntax candidates. Directed pair and triple metrics preserve argument order. Gate outcomes are retained as diagnostics, but the ceiling decision is made before type-policy rejection so it measures whether deterministic syntax can propose the gold pair at all.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run(gold_path: Path, report_json: Path, report_md: Path) -> dict[str, Any]:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    entities = list(gold["entities"])
    relations = [row for row in gold["relations"] if row.get("lane") != "reject"]
    groups: dict[tuple[int, int, str], dict[str, Any]] = {}

    for relation in relations:
        key = (
            int(relation["evidence_start"]),
            int(relation["evidence_end"]),
            str(relation["evidence"]),
        )
        group = groups.setdefault(key, {"relations": [], "entities": []})
        group["relations"].append(relation)

    for (start, end, _text), group in groups.items():
        group["entities"] = [
            entity for entity in entities
            if int(entity["start"]) >= start and int(entity["end"]) <= end
        ]

    extractor = FrameExtractor()
    policy = load_policy()
    ordered_groups = sorted(groups.items())
    texts = [key[2] for key, _group in ordered_groups]
    started = time.perf_counter()
    docs = list(extractor._nlp.pipe(texts, batch_size=32))  # noqa: SLF001

    predicted_directed_pairs: set[tuple[int, int, int, int]] = set()
    predicted_undirected_pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    predicted_triples: set[tuple[int, int, int, int, str]] = set()
    predicted_open_pairs: set[tuple[int, int, int, int]] = set()
    gated_pairs: set[tuple[int, int, int, int]] = set()
    gated_triples: set[tuple[int, int, int, int, str]] = set()
    gate_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    group_rows: list[dict[str, Any]] = []

    accepted_gate_statuses = {
        GateStatus.ACCEPT_HIGH,
        GateStatus.ACCEPT_CORROBORATED,
        GateStatus.ACCEPT_SYNTAX_HIGH,
    }

    for index, (((base, _end, text), group), doc) in enumerate(zip(ordered_groups, docs)):
        local_entities = [
            {
                "text": entity["surface"],
                "type": entity["type"],
                "start": int(entity["start"]) - base,
                "end": int(entity["end"]) - base,
                "score": 1.0,
            }
            for entity in group["entities"]
        ]
        chunk_id = f"gold-syntax-{index:04d}"
        resolved, unmapped = generate_syntax_records(
            text, local_entities, chunk_id, extractor, doc,
        )
        all_records = resolved + unmapped
        for record in all_records:
            local = _record_pair(record)
            absolute = tuple(value + base for value in local)
            predicted_directed_pairs.add(absolute)  # type: ignore[arg-type]
            predicted_undirected_pairs.add(_undirected(absolute))  # type: ignore[arg-type]
            canonical = str(record.get("canonical_predicate") or "")
            if canonical:
                predicted_triples.add((*absolute, canonical))  # type: ignore[arg-type]
            else:
                predicted_open_pairs.add(absolute)  # type: ignore[arg-type]
            source_counts[str(record.get("source") or "unknown")] += 1

        prediction_row = {"entities": local_entities, "relations": [], "raw_pair_scores": []}
        union = build_union_evidence(chunk_id, prediction_row, resolved, unmapped, text)
        for evidence in union:
            decision = evaluate_relation(evidence, policy)
            gate_counts[decision.status.value] += 1
            if decision.status not in accepted_gate_statuses:
                continue
            local = (
                evidence.subject_start,
                evidence.subject_end,
                evidence.object_start,
                evidence.object_end,
            )
            absolute = tuple(value + base for value in local)
            gated_pairs.add(absolute)  # type: ignore[arg-type]
            if decision.predicate:
                gated_triples.add((*absolute, str(decision.predicate)))  # type: ignore[arg-type]

        group_rows.append({
            "chunk_id": chunk_id,
            "evidence_start": base,
            "evidence": text,
            "gold_relations": len(group["relations"]),
            "gold_entities": len(local_entities),
            "resolved_records": len(resolved),
            "unmapped_records": len(unmapped),
            "union_evidence": len(union),
        })

    elapsed = time.perf_counter() - started
    gold_pairs = {_span_pair(row) for row in relations}
    gold_undirected = {_undirected(pair) for pair in gold_pairs}
    canonical_relations = [row for row in relations if row.get("predicate")]
    gold_triples = {(*_span_pair(row), str(row["predicate"])) for row in canonical_relations}
    open_relations = [row for row in relations if row.get("lane") == "open"]
    gold_open_pairs = {_span_pair(row) for row in open_relations}

    undirected_tp = len(gold_undirected & predicted_undirected_pairs)
    directed_tp = len(gold_pairs & predicted_directed_pairs)
    triple_tp = len(gold_triples & predicted_triples)
    open_tp = len(gold_open_pairs & predicted_open_pairs)
    directed_recovered_gold = {
        pair for pair in gold_pairs if pair in predicted_directed_pairs
    }
    correct_predicate_pairs = {
        pair for pair in directed_recovered_gold
        if any((*pair, str(row["predicate"])) in predicted_triples
               for row in canonical_relations if _span_pair(row) == pair and row.get("predicate"))
    }
    gold_pair_recall = _ratio(undirected_tp, len(gold_undirected))
    if gold_pair_recall >= 0.65:
        required_path = "grammar_only_full_benchmark"
        rescue_required = False
    elif gold_pair_recall >= 0.50:
        required_path = "semantic_rescue_mandatory"
        rescue_required = True
    else:
        required_path = "semantic_rescue_mandatory_low_ceiling"
        rescue_required = True

    digest_payload = {
        "pairs": sorted(predicted_directed_pairs),
        "triples": sorted(predicted_triples),
        "open_pairs": sorted(predicted_open_pairs),
        "gate_counts": dict(sorted(gate_counts.items())),
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    report = {
        "schema_version": "polymath.gold_entity_syntax_ceiling.v1",
        "status": "passed",
        "fixture": str(gold_path.relative_to(REPO_ROOT)),
        "fixture_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "metrics": {
            "gold_pair_recall": gold_pair_recall,
            "directed_pair_recall": _ratio(directed_tp, len(gold_pairs)),
            "canonical_predicate_accuracy": _ratio(len(correct_predicate_pairs), len(directed_recovered_gold)),
            "directed_triple_recall": _ratio(triple_tp, len(gold_triples)),
            "open_relation_recall": _ratio(open_tp, len(gold_open_pairs)),
            "gold_relation_pairs": len(gold_pairs),
            "gold_canonical_relations": len(gold_triples),
            "gold_open_relations": len(gold_open_pairs),
            "undirected_pair_true_positives": undirected_tp,
            "directed_pair_true_positives": directed_tp,
            "directed_triple_true_positives": triple_tp,
            "open_relation_true_positives": open_tp,
            "predicted_directed_pairs": len(predicted_directed_pairs),
            "predicted_directed_triples": len(predicted_triples),
            "predicted_open_pairs": len(predicted_open_pairs),
            "gated_positive_pairs": len(gated_pairs),
            "gated_positive_triples": len(gated_triples),
            "evidence_groups": len(ordered_groups),
            "spacy_parses": len(docs),
            "parse_once": len(docs) == len(ordered_groups),
            "model_calls": 0,
            "elapsed_seconds": elapsed,
        },
        "decision": {
            "required_path": required_path,
            "semantic_rescue_required": rescue_required,
            "thresholds": {"grammar_only": 0.65, "rescue": 0.50},
        },
        "gate_status_counts": dict(sorted(gate_counts.items())),
        "syntax_source_counts": dict(sorted(source_counts.items())),
        "determinism_digest": digest,
        "groups": group_rows,
    }
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_markdown(report, report_md)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gold",
        type=Path,
        default=REPO_ROOT / "GRAPHIFY_GLINER2_CPU_AGENT/fixtures/graphify_quality_gold.json",
    )
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.gold.resolve(), args.report_json.resolve(), args.report_md.resolve())
    print(json.dumps({"status": report["status"], **report["metrics"], **report["decision"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
