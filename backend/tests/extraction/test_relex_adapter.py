"""Tests for relex_adapter and mention_normalizer.

relex_adapter: Relex JSONL → RelationEvidence, syntax joining, SVO conversion.
mention_normalizer: singular/plural, morphological variants, truncation guard.

Run: cd backend && pytest tests/extraction/test_relex_adapter.py -q
"""

from __future__ import annotations

import json
import pytest

from services.extraction.relation_evidence import (
    GateStatus,
    JoinMode,
    PredicateScore,
    RelationEvidence,
    SyntaxEvidence,
)
from services.extraction.relation_evidence import RelationEvidence
from services.extraction.corroboration_gate import evaluate_relation, load_policy
from services.extraction.relex_adapter import (
    build_relation_evidence,
    canonicalize_predicate_label,
    join_syntax_evidence,
    load_predictions_jsonl,
)
from services.extraction.mention_normalizer import (
    is_morphological_variant,
    normalized_mention,
    resolve_mention_pair,
)


# ---------------------------------------------------------------------------
# mention_normalizer
# ---------------------------------------------------------------------------


class TestNormalizedMention:
    def test_plural_to_singular(self):
        assert normalized_mention("cold leads") == "cold lead"

    def test_proper_noun_lowercased(self):
        assert normalized_mention("CAPTCHA") == "captcha"

    def test_company_plural(self):
        assert normalized_mention("Companies") == "company"

    def test_empty_string(self):
        assert normalized_mention("") == ""

    def test_none_input(self):
        assert normalized_mention(None) == ""

    def test_whitespace_collapsed(self):
        assert normalized_mention("cold   lead") == "cold lead"


class TestIsMorphologicalVariant:
    def test_singular_plural_is_variant(self):
        assert is_morphological_variant("cold lead", "cold leads") is True

    def test_plural_singular_is_variant(self):
        assert is_morphological_variant("cold leads", "cold lead") is True

    def test_different_entities_not_variant(self):
        assert is_morphological_variant("Shadow Cities", "Cities") is False

    def test_same_surface_is_variant(self):
        assert is_morphological_variant("CAPTCHA", "captcha") is True

    def test_completely_different_not_variant(self):
        assert is_morphological_variant("Google", "Microsoft") is False


class TestResolveMentionPair:
    def test_plural_resolved_to_singular(self):
        result = resolve_mention_pair(
            model_surface="cold leads",
            model_span=(333, 343),
            gold_surface="cold lead",
            gold_span=(333, 342),
        )
        assert result is not None
        assert result["model_surface"] == "cold leads"
        assert result["canonical_surface"] == "cold lead"
        assert result["model_span"] == [333, 343]

    def test_truncation_not_resolved(self):
        result = resolve_mention_pair(
            model_surface="Cities",
            model_span=(8, 14),
            gold_surface="Shadow Cities",
            gold_span=(1, 14),
        )
        assert result is None


# ---------------------------------------------------------------------------
# relex_adapter
# ---------------------------------------------------------------------------


class TestBuildRelationEvidence:
    def _make_prediction_row(self) -> dict:
        return {
            "sample_id": "test_001",
            "entities": [
                {"text": "Companies", "start": 0, "end": 9, "type": "organization"},
                {"text": "CAPTCHA", "start": 19, "end": 26, "type": "software"},
            ],
            "raw_pair_scores": [
                {
                    "head": {"text": "Companies", "start": 0, "end": 9, "type": "organization"},
                    "tail": {"text": "CAPTCHA", "start": 19, "end": 26, "type": "software"},
                    "scores": {"uses": 0.281, "produces": 0.003, "part of": 0.001},
                },
                {
                    "head": {"text": "CAPTCHA", "start": 19, "end": 26, "type": "software"},
                    "tail": {"text": "Companies", "start": 0, "end": 9, "type": "organization"},
                    "scores": {"uses": 0.0002, "produces": 0.001},
                },
            ],
        }

    def test_basic_conversion(self):
        row = self._make_prediction_row()
        evidence = build_relation_evidence("test_001", row)
        assert len(evidence) == 2  # both ordered pairs

    def test_predicate_scores_preserved(self):
        row = self._make_prediction_row()
        evidence = build_relation_evidence("test_001", row)
        forward = [e for e in evidence if e.subject_text == "Companies"][0]
        assert len(forward.predicate_scores) == 3
        scores = {ps.predicate: ps.score for ps in forward.predicate_scores}
        assert scores["uses"] == pytest.approx(0.281)

    def test_reverse_scores_attached(self):
        row = self._make_prediction_row()
        evidence = build_relation_evidence("test_001", row)
        forward = [e for e in evidence if e.subject_text == "Companies"][0]
        assert forward.reverse_scores.get("uses") == pytest.approx(0.0002)

    def test_span_offsets_correct(self):
        row = self._make_prediction_row()
        evidence = build_relation_evidence("test_001", row)
        forward = [e for e in evidence if e.subject_text == "Companies"][0]
        assert forward.subject_start == 0
        assert forward.subject_end == 9
        assert forward.object_start == 19
        assert forward.object_end == 26

    def test_empty_predictions(self):
        evidence = build_relation_evidence("empty", {"raw_pair_scores": []})
        assert evidence == []


class TestPredicateLabelCanonicalization:
    """Relex emits space-form labels ("part of"); the shared contract uses
    underscore-form ("part_of"). The adapter must canonicalize."""

    def test_single_word_unchanged(self):
        assert canonicalize_predicate_label("uses") == "uses"
        assert canonicalize_predicate_label("produces") == "produces"

    def test_space_to_underscore(self):
        assert canonicalize_predicate_label("part of") == "part_of"
        assert canonicalize_predicate_label("instance of") == "instance_of"
        assert canonicalize_predicate_label("works for") == "works_for"
        assert canonicalize_predicate_label("located in") == "located_in"
        assert canonicalize_predicate_label("created by") == "created_by"
        assert canonicalize_predicate_label("synonym of") == "synonym_of"

    def test_production_alias_consistency(self):
        """canonicalize must agree with production relex_gate mapping.

        The model emits "instance of" (not "is an instance of") and "runs on"
        maps to "runs" (not "runs_on"). These are the divergent cases found in
        the actual prediction dump.
        """
        # Model drops "is an" from the prompt label
        assert canonicalize_predicate_label("is an instance of") == "instance_of"
        assert canonicalize_predicate_label("instance of") == "instance_of"
        # "runs on" maps to ontology predicate "runs", not "runs_on"
        assert canonicalize_predicate_label("runs on") == "runs"

    def test_case_normalized(self):
        assert canonicalize_predicate_label("Part Of") == "part_of"
        assert canonicalize_predicate_label("USES") == "uses"

    def test_canonicalized_in_build(self):
        """build_relation_evidence must canonicalize ALL labels."""
        row = {
            "raw_pair_scores": [{
                "head": {"text": "A", "start": 0, "end": 1},
                "tail": {"text": "B", "start": 2, "end": 3},
                "scores": {"part of": 0.5, "instance of": 0.3, "uses": 0.1},
            }],
        }
        evidence = build_relation_evidence("c1", row)
        preds = {ps.predicate for ps in evidence[0].predicate_scores}
        assert "part_of" in preds
        assert "instance_of" in preds
        assert "uses" in preds
        assert "part of" not in preds

    def test_reverse_scores_canonicalized(self):
        """reverse_scores must use underscore-form so the gate can look them up."""
        row = {
            "raw_pair_scores": [
                {
                    "head": {"text": "A", "start": 0, "end": 1},
                    "tail": {"text": "B", "start": 2, "end": 3},
                    "scores": {"part of": 0.5},
                },
                {
                    "head": {"text": "B", "start": 2, "end": 3},
                    "tail": {"text": "A", "start": 0, "end": 1},
                    "scores": {"part of": 0.02},
                },
            ],
        }
        evidence = build_relation_evidence("c1", row)
        forward = [e for e in evidence if e.subject_text == "A"][0]
        # reverse_scores must be keyed by canonicalized label
        assert forward.reverse_scores.get("part_of") == pytest.approx(0.02)
        assert forward.reverse_scores.get("part of") is None  # space-form must not exist


class TestJoinSyntaxEvidence:
    def test_exact_span_match(self):
        ev = RelationEvidence(
            chunk_id="c1",
            subject_id="s", subject_text="A", subject_type="org",
            subject_start=0, subject_end=5,
            object_id="o", object_text="B", object_type="software",
            object_start=10, object_end=15,
            predicate_scores=(PredicateScore("uses", 0.25),),
            reverse_scores={},
        )
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 0, "subject_end": 5,
            "object_start": 10, "object_end": 15,
            "canonical_predicate": "uses",
            "surface_predicate": "use",
            "pattern_id": "ACTIVE_SVO",
            "confidence": 1.0,
        }]
        result = join_syntax_evidence([ev], syntax)
        assert len(result[0].syntax_evidence) == 1
        assert result[0].syntax_evidence[0].canonical_predicate == "uses"

    def test_no_match_empty_syntax(self):
        ev = RelationEvidence(
            chunk_id="c1",
            subject_id="s", subject_text="A", subject_type="org",
            subject_start=0, subject_end=5,
            object_id="o", object_text="B", object_type="software",
            object_start=10, object_end=15,
            predicate_scores=(PredicateScore("uses", 0.25),),
            reverse_scores={},
        )
        result = join_syntax_evidence([ev], [])
        assert len(result[0].syntax_evidence) == 0

    def test_morphological_variant_join(self):
        """Pair at span (0,5,10,15) should join syntax at (0,6,10,15) when
        the spans are close enough to be morphological variants."""
        ev = RelationEvidence(
            chunk_id="c1",
            subject_id="s", subject_text="leads", subject_type="concept",
            subject_start=0, subject_end=5,
            object_id="o", object_text="lead", object_type="concept",
            object_start=10, object_end=15,
            predicate_scores=(PredicateScore("instance_of", 0.27),),
            reverse_scores={},
        )
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 0, "subject_end": 6,  # 1 char off
            "object_start": 10, "object_end": 15,
            "canonical_predicate": "instance_of",
            "surface_predicate": "be",
            "pattern_id": "COPULAR_ATTR",
            "confidence": 1.0,
        }]
        result = join_syntax_evidence([ev], syntax)
        # Should join via morphological proximity
        assert len(result[0].syntax_evidence) == 1


class TestSurfaceFormJoin:
    """Surface-form join: match syntax evidence from a different mention
    of the same entity (e.g., CAPTCHA appears at position 62 in sentence 1
    and position 139 in sentence 2; syntax evidence is at 139 but the
    Relex pair is at 62)."""

    def test_surface_join_different_position(self):
        ev = RelationEvidence(
            chunk_id="c1",
            subject_id="s", subject_text="Companies", subject_type="",
            subject_start=119, subject_end=128,
            object_id="o", object_text="CAPTCHA", object_type="",
            object_start=62, object_end=69,
            predicate_scores=(PredicateScore("uses", 0.28),),
            reverse_scores={},
        )
        # Syntax evidence at a DIFFERENT mention position (139, not 62)
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 119, "subject_end": 128,
            "subject_text": "Companies",
            "object_start": 139, "object_end": 146,
            "object_text": "CAPTCHA",
            "canonical_predicate": "uses",
            "surface_predicate": "use",
            "pattern_id": "ACTIVE_SVO",
            "confidence": 1.0,
        }]
        # Without surface_join: no match (spans differ)
        result_no_surface = join_syntax_evidence([ev], syntax, surface_join=False)
        assert len(result_no_surface[0].syntax_evidence) == 0

        # With surface_join: matches by normalized surface form
        result_surface = join_syntax_evidence([ev], syntax, surface_join=True)
        assert len(result_surface[0].syntax_evidence) == 1
        assert result_surface[0].syntax_evidence[0].canonical_predicate == "uses"

    def test_surface_join_requires_both_text_fields(self):
        """Without subject_text/object_text in the record, surface_join can't fire."""
        ev = RelationEvidence(
            chunk_id="c1",
            subject_id="s", subject_text="Companies", subject_type="",
            subject_start=119, subject_end=128,
            object_id="o", object_text="CAPTCHA", object_type="",
            object_start=62, object_end=69,
            predicate_scores=(PredicateScore("uses", 0.28),),
            reverse_scores={},
        )
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 139, "subject_end": 146,
            # NO subject_text / object_text
            "object_start": 200, "object_end": 207,
            "canonical_predicate": "uses",
            "surface_predicate": "use",
            "pattern_id": "ACTIVE_SVO",
            "confidence": 1.0,
        }]
        result = join_syntax_evidence([ev], syntax, surface_join=True)
        assert len(result[0].syntax_evidence) == 0


class TestEndToEndCorroboration:
    """Full cycle: predictions → evidence → join syntax → gate decision."""

    def test_companies_uses_captcha_recovery(self):
        """Simulates the Companies→uses→CAPTCHA recovery from gold diagnostic."""
        prediction = {
            "sample_id": "relex_v1_000",
            "entities": [],
            "raw_pair_scores": [
                {
                    "head": {"text": "Companies", "start": 119, "end": 128, "type": "organization"},
                    "tail": {"text": "CAPTCHA", "start": 62, "end": 69, "type": "software"},
                    "scores": {
                        "uses": 0.2806,
                        "produces": 0.003,
                        "part of": 0.001,
                        "synonym of": 0.0002,
                    },
                },
                {
                    "head": {"text": "CAPTCHA", "start": 62, "end": 69, "type": "software"},
                    "tail": {"text": "Companies", "start": 119, "end": 128, "type": "organization"},
                    "scores": {"uses": 0.0002, "produces": 0.001},
                },
            ],
        }
        evidence = build_relation_evidence("relex_v1_000", prediction)
        forward = [e for e in evidence if "uses" in {ps.predicate for ps in e.predicate_scores}][0]
        forward = [e for e in evidence if e.subject_text == "Companies"][0]

        # Add syntax evidence
        syntax = [{
            "chunk_id": "relex_v1_000",
            "subject_start": 119, "subject_end": 128,
            "object_start": 62, "object_end": 69,
            "canonical_predicate": "uses",
            "surface_predicate": "use",
            "pattern_id": "ACTIVE_SVO",
            "confidence": 1.0,
        }]
        joined = join_syntax_evidence([forward], syntax)

        policy = load_policy()
        decision = evaluate_relation(joined[0], policy)

        assert decision.status == GateStatus.ACCEPT_CORROBORATED
        assert decision.score == pytest.approx(0.2806, abs=0.001)


# ---------------------------------------------------------------------------
# Join-mode tagging (JoinMode on SyntaxEvidence)
# ---------------------------------------------------------------------------


class TestJoinModeTagging:
    """Verify that join_syntax_evidence tags each SyntaxEvidence with the
    correct join_mode describing how the match was made."""

    def _make_evidence_at(self, s_start, s_end, o_start, o_end, s_text="A", o_text="B"):
        return RelationEvidence(
            chunk_id="c1",
            subject_id=f"span:{s_start}:{s_end}", subject_text=s_text, subject_type="organization",
            subject_start=s_start, subject_end=s_end,
            object_id=f"span:{o_start}:{o_end}", object_text=o_text, object_type="software",
            object_start=o_start, object_end=o_end,
            predicate_scores=(PredicateScore("uses", 0.30),),
            reverse_scores={},
        )

    def test_exact_span_join_tagged(self):
        """Exact span matches get join_mode=exact_span."""
        ev = self._make_evidence_at(0, 5, 10, 15)
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 0, "subject_end": 5,
            "object_start": 10, "object_end": 15,
            "subject_text": "A", "object_text": "B",
            "canonical_predicate": "uses", "surface_predicate": "use",
            "pattern_id": "DEP_1", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax)
        assert joined[0].syntax_evidence
        assert joined[0].syntax_evidence[0].join_mode == "exact_span"

    def test_morphological_variant_tagged_canonical_entity(self):
        """Span-proximity joins get join_mode=canonical_entity."""
        ev = self._make_evidence_at(0, 5, 10, 15)
        # Syntax at slightly different spans (within 2 chars)
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 0, "subject_end": 6,
            "object_start": 10, "object_end": 16,
            "subject_text": "A", "object_text": "B",
            "canonical_predicate": "uses", "surface_predicate": "use",
            "pattern_id": "DEP_1", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax)
        assert joined[0].syntax_evidence
        assert joined[0].syntax_evidence[0].join_mode == "canonical_entity"

    def test_normalized_surface_join_tagged(self):
        """Surface-form joins (shadow-only) get join_mode=normalized_surface."""
        ev = self._make_evidence_at(0, 5, 10, 15, s_text="Companies", o_text="CAPTCHA")
        # No span overlap — different mention positions of the same entity
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 100, "subject_end": 109,
            "object_start": 200, "object_end": 207,
            "subject_text": "Companies", "object_text": "CAPTCHA",
            "canonical_predicate": "uses", "surface_predicate": "use",
            "pattern_id": "SVO", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax, surface_join=True)
        assert joined[0].syntax_evidence
        assert joined[0].syntax_evidence[0].join_mode == "normalized_surface"

    def test_no_surface_join_when_disabled(self):
        """Surface-form join requires surface_join=True."""
        ev = self._make_evidence_at(0, 5, 10, 15, s_text="X", o_text="Y")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 100, "subject_end": 109,
            "object_start": 200, "object_end": 207,
            "subject_text": "X", "object_text": "Y",
            "canonical_predicate": "uses", "surface_predicate": "use",
            "pattern_id": "SVO", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax, surface_join=False)
        assert not joined[0].syntax_evidence


# ---------------------------------------------------------------------------
# Type normalization at the adapter boundary
# ---------------------------------------------------------------------------


class TestAdapterTypeNormalization:
    """build_relation_evidence normalizes types via canonical_entity_type."""

    def test_empty_type_becomes_unknown(self):
        """Missing types become 'unknown' (visible) rather than '' (silent)."""
        prediction = {
            "sample_id": "s1",
            "entities": [],
            "raw_pair_scores": [
                {
                    "head": {"text": "X", "start": 0, "end": 1, "type": ""},
                    "tail": {"text": "Y", "start": 5, "end": 6, "type": ""},
                    "scores": {"uses": 0.3},
                },
            ],
        }
        evidence = build_relation_evidence("s1", prediction)
        assert evidence[0].subject_type == "unknown"
        assert evidence[0].object_type == "unknown"


# ---------------------------------------------------------------------------
# P2A: Head-token entity joining
# ---------------------------------------------------------------------------


class TestHeadTokenJoin:
    """Head-token and span-containment joins for syntax-to-Relex matching.

    HEAD_TOKEN (trust 0.60): the syntax argument's actual spaCy head token
    offset lies inside the Relex entity span. Requires subject_head_start/end
    and object_head_start/end fields in the syntax record.

    SPAN_CONTAINMENT (trust 0.50): phrase-span containment without head-token
    verification. Diagnostic only — cannot independently authorize production
    acceptance or satisfy corroboration.
    """

    def _make_evidence_at(self, s_start, s_end, o_start, o_end, s_text="A", o_text="B"):
        return RelationEvidence(
            chunk_id="c1",
            subject_id=f"span:{s_start}:{s_end}", subject_text=s_text, subject_type="organization",
            subject_start=s_start, subject_end=s_end,
            object_id=f"span:{o_start}:{o_end}", object_text=o_text, object_type="software",
            object_start=o_start, object_end=o_end,
            predicate_scores=(PredicateScore("uses", 0.3),),
            reverse_scores={},
        )

    def test_long_entity_contains_argument_head(self):
        """Entity 'A CHRISTMAS STORY' [0,17] contains syntax arg 'story' [12,17].

        Without head-token offset fields, this falls through to SPAN_CONTAINMENT.
        """
        ev = self._make_evidence_at(0, 17, 20, 33, s_text="A CHRISTMAS STORY", o_text="Jean Shepherd")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 12, "subject_end": 17,  # "story" within [0,17]
            "object_start": 20, "object_end": 33,   # exact match on object
            "subject_text": "story", "object_text": "Jean Shepherd",
            "canonical_predicate": "created_by", "surface_predicate": "write",
            "pattern_id": "PASSIVE_BY", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax)
        assert len(joined[0].syntax_evidence) == 1
        assert joined[0].syntax_evidence[0].join_mode == JoinMode.SPAN_CONTAINMENT.value

    def test_true_head_token_with_offsets(self):
        """True HEAD_TOKEN: head-token offsets inside entity span."""
        ev = self._make_evidence_at(0, 17, 20, 33, s_text="A CHRISTMAS STORY", o_text="Jean Shepherd")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 12, "subject_end": 17,  # "story" within [0,17]
            "object_start": 20, "object_end": 33,   # exact match on object
            "subject_text": "story", "object_text": "Jean Shepherd",
            "canonical_predicate": "created_by", "surface_predicate": "write",
            "pattern_id": "PASSIVE_BY", "confidence": 1.0,
            # Head-token offsets: 'story' head is at [12,17], inside entity [0,17]
            "subject_head_start": 12, "subject_head_end": 17,
            # Object head: 'Jean' at [20,24], inside entity [20,33]
            "object_head_start": 20, "object_head_end": 24,
        }]
        joined = join_syntax_evidence([ev], syntax, enable_head_token=True)
        assert len(joined[0].syntax_evidence) == 1
        assert joined[0].syntax_evidence[0].join_mode == JoinMode.HEAD_TOKEN.value

    def test_entity_span_contained_in_syntax_span(self):
        """Entity 'cold lead' [0,9] contained in syntax 'cold leads' [0,10].

        Normalized mention matching fires first (CANONICAL_ENTITY at Level 2)
        because 'cold lead' normalizes to the same key as 'cold leads'.
        """
        ev = self._make_evidence_at(0, 9, 20, 30, s_text="cold lead", o_text="sales type")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 0, "subject_end": 10,  # "cold leads" contains [0,9]
            "object_start": 20, "object_end": 31,   # "sales types" contains [20,30]
            "subject_text": "cold leads", "object_text": "sales types",
            "canonical_predicate": "instance_of", "surface_predicate": "be",
            "pattern_id": "COPULAR", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax)
        assert len(joined[0].syntax_evidence) == 1
        assert joined[0].syntax_evidence[0].join_mode == JoinMode.CANONICAL_ENTITY.value

    def test_identical_spans_not_head_token(self):
        """Identical spans → NOT head-token (handled by EXACT_SPAN at Level 1)."""
        ev = self._make_evidence_at(0, 5, 10, 15, s_text="X", o_text="Y")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 0, "subject_end": 5,   # identical to entity
            "object_start": 10, "object_end": 15,  # identical to entity
            "subject_text": "X", "object_text": "Y",
            "canonical_predicate": "uses", "surface_predicate": "use",
            "pattern_id": "SVO", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax)
        # Should match at Level 1 (EXACT_SPAN), not Level 4
        assert len(joined[0].syntax_evidence) == 1
        assert joined[0].syntax_evidence[0].join_mode == JoinMode.EXACT_SPAN.value

    def test_nested_entity_spans(self):
        """Syntax 'CAPTCHA' [20,27] contained in entity 'CAPTCHA fields' [20,34].

        Without head-token offsets, this is SPAN_CONTAINMENT (trust 0.50).
        """
        ev = self._make_evidence_at(0, 9, 20, 34, s_text="Companies", o_text="CAPTCHA fields")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 0, "subject_end": 9,   # exact match on subject
            "object_start": 20, "object_end": 27,  # "CAPTCHA" within [20,34]
            "subject_text": "Companies", "object_text": "CAPTCHA",
            "canonical_predicate": "uses", "surface_predicate": "use",
            "pattern_id": "ACTIVE_SVO", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax)
        assert len(joined[0].syntax_evidence) == 1
        assert joined[0].syntax_evidence[0].join_mode == JoinMode.SPAN_CONTAINMENT.value

    def test_disjoint_spans_no_match(self):
        """Disjoint spans (no containment) → no HEAD_TOKEN join."""
        ev = self._make_evidence_at(0, 17, 20, 33, s_text="A CHRISTMAS STORY", o_text="Jean Shepherd")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 50, "subject_end": 55,  # far from [0,17]
            "object_start": 60, "object_end": 75,   # far from [20,33]
            "subject_text": "story", "object_text": "Shepherd",
            "canonical_predicate": "created_by", "surface_predicate": "write",
            "pattern_id": "PASSIVE_BY", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax, surface_join=False)
        assert not joined[0].syntax_evidence

    def test_subject_and_object_resolve_to_distinct_mentions(self):
        """Subject matches but object is disjoint → no HEAD_TOKEN join."""
        ev = self._make_evidence_at(0, 17, 20, 33, s_text="A CHRISTMAS STORY", o_text="Jean Shepherd")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 12, "subject_end": 17,  # contained in [0,17]
            "object_start": 60, "object_end": 70,   # NOT contained in [20,33]
            "subject_text": "story", "object_text": "Bob Clark",
            "canonical_predicate": "created_by", "surface_predicate": "direct",
            "pattern_id": "PASSIVE_BY", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax)
        assert not joined[0].syntax_evidence

    def test_partial_overlap_not_containment(self):
        """Partial overlap (not full containment) → no HEAD_TOKEN join."""
        ev = self._make_evidence_at(0, 10, 20, 30, s_text="entity_a", o_text="entity_b")
        syntax = [{
            "chunk_id": "c1",
            "subject_start": 5, "subject_end": 15,  # overlaps [0,10] but not contained
            "object_start": 20, "object_end": 30,   # exact match
            "subject_text": "overlap", "object_text": "entity_b",
            "canonical_predicate": "uses", "surface_predicate": "use",
            "pattern_id": "SVO", "confidence": 1.0,
        }]
        joined = join_syntax_evidence([ev], syntax, surface_join=False)
        assert not joined[0].syntax_evidence

    def test_known_type_normalized_to_lowercase(self):
        """Title Case types become lowercase canonical form."""
        prediction = {
            "sample_id": "s1",
            "entities": [],
            "raw_pair_scores": [
                {
                    "head": {"text": "X", "start": 0, "end": 1, "type": "Organization"},
                    "tail": {"text": "Y", "start": 5, "end": 6, "type": "Software"},
                    "scores": {"uses": 0.3},
                },
            ],
        }
        evidence = build_relation_evidence("s1", prediction)
        assert evidence[0].subject_type == "organization"
        assert evidence[0].object_type == "software"

    def test_synonym_folded(self):
        """Synonyms like 'AGENT' fold onto canonical 'person'."""
        prediction = {
            "sample_id": "s1",
            "entities": [],
            "raw_pair_scores": [
                {
                    "head": {"text": "X", "start": 0, "end": 1, "type": "AGENT"},
                    "tail": {"text": "Y", "start": 5, "end": 6, "type": "PLACE"},
                    "scores": {"uses": 0.3},
                },
            ],
        }
        evidence = build_relation_evidence("s1", prediction)
        assert evidence[0].subject_type == "person"
        assert evidence[0].object_type == "location"

    def test_unknown_type_becomes_unknown(self):
        """Out-of-ontology types collapse to 'unknown' for visibility."""
        prediction = {
            "sample_id": "s1",
            "entities": [],
            "raw_pair_scores": [
                {
                    "head": {"text": "X", "start": 0, "end": 1, "type": "WibblyWoo"},
                    "tail": {"text": "Y", "start": 5, "end": 6, "type": "Gadget"},
                    "scores": {"uses": 0.3},
                },
            ],
        }
        evidence = build_relation_evidence("s1", prediction)
        assert evidence[0].subject_type == "unknown"
        assert evidence[0].object_type == "unknown"
