from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.semantic_artifacts import (
    ClaimAssertionCandidate,
    QualifierObservation,
    SpanObservation,
    domain_hash,
    make_evidence_ref,
)
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_claim_candidates,
    compile_local_extraction_v1,
    validate_evidence_round_trip,
)


FIXTURE = (
    Path(__file__).resolve().parents[1] / "evals" / "semantic_extraction_gold_v1.json"
)


def _nlp():
    spacy = pytest.importorskip("spacy")
    try:
        return spacy.load("en_core_web_sm")
    except OSError:
        pytest.skip("trained spaCy fixture model is not installed")


def _bundle(text: str, node: str = "child:test"):
    return build_spacy_observation_bundle(
        text=text,
        nlp=_nlp(),
        source_version_id="source-version:test",
        hierarchy_node_id=node,
        parser_id="en_core_web_sm",
        parser_version="3.8.0",
    )


def test_domain_hash_is_canonical_and_namespaced() -> None:
    assert domain_hash("a", {"b": 2, "a": 1}) == domain_hash("a", {"a": 1, "b": 2})
    assert domain_hash("a", {"a": 1}) != domain_hash("b", {"a": 1})


def test_evidence_ref_requires_exact_bounds_and_quote_hash() -> None:
    text = "A source-backed claim."
    item = make_evidence_ref(
        text=text,
        start=2,
        end=15,
        source_version_id="source-version:test",
        hierarchy_node_id="child:test",
    )
    assert text[item.start : item.end] == item.quote
    with pytest.raises(ValidationError, match="quote_hash"):
        item.model_copy(update={"quote_hash": "sha256:bad"}).model_validate(
            {**item.model_dump(), "quote_hash": "sha256:bad"}
        )


def test_coordinate_bearing_text_preserves_boundary_whitespace() -> None:
    text = "  Exact source claim.  "
    evidence = make_evidence_ref(
        text=text,
        start=0,
        end=len(text),
        source_version_id="source-version:whitespace",
        hierarchy_node_id="child:whitespace",
    )
    assert evidence.quote == text
    assert text[evidence.start : evidence.end] == evidence.quote

    span = SpanObservation(
        observation_id="observation:whitespace",
        kind="subject",
        label="subject",
        text=" subject ",
        start=0,
        end=9,
        producer="spacy:fixture",
    )
    qualifier = QualifierObservation(
        observation_id="observation:qualifier:whitespace",
        target_observation_id="observation:predicate",
        kind="condition",
        cue=" if needed ",
        normalized_value="conditional",
        start=0,
        end=11,
        producer="spacy:fixture",
    )
    assert span.text == " subject "
    assert qualifier.cue == " if needed "


def test_negation_is_attached_to_subordinate_predicate_not_main_claim() -> None:
    text = (
        "When buyers cannot directly inspect quality, price may signal "
        "product quality and influence purchase choice."
    )
    candidates = {
        item.predicate_lemma: item for item in compile_claim_candidates(_bundle(text))
    }
    assert candidates["inspect"].polarity == "negated"
    assert candidates["inspect"].modal_force == "possible"
    assert candidates["signal"].polarity == "affirmed"
    assert candidates["signal"].modal_force == "possible"
    assert any(
        "When buyers cannot directly inspect quality" in value
        for value in candidates["signal"].conditions
    )


def test_attribution_exception_and_temporal_cues_survive_compilation() -> None:
    attributed = compile_claim_candidates(
        _bundle(
            "The authors hypothesize that repeated criticism may lower a "
            "person's evaluation of competence.",
            "child:attributed",
        )
    )
    lower = next(item for item in attributed if item.predicate_lemma == "lower")
    assert lower.assertion_mode == "attributed"
    assert lower.modal_force == "possible"

    exception = compile_claim_candidates(
        _bundle(
            "Discounting usually lowers reference price, except when the "
            "discount is clearly temporary.",
            "child:exception",
        )
    )
    lower = next(item for item in exception if item.predicate_lemma == "lower")
    assert any("except" in value.lower() for value in lower.exceptions)

    temporal_bundle = _bundle(
        "The policy became effective on 2025-01-01 and was revised in March 2026.",
        "child:temporal",
    )
    temporal_cues = {
        item.cue for item in temporal_bundle.qualifiers if item.kind == "temporal"
    }
    assert {"2025-01-01", "March 2026"} <= temporal_cues


def test_fixture_round_trips_and_extractors_cannot_self_promote() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == "polymath.semantic_extraction_gold.v1"
    for sample in fixture["samples"]:
        bundle = _bundle(sample["text"], f"child:{sample['id']}")
        assert not validate_evidence_round_trip(bundle, sample["text"])
        assert all(
            item.knowledge_status == "candidate"
            for item in compile_claim_candidates(bundle)
        )

    candidate = compile_claim_candidates(_bundle("Analysts should compare prices."))[0]
    with pytest.raises(ValidationError, match="cannot mark a claim accepted"):
        ClaimAssertionCandidate.model_validate(
            {**candidate.model_dump(), "validation_status": "accepted"}
        )


def test_spacy_observations_compile_into_local_extraction_v1() -> None:
    result = compile_local_extraction_v1(
        _bundle("Discounts decrease reference prices.", "child:local"),
        document_id="doc:local",
        child_id="child:local",
    )
    extraction = result.extraction
    decrease = next(item for item in extraction.predicates if item.lemma == "decrease")
    assert extraction.schema_version == "local_extraction.v1"
    assert extraction.entities == []
    assert extraction.relations == []
    assert decrease.normalized_predicate == "DECREASES"
    assert decrease.negated is False
    assert decrease.modality == "asserted"
    assert extraction.sentence_ids
    assert result.receipt()["normalization_registry_version"] == "v1"


def test_unknown_predicate_is_counted_as_unresolved_not_generic_relation() -> None:
    result = compile_local_extraction_v1(
        _bundle("Analysts discuss prices.", "child:unknown"),
        document_id="doc:unknown",
        child_id="child:unknown",
    )
    assert result.extraction.predicates == []
    assert result.extraction.unresolved_spans == ["9:16:discuss"]
    assert result.observed_predicate_count == 1
    assert result.matched_predicate_count == 0
    assert result.unresolved_predicate_count == 1
    assert result.unresolved_rate == 1.0


def test_negation_does_not_flip_controlled_predicate_direction() -> None:
    result = compile_local_extraction_v1(
        _bundle("Discounting does not increase reference price.", "child:negated"),
        document_id="doc:negated",
        child_id="child:negated",
    )
    increase = next(
        item for item in result.extraction.predicates if item.lemma == "increase"
    )
    assert increase.normalized_predicate == "INCREASES"
    assert increase.negated is True


def test_local_extraction_child_scope_must_match_observation_bundle() -> None:
    with pytest.raises(ValueError, match="child_id"):
        compile_local_extraction_v1(
            _bundle("Discounting lowers price.", "child:one"),
            document_id="doc:one",
            child_id="child:two",
        )
