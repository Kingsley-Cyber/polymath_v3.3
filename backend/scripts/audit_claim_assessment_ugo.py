#!/usr/bin/env python3
"""Read-only, count-only T8.4 UGO claim-assessment census.

The input is an operator-exported JSONL projection containing only ``doc_id``,
``chunk_id``, and ``text``. Every row passes through the trained-spaCy
observation compiler, the deterministic ClaimRecord compiler, and the
additive negation/signature assessment sidecar. The committed report contains
only aggregate counts and contract hashes; raw text and row identifiers never
enter it.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any

from models.claim_assessment import (
    AssessmentProvenanceV1,
    CLAIM_ASSESSMENT_AUTHORITY,
    CLAIM_ASSESSMENT_CHANGE_POLICY,
    CLAIM_ASSESSMENT_OWNER_RATIFICATION_REQUIRED,
    ClaimNegationAssessmentV1,
    ClaimSemanticAssessmentV1,
    RelationSemanticAssessmentV1,
)
from models.claim_record import ClaimRecordV1
from models.hash_taxonomy import namespace_hash
from services.ingestion.claim_assessment import (
    ASSESSMENT_VERSION,
    SIGNATURE_CONTRACT_ID,
    assess_claim_compilation_v1,
    signature_contract_hash_v1,
    signature_contract_identity_v1,
)
from services.ingestion.claim_compiler import (
    COMPILER_VERSION,
    compile_claim_records_v1,
)
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
    validate_evidence_round_trip,
)


SCHEMA_VERSION = "polymath.claim_assessment_ugo_census.v1"


class AuditError(RuntimeError):
    """The read-only input or assessment result violated its contract."""


def _load_rows(path: Path, *, expected_row_count: int) -> list[dict[str, str]]:
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
            if not payload["text"].strip():
                raise AuditError(f"line {line_number} has empty text")
            rows.append(payload)
    rows.sort(key=lambda row: row["chunk_id"])
    if len(rows) != expected_row_count:
        raise AuditError(
            f"expected {expected_row_count} complete rows, found {len(rows)}"
        )
    chunk_ids = [row["chunk_id"] for row in rows]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise AuditError("child projection contains duplicate chunk IDs")
    return rows


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    import spacy

    rows = _load_rows(args.input, expected_row_count=args.expected_row_count)
    nlp = spacy.load(args.spacy_model)
    model_version = str(nlp.meta.get("version") or "unknown")
    parser_version = f"spacy:{spacy.__version__};model:{model_version}"
    provenance_model = f"{args.spacy_model}@{model_version}"

    observed_predicates = 0
    typed_predicates = 0
    unresolved_predicates = 0
    sentence_count = 0
    compiler_claim_count = 0
    typed_claim_count = 0
    untyped_claim_count = 0
    skipped_typed_predicate_count = 0
    same_sentence_repeated_claim_count = 0
    unresolved_coreference_count = 0
    glirel_agree_count = 0
    glirel_conflict_count = 0
    link_count = 0
    cross_sentence_candidate_count = 0
    cross_sentence_accepted_count = 0
    cross_sentence_rejected_count = 0
    emitted_relation_count = 0
    claim_assessment_count = 0
    relation_assessment_count = 0
    negated_claim_count = 0
    negated_relation_count = 0
    polarity_conflict_count = 0
    dependency_agree_count = 0
    dependency_conflict_count = 0
    signature_assessed_count = 0
    signature_valid_count = 0
    signature_invalid_count = 0
    signature_unassessed_count = 0

    claim_derivations: Counter[str] = Counter()
    relation_derivations: Counter[str] = Counter()
    relation_predicates: Counter[str] = Counter()
    signature_reasons: Counter[str] = Counter()
    dependency_reasons: Counter[str] = Counter()
    polarity_reasons: Counter[str] = Counter()
    promotion_dispositions: Counter[str] = Counter()
    link_families: Counter[str] = Counter()

    evidence_round_trip_errors = 0
    claim_conservation_errors = 0
    relation_conservation_errors = 0
    claim_polarity_errors = 0
    candidate_status_errors = 0
    receipt_accounting_errors = 0

    normalization_recipe_hashes: set[str] = set()
    compiler_recipe_hashes: set[str] = set()
    assessment_recipe_hashes: set[str] = set()
    signature_contract_hashes: set[str] = set()
    provenance_identities: set[tuple[str, str, str, str]] = set()

    for row in rows:
        bundle = build_spacy_observation_bundle(
            text=row["text"],
            nlp=nlp,
            source_version_id=args.source_version_id,
            hierarchy_node_id=row["chunk_id"],
            parser_id=args.spacy_model,
            parser_version=parser_version,
        )
        evidence_round_trip_errors += len(
            validate_evidence_round_trip(bundle, row["text"])
        )
        local = compile_local_extraction_v1(
            bundle,
            document_id=row["doc_id"],
            child_id=row["chunk_id"],
        )
        compilation = compile_claim_records_v1(
            bundle=bundle,
            extraction=local.extraction,
        )
        assessment = assess_claim_compilation_v1(
            bundle=bundle,
            extraction=local.extraction,
            compilation=compilation,
            provenance=AssessmentProvenanceV1(
                corpus_id=args.corpus_id,
                provider=args.provider,
                model=provenance_model,
                engine=args.engine,
            ),
        )
        compiler_receipt = compilation.receipt()
        assessment_receipt = assessment.receipt()

        observed_predicates += local.observed_predicate_count
        typed_predicates += local.matched_predicate_count
        unresolved_predicates += local.unresolved_predicate_count
        sentence_count += len(local.extraction.sentence_ids)
        compiler_claim_count += compiler_receipt["claim_count"]
        typed_claim_count += compiler_receipt["typed_claim_count"]
        untyped_claim_count += compiler_receipt["untyped_claim_count"]
        skipped_typed_predicate_count += compiler_receipt["skipped_predicate_count"]
        same_sentence_repeated_claim_count += compiler_receipt[
            "same_sentence_repeated_claim_count"
        ]
        unresolved_coreference_count += compiler_receipt["unresolved_coreference_count"]
        glirel_agree_count += compiler_receipt["glirel_agree_count"]
        glirel_conflict_count += compiler_receipt["glirel_conflict_count"]
        link_count += compiler_receipt["link_count"]
        link_families.update(compiler_receipt["links_by_connective_family"])
        cross_sentence_candidate_count += compiler_receipt[
            "cross_sentence_candidate_count"
        ]
        cross_sentence_accepted_count += compiler_receipt[
            "cross_sentence_accepted_count"
        ]
        cross_sentence_rejected_count += compiler_receipt[
            "cross_sentence_rejected_count"
        ]
        emitted_relation_count += len(local.extraction.relations)
        claim_assessment_count += assessment_receipt["claim_count"]
        relation_assessment_count += assessment_receipt["relation_count"]
        negated_claim_count += assessment_receipt["negated_claim_count"]
        negated_relation_count += assessment_receipt["negated_relation_count"]
        polarity_conflict_count += assessment_receipt["polarity_conflict_count"]
        dependency_agree_count += assessment_receipt["dependency_agree_count"]
        dependency_conflict_count += assessment_receipt["dependency_conflict_count"]
        signature_assessed_count += assessment_receipt["signature_assessed_count"]
        signature_valid_count += assessment_receipt["signature_valid_count"]
        signature_invalid_count += assessment_receipt["signature_invalid_count"]
        signature_unassessed_count += assessment_receipt["signature_unassessed_count"]

        normalization_recipe_hashes.add(local.recipe_hash)
        compiler_recipe_hashes.add(compilation.compiler_recipe_hash)
        assessment_recipe_hashes.add(assessment.assessment_recipe_hash)
        signature_contract_hashes.add(assessment.signature_contract_hash)
        provenance_identities.add(
            (
                assessment.provenance.corpus_id,
                assessment.provenance.provider,
                assessment.provenance.model,
                assessment.provenance.engine,
            )
        )

        original_claim_ids = {item.claim_id for item in compilation.claims}
        assessed_claim_ids = {
            item.claim_id for item in assessment.claim_negation_assessments
        }
        if original_claim_ids != assessed_claim_ids:
            claim_conservation_errors += 1
        original_relations = {
            item.relation_id: item for item in local.extraction.relations
        }
        assessed_relation_ids = {
            item.relation_id for item in assessment.relation_assessments
        }
        if set(original_relations) != assessed_relation_ids:
            relation_conservation_errors += 1

        claims_by_id = {item.claim_id: item for item in compilation.claims}
        for item in assessment.claim_negation_assessments:
            claim_derivations[item.derivation] += 1
            source_claim = claims_by_id[item.claim_id]
            if item.negated != (source_claim.polarity == "negative"):
                claim_polarity_errors += 1
            if (
                item.knowledge_status != "candidate"
                or item.validation_status != "candidate"
            ):
                candidate_status_errors += 1

        for item in assessment.relation_assessments:
            source_relation = original_relations[item.relation_id]
            if (
                item.predicate_id != source_relation.predicate_id
                or item.relation_type != source_relation.relation_type
                or item.source_mention_id != source_relation.source_mention_id
                or item.target_mention_id != source_relation.target_mention_id
            ):
                relation_conservation_errors += 1
            relation_derivations[item.negation_derivation] += 1
            relation_predicates[item.relation_type] += 1
            if item.signature_violation_reason:
                signature_reasons[item.signature_violation_reason] += 1
            if item.dependency_conflict_reason:
                dependency_reasons[item.dependency_conflict_reason] += 1
            polarity_reasons.update(item.polarity_conflict_reasons)
            promotion_dispositions[item.promotion_disposition] += 1
            if (
                not item.observation_only
                or item.knowledge_status != "candidate"
                or item.validation_status != "candidate"
            ):
                candidate_status_errors += 1

        if assessment_receipt["claim_count"] != len(compilation.claims):
            receipt_accounting_errors += 1
        if assessment_receipt["relation_count"] != len(local.extraction.relations):
            receipt_accounting_errors += 1
        if (
            assessment_receipt["signature_assessed_count"]
            + assessment_receipt["signature_unassessed_count"]
            != assessment_receipt["relation_count"]
        ):
            receipt_accounting_errors += 1
        if (
            assessment_receipt["signature_valid_count"]
            + assessment_receipt["signature_invalid_count"]
            != assessment_receipt["signature_assessed_count"]
        ):
            receipt_accounting_errors += 1

    invariant_errors = {
        "evidence_round_trip_errors": evidence_round_trip_errors,
        "claim_conservation_errors": claim_conservation_errors,
        "relation_conservation_errors": relation_conservation_errors,
        "claim_polarity_errors": claim_polarity_errors,
        "candidate_status_errors": candidate_status_errors,
        "receipt_accounting_errors": receipt_accounting_errors,
    }
    if any(invariant_errors.values()):
        raise AuditError(f"assessment invariant errors: {invariant_errors}")
    if typed_claim_count + untyped_claim_count != compiler_claim_count:
        raise AuditError("typed/untyped claim accounting does not close")
    if typed_predicates + unresolved_predicates != observed_predicates:
        raise AuditError("typed/unresolved predicate accounting does not close")
    if typed_claim_count + skipped_typed_predicate_count != typed_predicates:
        raise AuditError("typed claim/skip accounting does not close")
    if cross_sentence_accepted_count + cross_sentence_rejected_count != (
        cross_sentence_candidate_count
    ):
        raise AuditError("cross-sentence claim-link accounting does not close")
    if claim_assessment_count != compiler_claim_count:
        raise AuditError("claim assessment conservation does not close")
    if relation_assessment_count != emitted_relation_count:
        raise AuditError("relation assessment conservation does not close")
    if dependency_agree_count + dependency_conflict_count != emitted_relation_count:
        raise AuditError("dependency assessment accounting does not close")
    if signature_assessed_count + signature_unassessed_count != emitted_relation_count:
        raise AuditError("signature assessment accounting does not close")
    if signature_valid_count + signature_invalid_count != signature_assessed_count:
        raise AuditError("signature validity accounting does not close")
    if sum(claim_derivations.values()) != claim_assessment_count:
        raise AuditError("claim-negation derivation accounting does not close")
    derived_negated_claims = sum(
        claim_derivations[key]
        for key in (
            "predicate_and_qualifier_agree",
            "predicate_only",
            "qualifier_only",
        )
    )
    if derived_negated_claims != negated_claim_count:
        raise AuditError("negated-claim derivation accounting does not close")
    if sum(relation_derivations.values()) != relation_assessment_count:
        raise AuditError("relation-negation derivation accounting does not close")
    if sum(promotion_dispositions.values()) != relation_assessment_count:
        raise AuditError("relation promotion-disposition accounting does not close")
    if sum(signature_reasons.values()) != (
        signature_invalid_count + signature_unassessed_count
    ):
        raise AuditError("signature reason accounting does not close")
    if len(normalization_recipe_hashes) != 1:
        raise AuditError("normalization recipe identity drifted within one run")
    if len(compiler_recipe_hashes) != 1:
        raise AuditError("claim compiler recipe identity drifted within one run")
    if len(assessment_recipe_hashes) != 1:
        raise AuditError("assessment recipe identity drifted within one run")
    if signature_contract_hashes != {signature_contract_hash_v1()}:
        raise AuditError("signature contract identity drifted within one run")
    expected_provenance = {
        (args.corpus_id, args.provider, provenance_model, args.engine)
    }
    if provenance_identities != expected_provenance:
        raise AuditError("assessment provenance identity drifted within one run")

    projection_hash = "sha256:" + hashlib.sha256(args.input.read_bytes()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "corpus": {
            "name": args.corpus_name,
            "corpus_id": args.corpus_id,
            "source_version_id": args.source_version_id,
            "source_child_rows": len(rows),
            "processed_child_rows": len(rows),
            "selection": "all_sanitized_nonempty_child_rows",
            "input_projection_hash": projection_hash,
        },
        "runtime": {
            "spacy_library_version": str(spacy.__version__),
            "spacy_model": args.spacy_model,
            "spacy_model_version": model_version,
            "parser_version": parser_version,
        },
        "provenance_dimensions": {
            "corpus_id": args.corpus_id,
            "provider": args.provider,
            "model": provenance_model,
            "engine": args.engine,
        },
        "contract": {
            "assessment_authority": CLAIM_ASSESSMENT_AUTHORITY,
            "owner_ratification_required": (
                CLAIM_ASSESSMENT_OWNER_RATIFICATION_REQUIRED
            ),
            "change_policy": CLAIM_ASSESSMENT_CHANGE_POLICY,
            "assessment_version": ASSESSMENT_VERSION,
            "assessment_schema_hash": namespace_hash(
                "schema", ClaimSemanticAssessmentV1.model_json_schema()
            ),
            "claim_negation_schema_hash": namespace_hash(
                "schema", ClaimNegationAssessmentV1.model_json_schema()
            ),
            "relation_assessment_schema_hash": namespace_hash(
                "schema", RelationSemanticAssessmentV1.model_json_schema()
            ),
            "claim_record_schema_hash": namespace_hash(
                "schema", ClaimRecordV1.model_json_schema()
            ),
            "normalization_recipe_hash": next(iter(normalization_recipe_hashes)),
            "compiler_version": COMPILER_VERSION,
            "compiler_recipe_hash": next(iter(compiler_recipe_hashes)),
            "assessment_recipe_hash": next(iter(assessment_recipe_hashes)),
            "signature_contract_id": SIGNATURE_CONTRACT_ID,
            "signature_contract_hash": next(iter(signature_contract_hashes)),
            "signature_compatibility_source": signature_contract_identity_v1()[
                "compatibility_source"
            ],
        },
        "compiler_census": {
            "sentence_count": sentence_count,
            "observed_predicate_count": observed_predicates,
            "typed_predicate_count": typed_predicates,
            "unresolved_predicate_count": unresolved_predicates,
            "claim_count": compiler_claim_count,
            "typed_claim_count": typed_claim_count,
            "untyped_claim_count": untyped_claim_count,
            "skipped_typed_predicate_count": skipped_typed_predicate_count,
            "skipped_typed_predicate_reason": "missing_subject_argument",
            "claim_yield_rate": (
                compiler_claim_count / observed_predicates
                if observed_predicates
                else 0.0
            ),
            "same_sentence_repeated_claim_count": (same_sentence_repeated_claim_count),
            "unresolved_coreference_count": unresolved_coreference_count,
            "glirel_agree_count": glirel_agree_count,
            "glirel_conflict_count": glirel_conflict_count,
            "link_count": link_count,
            "links_by_connective_family": _sorted_counts(link_families),
            "cross_sentence_candidate_count": cross_sentence_candidate_count,
            "cross_sentence_accepted_count": cross_sentence_accepted_count,
            "cross_sentence_rejected_count": cross_sentence_rejected_count,
            "emitted_relation_count": emitted_relation_count,
        },
        "assessment_census": {
            "claim_assessment_count": claim_assessment_count,
            "negated_claim_count": negated_claim_count,
            "negated_claim_rate": (
                negated_claim_count / claim_assessment_count
                if claim_assessment_count
                else 0.0
            ),
            "qualifier_only_share_of_negated_claims": (
                claim_derivations["qualifier_only"] / negated_claim_count
                if negated_claim_count
                else 0.0
            ),
            "claim_negation_derivations": _sorted_counts(claim_derivations),
            "relation_assessment_count": relation_assessment_count,
            "negated_relation_count": negated_relation_count,
            "relation_negation_derivations": _sorted_counts(relation_derivations),
            "polarity_conflict_count": polarity_conflict_count,
            "polarity_conflict_reasons": _sorted_counts(polarity_reasons),
            "dependency_agree_count": dependency_agree_count,
            "dependency_conflict_count": dependency_conflict_count,
            "dependency_conflict_reasons": _sorted_counts(dependency_reasons),
            "promotion_dispositions": _sorted_counts(promotion_dispositions),
            "signature_assessed_count": signature_assessed_count,
            "signature_valid_count": signature_valid_count,
            "signature_invalid_count": signature_invalid_count,
            "signature_unassessed_count": signature_unassessed_count,
            "signature_assessment_reasons": _sorted_counts(signature_reasons),
            "relations_by_predicate": _sorted_counts(relation_predicates),
        },
        "invariants": invariant_errors,
        "scope": {
            "read_only": True,
            "annotation_writes": 0,
            "provider_calls": 0,
            "persistence_writes": 0,
            "promotion_writes": 0,
            "graph_writes": 0,
            "vector_writes": 0,
            "raw_text_in_receipt": False,
            "child_ids_in_receipt": False,
            "claims_are_candidates": True,
            "relations_are_observation_only": True,
            "signature_enforcement": "annotate_only_no_drop_no_remap",
            "domains_frames_motifs_out_of_scope": True,
            "empty_relation_lane_is_observed_not_inferred": True,
            "typed_signature_rate_requires_relation_candidates": True,
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
    parser.add_argument("--expected-row-count", type=int, required=True)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--corpus-name", required=True)
    parser.add_argument("--source-version-id", required=True)
    parser.add_argument("--spacy-model", default="en_core_web_sm")
    parser.add_argument("--provider", default="deterministic_local")
    parser.add_argument(
        "--engine",
        default="spacy_observation+claim_compiler+claim_assessment.v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run(args)
    _write_report(args.output, report)
    compiler = report["compiler_census"]
    assessment = report["assessment_census"]
    print(
        f"processed={report['corpus']['processed_child_rows']} "
        f"sentences={compiler['sentence_count']} "
        f"claims={compiler['claim_count']} "
        f"negated_claims={assessment['negated_claim_count']} "
        f"relations={assessment['relation_assessment_count']} "
        f"signature_invalid={assessment['signature_invalid_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
