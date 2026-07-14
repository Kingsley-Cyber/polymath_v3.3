#!/usr/bin/env python3
"""Read-only, count-only UGO audit of deterministic ClaimRecordV1 compilation."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Sequence

from models.claim_record import (
    CLAIM_RECORD_AUTHORITY,
    CLAIM_RECORD_CHANGE_POLICY,
    CLAIM_RECORD_OWNER_RATIFICATION_REQUIRED,
    ClaimAssertionV1,
    ClaimRecordV1,
)
from models.hash_taxonomy import namespace_hash
from services.ingestion.claim_compiler import (
    COMPILER_VERSION,
    compile_claim_records_v1,
    project_claim_record_to_assertion,
    restore_claim_record_from_assertion,
)
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
    validate_evidence_round_trip,
)


SCHEMA_VERSION = "polymath.claim_compiler_ugo_audit.v1"


class AuditError(RuntimeError):
    """The read-only audit input or compiler result violated its contract."""


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

    observed_predicates = 0
    typed_predicates = 0
    unresolved_predicates = 0
    sentence_count = 0
    claim_count = 0
    typed_claims = 0
    untyped_claims = 0
    skipped_predicates = 0
    same_sentence_repeated_claims = 0
    unresolved_coreference = 0
    glirel_agree = 0
    glirel_conflict = 0
    link_count = 0
    link_families: Counter[str] = Counter()
    cross_candidates = 0
    cross_accepted = 0
    cross_rejected = 0
    untyped_lemmas: Counter[str] = Counter()
    evidence_errors = 0
    claim_evidence_errors = 0
    projection_round_trip_errors = 0
    untyped_carry_forward_errors = 0
    compiler_recipe_hashes: set[str] = set()

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
        local = compile_local_extraction_v1(
            bundle,
            document_id=row["doc_id"],
            child_id=row["chunk_id"],
        )
        compiled = compile_claim_records_v1(
            bundle=bundle,
            extraction=local.extraction,
        )
        receipt = compiled.receipt()
        compiler_recipe_hashes.add(compiled.compiler_recipe_hash)

        observed_predicates += local.observed_predicate_count
        typed_predicates += local.matched_predicate_count
        unresolved_predicates += local.unresolved_predicate_count
        sentence_count += len(local.extraction.sentence_ids)
        claim_count += receipt["claim_count"]
        typed_claims += receipt["typed_claim_count"]
        untyped_claims += receipt["untyped_claim_count"]
        skipped_predicates += receipt["skipped_predicate_count"]
        same_sentence_repeated_claims += receipt["same_sentence_repeated_claim_count"]
        unresolved_coreference += receipt["unresolved_coreference_count"]
        glirel_agree += receipt["glirel_agree_count"]
        glirel_conflict += receipt["glirel_conflict_count"]
        link_count += receipt["link_count"]
        link_families.update(receipt["links_by_connective_family"])
        cross_candidates += receipt["cross_sentence_candidate_count"]
        cross_accepted += receipt["cross_sentence_accepted_count"]
        cross_rejected += receipt["cross_sentence_rejected_count"]
        untyped_lemmas.update(
            item.predicate_lemma
            for item in compiled.claims
            if item.typing_status == "untyped"
        )

        evidence = {item.evidence_ref_id: item for item in bundle.evidence_refs}
        for claim in compiled.claims:
            if any(
                claim.proposition_text != evidence[evidence_id].quote
                for evidence_id in claim.evidence_sentence_ids
            ):
                claim_evidence_errors += 1
            assertion = project_claim_record_to_assertion(claim)
            if restore_claim_record_from_assertion(assertion) != claim:
                projection_round_trip_errors += 1
        if receipt["untyped_claim_count"] != local.unresolved_predicate_count:
            untyped_carry_forward_errors += 1

    if evidence_errors or claim_evidence_errors or projection_round_trip_errors:
        raise AuditError(
            "round-trip errors: "
            f"evidence={evidence_errors} claim={claim_evidence_errors} "
            f"projection={projection_round_trip_errors}"
        )
    if untyped_carry_forward_errors:
        raise AuditError(
            f"untyped carry-forward errors: {untyped_carry_forward_errors}"
        )
    if len(compiler_recipe_hashes) != 1:
        raise AuditError("claim compiler recipe identity drifted within one run")
    if typed_claims + skipped_predicates != typed_predicates:
        raise AuditError("typed claim/skip accounting does not close")
    if cross_accepted + cross_rejected != cross_candidates:
        raise AuditError("cross-sentence accounting does not close")

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
        "contract": {
            "claim_record_authority": CLAIM_RECORD_AUTHORITY,
            "owner_ratification_required": CLAIM_RECORD_OWNER_RATIFICATION_REQUIRED,
            "change_policy": CLAIM_RECORD_CHANGE_POLICY,
            "claim_record_schema_hash": namespace_hash(
                "schema", ClaimRecordV1.model_json_schema()
            ),
            "claim_assertion_schema_hash": namespace_hash(
                "schema", ClaimAssertionV1.model_json_schema()
            ),
            "compiler_version": COMPILER_VERSION,
            "compiler_recipe_hash": next(iter(compiler_recipe_hashes)),
        },
        "compiler": {
            "sentence_count": sentence_count,
            "observed_predicate_count": observed_predicates,
            "typed_predicate_count": typed_predicates,
            "unresolved_predicate_count": unresolved_predicates,
            "claim_count": claim_count,
            "typed_claim_count": typed_claims,
            "untyped_claim_count": untyped_claims,
            "skipped_typed_predicate_count": skipped_predicates,
            "same_sentence_repeated_claim_count": (same_sentence_repeated_claims),
            "untyped_carry_forward_errors": untyped_carry_forward_errors,
            "claim_yield_rate": claim_count / observed_predicates
            if observed_predicates
            else 0.0,
            "top_untyped_lemmas": [
                {"lemma": lemma, "count": count}
                for lemma, count in sorted(
                    untyped_lemmas.items(), key=lambda item: (-item[1], item[0])
                )[:20]
            ],
            "glirel_agree_count": glirel_agree,
            "glirel_conflict_count": glirel_conflict,
            "link_count": link_count,
            "links_by_connective_family": dict(sorted(link_families.items())),
            "cross_sentence_candidate_count": cross_candidates,
            "cross_sentence_accepted_count": cross_accepted,
            "cross_sentence_rejected_count": cross_rejected,
            "unresolved_coreference_count": unresolved_coreference,
            "evidence_round_trip_errors": evidence_errors,
            "claim_evidence_errors": claim_evidence_errors,
            "assertion_projection_round_trip_errors": projection_round_trip_errors,
        },
        "scope": {
            "read_only": True,
            "annotation_writes": 0,
            "provider_calls": 0,
            "raw_text_in_receipt": False,
            "child_ids_in_receipt": False,
            "top_untyped_lemmas_are_aggregate_only": True,
            "untyped_claims_are_observation_only": True,
            "relations_are_observation_only": True,
            "domains_frames_out_of_scope": True,
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
        f"claims={compiler['claim_count']} "
        f"typed={compiler['typed_claim_count']} "
        f"untyped={compiler['untyped_claim_count']} "
        f"links={compiler['link_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
