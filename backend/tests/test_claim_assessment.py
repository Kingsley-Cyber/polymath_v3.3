from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from models.claim_assessment import (
    AssessmentProvenanceV1,
    CLAIM_ASSESSMENT_AUTHORITY,
    CLAIM_ASSESSMENT_CHANGE_POLICY,
    CLAIM_ASSESSMENT_OWNER_RATIFICATION_REQUIRED,
    ClaimSemanticAssessmentV1,
    RelationSemanticAssessmentV1,
)
from models.local_extraction import (
    EntityMention,
    LocalExtractionV1,
    PredicateMention,
    RelationCandidate,
)
from models.semantic_artifacts import (
    ObservationBundle,
    PredicateObservation,
    QualifierObservation,
    SpanObservation,
    make_evidence_ref,
)
from services.ingestion.claim_assessment import (
    assess_claim_compilation_v1,
    signature_contract_hash_v1,
    signature_contract_identity_v1,
)
from services.ingestion.claim_compiler import compile_claim_records_v1
from services.ghost_b import DOMAIN_RANGE_MAP


SIGNATURE_CONTRACT_HASH_GOLDEN = (
    "sha256:bc5f9fd57613a26524e98c2b3484c62a9261b192d0d767062e067cef7c327239"
)
ASSESSMENT_RECIPE_HASH_FIXTURE_GOLDEN = (
    "sha256:cb8d371700e750ba931c18ae88b5dd3d7942ecf7c2c535bad7181aecdfe4c5a7"
)


def _occurrence(text: str, surface: str, occurrence: int = 0) -> tuple[int, int]:
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(surface, start + 1)
    return start, start + len(surface)


def _assessment_fixture(
    *, reverse_first_relation: bool = False
) -> tuple[ObservationBundle, LocalExtractionV1]:
    text = (
        "Alpha does not cause Beta. "
        "Gamma causes Delta. "
        "Epsilon associates with Zeta."
    )
    second_start = text.index("Gamma")
    third_start = text.index("Epsilon")
    ranges = [
        (0, second_start - 1),
        (second_start, third_start - 1),
        (third_start, len(text)),
    ]
    evidence = [
        make_evidence_ref(
            text=text,
            start=start,
            end=end,
            source_version_id="source-version:assessment",
            hierarchy_node_id="child:assessment",
        )
        for start, end in ranges
    ]
    rows = [
        ("negative", "cause", "cause", "Alpha", "Beta", "CAUSES", True),
        ("invalid", "cause", "causes", "Gamma", "Delta", "CAUSES", False),
        (
            "unsupported",
            "associate",
            "associates",
            "Epsilon",
            "Zeta",
            "ASSOCIATED_WITH",
            False,
        ),
    ]
    entity_types = {
        "Alpha": "CONCEPT",
        "Beta": "CONCEPT",
        "Gamma": "PERSON",
        "Delta": "ORGANIZATION",
        "Epsilon": "CONCEPT",
        "Zeta": "CONCEPT",
    }
    spans: list[SpanObservation] = []
    predicate_observations: list[PredicateObservation] = []
    predicate_mentions: list[PredicateMention] = []
    entities: list[EntityMention] = []
    relations: list[RelationCandidate] = []
    for index, (
        name,
        lemma,
        surface,
        subject_surface,
        object_surface,
        relation_type,
        negated,
    ) in enumerate(rows):
        predicate_start, predicate_end = _occurrence(text, surface)
        subject_start, subject_end = _occurrence(text, subject_surface)
        object_start, object_end = _occurrence(text, object_surface)
        predicate_span_id = f"span:{name}:predicate"
        subject_span_id = f"span:{name}:subject"
        object_span_id = f"span:{name}:object"
        spans.extend(
            [
                SpanObservation(
                    observation_id=predicate_span_id,
                    kind="predicate",
                    label=lemma,
                    text=surface,
                    start=predicate_start,
                    end=predicate_end,
                    producer="spacy:fixture",
                ),
                SpanObservation(
                    observation_id=subject_span_id,
                    kind="subject",
                    label="noun",
                    text=subject_surface,
                    start=subject_start,
                    end=subject_end,
                    producer="spacy:fixture",
                ),
                SpanObservation(
                    observation_id=object_span_id,
                    kind="object",
                    label="noun",
                    text=object_surface,
                    start=object_start,
                    end=object_end,
                    producer="spacy:fixture",
                ),
            ]
        )
        sentence = evidence[index]
        observation_id = f"predicate-observation:{name}"
        predicate_id = f"predicate:{name}"
        source_mention_id = f"mention:{subject_surface.lower()}"
        target_mention_id = f"mention:{object_surface.lower()}"
        predicate_observations.append(
            PredicateObservation(
                observation_id=observation_id,
                predicate_span_id=predicate_span_id,
                predicate_lemma=lemma,
                subject_span_ids=[subject_span_id],
                object_span_ids=[object_span_id],
                evidence_ref_id=sentence.evidence_ref_id,
                producer="spacy:fixture",
            )
        )
        predicate_mentions.append(
            PredicateMention(
                predicate_id=predicate_id,
                surface_text=surface,
                lemma=lemma,
                normalized_predicate=relation_type,
                start_char=predicate_start,
                end_char=predicate_end,
                negated=negated,
                modality="asserted",
                confidence=1.0,
            )
        )
        entities.extend(
            [
                EntityMention(
                    mention_id=source_mention_id,
                    text=subject_surface,
                    entity_type=entity_types[subject_surface],
                    start_char=subject_start,
                    end_char=subject_end,
                    canonical_label=subject_surface.lower(),
                    confidence=1.0,
                ),
                EntityMention(
                    mention_id=target_mention_id,
                    text=object_surface,
                    entity_type=entity_types[object_surface],
                    start_char=object_start,
                    end_char=object_end,
                    canonical_label=object_surface.lower(),
                    confidence=1.0,
                ),
            ]
        )
        relation_source = source_mention_id
        relation_target = target_mention_id
        if index == 0 and reverse_first_relation:
            relation_source, relation_target = relation_target, relation_source
        relations.append(
            RelationCandidate(
                relation_id=f"relation:{name}",
                source_mention_id=relation_source,
                predicate_id=predicate_id,
                target_mention_id=relation_target,
                relation_type=relation_type,
                condition_mention_ids=[],
                temporal_mention_ids=[],
                evidence_sentence_ids=[sentence.evidence_ref_id],
                confidence=0.9,
            )
        )

    not_start, not_end = _occurrence(text, "not")
    bundle = ObservationBundle(
        bundle_id="observation-bundle:assessment",
        source_version_id="source-version:assessment",
        hierarchy_node_id="child:assessment",
        text_hash="sha256:text",
        text_length=len(text),
        producer="spacy:fixture",
        producer_version="fixture.v1",
        recipe_hash="sha256:observation-recipe",
        spans=spans,
        predicates=predicate_observations,
        qualifiers=[
            QualifierObservation(
                observation_id="qualifier:not",
                target_observation_id="predicate-observation:negative",
                kind="negation",
                cue="not",
                normalized_value="negated",
                start=not_start,
                end=not_end,
                producer="spacy:fixture",
            )
        ],
        evidence_refs=evidence,
    )
    extraction = LocalExtractionV1(
        schema_version="local_extraction.v1",
        document_id="doc:assessment",
        child_id="child:assessment",
        sentence_ids=[item.evidence_ref_id for item in evidence],
        entities=entities,
        predicates=predicate_mentions,
        relations=relations,
        unresolved_spans=[],
    )
    return bundle, extraction


def _provenance() -> AssessmentProvenanceV1:
    return AssessmentProvenanceV1(
        corpus_id="ugo",
        provider="deterministic_local",
        model="en_core_web_sm@3.8.0",
        engine="spacy+controlled_relation_fixture",
    )


def test_assessment_preserves_negation_boundaries_and_annotates_signatures() -> None:
    bundle, extraction = _assessment_fixture()
    extraction_before = deepcopy(extraction.model_dump(mode="json"))
    compilation = compile_claim_records_v1(bundle=bundle, extraction=extraction)

    assessed = assess_claim_compilation_v1(
        bundle=bundle,
        extraction=extraction,
        compilation=compilation,
        provenance=_provenance(),
    )

    assert extraction.model_dump(mode="json") == extraction_before
    assert len(assessed.claim_negation_assessments) == len(compilation.claims) == 3
    assert len(assessed.relation_assessments) == len(extraction.relations) == 3
    negative = next(
        item
        for item in assessed.relation_assessments
        if item.relation_id == "relation:negative"
    )
    assert negative.negated is True
    assert negative.negation_cues[0].cue == "not"
    assert negative.negation_cues[0].start_char == bundle.qualifiers[0].start
    assert negative.negation_derivation == "predicate_and_qualifier_agree"
    assert negative.negation_source_agrees is True
    assert negative.claim_polarity == "negative"
    assert negative.claim_polarity_agrees is True
    assert negative.polarity_conflict_reasons == []
    assert negative.promotion_disposition == "owner_pending_negated"
    assert negative.signature_predicate == "causes"
    assert negative.signature_valid is True
    assert negative.signature_violation_reason is None
    assert negative.dependency_agrees is True
    assert negative.claim_id is not None
    assert len(negative.evidence_sentences) == 1
    assert (
        negative.evidence_sentences[0].evidence_sentence_id
        == extraction.relations[0].evidence_sentence_ids[0]
    )
    assert negative.evidence_sentences[0].end_char < bundle.evidence_refs[1].start

    invalid = next(
        item
        for item in assessed.relation_assessments
        if item.relation_id == "relation:invalid"
    )
    assert invalid.signature_valid is False
    assert invalid.signature_violation_reason == "subject_and_target_type_not_allowed"
    assert invalid.relation_type == "CAUSES"

    unsupported = next(
        item
        for item in assessed.relation_assessments
        if item.relation_id == "relation:unsupported"
    )
    assert unsupported.signature_valid is None
    assert unsupported.signature_violation_reason == "predicate_mapping_unavailable"
    assert unsupported.signature_predicate is None

    receipt = assessed.receipt()
    assert receipt["corpus_id"] == "ugo"
    assert receipt["provider"] == "deterministic_local"
    assert receipt["model"] == "en_core_web_sm@3.8.0"
    assert receipt["signature_assessed_count"] == 2
    assert receipt["signature_valid_count"] == 1
    assert receipt["signature_invalid_count"] == 1
    assert receipt["signature_unassessed_count"] == 1
    assert receipt["relations_by_predicate"]["CAUSES"]["relations"] == 2
    assert assessed.assessment_recipe_hash == ASSESSMENT_RECIPE_HASH_FIXTURE_GOLDEN


def test_dependency_conflict_is_retained_as_observation_only() -> None:
    bundle, extraction = _assessment_fixture(reverse_first_relation=True)
    compilation = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assessed = assess_claim_compilation_v1(
        bundle=bundle,
        extraction=extraction,
        compilation=compilation,
        provenance=_provenance(),
    )

    row = next(
        item
        for item in assessed.relation_assessments
        if item.relation_id == "relation:negative"
    )
    assert row.dependency_agrees is False
    assert row.claim_id is None
    assert row.dependency_conflict_reason == "multiple_disagreements"
    assert row.observation_only is True
    assert row.relation_id in compilation.rejected_relation_ids


def test_attached_negation_conflict_is_annotated_without_live_drop() -> None:
    bundle, extraction = _assessment_fixture()
    extraction.predicates[0] = extraction.predicates[0].model_copy(
        update={"negated": False}
    )
    compilation = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assessed = assess_claim_compilation_v1(
        bundle=bundle,
        extraction=extraction,
        compilation=compilation,
        provenance=_provenance(),
    )

    row = next(
        item
        for item in assessed.relation_assessments
        if item.relation_id == "relation:negative"
    )
    assert row.dependency_agrees is True
    assert row.negated is True
    assert row.negation_source_agrees is False
    assert row.claim_polarity == "positive"
    assert row.claim_polarity_agrees is False
    assert row.polarity_conflict_reasons == [
        "attached_cue_missing_predicate_flag",
        "relation_disagrees_with_compiled_claim",
    ]
    assert row.promotion_disposition == "owner_pending_negated"
    assert row.observation_only is True
    assert assessed.receipt()["polarity_conflict_count"] == 1


def test_predicate_only_negation_stays_owner_pending_with_missing_cue_reason() -> None:
    bundle, extraction = _assessment_fixture()
    bundle.qualifiers = []
    compilation = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assessed = assess_claim_compilation_v1(
        bundle=bundle,
        extraction=extraction,
        compilation=compilation,
        provenance=_provenance(),
    )

    row = next(
        item
        for item in assessed.relation_assessments
        if item.relation_id == "relation:negative"
    )
    assert row.negated is True
    assert row.negation_derivation == "predicate_only"
    assert row.negation_source_agrees is False
    assert row.claim_polarity_agrees is True
    assert row.polarity_conflict_reasons == ["predicate_flag_without_attached_cue"]
    assert row.promotion_disposition == "owner_pending_negated"


def test_signature_contract_hashes_only_existing_table_subset_and_adapters() -> None:
    identity = signature_contract_identity_v1()

    assert identity["compatibility_source"] == "services.ghost_b.DOMAIN_RANGE_MAP"
    assert sorted(identity["constraints"]) == ["causes", "part_of"]
    assert identity["predicate_adapter"] == {
        "CAUSES": "causes",
        "PART_OF": "part_of",
    }
    assert identity["constraints"] == {
        predicate: {
            "subject_types": DOMAIN_RANGE_MAP[predicate]["subject_types"],
            "object_types": DOMAIN_RANGE_MAP[predicate]["object_types"],
        }
        for predicate in ("causes", "part_of")
    }
    assert signature_contract_hash_v1() == SIGNATURE_CONTRACT_HASH_GOLDEN


def test_assessment_contract_is_strict_and_owner_ratifiable() -> None:
    assert CLAIM_ASSESSMENT_AUTHORITY == "executor-proposed, owner-ratifiable"
    assert CLAIM_ASSESSMENT_OWNER_RATIFICATION_REQUIRED is True
    assert CLAIM_ASSESSMENT_CHANGE_POLICY == "changes-require-new-schema-version"
    schema = ClaimSemanticAssessmentV1.model_json_schema()
    assert schema["authority"] == CLAIM_ASSESSMENT_AUTHORITY
    assert schema["owner_ratification_required"] is True
    assert schema["change_policy"] == CLAIM_ASSESSMENT_CHANGE_POLICY
    payload = _provenance().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        AssessmentProvenanceV1.model_validate(payload)


def test_relation_assessment_rejects_unexplained_null_signature() -> None:
    bundle, extraction = _assessment_fixture()
    compilation = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assessed = assess_claim_compilation_v1(
        bundle=bundle,
        extraction=extraction,
        compilation=compilation,
        provenance=_provenance(),
    )
    row = next(
        item for item in assessed.relation_assessments if item.signature_valid is None
    )
    payload = row.model_dump(mode="json")
    payload["signature_violation_reason"] = None

    with pytest.raises(ValidationError):
        RelationSemanticAssessmentV1.model_validate(payload)


def test_relation_assessment_rejects_negation_cue_outside_evidence() -> None:
    bundle, extraction = _assessment_fixture()
    compilation = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assessed = assess_claim_compilation_v1(
        bundle=bundle,
        extraction=extraction,
        compilation=compilation,
        provenance=_provenance(),
    )
    row = next(item for item in assessed.relation_assessments if item.negated)
    payload = row.model_dump(mode="json")
    boundary_end = payload["evidence_sentences"][0]["end_char"]
    payload["negation_cues"][0]["start_char"] = boundary_end + 1
    payload["negation_cues"][0]["end_char"] = boundary_end + 2

    with pytest.raises(ValidationError):
        RelationSemanticAssessmentV1.model_validate(payload)
