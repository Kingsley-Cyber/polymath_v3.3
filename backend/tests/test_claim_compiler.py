from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from models.claim_record import (
    CLAIM_RECORD_AUTHORITY,
    CLAIM_RECORD_CHANGE_POLICY,
    CLAIM_RECORD_OWNER_RATIFICATION_REQUIRED,
    ClaimArgumentV1,
    ClaimCompilationV1,
    ClaimLinkV1,
    ClaimRecordV1,
)
from models.hash_taxonomy import namespace_hash
from models.local_extraction import (
    EntityMention,
    LocalExtractionV1,
    PredicateMention,
    RelationCandidate,
)
from models.semantic_artifacts import (
    ObservationBundle,
    PredicateObservation,
    SpanObservation,
    make_evidence_ref,
)
from services.ingestion.claim_compiler import (
    compile_claim_records_v1,
    project_claim_record_to_assertion,
    restore_claim_record_from_assertion,
)
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
)


def _spec(
    text: str,
    *,
    name: str,
    lemma: str,
    predicate_surface: str,
    subject_surface: str | None,
    object_surface: str | None,
    predicate_occurrence: int = 0,
    subject_occurrence: int = 0,
    object_occurrence: int = 0,
) -> dict:
    def locate(surface: str, occurrence: int) -> tuple[int, int]:
        start = -1
        for _ in range(occurrence + 1):
            start = text.index(surface, start + 1)
        return start, start + len(surface)

    predicate_start, predicate_end = locate(predicate_surface, predicate_occurrence)
    subject = (
        (*locate(subject_surface, subject_occurrence), subject_surface)
        if subject_surface is not None
        else None
    )
    object_ = (
        (*locate(object_surface, object_occurrence), object_surface)
        if object_surface is not None
        else None
    )
    return {
        "name": name,
        "lemma": lemma,
        "predicate_surface": predicate_surface,
        "predicate_start": predicate_start,
        "predicate_end": predicate_end,
        "subject": subject,
        "object": object_,
    }


def _fixture(
    text: str,
    specs: list[dict],
    *,
    sentence_ranges: list[tuple[int, int]] | None = None,
    typed: dict[str, str] | None = None,
    entities: list[EntityMention] | None = None,
    relations: list[RelationCandidate] | None = None,
) -> tuple[ObservationBundle, LocalExtractionV1]:
    ranges = sentence_ranges or [(0, len(text))]
    evidence = [
        make_evidence_ref(
            text=text,
            start=start,
            end=end,
            source_version_id="source-version:claim",
            hierarchy_node_id="child:claim",
        )
        for start, end in ranges
    ]
    spans: list[SpanObservation] = []
    predicates: list[PredicateObservation] = []
    predicate_mentions: list[PredicateMention] = []
    unresolved: list[str] = []
    for spec in specs:
        predicate_span_id = f"span:{spec['name']}:predicate"
        spans.append(
            SpanObservation(
                observation_id=predicate_span_id,
                kind="predicate",
                label=spec["lemma"],
                text=spec["predicate_surface"],
                start=spec["predicate_start"],
                end=spec["predicate_end"],
                producer="spacy:fixture",
            )
        )
        subject_ids: list[str] = []
        object_ids: list[str] = []
        if spec["subject"] is not None:
            start, end, surface = spec["subject"]
            subject_id = f"span:{spec['name']}:subject"
            spans.append(
                SpanObservation(
                    observation_id=subject_id,
                    kind="subject",
                    label="noun",
                    text=surface,
                    start=start,
                    end=end,
                    producer="spacy:fixture",
                )
            )
            subject_ids.append(subject_id)
        if spec["object"] is not None:
            start, end, surface = spec["object"]
            object_id = f"span:{spec['name']}:object"
            spans.append(
                SpanObservation(
                    observation_id=object_id,
                    kind="object",
                    label="noun",
                    text=surface,
                    start=start,
                    end=end,
                    producer="spacy:fixture",
                )
            )
            object_ids.append(object_id)
        sentence = next(
            item
            for item in evidence
            if item.start <= spec["predicate_start"] < item.end
        )
        predicates.append(
            PredicateObservation(
                observation_id=f"predicate-observation:{spec['name']}",
                predicate_span_id=predicate_span_id,
                predicate_lemma=spec["lemma"],
                subject_span_ids=subject_ids,
                object_span_ids=object_ids,
                evidence_ref_id=sentence.evidence_ref_id,
                producer="spacy:fixture",
            )
        )
        normalized = (typed or {}).get(spec["name"])
        if normalized is None:
            unresolved.append(
                f"{spec['predicate_start']}:{spec['predicate_end']}:"
                f"{spec['predicate_surface']}"
            )
        else:
            predicate_mentions.append(
                PredicateMention(
                    predicate_id=f"predicate:{spec['name']}",
                    surface_text=spec["predicate_surface"],
                    lemma=spec["lemma"],
                    normalized_predicate=normalized,
                    start_char=spec["predicate_start"],
                    end_char=spec["predicate_end"],
                    negated=False,
                    modality="asserted",
                    confidence=1.0,
                )
            )
    bundle = ObservationBundle(
        bundle_id="observation-bundle:claim",
        source_version_id="source-version:claim",
        hierarchy_node_id="child:claim",
        text_hash="sha256:text",
        text_length=len(text),
        producer="spacy:fixture",
        producer_version="fixture.v1",
        recipe_hash="sha256:observation-recipe",
        spans=spans,
        predicates=predicates,
        qualifiers=[],
        evidence_refs=evidence,
    )
    extraction = LocalExtractionV1(
        schema_version="local_extraction.v1",
        document_id="doc:claim",
        child_id="child:claim",
        sentence_ids=[item.evidence_ref_id for item in evidence],
        entities=entities or [],
        predicates=predicate_mentions,
        relations=relations or [],
        unresolved_spans=unresolved,
    )
    return bundle, extraction


def _trained_compilation(text: str, child_id: str = "child:trained"):
    spacy = pytest.importorskip("spacy")
    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        pytest.skip("trained spaCy fixture model is not installed")
    bundle = build_spacy_observation_bundle(
        text=text,
        nlp=nlp,
        source_version_id="source-version:trained",
        hierarchy_node_id=child_id,
        parser_id="en_core_web_sm",
        parser_version="3.8.0",
    )
    extraction = compile_local_extraction_v1(
        bundle,
        document_id="doc:trained",
        child_id=child_id,
    ).extraction
    return compile_claim_records_v1(bundle=bundle, extraction=extraction)


def test_unresolved_predicate_flows_to_untyped_candidate_without_coercion() -> None:
    text = "Analysts discuss prices."
    bundle, extraction = _fixture(
        text,
        [
            _spec(
                text,
                name="discuss",
                lemma="discuss",
                predicate_surface="discuss",
                subject_surface="Analysts",
                object_surface="prices",
            )
        ],
    )
    compiled = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    claim = compiled.claims[0]
    assert claim.typing_status == "untyped"
    assert claim.predicate_surface == "discuss"
    assert claim.predicate_lemma == "discuss"
    assert claim.normalized_predicate is None
    assert claim.predicate_id is None
    assert "UNTYPED[discuss]" in claim.canonical_proposition
    assert claim.knowledge_status == "candidate"
    assert claim.validation_status == "candidate"


def test_unresolved_predicate_without_subject_still_survives_observation_only() -> None:
    text = "Discuss prices."
    bundle, extraction = _fixture(
        text,
        [
            _spec(
                text,
                name="discuss",
                lemma="discuss",
                predicate_surface="Discuss",
                subject_surface=None,
                object_surface="prices",
            )
        ],
    )
    compiled = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assert len(compiled.claims) == 1
    assert compiled.claims[0].typing_status == "untyped"
    assert compiled.skipped_predicate_observation_ids == []


def test_multiple_predicates_compile_to_multiple_atomic_claims() -> None:
    text = "Prices signal quality and influence choices."
    specs = [
        _spec(
            text,
            name="signal",
            lemma="signal",
            predicate_surface="signal",
            subject_surface="Prices",
            object_surface="quality",
        ),
        _spec(
            text,
            name="influence",
            lemma="influence",
            predicate_surface="influence",
            subject_surface="Prices",
            object_surface="choices",
        ),
    ]
    bundle, extraction = _fixture(
        text,
        specs,
        typed={"signal": "SIGNALS", "influence": "INFLUENCES"},
    )
    compiled = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assert len(compiled.claims) == 2
    assert {item.normalized_predicate for item in compiled.claims} == {
        "SIGNALS",
        "INFLUENCES",
    }
    assert compiled.links == []


def test_repeated_same_sentence_claim_surfaces_keep_observation_identity() -> None:
    text = "Analysts discuss prices and analysts discuss prices."
    specs = [
        _spec(
            text,
            name="discuss-one",
            lemma="discuss",
            predicate_surface="discuss",
            subject_surface="Analysts",
            object_surface="prices",
        ),
        _spec(
            text,
            name="discuss-two",
            lemma="discuss",
            predicate_surface="discuss",
            subject_surface="analysts",
            object_surface="prices",
            predicate_occurrence=1,
            subject_occurrence=0,
            object_occurrence=1,
        ),
    ]
    bundle, extraction = _fixture(text, specs)
    compiled = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assert len(compiled.claims) == 2
    assert len({item.claim_id for item in compiled.claims}) == 2
    assert {item.predicate_observation_id for item in compiled.claims} == {
        "predicate-observation:discuss-one",
        "predicate-observation:discuss-two",
    }
    assert compiled.receipt()["same_sentence_repeated_claim_count"] == 1


def test_relation_attaches_only_when_dependency_direction_agrees() -> None:
    text = "Discounts decrease prices."
    spec = _spec(
        text,
        name="decrease",
        lemma="decrease",
        predicate_surface="decrease",
        subject_surface="Discounts",
        object_surface="prices",
    )
    source = EntityMention(
        mention_id="mention:discounts",
        text="Discounts",
        entity_type="INTERVENTION",
        start_char=0,
        end_char=9,
        canonical_label="discount",
        confidence=0.9,
    )
    target = EntityMention(
        mention_id="mention:prices",
        text="prices",
        entity_type="QUALITY",
        start_char=19,
        end_char=25,
        canonical_label="price",
        confidence=0.9,
    )
    bundle, extraction_without_relation = _fixture(
        text,
        [spec],
        typed={"decrease": "DECREASES"},
        entities=[source, target],
    )
    evidence_id = extraction_without_relation.sentence_ids[0]
    relation = RelationCandidate(
        relation_id="relation:agrees",
        source_mention_id=source.mention_id,
        predicate_id="predicate:decrease",
        target_mention_id=target.mention_id,
        relation_type="DECREASES",
        condition_mention_ids=[],
        temporal_mention_ids=[],
        evidence_sentence_ids=[evidence_id],
        confidence=0.8,
    )
    extraction = LocalExtractionV1.model_validate(
        {
            **extraction_without_relation.model_dump(),
            "relations": [relation.model_dump()],
        }
    )
    compiled = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assert compiled.claims[0].source_relation_ids == ["relation:agrees"]
    assert compiled.rejected_relation_ids == []
    assert compiled.receipt()["glirel_agree_count"] == 1
    assert compiled.receipt()["glirel_conflict_count"] == 0

    reversed_relation = relation.model_copy(
        update={
            "relation_id": "relation:reversed",
            "source_mention_id": target.mention_id,
            "target_mention_id": source.mention_id,
        }
    )
    reversed_extraction = LocalExtractionV1.model_validate(
        {
            **extraction_without_relation.model_dump(),
            "relations": [reversed_relation.model_dump()],
        }
    )
    rejected = compile_claim_records_v1(bundle=bundle, extraction=reversed_extraction)
    assert rejected.claims[0].source_relation_ids == []
    assert rejected.rejected_relation_ids == ["relation:reversed"]
    assert rejected.receipt()["glirel_agree_count"] == 0
    assert rejected.receipt()["glirel_conflict_count"] == 1


def test_explicit_result_phrase_emits_claim_to_claim_link() -> None:
    text = "Pressure increases demand, resulting in shortages."
    specs = [
        _spec(
            text,
            name="increase",
            lemma="increase",
            predicate_surface="increases",
            subject_surface="Pressure",
            object_surface="demand",
        ),
        _spec(
            text,
            name="result",
            lemma="result",
            predicate_surface="resulting",
            subject_surface="demand",
            object_surface="shortages",
        ),
    ]
    bundle, extraction = _fixture(
        text,
        specs,
        typed={"increase": "INCREASES"},
    )
    compiled = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assert len(compiled.claims) == 2
    assert len(compiled.links) == 1
    assert compiled.links[0].relation_type == "RESULTS_IN"
    assert compiled.links[0].derivation_method == "dependency_rule"
    assert compiled.links[0].triggering_connective == "resulting in"
    assert compiled.links[0].rule_id == "claim_results_in.explicit_dependency.v1"
    assert compiled.receipt()["links_by_connective_family"] == {
        "explicit_result_phrase": 1,
        "discourse_result": 0,
    }


def test_adjacent_sentence_result_link_requires_endpoint_continuity() -> None:
    text = "Pressure increases demand. Consequently, demand decreases supply."
    second_start = text.index("Consequently")
    sentence_ranges = [(0, second_start - 1), (second_start, len(text))]
    specs = [
        _spec(
            text,
            name="increase",
            lemma="increase",
            predicate_surface="increases",
            subject_surface="Pressure",
            object_surface="demand",
            object_occurrence=0,
        ),
        _spec(
            text,
            name="decrease",
            lemma="decrease",
            predicate_surface="decreases",
            subject_surface="demand",
            object_surface="supply",
            subject_occurrence=1,
        ),
    ]
    bundle, extraction = _fixture(
        text,
        specs,
        sentence_ranges=sentence_ranges,
        typed={"increase": "INCREASES", "decrease": "DECREASES"},
    )
    compiled = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assert len(compiled.links) == 1
    assert compiled.links[0].derivation_method == "discourse_rule"
    assert len(compiled.links[0].evidence_sentence_ids) == 2
    assert compiled.links[0].triggering_connective == "Consequently"
    assert (
        compiled.links[0].rule_id == "claim_results_in.explicit_discourse_continuity.v1"
    )
    assert compiled.receipt()["cross_sentence_candidate_count"] == 1
    assert compiled.receipt()["cross_sentence_accepted_count"] == 1
    assert compiled.receipt()["cross_sentence_rejected_count"] == 0

    changed = deepcopy(bundle.model_dump())
    second_subject = next(
        item
        for item in changed["spans"]
        if item["observation_id"] == "span:decrease:subject"
    )
    second_subject["text"] = "inventory"
    no_continuity_bundle = ObservationBundle.model_validate(changed)
    no_link = compile_claim_records_v1(
        bundle=no_continuity_bundle, extraction=extraction
    )
    assert no_link.links == []
    assert no_link.receipt()["cross_sentence_candidate_count"] == 1
    assert no_link.receipt()["cross_sentence_accepted_count"] == 0
    assert no_link.receipt()["cross_sentence_rejected_count"] == 1


def test_predicate_accounting_and_scope_fail_closed() -> None:
    text = "Analysts discuss prices."
    bundle, extraction = _fixture(
        text,
        [
            _spec(
                text,
                name="discuss",
                lemma="discuss",
                predicate_surface="discuss",
                subject_surface="Analysts",
                object_surface="prices",
            )
        ],
    )
    missing = LocalExtractionV1.model_validate(
        {**extraction.model_dump(), "unresolved_spans": []}
    )
    with pytest.raises(ValueError, match="account for every"):
        compile_claim_records_v1(bundle=bundle, extraction=missing)

    wrong_child = LocalExtractionV1.model_validate(
        {**extraction.model_dump(), "child_id": "child:other"}
    )
    with pytest.raises(ValueError, match="share child scope"):
        compile_claim_records_v1(bundle=bundle, extraction=wrong_child)

    normalized_text = "Prices increase."
    normalized_bundle, normalized_extraction = _fixture(
        normalized_text,
        [
            _spec(
                normalized_text,
                name="increase",
                lemma="increase",
                predicate_surface="increase",
                subject_surface="Prices",
                object_surface=None,
            )
        ],
        typed={"increase": "DECREASES"},
    )
    with pytest.raises(ValueError, match="normalization registry"):
        compile_claim_records_v1(
            bundle=normalized_bundle,
            extraction=normalized_extraction,
        )


def test_claim_contract_is_strict_candidate_only_and_replay_stable() -> None:
    text = "Analysts discuss prices."
    bundle, extraction = _fixture(
        text,
        [
            _spec(
                text,
                name="discuss",
                lemma="discuss",
                predicate_surface="discuss",
                subject_surface="Analysts",
                object_surface="prices",
            )
        ],
    )
    first = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    second = compile_claim_records_v1(bundle=bundle, extraction=extraction)
    assert first == second
    assert ClaimCompilationV1.model_validate_json(first.model_dump_json()) == first

    claim = first.claims[0]
    with pytest.raises(ValidationError):
        ClaimRecordV1.model_validate({**claim.model_dump(), "extra": "forbidden"})
    with pytest.raises(ValidationError):
        ClaimRecordV1.model_validate(
            {**claim.model_dump(), "validation_status": "accepted"}
        )


def test_executor_contract_authority_is_explicit_and_versioned() -> None:
    for model in (
        ClaimArgumentV1,
        ClaimRecordV1,
        ClaimLinkV1,
        ClaimCompilationV1,
    ):
        schema = model.model_json_schema()
        assert schema["authority"] == CLAIM_RECORD_AUTHORITY
        assert (
            schema["owner_ratification_required"]
            is CLAIM_RECORD_OWNER_RATIFICATION_REQUIRED
        )
        assert schema["change_policy"] == CLAIM_RECORD_CHANGE_POLICY


def test_claim_record_projects_losslessly_to_claim_assertion_golden() -> None:
    text = "Analysts discuss prices."
    bundle, extraction = _fixture(
        text,
        [
            _spec(
                text,
                name="discuss",
                lemma="discuss",
                predicate_surface="discuss",
                subject_surface="Analysts",
                object_surface="prices",
            )
        ],
    )
    claim = compile_claim_records_v1(bundle=bundle, extraction=extraction).claims[0]
    assertion = project_claim_record_to_assertion(claim)
    assert assertion.schema_version == "polymath.claim_assertion.v1"
    assert assertion.predicate_id is None
    assert assertion.source_compilation.typing_status == "untyped"
    assert assertion.knowledge_status == "candidate"
    assert restore_claim_record_from_assertion(assertion) == claim
    assert namespace_hash("body", assertion.model_dump()) == (
        "sha256:320f76c2c30cbcbff32a741163ba631ac3f8fc527f351c0549bb29ae006793ec"
    )


def test_trained_spacy_preserves_typed_and_untyped_claim_records() -> None:
    compiled = _trained_compilation(
        "Discounts decrease reference prices. Analysts discuss implications."
    )
    by_lemma = {item.predicate_lemma: item for item in compiled.claims}
    assert by_lemma["decrease"].normalized_predicate == "DECREASES"
    assert by_lemma["decrease"].typing_status == "typed"
    assert by_lemma["discuss"].normalized_predicate is None
    assert by_lemma["discuss"].typing_status == "untyped"
    assert compiled.receipt()["typed_claim_count"] == 1
    assert compiled.receipt()["untyped_claim_count"] == 1


def test_trained_spacy_explicit_result_phrase_emits_candidate_link() -> None:
    compiled = _trained_compilation(
        "Pressure increases demand, resulting in shortages.",
        "child:trained-result",
    )
    assert len(compiled.claims) == 2
    assert len(compiled.links) == 1
    assert compiled.links[0].triggering_connective == "resulting in"
    assert compiled.links[0].derivation_method == "dependency_rule"


def test_trained_spacy_discourse_link_requires_explicit_connective() -> None:
    compiled = _trained_compilation(
        "Pressure increases demand. Consequently, demand decreases supply.",
        "child:trained-discourse",
    )
    assert len(compiled.links) == 1
    assert compiled.links[0].triggering_connective == "Consequently"
    assert compiled.receipt()["cross_sentence_candidate_count"] == 1
    assert compiled.receipt()["cross_sentence_accepted_count"] == 1
