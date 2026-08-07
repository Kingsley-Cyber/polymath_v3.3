"""Contract tests for models/evidence_item.py (Slice 1 scaffolding).

EvidenceItem is a typed VIEW over hydrated SourceChunk: identity spine,
span-exact text retention, and the shadow graph-read rule (release metadata
fails closed on canonical matching).
"""

from dataclasses import dataclass, field

import pytest

from models.evidence_item import (
    EVIDENCE_ITEM_SCHEMA_VERSION,
    EvidenceItem,
    EvidenceReleaseMetadata,
)


@dataclass
class FakeChunk:
    """Duck-typed hydrated SourceChunk stand-in."""

    corpus_id: str = "c1"
    doc_id: str = "doc-1"
    parent_id: str = "doc-1_parent_0001"
    chunk_id: str = "doc-1_0003"
    text: str = "FACS scores facial movement with action units."
    score: float = 0.87
    source_tier: str = "child"
    provenance: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def test_from_source_chunk_copies_identity_spine():
    item = EvidenceItem.from_source_chunk(FakeChunk(), assertion_lane="vector")
    assert item.schema_version == EVIDENCE_ITEM_SCHEMA_VERSION
    assert item.corpus_id == "c1"
    assert item.doc_id == "doc-1"
    assert item.parent_id == "doc-1_parent_0001"
    assert item.chunk_id == "doc-1_0003"
    assert item.text == "FACS scores facial movement with action units."
    assert item.assertion_lane == "vector"


def test_unknown_lane_falls_back_to_unknown():
    item = EvidenceItem.from_source_chunk(FakeChunk(), assertion_lane="bogus")
    assert item.assertion_lane == "unknown"


def test_metadata_keys_recorded_without_leaking_values():
    chunk = FakeChunk(metadata={"heading_path": ["A"], "facets": {"x": 1}})
    item = EvidenceItem.from_source_chunk(chunk)
    assert item.metadata_keys == ("facets", "heading_path")


def test_evidence_key_is_stable_identity():
    item = EvidenceItem.from_source_chunk(FakeChunk())
    assert item.evidence_key() == "c1:doc-1:doc-1_0003"


def test_missing_identity_fields_fail_validation():
    with pytest.raises(ValueError):
        EvidenceItem(corpus_id="", doc_id="d", chunk_id="c")


# ---------------------------------------------------------------------------
# Shadow graph-read rule: release matching fails closed
# ---------------------------------------------------------------------------

_PINS = {
    "extractor_release": "extraction-core-v1.0.0",
    "ontology_release": "ontology.v1",
    "acceptance_policy_release": "acceptance.v1",
}


def test_canonical_match_requires_matching_pins_and_canonical_label():
    stamped = EvidenceReleaseMetadata(
        extractor_release="extraction-core-v1.0.0",
        ontology_release="ontology.v1",
        acceptance_policy_release="acceptance.v1",
        promotion_version="promote.v9",
        canonical_or_shadow="canonical",
    )
    assert stamped.matches(_PINS) is True


def test_shadow_record_never_matches_canonical_pins():
    shadow = EvidenceReleaseMetadata(
        extractor_release="extraction-core-v1.0.0",
        ontology_release="ontology.v1",
        acceptance_policy_release="acceptance.v1",
        canonical_or_shadow="shadow",
    )
    assert shadow.matches(_PINS) is False


def test_unstamped_record_fails_closed():
    unstamped = EvidenceReleaseMetadata(canonical_or_shadow="canonical")
    assert unstamped.matches(_PINS) is False


def test_release_mismatch_excluded_from_canonical_synthesis():
    legacy = EvidenceReleaseMetadata(
        extractor_release="extraction-core-v0.9.0",
        ontology_release="ontology.v1",
        acceptance_policy_release="acceptance.v1",
        canonical_or_shadow="canonical",
    )
    assert legacy.matches(_PINS) is False
