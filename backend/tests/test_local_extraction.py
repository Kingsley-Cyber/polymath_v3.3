from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from models.local_extraction import (
    EntityMention,
    EntityType,
    LocalExtractionV1,
    Modality,
    Polarity,
    PredicateMention,
    PredicateType,
    RelationCandidate,
)
from models.semantic_artifacts import (
    ObservationBundle,
    PredicateObservation,
    QualifierObservation,
    SpanObservation,
    make_evidence_ref,
)
from services.ingestion import semantic_observations
from services.ingestion.semantic_observations import compile_local_extraction_v1


VOCAB_PATH = (
    Path(__file__).resolve().parents[1]
    / "registries"
    / "extraction_vocabularies.v1.json"
)


def _entity(**updates):
    values = {
        "mention_id": "mention:source",
        "text": "discounting",
        "entity_type": "PROCESS",
        "start_char": 0,
        "end_char": 11,
        "canonical_label": "discounting",
        "confidence": 0.95,
    }
    values.update(updates)
    return EntityMention.model_validate(values)


def _predicate(**updates):
    values = {
        "predicate_id": "predicate:lower",
        "surface_text": "lowers",
        "lemma": "lower",
        "normalized_predicate": "DECREASES",
        "start_char": 12,
        "end_char": 18,
        "negated": False,
        "modality": "asserted",
        "confidence": 1.0,
    }
    values.update(updates)
    return PredicateMention.model_validate(values)


def _relation(**updates):
    values = {
        "relation_id": "relation:lower",
        "source_mention_id": "mention:source",
        "predicate_id": "predicate:lower",
        "target_mention_id": "mention:target",
        "relation_type": "DECREASES",
        "condition_mention_ids": [],
        "temporal_mention_ids": [],
        "evidence_sentence_ids": ["evidence:sentence"],
        "confidence": 0.8,
    }
    values.update(updates)
    return RelationCandidate.model_validate(values)


def _extraction(**updates):
    values = {
        "schema_version": "local_extraction.v1",
        "document_id": "doc:test",
        "child_id": "child:test",
        "sentence_ids": ["evidence:sentence"],
        "entities": [
            _entity(),
            _entity(
                mention_id="mention:target",
                text="reference price",
                entity_type="BASELINE",
                start_char=19,
                end_char=34,
                canonical_label="reference price",
            ),
        ],
        "predicates": [_predicate()],
        "relations": [_relation()],
        "unresolved_spans": [],
    }
    values.update(updates)
    return LocalExtractionV1.model_validate(values)


def _observation_bundle(
    *,
    text: str,
    lemma: str,
    predicate_start: int,
    predicate_end: int,
    qualifiers: list[tuple[str, str, int, int]] | None = None,
) -> ObservationBundle:
    evidence = make_evidence_ref(
        text=text,
        start=0,
        end=len(text),
        source_version_id="source-version:local",
        hierarchy_node_id="child:local",
    )
    predicate_span = SpanObservation(
        observation_id="observation:predicate-span",
        kind="predicate",
        label=lemma,
        text=text[predicate_start:predicate_end],
        start=predicate_start,
        end=predicate_end,
        producer="spacy:fixture",
    )
    predicate = PredicateObservation(
        observation_id="observation:predicate",
        predicate_span_id=predicate_span.observation_id,
        predicate_lemma=lemma,
        subject_span_ids=[],
        object_span_ids=[],
        evidence_ref_id=evidence.evidence_ref_id,
        producer="spacy:fixture",
    )
    qualifier_rows = [
        QualifierObservation(
            observation_id=f"observation:qualifier:{index}",
            target_observation_id=predicate.observation_id,
            kind=kind,
            cue=text[start:end],
            normalized_value=normalized,
            start=start,
            end=end,
            producer="spacy:fixture",
        )
        for index, (kind, normalized, start, end) in enumerate(qualifiers or [])
    ]
    return ObservationBundle(
        bundle_id="observation-bundle:local",
        source_version_id="source-version:local",
        hierarchy_node_id="child:local",
        text_hash="sha256:text",
        text_length=len(text),
        producer="spacy:fixture",
        producer_version="fixture.v1",
        recipe_hash="sha256:recipe",
        spans=[predicate_span],
        predicates=[predicate],
        qualifiers=qualifier_rows,
        evidence_refs=[evidence],
    )


def test_vocab_type_literals_are_exactly_the_owner_registry() -> None:
    vocab = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    assert list(get_args(EntityType)) == vocab["entity_types"]
    assert list(get_args(PredicateType)) == vocab["predicate_types"]
    assert list(get_args(Modality)) == vocab["modalities"]
    assert list(get_args(Polarity)) == vocab["polarities"]


def test_owner_field_contract_is_closed_fully_required_and_strict() -> None:
    expected = {
        EntityMention: {
            "mention_id",
            "text",
            "entity_type",
            "start_char",
            "end_char",
            "canonical_label",
            "confidence",
        },
        PredicateMention: {
            "predicate_id",
            "surface_text",
            "lemma",
            "normalized_predicate",
            "start_char",
            "end_char",
            "negated",
            "modality",
            "confidence",
        },
        RelationCandidate: {
            "relation_id",
            "source_mention_id",
            "predicate_id",
            "target_mention_id",
            "relation_type",
            "condition_mention_ids",
            "temporal_mention_ids",
            "evidence_sentence_ids",
            "confidence",
        },
        LocalExtractionV1: {
            "schema_version",
            "document_id",
            "child_id",
            "sentence_ids",
            "entities",
            "predicates",
            "relations",
            "unresolved_spans",
        },
    }
    for model, fields in expected.items():
        schema = model.model_json_schema()
        assert set(model.model_fields) == fields
        assert set(schema["required"]) == fields
        assert schema["additionalProperties"] is False

    with pytest.raises(ValidationError):
        _extraction(extra_field="forbidden")
    with pytest.raises(ValidationError):
        _predicate(start_char="12")
    with pytest.raises(ValidationError):
        LocalExtractionV1.model_validate_json(
            json.dumps(
                {**_extraction().model_dump(), "sentence_ids": "evidence:sentence"}
            )
        )


@pytest.mark.parametrize(
    ("factory", "updates"),
    [
        (_entity, {"start_char": -1}),
        (_entity, {"end_char": 0}),
        (_entity, {"confidence": 1.01}),
        (_predicate, {"end_char": 12}),
        (_predicate, {"confidence": -0.01}),
        (_relation, {"confidence": 1.01}),
    ],
)
def test_span_and_confidence_validation_fails_closed(factory, updates) -> None:
    with pytest.raises(ValidationError):
        factory(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"entities": [_entity(), _entity()]},
        {"predicates": [_predicate(), _predicate()]},
        {"relations": [_relation(), _relation()]},
        {"sentence_ids": ["evidence:sentence", "evidence:sentence"]},
        {"relations": [_relation(source_mention_id="mention:missing")]},
        {"relations": [_relation(target_mention_id="mention:missing")]},
        {"relations": [_relation(predicate_id="predicate:missing")]},
        {"relations": [_relation(condition_mention_ids=["mention:missing"])]},
        {"relations": [_relation(temporal_mention_ids=["mention:missing"])]},
        {"relations": [_relation(evidence_sentence_ids=["sentence:missing"])]},
        {"relations": [_relation(relation_type="INCREASES")]},
    ],
)
def test_local_extraction_reference_closure_fails_closed(updates) -> None:
    with pytest.raises(ValidationError):
        _extraction(**updates)


def test_local_extraction_round_trips_without_coercion() -> None:
    extraction = _extraction()
    assert (
        LocalExtractionV1.model_validate_json(extraction.model_dump_json())
        == extraction
    )


def test_observation_compiler_emits_controlled_predicate_and_text_free_receipt() -> (
    None
):
    bundle = _observation_bundle(
        text="Discounting does not increase reference price.",
        lemma="increase",
        predicate_start=21,
        predicate_end=29,
        qualifiers=[("negation", "negated", 17, 20)],
    )
    result = compile_local_extraction_v1(
        bundle,
        document_id="doc:local",
        child_id="child:local",
    )
    predicate = result.extraction.predicates[0]
    assert predicate.surface_text == "increase"
    assert predicate.normalized_predicate == "INCREASES"
    assert predicate.negated is True
    assert predicate.modality == "asserted"
    assert result.extraction.entities == []
    assert result.extraction.relations == []
    assert result.extraction.sentence_ids == [bundle.evidence_refs[0].evidence_ref_id]
    assert result.matched_counts == (("INCREASES", 1),)
    assert result.unresolved_rate == 0.0
    assert "unresolved_spans" not in result.receipt()


def test_observation_compiler_counts_unknown_lemma_without_default_edge() -> None:
    result = compile_local_extraction_v1(
        _observation_bundle(
            text="Analysts discuss prices.",
            lemma="discuss",
            predicate_start=9,
            predicate_end=16,
        ),
        document_id="doc:local",
        child_id="child:local",
    )
    assert result.extraction.predicates == []
    assert result.extraction.unresolved_spans == ["9:16:discuss"]
    assert result.observed_predicate_count == 1
    assert result.matched_predicate_count == 0
    assert result.unresolved_predicate_count == 1
    assert result.unresolved_rate == 1.0


def test_normalization_registry_version_changes_predicate_identity(monkeypatch) -> None:
    bundle = _observation_bundle(
        text="Prices increase.",
        lemma="increase",
        predicate_start=7,
        predicate_end=15,
    )
    first = compile_local_extraction_v1(
        bundle,
        document_id="doc:local",
        child_id="child:local",
    )
    monkeypatch.setattr(
        semantic_observations,
        "load_normalization_identity",
        lambda: {
            "registry": "predicate_normalization",
            "version": "v2",
            "hash": "sha256:v2",
        },
    )
    monkeypatch.setattr(
        semantic_observations,
        "normalize_predicate_lemma",
        lambda lemma: {
            "lemma": lemma,
            "predicate_type": "INCREASES",
            "registry": "predicate_normalization",
            "registry_version": "v2",
            "authority": "owner-ratified",
        },
    )
    second = compile_local_extraction_v1(
        bundle,
        document_id="doc:local",
        child_id="child:local",
    )
    assert first.extraction.predicates[0].predicate_id != (
        second.extraction.predicates[0].predicate_id
    )
    assert second.normalization_registry_version == "v2"


def test_observation_compiler_requires_exact_child_scope() -> None:
    bundle = _observation_bundle(
        text="Prices increase.",
        lemma="increase",
        predicate_start=7,
        predicate_end=15,
    )
    with pytest.raises(ValueError, match="child_id"):
        compile_local_extraction_v1(
            bundle,
            document_id="doc:local",
            child_id="child:other",
        )
