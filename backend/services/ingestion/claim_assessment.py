"""Deterministic negation and typed-signature assessment for compiled claims.

The assessment is deliberately observational.  It joins exact compiler
evidence to relation candidates and reuses ``ghost_b.DOMAIN_RANGE_MAP`` via a
small exact-meaning adapter.  Unsupported local vocabulary remains explicitly
unassessed; no relation is dropped, remapped, accepted, or promoted here.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from models.claim_assessment import (
    AssessmentProvenanceV1,
    ClaimNegationAssessmentV1,
    ClaimSemanticAssessmentV1,
    EvidenceSentenceBoundaryV1,
    NegationCueV1,
    NegationDerivation,
    RelationSemanticAssessmentV1,
    SignatureAssessmentReason,
)
from models.claim_record import ClaimCompilationV1, ClaimRecordV1
from models.hash_taxonomy import namespace_hash
from models.local_extraction import (
    EntityMention,
    LocalExtractionV1,
    RelationCandidate,
)
from models.semantic_artifacts import (
    EvidenceRef,
    ObservationBundle,
    PredicateObservation,
    QualifierObservation,
)
from services.ghost_b import DOMAIN_RANGE_MAP


ASSESSMENT_VERSION = "claim_semantic_assessment.v1"
SIGNATURE_CONTRACT_ID = "relation_signature_adapter.v1"

# These are exact lexical bridges, not a second compatibility table.  Every
# predicate below points to the existing DOMAIN_RANGE_MAP mechanism.  Similar
# but non-identical meanings (for example USED_FOR -> uses) stay unassessed.
_EXACT_PREDICATE_ADAPTER = {
    "CAUSES": "causes",
    "PART_OF": "part_of",
}
_EXACT_ENTITY_TYPE_ADAPTER = {
    "PERSON": "Person",
    "ORGANIZATION": "Organization",
    "PLACE": "Location",
    "PRODUCT": "Product",
    "DOCUMENT": "Document",
    "CONCEPT": "Concept",
    "METHOD": "Method",
}


def signature_contract_identity_v1() -> dict:
    """Return the hashed, annotate-only adapter over the existing table."""

    predicates = sorted(set(_EXACT_PREDICATE_ADAPTER.values()))
    constraints = {
        predicate: {
            "subject_types": list(DOMAIN_RANGE_MAP[predicate]["subject_types"]),
            "object_types": list(DOMAIN_RANGE_MAP[predicate]["object_types"]),
        }
        for predicate in predicates
    }
    return {
        "contract_id": SIGNATURE_CONTRACT_ID,
        "compatibility_source": "services.ghost_b.DOMAIN_RANGE_MAP",
        "predicate_adapter": dict(sorted(_EXACT_PREDICATE_ADAPTER.items())),
        "entity_type_adapter": dict(sorted(_EXACT_ENTITY_TYPE_ADAPTER.items())),
        "constraints": constraints,
        "enforcement": "annotate_only_no_drop_no_remap",
        "unsupported_mapping": "signature_valid_null",
        "authority": "executor-proposed, owner-ratifiable",
    }


def signature_contract_hash_v1() -> str:
    return namespace_hash("recipe", signature_contract_identity_v1())


def _boundaries(
    evidence_ids: Iterable[str], evidence: dict[str, EvidenceRef]
) -> list[EvidenceSentenceBoundaryV1]:
    rows: list[EvidenceSentenceBoundaryV1] = []
    for evidence_id in evidence_ids:
        item = evidence.get(evidence_id)
        if item is None:
            raise ValueError("assessment references unknown sentence evidence")
        rows.append(
            EvidenceSentenceBoundaryV1(
                evidence_sentence_id=item.evidence_ref_id,
                start_char=item.start,
                end_char=item.end,
                quote_hash=item.quote_hash,
            )
        )
    return rows


def _negation_cues(
    predicate_observation_id: str,
    qualifiers_by_target: dict[str, list[QualifierObservation]],
) -> list[NegationCueV1]:
    return [
        NegationCueV1(
            qualifier_observation_id=item.observation_id,
            cue=item.cue,
            start_char=item.start,
            end_char=item.end,
            producer=item.producer,
        )
        for item in sorted(
            (
                qualifier
                for qualifier in qualifiers_by_target.get(predicate_observation_id, [])
                if qualifier.kind == "negation"
            ),
            key=lambda qualifier: (
                qualifier.start,
                qualifier.end,
                qualifier.observation_id,
            ),
        )
    ]


def _negation_derivation(
    *, predicate_negated: bool | None, cues: list[NegationCueV1]
) -> NegationDerivation:
    if predicate_negated is True and cues:
        return "predicate_and_qualifier_agree"
    if predicate_negated is True:
        return "predicate_only"
    if cues:
        return "qualifier_only"
    return "not_negated"


def _predicate_observation_by_mention(
    *,
    bundle: ObservationBundle,
    extraction: LocalExtractionV1,
) -> dict[str, PredicateObservation]:
    spans = {item.observation_id: item for item in bundle.spans}
    observations: dict[tuple[int, int, str], PredicateObservation] = {}
    for observation in bundle.predicates:
        span = spans.get(observation.predicate_span_id)
        if span is None:
            raise ValueError("predicate observation references an unknown span")
        key = (span.start, span.end, span.text)
        if key in observations:
            raise ValueError("predicate observation coordinates must be unique")
        observations[key] = observation

    rows: dict[str, PredicateObservation] = {}
    for mention in extraction.predicates:
        observation = observations.get(
            (mention.start_char, mention.end_char, mention.surface_text)
        )
        if observation is None:
            raise ValueError("typed predicate has no exact observation coordinate")
        rows[mention.predicate_id] = observation
    return rows


def _dependency_assessment(
    *, relation: RelationCandidate, claim: ClaimRecordV1 | None
) -> tuple[bool, str | None]:
    if claim is None:
        return False, "predicate_not_compiled"
    if relation.relation_id in claim.source_relation_ids:
        return True, None

    subjects = {
        item.filler_ref
        for item in claim.arguments
        if item.role == "subject" and item.filler_kind == "entity_mention"
    }
    objects = {
        item.filler_ref
        for item in claim.arguments
        if item.role == "object" and item.filler_kind == "entity_mention"
    }
    disagreements: list[str] = []
    if relation.source_mention_id not in subjects:
        disagreements.append("source_endpoint_disagrees")
    if relation.target_mention_id not in objects:
        disagreements.append("target_endpoint_disagrees")
    if not set(relation.evidence_sentence_ids) & set(claim.evidence_sentence_ids):
        disagreements.append("evidence_sentence_disagrees")
    if len(disagreements) == 1:
        return False, disagreements[0]
    if len(disagreements) > 1:
        return False, "multiple_disagreements"
    return False, "compiler_rejected_unspecified"


def _signature_assessment(
    *,
    relation: RelationCandidate,
    source: EntityMention,
    target: EntityMention,
) -> tuple[str | None, str | None, str | None, bool | None, str | None]:
    predicate = _EXACT_PREDICATE_ADAPTER.get(relation.relation_type)
    if predicate is None:
        return None, None, None, None, "predicate_mapping_unavailable"
    source_type = _EXACT_ENTITY_TYPE_ADAPTER.get(source.entity_type)
    if source_type is None:
        return predicate, None, None, None, "source_type_mapping_unavailable"
    target_type = _EXACT_ENTITY_TYPE_ADAPTER.get(target.entity_type)
    if target_type is None:
        return predicate, source_type, None, None, "target_type_mapping_unavailable"
    constraints = DOMAIN_RANGE_MAP.get(predicate)
    if constraints is None:
        return (
            predicate,
            source_type,
            target_type,
            None,
            "signature_contract_unavailable",
        )

    subject_ok = source_type in constraints.get("subject_types", [])
    target_ok = target_type in constraints.get("object_types", [])
    if subject_ok and target_ok:
        return predicate, source_type, target_type, True, None
    if not subject_ok and not target_ok:
        reason: SignatureAssessmentReason = "subject_and_target_type_not_allowed"
    elif not subject_ok:
        reason = "subject_type_not_allowed"
    else:
        reason = "target_type_not_allowed"
    return predicate, source_type, target_type, False, reason


def _validate_scope(
    *,
    bundle: ObservationBundle,
    extraction: LocalExtractionV1,
    compilation: ClaimCompilationV1,
) -> None:
    if extraction.child_id != bundle.hierarchy_node_id:
        raise ValueError("assessment inputs must share child scope")
    if (
        compilation.document_id != extraction.document_id
        or compilation.child_id != extraction.child_id
    ):
        raise ValueError("compiled claims and extraction must share ownership")
    expected_sentences = [item.evidence_ref_id for item in bundle.evidence_refs]
    if extraction.sentence_ids != expected_sentences:
        raise ValueError("assessment requires exact extraction sentence identity")


def assess_claim_compilation_v1(
    *,
    bundle: ObservationBundle,
    extraction: LocalExtractionV1,
    compilation: ClaimCompilationV1,
    provenance: AssessmentProvenanceV1,
) -> ClaimSemanticAssessmentV1:
    """Annotate every compiled claim and emitted relation without mutation."""

    _validate_scope(bundle=bundle, extraction=extraction, compilation=compilation)
    evidence = {item.evidence_ref_id: item for item in bundle.evidence_refs}
    qualifiers_by_target: dict[str, list[QualifierObservation]] = defaultdict(list)
    for qualifier in bundle.qualifiers:
        qualifiers_by_target[qualifier.target_observation_id].append(qualifier)

    mentions = {item.mention_id: item for item in extraction.entities}
    predicates = {item.predicate_id: item for item in extraction.predicates}
    observations_by_mention = _predicate_observation_by_mention(
        bundle=bundle, extraction=extraction
    )
    claims_by_predicate = {
        item.predicate_id: item
        for item in compilation.claims
        if item.predicate_id is not None
    }
    typed_claim_count = sum(
        item.predicate_id is not None for item in compilation.claims
    )
    if len(claims_by_predicate) != typed_claim_count:
        raise ValueError("compiled typed claims must reference unique predicates")

    claim_rows: list[ClaimNegationAssessmentV1] = []
    for claim in sorted(compilation.claims, key=lambda item: item.claim_id):
        mention = predicates.get(claim.predicate_id or "")
        cues = _negation_cues(claim.predicate_observation_id, qualifiers_by_target)
        claim_rows.append(
            ClaimNegationAssessmentV1(
                schema_version="claim_negation_assessment.v1",
                claim_id=claim.claim_id,
                predicate_observation_id=claim.predicate_observation_id,
                negated=claim.polarity == "negative",
                negation_cues=cues,
                evidence_sentences=_boundaries(claim.evidence_sentence_ids, evidence),
                derivation=_negation_derivation(
                    predicate_negated=mention.negated if mention else None,
                    cues=cues,
                ),
                knowledge_status="candidate",
                validation_status="candidate",
            )
        )

    contract_hash = signature_contract_hash_v1()
    relation_rows: list[RelationSemanticAssessmentV1] = []
    for relation in sorted(extraction.relations, key=lambda item: item.relation_id):
        source = mentions.get(relation.source_mention_id)
        target = mentions.get(relation.target_mention_id)
        predicate = predicates.get(relation.predicate_id)
        observation = observations_by_mention.get(relation.predicate_id)
        if source is None or target is None or predicate is None or observation is None:
            raise ValueError("local extraction relation reference closure drifted")
        claim = claims_by_predicate.get(relation.predicate_id)
        dependency_agrees, conflict_reason = _dependency_assessment(
            relation=relation, claim=claim
        )
        cues = _negation_cues(observation.observation_id, qualifiers_by_target)
        effective_negated = predicate.negated or bool(cues)
        negation_derivation = _negation_derivation(
            predicate_negated=predicate.negated, cues=cues
        )
        negation_source_agrees = negation_derivation in {
            "predicate_and_qualifier_agree",
            "not_negated",
        }
        claim_polarity = claim.polarity if dependency_agrees and claim else None
        claim_polarity_agrees = (
            (claim_polarity == "negative") == effective_negated
            if claim_polarity is not None
            else None
        )
        polarity_conflicts = []
        if negation_derivation == "predicate_only":
            polarity_conflicts.append("predicate_flag_without_attached_cue")
        elif negation_derivation == "qualifier_only":
            polarity_conflicts.append("attached_cue_missing_predicate_flag")
        if claim_polarity_agrees is False:
            polarity_conflicts.append("relation_disagrees_with_compiled_claim")
        (
            signature_predicate,
            signature_source_type,
            signature_target_type,
            signature_valid,
            signature_reason,
        ) = _signature_assessment(relation=relation, source=source, target=target)
        relation_rows.append(
            RelationSemanticAssessmentV1(
                schema_version="relation_semantic_assessment.v1",
                relation_id=relation.relation_id,
                predicate_id=relation.predicate_id,
                relation_type=relation.relation_type,
                source_mention_id=relation.source_mention_id,
                source_entity_type=source.entity_type,
                target_mention_id=relation.target_mention_id,
                target_entity_type=target.entity_type,
                claim_id=claim.claim_id if dependency_agrees and claim else None,
                dependency_agrees=dependency_agrees,
                dependency_conflict_reason=conflict_reason,
                negated=effective_negated,
                negation_cues=cues,
                evidence_sentences=_boundaries(
                    relation.evidence_sentence_ids, evidence
                ),
                negation_derivation=negation_derivation,
                negation_source_agrees=negation_source_agrees,
                claim_polarity=claim_polarity,
                claim_polarity_agrees=claim_polarity_agrees,
                polarity_conflict_reasons=polarity_conflicts,
                signature_predicate=signature_predicate,
                signature_source_type=signature_source_type,
                signature_target_type=signature_target_type,
                signature_valid=signature_valid,
                signature_violation_reason=signature_reason,
                signature_contract_id=SIGNATURE_CONTRACT_ID,
                signature_contract_hash=contract_hash,
                observation_only=True,
                promotion_disposition=(
                    "owner_pending_negated"
                    if effective_negated or polarity_conflicts
                    else "candidate_only"
                ),
                knowledge_status="candidate",
                validation_status="candidate",
            )
        )

    relation_ids = {item.relation_id for item in relation_rows}
    if relation_ids != {item.relation_id for item in extraction.relations}:
        raise ValueError("assessment must preserve every emitted relation")
    claim_ids = {item.claim_id for item in claim_rows}
    if claim_ids != {item.claim_id for item in compilation.claims}:
        raise ValueError("assessment must preserve every compiled claim")

    recipe = {
        "assessment": ASSESSMENT_VERSION,
        "compiler_recipe_hash": compilation.compiler_recipe_hash,
        "signature_contract_hash": contract_hash,
        "negation": "exact_qualifier_cues_plus_predicate_flag.v1",
        "evidence": "referenced_sentences_only_exact_boundaries.v1",
        "dependency": "compiler_direction_agreement_observation_only.v1",
        "promotion": "candidate_only_negated_owner_pending.v1",
    }
    return ClaimSemanticAssessmentV1(
        schema_version="claim_semantic_assessment.v1",
        document_id=extraction.document_id,
        child_id=extraction.child_id,
        provenance=provenance,
        claim_negation_assessments=claim_rows,
        relation_assessments=relation_rows,
        signature_contract_id=SIGNATURE_CONTRACT_ID,
        signature_contract_hash=contract_hash,
        assessment_recipe_hash=namespace_hash("recipe", recipe),
    )
