from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.semantic_artifacts import (
    ClaimAssertionCandidate,
    domain_hash,
    make_evidence_ref,
)
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_claim_candidates,
    validate_evidence_round_trip,
)


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "semantic_extraction_gold_v1.json"


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
    assert domain_hash("a", {"b": 2, "a": 1}) == domain_hash(
        "a", {"a": 1, "b": 2}
    )
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


def test_negation_is_attached_to_subordinate_predicate_not_main_claim() -> None:
    text = (
        "When buyers cannot directly inspect quality, price may signal "
        "product quality and influence purchase choice."
    )
    candidates = {item.predicate_lemma: item for item in compile_claim_candidates(_bundle(text))}
    assert candidates["inspect"].polarity == "negated"
    assert candidates["inspect"].modal_force == "possible"
    assert candidates["signal"].polarity == "affirmed"
    assert candidates["signal"].modal_force == "possible"
    assert any("When buyers cannot directly inspect quality" in value for value in candidates["signal"].conditions)


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
    temporal_cues = {item.cue for item in temporal_bundle.qualifiers if item.kind == "temporal"}
    assert {"2025-01-01", "March 2026"} <= temporal_cues


def test_fixture_round_trips_and_extractors_cannot_self_promote() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == "polymath.semantic_extraction_gold.v1"
    for sample in fixture["samples"]:
        bundle = _bundle(sample["text"], f"child:{sample['id']}")
        assert not validate_evidence_round_trip(bundle, sample["text"])
        assert all(item.knowledge_status == "candidate" for item in compile_claim_candidates(bundle))

    candidate = compile_claim_candidates(_bundle("Analysts should compare prices."))[0]
    with pytest.raises(ValidationError, match="cannot mark a claim accepted"):
        ClaimAssertionCandidate.model_validate(
            {**candidate.model_dump(), "validation_status": "accepted"}
        )
