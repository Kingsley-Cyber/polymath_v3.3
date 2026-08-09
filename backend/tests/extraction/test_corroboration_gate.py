"""Tests for the corroboration gate.

Covers all gate states:
  ACCEPT_HIGH, ACCEPT_CORROBORATED, REVIEW_CONFLICT,
  REJECT_ENDPOINT_SIGNATURE, REJECT_NEGATED, REJECT_LOW_EVIDENCE,
  REVIEW_UNKNOWN_TYPE, REVIEW_CROSS_SENTENCE

Plus: determinism, rank/margin computation, reverse_score direction,
policy loading from YAML, and the specific recovery cases from the
18-relation gold diagnostic.

Run: cd backend && pytest tests/extraction/test_corroboration_gate.py -q
"""

from __future__ import annotations

import pytest

from services.extraction.relation_evidence import (
    GateDecision,
    GateStatus,
    JoinMode,
    PredicateScore,
    RelationEvidence,
    SyntaxEvidence,
)
from services.extraction.corroboration_gate import (
    PredicatePolicy,
    RelationPolicy,
    evaluate_relation,
    load_policy,
    reset_policy_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_evidence(
    predicate_scores: list[tuple[str, float]],
    *,
    subject_type: str = "organization",
    object_type: str = "software",
    reverse_scores: dict[str, float] | None = None,
    syntax: list[SyntaxEvidence] | None = None,
    chunk_id: str = "test_001",
    s_start: int = 0,
    s_end: int = 5,
    o_start: int = 10,
    o_end: int = 15,
    same_sentence: bool = True,
    sentence_distance: int = 0,
    has_cross_sentence_link: bool = False,
    cross_sentence_link_type: str = "none",
) -> RelationEvidence:
    return RelationEvidence(
        chunk_id=chunk_id,
        subject_id=f"span:{s_start}:{s_end}",
        subject_text="entity_a",
        subject_type=subject_type,
        subject_start=s_start,
        subject_end=s_end,
        object_id=f"span:{o_start}:{o_end}",
        object_text="entity_b",
        object_type=object_type,
        object_start=o_start,
        object_end=o_end,
        predicate_scores=tuple(
            PredicateScore(p, s) for p, s in predicate_scores
        ),
        reverse_scores=reverse_scores or {},
        syntax_evidence=tuple(syntax or ()),
        same_sentence=same_sentence,
        sentence_distance=sentence_distance,
        has_cross_sentence_link=has_cross_sentence_link,
        cross_sentence_link_type=cross_sentence_link_type,
    )


def _make_policy(
    *,
    standard_threshold: float = 0.30,
    standard_margin: float = 0.05,
    corroborated_threshold: float = 0.25,
    corroborated_margin: float = 0.10,
    direction_margin: float = 0.00,
    allowed_types: list[tuple[str, str]] | None = None,
    subject_types: list[str] | None = None,
    object_types: list[str] | None = None,
) -> RelationPolicy:
    # Support both legacy allowed_types pairs and new subject/object lists
    if subject_types is not None or object_types is not None:
        st = frozenset(subject_types) if subject_types else None
        ot = frozenset(object_types) if object_types else None
    elif allowed_types:
        st = frozenset(h for h, _ in allowed_types)
        ot = frozenset(t for _, t in allowed_types)
    else:
        st = None
        ot = None
    pred = PredicatePolicy(
        standard_threshold=standard_threshold,
        standard_margin=standard_margin,
        corroborated_threshold=corroborated_threshold,
        corroborated_margin=corroborated_margin,
        direction_margin=direction_margin,
        subject_types=st,
        object_types=ot,
    )
    rp = RelationPolicy(
        defaults={"standard_threshold": 0.30, "standard_margin": 0.05,
                  "corroborated_threshold": 0.25, "corroborated_margin": 0.10,
                  "direction_margin": 0.0},
        predicates={"uses": {}},
    )
    rp._predicates["uses"] = pred
    return rp


# ---------------------------------------------------------------------------
# ACCEPT_HIGH
# ---------------------------------------------------------------------------


class TestAcceptHigh:
    def test_standard_gate_passed_relex_only_shadow(self):
        """Relex top-1 clears standard threshold + margin without syntax → SHADOW."""
        ev = _make_evidence([("uses", 0.45), ("produces", 0.02)])
        policy = _make_policy()
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.SHADOW_RELEX_HIGH
        assert decision.predicate == "uses"
        assert decision.score == pytest.approx(0.45)
        assert decision.margin == pytest.approx(0.43)
        assert "relex_standard_gate_passed" in decision.reasons
        assert "relex_only_shadow" in decision.reasons
        assert "NO_SYNTAX_SUPPORT" in decision.blocking_reasons

    def test_accept_high_with_syntax_present(self):
        """High score + syntax agrees → ACCEPT_HIGH (production-eligible)."""
        ev = _make_evidence(
            [("uses", 0.40), ("produces", 0.01)],
            syntax=[SyntaxEvidence("uses", "use", "ACTIVE_SVO", 1.0)],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_HIGH
        assert "syntax_agrees" in decision.reasons


# ---------------------------------------------------------------------------
# ACCEPT_CORROBORATED
# ---------------------------------------------------------------------------


class TestAcceptCorroborated:
    def test_low_score_recovered_by_syntax(self):
        """Below standard but above corroborated threshold, syntax agrees."""
        ev = _make_evidence(
            [("uses", 0.22), ("produces", 0.003)],
            syntax=[SyntaxEvidence("uses", "use", "ACTIVE_SVO", 1.0)],
        )
        policy = _make_policy(
            standard_threshold=0.30,
            corroborated_threshold=0.20,
            corroborated_margin=0.10,
        )
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.ACCEPT_CORROBORATED
        assert "syntax_corroborated" in decision.reasons
        assert "ACTIVE_SVO" in decision.reasons

    def test_no_corroboration_without_syntax(self):
        """Same score but no syntax → REJECT_LOW_EVIDENCE."""
        ev = _make_evidence([("uses", 0.22), ("produces", 0.003)])
        policy = _make_policy(
            standard_threshold=0.30,
            corroborated_threshold=0.20,
            corroborated_margin=0.10,
        )
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.REJECT_LOW_EVIDENCE

    def test_corroboration_below_threshold_rejected(self):
        """Score below corroborated threshold → reject even with agreeing syntax.
        Syntax agrees so there's no conflict — just insufficient evidence."""
        ev = _make_evidence(
            [("uses", 0.15), ("produces", 0.003)],
            syntax=[SyntaxEvidence("uses", "use", "ACTIVE_SVO", 1.0)],
        )
        policy = _make_policy(corroborated_threshold=0.20)
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.REJECT_LOW_EVIDENCE

    def test_companies_uses_captcha_recovery(self):
        """Simulate the Companies→uses→CAPTCHA gold diagnostic case."""
        ev = _make_evidence(
            [("uses", 0.281), ("produces", 0.003)],
            subject_type="organization",
            object_type="software",
            syntax=[SyntaxEvidence("uses", "use", "ACTIVE_SVO", 1.0)],
        )
        policy = _make_policy(
            standard_threshold=0.30,
            corroborated_threshold=0.20,
            corroborated_margin=0.10,
            allowed_types=[("organization", "software")],
        )
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.ACCEPT_CORROBORATED
        assert decision.score == pytest.approx(0.281, abs=0.001)


# ---------------------------------------------------------------------------
# REVIEW_CONFLICT
# ---------------------------------------------------------------------------


class TestReviewConflict:
    def test_syntax_disagrees_with_relex(self):
        """Relex says uses but syntax says produces."""
        ev = _make_evidence(
            [("uses", 0.22), ("produces", 0.20)],
            syntax=[SyntaxEvidence("produces", "produce", "ACTIVE_SVO", 1.0)],
        )
        policy = _make_policy()
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.REVIEW_CONFLICT
        assert "produces" in decision.reasons

    def test_conflict_reasons_sorted_alphabetically(self):
        ev = _make_evidence(
            [("uses", 0.22)],
            syntax=[
                SyntaxEvidence("produces", "produce", "SVO_1", 1.0),
                SyntaxEvidence("depends_on", "depend", "SVO_2", 1.0),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CONFLICT
        reasons = decision.reasons
        # Both conflicting predicates appear, sorted
        assert "depends_on" in reasons
        assert "produces" in reasons
        assert reasons.index("depends_on") < reasons.index("produces")

    def test_high_score_with_disagreement_still_reviews(self):
        """High Relex score MUST NOT bypass strong deterministic disagreement.

        This is the corrected precedence: trusted conflict is evaluated
        BEFORE the standard acceptance gate. A high score that contradicts
        trusted syntax evidence routes to REVIEW_CONFLICT.
        """
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            syntax=[SyntaxEvidence("produces", "produce", "ACTIVE_SVO", 1.0)],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CONFLICT
        assert decision.score == pytest.approx(0.45)
        assert "produces" in decision.reasons

    def test_shadow_only_disagreement_does_not_block(self):
        """normalized_surface joins cannot trigger REVIEW_CONFLICT.

        Even when shadow-only syntax disagrees with Relex top-1, the gate
        does NOT route to REVIEW_CONFLICT. The Relex score governs.
        """
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            syntax=[
                SyntaxEvidence(
                    "produces", "produce", "SHADOW_SVO", 1.0,
                    join_mode="normalized_surface",
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.SHADOW_RELEX_HIGH

    def test_canonical_entity_disagreement_blocks(self):
        """canonical_entity join (trust 0.80) still triggers REVIEW_CONFLICT."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            syntax=[
                SyntaxEvidence(
                    "produces", "produce", "CANON_JOIN", 1.0,
                    join_mode="canonical_entity",
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CONFLICT

    def test_resolved_alias_disagreement_blocks(self):
        """resolved_alias join (trust 0.70) still triggers REVIEW_CONFLICT."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            syntax=[
                SyntaxEvidence(
                    "produces", "produce", "ALIAS_JOIN", 1.0,
                    join_mode="resolved_alias",
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CONFLICT


# ---------------------------------------------------------------------------
# Source family — DependencyMatcher and SVO are NOT independent
# ---------------------------------------------------------------------------


class TestSourceFamily:
    def test_default_source_family_is_spacy_dep(self):
        """SyntaxEvidence defaults to spacy_dependency_parse."""
        se = SyntaxEvidence("uses", "use", "P1", 1.0)
        assert se.source_family == "spacy_dependency_parse"

    def test_dep_matcher_and_svo_share_family(self):
        """Both default to the same family — not independent confirmations."""
        dep_matcher_ev = SyntaxEvidence("uses", "use", "DEP_PATH_1", 1.0)
        svo_ev = SyntaxEvidence("uses", "use", "NATIVE_SVO", 1.0)
        assert dep_matcher_ev.source_family == svo_ev.source_family


# ---------------------------------------------------------------------------
# REJECT_ENDPOINT_SIGNATURE
# ---------------------------------------------------------------------------


class TestRejectEndpointSignature:
    def test_type_mismatch_rejected(self):
        """Predicate endpoint signature doesn't include this pair."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            subject_type="concept",
            object_type="location",
        )
        policy = _make_policy(
            subject_types=["organization"],
            object_types=["software"],
        )
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.REJECT_ENDPOINT_SIGNATURE

    def test_unknown_type_not_rejected(self):
        """Empty types pass through — don't reject on missing type info.

        Without syntax the pair is shadowed (Relex-only precision unknown).
        """
        ev = _make_evidence(
            [("uses", 0.45)],
            subject_type="",
            object_type="",
        )
        policy = _make_policy(subject_types=["organization"], object_types=["software"])
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.SHADOW_RELEX_HIGH


# ---------------------------------------------------------------------------
# REJECT_NEGATED
# ---------------------------------------------------------------------------


class TestRejectNegated:
    def test_negated_syntax_rejects(self):
        ev = _make_evidence(
            [("uses", 0.40)],
            syntax=[SyntaxEvidence("uses", "use", "ACTIVE_SVO", 1.0, negated=True)],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_NEGATED
        assert "negated_relation_evidence" in decision.reasons


# ---------------------------------------------------------------------------
# REJECT_LOW_EVIDENCE
# ---------------------------------------------------------------------------


class TestRejectLowEvidence:
    def test_no_predicate_scores(self):
        ev = _make_evidence([])
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_LOW_EVIDENCE
        assert decision.predicate is None
        # With no Relex scores AND no trusted syntax, the syntax-only path
        # rejects with "no_trusted_syntax" (the no-scores case is now handled
        # by _evaluate_syntax_only instead of a direct REJECT_LOW_EVIDENCE).
        assert "no_trusted_syntax" in decision.reasons

    def test_score_too_low_no_syntax(self):
        ev = _make_evidence([("uses", 0.024), ("works_for", 0.000)])
        policy = _make_policy(corroborated_threshold=0.20)
        decision = evaluate_relation(ev, policy)
        assert decision.status == GateStatus.REJECT_LOW_EVIDENCE


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_inputs_identical_output(self):
        ev = _make_evidence([("uses", 0.28), ("produces", 0.02)])
        policy = _make_policy()
        d1 = evaluate_relation(ev, policy)
        d2 = evaluate_relation(ev, policy)
        assert d1 == d2

    def test_tie_break_alphabetical(self):
        """Same score for two predicates → alphabetical wins (deterministic)."""
        ev = _make_evidence([("produces", 0.30), ("uses", 0.30)])
        policy = _make_policy()
        decision = evaluate_relation(ev, policy)
        # "produces" < "uses" alphabetically → produces is top
        assert decision.predicate == "produces"

    def test_hash_stable_across_runs(self):
        """Frozen dataclass hash is stable."""
        ev = _make_evidence([("uses", 0.28)])
        d = evaluate_relation(ev, _make_policy())
        h1 = hash(d)
        d2 = evaluate_relation(ev, _make_policy())
        assert h1 == hash(d2)


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------


class TestPolicyLoading:
    def test_load_from_yaml(self):
        """Policy loads from the real config file."""
        reset_policy_cache()
        policy = load_policy()
        assert "uses" in policy
        assert "part_of" in policy
        assert "created_by" in policy

    def test_unknown_predicate_uses_defaults(self):
        """Predicate not in config → defaults apply, no type gate."""
        policy = load_policy()
        pp = policy["nonexistent_predicate"]
        assert pp.subject_types is None
        assert pp.object_types is None

    def test_per_predicate_override(self):
        """uses has corroborated_threshold 0.20, not default 0.25."""
        policy = load_policy()
        pp = policy["uses"]
        assert pp.corroborated_threshold == 0.20

    def test_part_of_lower_threshold(self):
        """part_of has corroborated_threshold 0.15 (allows ADHD recovery)."""
        policy = load_policy()
        pp = policy["part_of"]
        assert pp.corroborated_threshold == 0.15


# ---------------------------------------------------------------------------
# Direction margin
# ---------------------------------------------------------------------------


class TestDirectionMargin:
    def test_reverse_score_computes_direction(self):
        ev = _make_evidence(
            [("uses", 0.30), ("produces", 0.01)],
            reverse_scores={"uses": 0.005},
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.direction_margin == pytest.approx(0.295)


# ---------------------------------------------------------------------------
# Canonical entity IDs (extraction-boundary identity)
# ---------------------------------------------------------------------------


class TestCanonicalEntityIDs:
    """Evidence records should expose canonical entity IDs computed from the
    shared canonical module, so identity-based joins work at extraction time."""

    def test_canonical_subject_id_matches_graph_id(self):
        ev = _make_evidence([("uses", 0.30)])
        # entity_a → normalize → "entity_a" → slug → "entity_a" (underscore preserved)
        assert ev.canonical_subject_id == "entity:entity_a"

    def test_canonical_object_id_matches_graph_id(self):
        ev = _make_evidence([("uses", 0.30)])
        assert ev.canonical_object_id == "entity:entity_b"

    def test_same_surface_same_canonical_id_different_spans(self):
        """Two mentions of the same surface at different positions get the same ID."""
        ev_a = RelationEvidence(
            chunk_id="c1",
            subject_id="span:0:5", subject_text="CAPTCHA", subject_type="software",
            subject_start=0, subject_end=5,
            object_id="span:10:15", object_text="spam", object_type="concept",
            object_start=10, object_end=15,
            predicate_scores=(PredicateScore("detects", 0.5),),
            reverse_scores={},
        )
        ev_b = RelationEvidence(
            chunk_id="c1",
            subject_id="span:100:105", subject_text="CAPTCHA", subject_type="software",
            subject_start=100, subject_end=105,
            object_id="span:110:115", object_text="spam", object_type="concept",
            object_start=110, object_end=115,
            predicate_scores=(PredicateScore("detects", 0.3),),
            reverse_scores={},
        )
        assert ev_a.canonical_subject_id == ev_b.canonical_subject_id
        assert ev_a.canonical_object_id == ev_b.canonical_object_id
        assert ev_a.relation_key != ev_b.relation_key  # spans differ


# ---------------------------------------------------------------------------
# Syntax-only entry (no Relex predicate scores)
# ---------------------------------------------------------------------------


class TestSyntaxOnlyEntry:
    """Pairs that arrive from FrameExtractor without a matching Relex pair.

    These exercise the syntax → Relex entry direction added to the gate.
    """

    def test_syntax_only_with_known_predicate_accepted(self):
        """Empty predicate_scores, trusted syntax with canonical predicate → ACCEPT_SYNTAX_HIGH."""
        ev = _make_evidence(
            [],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_SYNTAX_HIGH
        assert decision.predicate == "uses"
        assert "syntax_only_deterministic" in decision.reasons
        assert "no_relex_evidence" in decision.reasons

    def test_syntax_only_with_unknown_predicate_stored(self):
        """Empty predicate_scores, trusted syntax with empty canonical, meaningful surface → STORE_UNMAPPED."""
        ev = _make_evidence(
            [],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="",
                    surface_predicate="serve in",
                    pattern_id="nsubj-VERB-prep-pobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                    subject_dependency_role="agent",
                    object_dependency_role="patient",
                    voice="active",
                    direction_source="DEPENDENCY_FRAME",
                    direction_confidence="high",
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.STORE_UNMAPPED_SURFACE_RELATION
        assert decision.predicate is None
        assert "no_semantically_adequate_canonical_mapping" in decision.reasons
        assert "serve in" in decision.reasons

    def test_syntax_only_shadow_join_rejected(self):
        """Syntax joined via normalized_surface only → REJECT_LOW_EVIDENCE (not enough trust)."""
        ev = _make_evidence(
            [],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.NORMALIZED_SURFACE.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_LOW_EVIDENCE
        assert "no_trusted_syntax" in decision.reasons

    def test_syntax_only_negated_rejected(self):
        """Negated syntax evidence with no Relex scores → REJECT_NEGATED."""
        ev = _make_evidence(
            [],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    negated=True,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_NEGATED
        assert decision.predicate is None


# ---------------------------------------------------------------------------
# STORE_UNMAPPED_SURFACE_RELATION
# ---------------------------------------------------------------------------


class TestStoreUnmappedSurfaceRelation:
    """Tests for the STORE_UNMAPPED_SURFACE_RELATION gate status.

    This status preserves grammatically licensed relations that have no
    adequate canonical predicate mapping. It does NOT fire for:
      - weak Relex scores (those are ACCEPT_CORROBORATED or REJECT_LOW_EVIDENCE)
      - canonical conflicts (those are REVIEW_CONFLICT)
      - mere co-presence (those are REJECT_LOW_EVIDENCE)
    """

    def test_unmapped_fires_when_no_canonical_mapping(self):
        """Syntax licensed a relation, surface meaningful, canonical absent → STORE_UNMAPPED."""
        ev = _make_evidence(
            [("uses", 0.01), ("related_to", 0.005)],  # Relex scores too low to accept
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="",
                    surface_predicate="manifest in",
                    pattern_id="nsubj-VERB-prep-pobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                    subject_dependency_role="agent",
                    object_dependency_role="patient",
                    voice="active",
                    direction_source="DEPENDENCY_FRAME",
                    direction_confidence="high",
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.STORE_UNMAPPED_SURFACE_RELATION
        assert "no_semantically_adequate_canonical_mapping" in decision.reasons
        assert "manifest in" in decision.reasons

    def test_unmapped_does_not_fire_for_canonical_conflict(self):
        """Both sources have predicates but disagree → REVIEW_CONFLICT, not STORE_UNMAPPED."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="supports",  # disagrees with Relex top-1 "uses"
                    surface_predicate="support",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CONFLICT
        assert decision.status != GateStatus.STORE_UNMAPPED_SURFACE_RELATION

    def test_unmapped_does_not_fire_for_weak_relex_with_syntax_agreement(self):
        """Relex low score but syntax agrees on predicate → ACCEPT_CORROBORATED, not STORE_UNMAPPED."""
        ev = _make_evidence(
            [("uses", 0.26), ("produces", 0.01)],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",  # agrees with Relex top-1
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_CORROBORATED
        assert decision.status != GateStatus.STORE_UNMAPPED_SURFACE_RELATION

    def test_unmapped_does_not_fire_for_co_presence(self):
        """Entities co-occur with Relex scores but no syntax evidence → REJECT_LOW_EVIDENCE."""
        ev = _make_evidence(
            [("uses", 0.10), ("related_to", 0.08)],  # below all thresholds
            syntax=[],  # no syntax evidence at all
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_LOW_EVIDENCE
        assert decision.status != GateStatus.STORE_UNMAPPED_SURFACE_RELATION


# ---------------------------------------------------------------------------
# Bidirectional union
# ---------------------------------------------------------------------------


class TestBidirectionalUnion:
    """Tests for the union of Relex-scored pairs and FrameExtractor triples."""

    def test_relex_pair_with_no_syntax_shadow(self):
        """Relex strong score, no syntax → SHADOW_RELEX_HIGH (precision unverified)."""
        ev = _make_evidence([("uses", 0.45), ("produces", 0.02)])
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.SHADOW_RELEX_HIGH
        assert decision.predicate == "uses"

    def test_syntax_triple_with_no_relex_pair_accepted(self):
        """Frame triple with known canonical predicate, no Relex → ACCEPT_SYNTAX_HIGH."""
        ev = _make_evidence(
            [],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_SYNTAX_HIGH
        assert decision.predicate == "uses"
        assert "syntax_only_deterministic" in decision.reasons

    def test_both_sources_agree_corroborated(self):
        """Both Relex and syntax present and agree on predicate → ACCEPT_CORROBORATED."""
        ev = _make_evidence(
            [("uses", 0.26), ("produces", 0.01)],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_CORROBORATED
        assert decision.predicate == "uses"
        assert "syntax_corroborated" in decision.reasons


# ---------------------------------------------------------------------------
# P4-lite: Cross-sentence containment
# ---------------------------------------------------------------------------


class TestCrossSentenceContainment:
    """P4-lite: cross-sentence pairs are contained by distance and link."""

    def test_same_sentence_unaffected(self):
        """Same-sentence pairs pass through the scope gate normally."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            same_sentence=True,
            sentence_distance=0,
        )
        decision = evaluate_relation(ev, _make_policy())
        # Should reach acceptance (shadow since no syntax)
        assert decision.status == GateStatus.SHADOW_RELEX_HIGH

    def test_adjacent_with_link_reviews(self):
        """Adjacent sentence + explicit typed link → REVIEW_CROSS_SENTENCE."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="exact_entity_repeat",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CROSS_SENTENCE
        assert "cross_sentence_adjacent_with_link" in decision.reasons
        assert "link_type=exact_entity_repeat" in decision.reasons
        assert "CROSS_SENTENCE_ADJACENT" in decision.blocking_reasons

    def test_adjacent_without_link_rejects(self):
        """Adjacent sentence without explicit link → REJECT_SCOPE."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=False,
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SCOPE
        assert "cross_sentence_no_link" in decision.reasons
        assert "SCOPE_REJECTED" in decision.blocking_reasons

    def test_distant_rejects(self):
        """Distance ≥ 2 → REJECT_SCOPE regardless of link."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=3,
            has_cross_sentence_link=True,  # link doesn't help at distance
            cross_sentence_link_type="exact_entity_repeat",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SCOPE
        assert "cross_sentence_distant_3" in decision.reasons

    def test_cross_sentence_with_syntax_bypasses_scope_gate(self):
        """Syntax evidence validates cross-sentence scope → normal gate."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            syntax=[SyntaxEvidence("uses", "use", "ACTIVE_SVO", 1.0)],
            same_sentence=False,
            sentence_distance=2,
            has_cross_sentence_link=False,
        )
        decision = evaluate_relation(ev, _make_policy())
        # Syntax bypasses scope gate; standard acceptance with syntax → ACCEPT_HIGH
        assert decision.status == GateStatus.ACCEPT_HIGH


# ---------------------------------------------------------------------------
# P4-lite hardening: typed link enforcement
# ---------------------------------------------------------------------------


class TestCrossSentenceLinkTypes:
    """Per-predicate allowed_links enforcement (P4-lite hardening)."""

    def test_disallowed_link_type_rejects_scope(self):
        """Link type not in predicate's allowed set → REJECT_SCOPE."""
        # created_by allows HEADING_CONTINUATION, LIST_CONTINUATION,
        # EXACT_ENTITY_REPEAT — but NOT VALIDATED_COREFERENCE.
        ev = _make_evidence(
            [("created_by", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="validated_coreference",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SCOPE
        assert any("link_type_not_allowed" in r for r in decision.reasons)

    def test_allowed_link_type_reviews(self):
        """Link type in predicate's allowed set AND implemented → REVIEW."""
        # created_by allows EXACT_ENTITY_REPEAT (and it's implemented).
        ev = _make_evidence(
            [("created_by", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="exact_entity_repeat",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CROSS_SENTENCE
        assert "link_type=exact_entity_repeat" in decision.reasons

    def test_none_link_type_rejects(self):
        """Boolean flag True but link_type='none' → REJECT_SCOPE."""
        ev = _make_evidence(
            [("uses", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="none",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SCOPE

    def test_located_in_allows_exact_repeat(self):
        """located_in + exact_entity_repeat → REVIEW_CROSS_SENTENCE."""
        ev = _make_evidence(
            [("located_in", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="exact_entity_repeat",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CROSS_SENTENCE

    def test_located_in_rejects_heading_continuation(self):
        """located_in does NOT allow heading_continuation → REJECT_SCOPE."""
        ev = _make_evidence(
            [("located_in", 0.45), ("produces", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="heading_continuation",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SCOPE

    def test_unlisted_predicate_uses_default_links(self):
        """Predicate not in PREDICATE_ALLOWED_LINKS uses default set.

        Default allows EXACT_ENTITY_REPEAT (the only implemented type).
        """
        ev = _make_evidence(
            [("produces", 0.45), ("uses", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="exact_entity_repeat",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_CROSS_SENTENCE

    def test_unimplemented_link_type_rejects(self):
        """Configured but unimplemented link type → REJECT_SCOPE.

        resolved_alias is in the default allowed set but NOT in
        IMPLEMENTED_LINK_TYPES, so it must not authorize review.
        """
        ev = _make_evidence(
            [("produces", 0.45), ("uses", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="resolved_alias",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SCOPE

    def test_unlisted_predicate_rejects_list_continuation(self):
        """Default set does NOT include LIST_CONTINUATION."""
        ev = _make_evidence(
            [("produces", 0.45), ("uses", 0.02)],
            same_sentence=False,
            sentence_distance=1,
            has_cross_sentence_link=True,
            cross_sentence_link_type="list_continuation",
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SCOPE


# ---------------------------------------------------------------------------
# Acceptance bug regression: zero-score corroboration + self-loop
# ---------------------------------------------------------------------------


class TestAcceptanceBugRegression:
    """Regression tests for the zero-score corroboration and self-loop bugs.

    A relation with zero Relex support cannot be labeled ACCEPT_CORROBORATED.
    A self-loop (subject canonical_id == object canonical_id) must be rejected.
    """

    def test_zero_relex_score_with_agreeing_syntax(self):
        """Relex score=0.0 + syntax agrees → NOT ACCEPT_CORROBORATED (score below floor)."""
        ev = _make_evidence(
            [("uses", 0.0), ("produces", 0.0)],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        # Score 0.0 is below corroborated_threshold (0.25) → not corroborated
        assert decision.status != GateStatus.ACCEPT_CORROBORATED
        assert decision.status != GateStatus.ACCEPT_HIGH

    def test_missing_relex_score_with_agreeing_syntax(self):
        """No Relex scores at all + syntax agrees → ACCEPT_SYNTAX_HIGH (not CORROBORATED)."""
        ev = _make_evidence(
            [],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="active_transitive:nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_SYNTAX_HIGH
        assert decision.status != GateStatus.ACCEPT_CORROBORATED
        assert decision.predicate == "uses"

    def test_positive_relex_score_with_agreeing_syntax(self):
        """Positive Relex score above corroborated floor + syntax agrees → ACCEPT_CORROBORATED."""
        ev = _make_evidence(
            [("uses", 0.26), ("produces", 0.01)],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_CORROBORATED
        assert decision.predicate == "uses"
        assert decision.score == 0.26

    def test_syntax_only_trusted_relation(self):
        """Trusted syntax-only with valid types and direction → ACCEPT_SYNTAX_HIGH."""
        ev = _make_evidence(
            [],
            subject_type="person",
            object_type="organization",
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="works_for",
                    surface_predicate="work for",
                    pattern_id="prep_object:nsubj-VERB-prep:for-pobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.ACCEPT_SYNTAX_HIGH
        assert decision.predicate == "works_for"

    def test_syntax_only_ambiguous_direction(self):
        """Syntax-only with unrecognized pattern → REVIEW_SYNTAX_ONLY."""
        ev = _make_evidence(
            [],
            syntax=[
                SyntaxEvidence(
                    canonical_predicate="uses",
                    surface_predicate="use",
                    pattern_id="passive_subject:nsubjpass-VERB",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ],
        )
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REVIEW_SYNTAX_ONLY
        assert "DIRECTION_UNVERIFIED" in decision.blocking_reasons

    def test_canonicalization_created_self_loop(self):
        """Subject and object with same text → same canonical ID → REJECT_SELF_LOOP."""
        ev = RelationEvidence(
            chunk_id="test_self",
            subject_id="span:0:20",
            subject_text="emotional investment",
            subject_type="concept",
            subject_start=0,
            subject_end=20,
            object_id="span:50:70",
            object_text="emotional investment",
            object_type="concept",
            object_start=50,
            object_end=70,
            predicate_scores=(PredicateScore("supports", 0.8),),
            reverse_scores={},
            syntax_evidence=(
                SyntaxEvidence(
                    canonical_predicate="supports",
                    surface_predicate="amplify",
                    pattern_id="nsubj-VERB-dobj",
                    confidence=1.0,
                    join_mode=JoinMode.EXACT_SPAN.value,
                ),
            ),
            same_sentence=True,
            sentence_distance=0,
            has_cross_sentence_link=False,
            cross_sentence_link_type="none",
        )
        # Verify canonical IDs collide
        assert ev.canonical_subject_id == ev.canonical_object_id
        decision = evaluate_relation(ev, _make_policy())
        assert decision.status == GateStatus.REJECT_SELF_LOOP
        assert "SELF_LOOP" in decision.blocking_reasons
