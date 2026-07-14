#!/usr/bin/env python3
"""Read-only trained-spaCy audit of LocalExtractionV1 on UGO child text.

The input is an operator-exported JSONL projection containing only doc_id,
chunk_id, and text. The committed receipt contains counts and identities only;
raw text and child identifiers never enter the report.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Sequence

from models.registry_loader import normalize_predicate_lemma
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
    validate_evidence_round_trip,
)


SCHEMA_VERSION = "polymath.local_extraction_ugo_audit.v1"


class AuditError(RuntimeError):
    """The read-only audit input or result violated its contract."""


def _sample_evenly(rows: Sequence[dict[str, str]], count: int) -> list[dict[str, str]]:
    if count < 1:
        raise AuditError("sample count must be positive")
    if len(rows) < count:
        raise AuditError(f"need {count} nonempty rows, found {len(rows)}")
    if count == 1:
        return [rows[0]]
    indexes = [(index * (len(rows) - 1)) // (count - 1) for index in range(count)]
    if len(set(indexes)) != count:
        raise AuditError("even sample produced duplicate indexes")
    return [rows[index] for index in indexes]


def _load_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if set(payload) != {"doc_id", "chunk_id", "text"}:
                raise AuditError(f"line {line_number} fields are not exact")
            if any(not isinstance(payload[field], str) for field in payload):
                raise AuditError(f"line {line_number} fields must be strings")
            if not payload["doc_id"] or not payload["chunk_id"]:
                raise AuditError(f"line {line_number} has an empty identity")
            if payload["text"].strip():
                rows.append(payload)
    rows.sort(key=lambda row: row["chunk_id"])
    return rows


def run(args: argparse.Namespace) -> dict[str, Any]:
    import spacy

    rows = _load_rows(args.input)
    selected = _sample_evenly(rows, args.sample_count)
    nlp = spacy.load(args.spacy_model)
    model_version = str(nlp.meta.get("version") or "unknown")
    parser_version = f"spacy:{spacy.__version__};model:{model_version}"

    observed = 0
    matched = 0
    unresolved = 0
    sentence_count = 0
    evidence_errors = 0
    matched_counts: Counter[str] = Counter()
    unresolved_lemmas: Counter[str] = Counter()
    receipt_identities: set[tuple[str, str, str, str]] = set()
    for row in selected:
        bundle = build_spacy_observation_bundle(
            text=row["text"],
            nlp=nlp,
            source_version_id=args.source_version_id,
            hierarchy_node_id=row["chunk_id"],
            parser_id=args.spacy_model,
            parser_version=parser_version,
        )
        evidence_errors += len(validate_evidence_round_trip(bundle, row["text"]))
        result = compile_local_extraction_v1(
            bundle,
            document_id=row["doc_id"],
            child_id=row["chunk_id"],
        )
        observed += result.observed_predicate_count
        matched += result.matched_predicate_count
        unresolved += result.unresolved_predicate_count
        sentence_count += len(result.extraction.sentence_ids)
        matched_counts.update(dict(result.matched_counts))
        unresolved_lemmas.update(
            predicate.predicate_lemma
            for predicate in bundle.predicates
            if normalize_predicate_lemma(predicate.predicate_lemma) is None
        )
        receipt_identities.add(
            (
                result.normalization_registry,
                result.normalization_registry_version,
                result.normalization_registry_hash,
                result.recipe_hash,
            )
        )
    if evidence_errors:
        raise AuditError(f"evidence round-trip errors: {evidence_errors}")
    if len(receipt_identities) != 1:
        raise AuditError("compiler provenance identity drifted within one run")
    registry, registry_version, registry_hash, recipe_hash = next(
        iter(receipt_identities)
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "corpus": {
            "name": args.corpus_name,
            "corpus_id": args.corpus_id,
            "source_version_id": args.source_version_id,
            "source_child_rows": len(rows),
            "sampled_child_rows": len(selected),
            "sample_strategy": "evenly_spaced_by_child_id",
        },
        "runtime": {
            "spacy_library_version": str(spacy.__version__),
            "spacy_model": args.spacy_model,
            "spacy_model_version": model_version,
            "parser_version": parser_version,
        },
        "compiler": {
            "normalization_registry": registry,
            "normalization_registry_version": registry_version,
            "normalization_registry_hash": registry_hash,
            "recipe_hash": recipe_hash,
            "observed_predicate_count": observed,
            "matched_predicate_count": matched,
            "unresolved_predicate_count": unresolved,
            "unresolved_rate": unresolved / observed if observed else 0.0,
            "matched_counts": dict(sorted(matched_counts.items())),
            "top_unresolved_lemmas": [
                {"lemma": lemma, "count": count}
                for lemma, count in sorted(
                    unresolved_lemmas.items(), key=lambda item: (-item[1], item[0])
                )[:20]
            ],
            "sentence_count": sentence_count,
            "evidence_round_trip_errors": evidence_errors,
        },
        "scope": {
            "read_only": True,
            "annotation_writes": 0,
            "provider_calls": 0,
            "raw_text_in_receipt": False,
            "child_ids_in_receipt": False,
            "top_unresolved_lemmas_are_aggregate_only": True,
            "unresolved_rate_is_finding_not_failure": True,
        },
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--corpus-name", required=True)
    parser.add_argument("--source-version-id", required=True)
    parser.add_argument("--spacy-model", default="en_core_web_sm")
    parser.add_argument("--sample-count", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run(args)
    _write_report(args.output, report)
    compiler = report["compiler"]
    print(
        f"sampled={report['corpus']['sampled_child_rows']} "
        f"sentences={compiler['sentence_count']} "
        f"predicates={compiler['observed_predicate_count']} "
        f"matched={compiler['matched_predicate_count']} "
        f"unresolved={compiler['unresolved_predicate_count']} "
        f"unresolved_rate={compiler['unresolved_rate']:.6f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
