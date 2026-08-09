from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.graphify_contracts import (
    GRAPHIFY_CONTRACT_RELEASE,
    FROZEN_ENTITY_TYPES,
    MentionTerminalState,
    RawMentionV1,
    contract_schema_hash,
)
from services.extraction.canonical import CANONICAL_ENTITY_TYPES
from services.extraction.graphify_normalization import (
    NORMALIZATION_RELEASE,
    assert_round_trip,
    normalize_document,
    to_original_span,
)
from services.extraction.graphify_survey import SURVEY_RELEASE, survey_document


def test_normalization_reverses_crlf_and_compatibility_characters() -> None:
    original = "Title\r\nThe ﬃ tool costs ① unit.\rNext"
    document = normalize_document("doc-1", original)
    assert document.normalized_text == "Title\nThe ffi tool costs 1 unit.\nNext"
    for needle in ("Title", "ffi", "1 unit", "Next"):
        start = document.normalized_text.index(needle)
        mapped = assert_round_trip(document, start, start + len(needle))
        assert normalize_document("slice", original[mapped.start:mapped.end]).normalized_text == needle
    assert document.normalized_to_original[-1] == len(original)


def test_partial_compatibility_expansion_is_explicitly_not_exact() -> None:
    document = normalize_document("doc-1", "ﬃ")
    assert to_original_span(document, 1, 2).exact is False


def test_normalization_is_deterministic() -> None:
    first = normalize_document("doc-1", "A\r\nB")
    second = normalize_document("doc-1", "A\r\nB")
    assert first == second
    assert first.normalized_sha256 == hashlib.sha256(b"A\nB").hexdigest()


def test_raw_mention_rejects_types_outside_frozen_inventory() -> None:
    base = dict(
        mention_id="mention:1", document_id="doc", window_id="window:1",
        sequence=0, surface="Polymath", confidence=0.9, local_start=0,
        local_end=8, normalized_start=0, normalized_end=8,
        original_start=0, original_end=8,
        terminal_state=MentionTerminalState.ALIGNED, provider_release="provider-v1",
    )
    mention = RawMentionV1(entity_type="Software", **base)
    assert mention.entity_type == "software"
    with pytest.raises(ValidationError):
        RawMentionV1(entity_type="NewUnratifiedType", **base)


def test_contract_and_release_hashes_are_stable() -> None:
    assert GRAPHIFY_CONTRACT_RELEASE == "graphify-contracts-v1"
    assert NORMALIZATION_RELEASE == "graphify-normalization-v1"
    assert SURVEY_RELEASE == "graphify-survey-v1"
    assert len(contract_schema_hash()) == 64
    assert len(CANONICAL_ENTITY_TYPES) == 15
    assert FROZEN_ENTITY_TYPES == CANONICAL_ENTITY_TYPES


def test_zero_model_survey_extracts_structural_signals_without_single_signal_suppression() -> None:
    text = """# Contents

## Systems

Retrieval-Augmented Generation (RAG) is a Method.
Polymath is a knowledge system.

Repeated footer.

Repeated footer.

# References
"""
    survey = survey_document(normalize_document("doc", text))
    assert [heading.text for heading in survey.headings] == ["Contents", "Systems", "References"]
    assert any(alias.alias == "RAG" for alias in survey.aliases)
    assert any(definition.term == "Polymath" for definition in survey.definitions)
    assert survey.shape_counts["capitalized_phrases"] > 0
    assert survey.token_frequency["repeated"] == 2
    assert any(item.surface == "RAG" and item.strength == "weak" for item in survey.gazetteer_candidates)
    assert all(
        not block.furniture_candidate or len(set(block.furniture_signals)) >= 2
        for block in survey.blocks
    )


def test_survey_is_repeatable_on_committed_quality_fixture() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / "GRAPHIFY_GLINER2_CPU_AGENT/fixtures/graphify_quality_fixture.md").read_text()
    document = normalize_document("quality", text)
    first = survey_document(document)
    second = survey_document(document)
    assert first == second
    assert first.survey_hash == second.survey_hash
    assert first.headings
    assert first.gazetteer_candidates
