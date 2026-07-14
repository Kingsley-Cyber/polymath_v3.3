from __future__ import annotations

import copy
import datetime as dt

import pytest

from models.identifier_recipes import logical_doc_id, source_version_id
from models.legacy_adapters import (
    LegacyAdapterError,
    adapt_document,
    adapt_ghost_b_extraction,
    adapt_lexicon_entry,
    adapt_parent_summary,
    assert_no_promoted_status,
)


RAW_SHA = "a" * 64


def _document_row(*, source_kind: str = "content_hash", source_key: str | None = None):
    key = source_key or f"sha256:{RAW_SHA}"
    return {
        "doc_id": "legacy-doc-1",
        "corpus_id": "corpus-1",
        "source_identity": {
            "source_kind": source_kind,
            "source_key": key,
            "content_sha256": RAW_SHA,
        },
        "title": "A Legacy Document",
        "author": "Maria Okafor",
        "filename": "legacy.pdf",
        "created_at": dt.datetime(2019, 2, 3, 4, 5, 6),
        "ignored_legacy_field": {"anything": True},
    }


def _ghost_row():
    text = "During winter 1911 the lamp dimmed."
    start = text.index("winter 1911")
    return {
        "chunk_id": "child-1",
        "doc_id": "legacy-doc-1",
        "corpus_id": "corpus-1",
        "schema_version": "polymad.extract.v2",
        "extractor": "runpod_flash",
        "status": "accepted",
        "text": text,
        "entities": [
            {
                "canonical_name": "lamp",
                "surface_form": "lamp",
                "entity_type": "object",
                "confidence": 0.91,
                "query_aliases": ["lantern"],
            }
        ],
        "relations": [
            {
                "subject": "lamp",
                "predicate": "has_state",
                "object": "dimmed",
                "confidence": 0.83,
                "validation_status": "accepted",
            }
        ],
        "facts": [
            {
                "subject": "lamp",
                "property_name": "state",
                "value": "dimmed",
                "fact_type": "status",
            }
        ],
        "temporal_captures": [
            {
                "text": "winter 1911",
                "char_start": start,
                "char_end": start + len("winter 1911"),
                "detector": "python_date_rule.v1",
                "role_candidates": ["event_time"],
            }
        ],
    }


def _parent_row():
    return {
        "parent_id": "parent-1",
        "doc_id": "legacy-doc-1",
        "corpus_id": "corpus-1",
        "summary_id": "summary-1",
        "summary": "The lamp dimmed during winter maintenance.",
        "summary_model": "deepseek-v4-flash",
        "summary_created_at": dt.datetime(
            2026, 7, 14, 2, 3, 4, 500, tzinfo=dt.timezone.utc
        ),
        "schema_version": "parent_summary.v1",
        "summary_type": "claim",
        "validation_status": "valid",
        "quality_score": 0.88,
        "quality_flags": ["legacy-scored"],
        "source_child_ids": ["child-1", "child-2"],
        "source_hash": "sha256:source",
        "latent_concepts": [
            {
                "concept": "seasonal maintenance",
                "evidence_basis": "inferred",
                "aliases": ["winter upkeep"],
            }
        ],
        "temporal_class": "event",
    }


def _lexicon_row():
    return {
        "lexicon_id": "lex-1",
        "corpus_id": "corpus-1",
        "canonical_key": "seasonal maintenance",
        "canonical_name": "Seasonal Maintenance",
        "utility_gloss": "Maintenance scheduled by season.",
        "retrieval_gloss": "winter upkeep maintenance",
        "aliases": ["winter upkeep"],
        "abbreviations": ["SM"],
        "entity_ids": ["entity:seasonal-maintenance"],
        "entity_types": ["process"],
        "source_document_ids": ["legacy-doc-1"],
        "source_chunk_ids": ["child-1"],
        "source_parent_ids": ["parent-1"],
        "lexicon_state": "accepted",
        "mean_confidence": 0.92,
        "schema_version": "corpus_lexicon.v3",
    }


def test_document_without_strong_key_keeps_legacy_identity_and_flags_lineage():
    row = _document_row()
    before = copy.deepcopy(row)

    adapted = adapt_document(row)

    assert row == before
    assert adapted.doc_id == row["doc_id"] == adapted.legacy_doc_id
    assert adapted.logical_doc_id_minted is False
    assert adapted.needs_owner_lineage is True
    assert adapted.strong_source_key is None
    assert adapted.legacy_content_sha256 == RAW_SHA
    assert adapted.source_content_hash == f"sha256:{RAW_SHA}"
    assert adapted.source_version_id == source_version_id(
        row["doc_id"], f"sha256:{RAW_SHA}"
    )
    assert adapted.created_at == "2019-02-03T04:05:06Z"


@pytest.mark.parametrize(
    ("source_kind", "source_key"),
    [
        ("url", "url:https://example.test/paper"),
        ("youtube_video", "youtube:abc12345"),
    ],
)
def test_document_strong_key_mints_logical_identity(source_kind, source_key):
    row = _document_row(source_kind=source_kind, source_key=source_key)

    adapted = adapt_document(row)

    expected = logical_doc_id(row["corpus_id"], source_key)
    assert adapted.doc_id == expected
    assert adapted.legacy_doc_id == "legacy-doc-1"
    assert adapted.logical_doc_id_minted is True
    assert adapted.needs_owner_lineage is False
    assert adapted.strong_source_key == source_key
    assert adapted.source_version_id == source_version_id(
        expected, f"sha256:{RAW_SHA}"
    )


def test_document_accepts_already_qualified_hash_without_double_prefix():
    row = _document_row()
    row["source_identity"]["content_sha256"] = f"sha256:{RAW_SHA}"

    adapted = adapt_document(row)

    assert adapted.legacy_content_sha256 == f"sha256:{RAW_SHA}"
    assert adapted.source_content_hash == f"sha256:{RAW_SHA}"


@pytest.mark.parametrize("bad_hash", ["short", "A" * 64, 123])
def test_document_rejects_malformed_source_hash(bad_hash):
    row = _document_row()
    row["source_identity"]["content_sha256"] = bad_hash

    with pytest.raises((TypeError, ValueError)):
        adapt_document(row)


def test_document_reports_all_missing_identity_paths():
    row = _document_row()
    del row["doc_id"]
    del row["source_identity"]["source_key"]
    del row["source_identity"]["content_sha256"]

    with pytest.raises(LegacyAdapterError) as raised:
        adapt_document(row)

    assert raised.value.collection == "documents"
    assert raised.value.missing == [
        "doc_id",
        "source_identity.content_sha256",
        "source_identity.source_key",
    ]


def test_ghost_b_stays_observation_only_and_preserves_legacy_status_as_echo():
    row = _ghost_row()
    before = copy.deepcopy(row)

    bundle = adapt_ghost_b_extraction(row)
    payload = bundle.model_dump()

    assert row == before
    assert bundle.schema_version == "polymath.observation_bundle.legacy_ere.v1"
    assert bundle.lane == "observation"
    assert bundle.legacy_status == "accepted"
    assert bundle.legacy_schema_version == "polymad.extract.v2"
    assert bundle.entities[0].assignment_state == "candidate"
    assert bundle.relations[0].assignment_state == "candidate"
    assert bundle.relations[0].legacy_validation_status == "accepted"
    assert bundle.facts[0].assignment_state == "candidate"
    assert "knowledge_status" not in payload
    assert "validation_status" not in payload["relations"][0]
    assert_no_promoted_status(payload)


def test_ghost_b_temporal_offsets_require_exact_round_trip():
    row = _ghost_row()

    verified = adapt_ghost_b_extraction(row)
    capture = verified.temporal_captures[0]
    assert capture.offsets_verified is True
    assert capture.quote_hash is not None
    assert verified.validation_drops == []

    row["temporal_captures"][0]["char_start"] += 1
    unverified = adapt_ghost_b_extraction(row)
    capture = unverified.temporal_captures[0]
    assert capture.offsets_verified is False
    assert capture.quote_hash is None
    assert len(unverified.validation_drops) == 1


def test_ghost_b_reports_all_missing_nested_required_fields():
    row = _ghost_row()
    del row["entities"][0]["canonical_name"]
    del row["relations"][0]["subject"]
    del row["relations"][0]["object"]
    del row["facts"][0]["value"]

    with pytest.raises(LegacyAdapterError) as raised:
        adapt_ghost_b_extraction(row)

    assert raised.value.missing == [
        "entities[0].canonical_name",
        "facts[0].value",
        "relations[0].object",
        "relations[0].subject",
    ]


def test_ghost_b_requires_present_core_arrays_but_allows_empty_arrays():
    row = _ghost_row()
    del row["relations"]
    with pytest.raises(LegacyAdapterError, match="relations"):
        adapt_ghost_b_extraction(row)

    row = _ghost_row()
    row.update(entities=[], relations=[], facts=[])
    bundle = adapt_ghost_b_extraction(row)
    assert bundle.entities == bundle.relations == bundle.facts == []


def test_parent_summary_remains_unvalidated_retrieval_summary():
    row = _parent_row()
    before = copy.deepcopy(row)

    record = adapt_parent_summary(row)
    payload = record.model_dump()

    assert row == before
    assert record.artifact_kind == "retrieval_summary"
    assert record.validation_status == "unvalidated"
    assert record.legacy_validation_status == "valid"
    assert record.legacy_summary_type == "claim"
    assert record.captured_fields.derivation_method == "llm_proposal"
    assert record.captured_fields.validation_status == "unvalidated"
    assert record.captured_fields.latent_concepts[0].concept == (
        "seasonal maintenance"
    )
    assert record.summary_created_at == "2026-07-14T02:03:04.000500Z"
    assert "knowledge_status" not in payload
    assert "semantic_digest" not in str(payload).lower()
    assert_no_promoted_status(payload)


def test_parent_summary_requires_attributed_nonempty_summary():
    row = _parent_row()
    row["summary"] = "  "
    row["summary_model"] = None

    with pytest.raises(LegacyAdapterError) as raised:
        adapt_parent_summary(row)

    assert raised.value.missing == ["summary", "summary_model"]


def test_lexicon_entry_is_candidate_identity_mapping_not_promoted_semantics():
    row = _lexicon_row()
    before = copy.deepcopy(row)

    sense = adapt_lexicon_entry(row)
    payload = sense.model_dump()

    assert row == before
    assert sense.canonical_key == row["canonical_key"]
    assert sense.legacy_lexicon_state == "accepted"
    assert sense.mapping.mapping_type == "exact"
    assert sense.mapping.method == "legacy_lexicon_identity"
    assert sense.mapping.validation_status == "candidate"
    assert sense.mapping.target_lexicon_id == row["lexicon_id"]
    assert sense.mapping.target_canonical_key == row["canonical_key"]
    assert_no_promoted_status(payload)


def test_adapter_identifiers_are_deterministic_and_identity_sensitive():
    ghost_a = adapt_ghost_b_extraction(_ghost_row())
    ghost_b = adapt_ghost_b_extraction(copy.deepcopy(_ghost_row()))
    summary_a = adapt_parent_summary(_parent_row())
    summary_b = adapt_parent_summary(copy.deepcopy(_parent_row()))
    lexicon_a = adapt_lexicon_entry(_lexicon_row())
    lexicon_b = adapt_lexicon_entry(copy.deepcopy(_lexicon_row()))

    assert ghost_a.bundle_id == ghost_b.bundle_id
    assert summary_a.record_id == summary_b.record_id
    assert lexicon_a.sense_id == lexicon_b.sense_id

    changed_ghost = _ghost_row()
    changed_ghost["text"] += " Extra evidence."
    changed_summary = _parent_row()
    changed_summary["summary"] += " Revised."
    changed_lexicon = _lexicon_row()
    changed_lexicon["canonical_key"] = "different sense"

    assert adapt_ghost_b_extraction(changed_ghost).bundle_id != ghost_a.bundle_id
    assert adapt_parent_summary(changed_summary).record_id != summary_a.record_id
    assert adapt_lexicon_entry(changed_lexicon).sense_id != lexicon_a.sense_id


def test_adapter_identifier_goldens_are_byte_exact():
    """Identity recipe changes require a new adapter version, never drift."""

    assert adapt_ghost_b_extraction(_ghost_row()).bundle_id == (
        "legacyobs:75fbda320c2908619347a6ad047c0e05470bf84b88b5757873ea61609d64f03a"
    )
    assert adapt_parent_summary(_parent_row()).record_id == (
        "legacysum:45edda8d5297b091ca37d24793df93ac8df78dbe5187f6ee944f7b435ecb03a0"
    )
    assert adapt_lexicon_entry(_lexicon_row()).sense_id == (
        "legacysense:749e1b996eb57d593ff3d96dcb1a035991198cdf287346fa6120fd3d15560c46"
    )


def test_no_promotion_guard_is_recursive_but_legacy_echoes_are_allowed():
    with pytest.raises(ValueError, match="relabels legacy data as promoted"):
        assert_no_promoted_status({"nested": [{"validation_status": "accepted"}]})

    assert_no_promoted_status(
        {
            "legacy_status": "accepted",
            "nested": {"legacy_validation_status": "validated"},
            "validation_status": "candidate",
        }
    )
