"""Tests for credit/metadata fragment patterns (P2B).

Run: cd backend && pytest tests/extraction/test_credit_patterns.py -q
"""

from __future__ import annotations

import pytest

from services.extraction.credit_patterns import (
    extract_credit_patterns,
    _looks_like_person_name,
    _extract_person_names,
    _marker_to_predicate,
)


# ---------------------------------------------------------------------------
# Core extraction tests
# ---------------------------------------------------------------------------


class TestCreditPatternExtraction:
    """Credit patterns extract work → created_by → person relations."""

    def test_screenplay_by_pattern(self):
        """A CHRISTMAS STORY (screenplay by Jean Shepherd & Leigh Brown & Bob Clark, 1983)"""
        text = (
            "A CHRISTMAS STORY\n\n"
            "(screenplay by Jean Shepherd & Leigh Brown & Bob Clark, 1983)"
        )
        entities = [
            {"start": 0, "end": 17, "text": "A CHRISTMAS STORY", "type": "concept"},
            {"start": 35, "end": 48, "text": "Jean Shepherd", "type": "concept"},
            {"start": 51, "end": 62, "text": "Leigh Brown", "type": "concept"},
            {"start": 65, "end": 74, "text": "Bob Clark", "type": "concept"},
        ]
        records = extract_credit_patterns(text, entities, "test_chunk")
        # Should find 3 relations: work → created_by → each person
        assert len(records) == 3
        for rec in records:
            assert rec["subject_text"] == "A CHRISTMAS STORY"
            assert rec["canonical_predicate"] == "created_by"
            assert rec["source_family"] == "TOKEN_PATTERN"
            assert rec["dependency_parse_used"] is False
            assert rec["confidence"] == 1.0
        # Check all three persons are found
        objects = {rec["object_text"] for rec in records}
        assert "Jean Shepherd" in objects
        assert "Leigh Brown" in objects
        assert "Bob Clark" in objects

    def test_written_by_pattern(self):
        """Written by Jane Doe"""
        text = "THE DOCUMENT\n\nWritten by Jane Doe"
        entities = [
            {"start": 0, "end": 12, "text": "THE DOCUMENT", "type": "document"},
            {"start": 25, "end": 33, "text": "Jane Doe", "type": "person"},
        ]
        records = extract_credit_patterns(text, entities, "c1")
        assert len(records) == 1
        assert records[0]["subject_text"] == "THE DOCUMENT"
        assert records[0]["object_text"] == "Jane Doe"
        assert records[0]["canonical_predicate"] == "created_by"

    def test_director_colon_pattern(self):
        """Director: John Smith"""
        text = "THE FILM\n\nDirector: John Smith"
        entities = [
            {"start": 0, "end": 8, "text": "THE FILM", "type": "concept"},
            {"start": 20, "end": 30, "text": "John Smith", "type": "person"},
        ]
        records = extract_credit_patterns(text, entities, "c1")
        assert len(records) == 1
        assert records[0]["canonical_predicate"] == "created_by"
        assert records[0]["object_text"] == "John Smith"

    def test_no_credit_marker_no_extraction(self):
        """Regular prose without credit markers produces nothing."""
        text = "Microsoft acquired GitHub in 2018 for $7.5 billion."
        entities = [
            {"start": 0, "end": 9, "text": "Microsoft", "type": "organization"},
            {"start": 19, "end": 25, "text": "GitHub", "type": "software"},
        ]
        records = extract_credit_patterns(text, entities, "c1")
        assert records == []

    def test_empty_inputs(self):
        """Empty text or entities returns empty list."""
        assert extract_credit_patterns("", [], "c1") == []
        assert extract_credit_patterns("some text", [], "c1") == []


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_person_name_heuristic(self):
        assert _looks_like_person_name("Jean Shepherd") is True
        assert _looks_like_person_name("Bob Clark") is True
        assert _looks_like_person_name("Jane Mary Doe") is True
        # Not person names
        assert _looks_like_person_name("A CHRISTMAS STORY") is False  # ALLCAPS
        assert _looks_like_person_name("Microsoft") is False  # single word
        assert _looks_like_person_name("") is False

    def test_extract_person_names_with_coordination(self):
        region = "Jean Shepherd & Leigh Brown & Bob Clark, 1983"
        names = _extract_person_names(region)
        assert "Jean Shepherd" in names
        assert "Leigh Brown" in names
        assert "Bob Clark" in names

    def test_extract_person_names_stops_at_year(self):
        region = "Jane Doe, 2020"
        names = _extract_person_names(region)
        assert names == ["Jane Doe"]

    def test_marker_to_predicate(self):
        assert _marker_to_predicate("screenplay by") == "created_by"
        assert _marker_to_predicate("written by") == "created_by"
        assert _marker_to_predicate("directed by") == "created_by"
        assert _marker_to_predicate("director:") == "created_by"
        assert _marker_to_predicate("author:") == "created_by"
        assert _marker_to_predicate("random text") == ""


# ---------------------------------------------------------------------------
# Integration: credit patterns flow through the gate correctly
# ---------------------------------------------------------------------------


class TestCreditPatternGateIntegration:
    """Credit pattern records have the correct shape for the gate pipeline."""

    def test_record_shape(self):
        """Records have all required fields for join_syntax_evidence."""
        text = "MY BOOK\n\nWritten by Alice Walker"
        entities = [
            {"start": 0, "end": 7, "text": "MY BOOK", "type": "document"},
            {"start": 20, "end": 32, "text": "Alice Walker", "type": "person"},
        ]
        records = extract_credit_patterns(text, entities, "c1")
        assert len(records) == 1
        rec = records[0]
        # Required fields for join_syntax_evidence
        assert "chunk_id" in rec
        assert "subject_start" in rec
        assert "subject_end" in rec
        assert "object_start" in rec
        assert "object_end" in rec
        assert "canonical_predicate" in rec
        assert "surface_predicate" in rec
        assert "pattern_id" in rec
        assert "confidence" in rec
        # P2B-specific fields
        assert rec["source_family"] == "TOKEN_PATTERN"
        assert rec["structural_confidence"] == "high"
        assert rec["dependency_parse_used"] is False
