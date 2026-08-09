"""E — Verb-preposition fixture regression tests.

Validates each p2_verb_prep resolver rule via the frame extractor against
adjudicated positive and adversarial-negative fixtures.

Rules tested:
  work + for   → works_for
  serve + in   → member_of
  base + in    → located_in
  locate + in  → located_in  (T3 synonym)
  member + of  → member_of   (copular_prep frame)
  part + of    → part_of     (copular_prep frame)

Fixtures: tests/extraction/fixtures/verb_prep_adjudicated.yaml

Causal assertion: E − D → only feature_group=p2_verb_prep records.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from services.extraction.dep_path_extractor import EntitySpan
from services.extraction.frame_extractor import FrameExtractor

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "verb_prep_adjudicated.yaml"

# The canonical predicates that p2_verb_prep rules can produce.
_P2_PREDICATES = frozenset({"works_for", "member_of", "located_in", "part_of"})


def _load_fixtures() -> list[dict]:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data["fixtures"]


FIXTURES = _load_fixtures()
POSITIVE = [f for f in FIXTURES if f.get("expected_predicate")]
NEGATIVE = [f for f in FIXTURES if not f.get("expected_predicate")]

# Group positives by rule for per-rule reporting.
RULES = ("works_for", "member_of", "located_in", "part_of")
POSITIVE_BY_RULE = {r: [f for f in POSITIVE if f["rule"] == r] for r in RULES}
NEGATIVE_BY_RULE = {r: [f for f in NEGATIVE if f["rule"] == r] for r in RULES}


@pytest.fixture(scope="module")
def extractor():
    return FrameExtractor()


def _run_fixture(extractor: FrameExtractor, fixture: dict) -> list:
    """Run the FrameExtractor on a fixture and return all triples."""
    entities = [
        EntitySpan(
            surface=e["text"],
            start_char=e["start"],
            end_char=e["end"],
            entity_type=e["type"],
        )
        for e in fixture["entities"]
    ]
    return extractor.extract(fixture["text"], entities, chunk_id=fixture["id"])


# ---------------------------------------------------------------------------
# Positive tests: correct pair recovery + canonical predicate
# ---------------------------------------------------------------------------


class TestVerbPrepPositives:
    """Positive cases must extract with correct canonical predicate."""

    @pytest.mark.parametrize(
        "fixture", POSITIVE, ids=[f["id"] for f in POSITIVE]
    )
    def test_canonical_predicate_recovered(self, extractor, fixture):
        """The expected canonical predicate must appear in the output."""
        triples = _run_fixture(extractor, fixture)
        expected = fixture["expected_predicate"]
        matching = [t for t in triples if t.predicate == expected]
        assert matching, (
            f"{fixture['id']}: expected predicate={expected}, "
            f"got {[(t.subject_surface, t.predicate, t.object_surface) for t in triples]}"
        )

    @pytest.mark.parametrize(
        "fixture", POSITIVE, ids=[f["id"] for f in POSITIVE]
    )
    def test_correct_subject_object_pair(self, extractor, fixture):
        """The expected subject→object pair must be recovered."""
        triples = _run_fixture(extractor, fixture)
        expected_pred = fixture["expected_predicate"]
        expected_subj = fixture["expected_subject"]
        expected_obj = fixture["expected_object"]
        matching = [
            t for t in triples
            if t.predicate == expected_pred
            and t.subject_surface == expected_subj
            and t.object_surface == expected_obj
        ]
        assert matching, (
            f"{fixture['id']}: expected ({expected_subj}, {expected_pred}, "
            f"{expected_obj}), got "
            f"{[(t.subject_surface, t.predicate, t.object_surface) for t in triples]}"
        )

    @pytest.mark.parametrize(
        "fixture", POSITIVE, ids=[f["id"] for f in POSITIVE]
    )
    def test_no_unrelated_predicates(self, extractor, fixture):
        """Positives should not emit unrelated canonical predicates."""
        triples = _run_fixture(extractor, fixture)
        expected_pred = fixture["expected_predicate"]
        # Allow the expected predicate and open relations (None).
        unexpected = [
            t for t in triples
            if t.predicate is not None and t.predicate != expected_pred
        ]
        assert not unexpected, (
            f"{fixture['id']}: unexpected predicates "
            f"{[(t.subject_surface, t.predicate, t.object_surface) for t in unexpected]}"
        )


# ---------------------------------------------------------------------------
# Negative tests: hard-negative rejection
# ---------------------------------------------------------------------------


class TestVerbPrepNegatives:
    """Adversarial negatives must NOT produce a p2_verb_prep canonical predicate."""

    @pytest.mark.parametrize(
        "fixture", NEGATIVE, ids=[f["id"] for f in NEGATIVE]
    )
    def test_no_p2_canonical_predicate(self, extractor, fixture):
        """No p2_verb_prep canonical predicate may be emitted."""
        triples = _run_fixture(extractor, fixture)
        false_positives = [
            t for t in triples if t.predicate in _P2_PREDICATES
        ]
        assert not false_positives, (
            f"{fixture['id']}: false positive p2_verb_prep mapping: "
            f"{[(t.subject_surface, t.predicate, t.object_surface) for t in false_positives]}. "
            f"Reason: {fixture.get('reason', 'N/A')}"
        )


# ---------------------------------------------------------------------------
# Per-rule measurement summary
# ---------------------------------------------------------------------------


class TestVerbPrepMeasurements:
    """Aggregate measurements per rule (required by the promotion gate)."""

    @pytest.mark.parametrize("rule", RULES)
    def test_positive_count_meets_minimum(self, rule):
        """Each rule must have at least 10 positive fixtures."""
        count = len(POSITIVE_BY_RULE[rule])
        assert count >= 10, (
            f"Rule {rule}: only {count} positives (minimum 10)"
        )

    @pytest.mark.parametrize("rule", RULES)
    def test_negative_count_meets_minimum(self, rule):
        """Each rule must have at least 5 adversarial negatives."""
        count = len(NEGATIVE_BY_RULE[rule])
        assert count >= 5, (
            f"Rule {rule}: only {count} negatives (minimum 5)"
        )

    @pytest.mark.parametrize("rule", RULES)
    def test_all_positives_recovered(self, extractor, rule):
        """100% pair recovery for positive fixtures of this rule."""
        fixtures = POSITIVE_BY_RULE[rule]
        failures = []
        for fixture in fixtures:
            triples = _run_fixture(extractor, fixture)
            matching = [
                t for t in triples
                if t.predicate == fixture["expected_predicate"]
                and t.subject_surface == fixture["expected_subject"]
                and t.object_surface == fixture["expected_object"]
            ]
            if not matching:
                failures.append(fixture["id"])
        assert not failures, (
            f"Rule {rule}: {len(failures)}/{len(fixtures)} positives failed: "
            f"{failures}"
        )

    @pytest.mark.parametrize("rule", RULES)
    def test_all_negatives_rejected(self, extractor, rule):
        """0 false positives from adversarial negatives of this rule."""
        fixtures = NEGATIVE_BY_RULE[rule]
        false_positives = []
        for fixture in fixtures:
            triples = _run_fixture(extractor, fixture)
            bad = [t for t in triples if t.predicate in _P2_PREDICATES]
            if bad:
                false_positives.append(fixture["id"])
        assert not false_positives, (
            f"Rule {rule}: {len(false_positives)} false positives: "
            f"{false_positives}"
        )

    def test_latency_delta(self, extractor):
        """p2_verb_prep rules must not add > 50ms per sentence."""
        texts = [f["text"] for f in FIXTURES[:20]]
        entities = [
            [
                EntitySpan(
                    surface=e["text"],
                    start_char=e["start"],
                    end_char=e["end"],
                    entity_type=e["type"],
                )
                for e in f["entities"]
            ]
            for f in FIXTURES[:20]
        ]
        start = time.perf_counter()
        for text, ents in zip(texts, entities):
            extractor.extract(text, ents, chunk_id="latency")
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_sentence = elapsed_ms / len(texts)
        assert per_sentence < 50, (
            f"Latency {per_sentence:.1f}ms/sentence exceeds 50ms threshold"
        )


# ---------------------------------------------------------------------------
# Causal assertion: E − D → only p2_verb_prep records
# ---------------------------------------------------------------------------


class TestCausalIsolation:
    """Fixtures must not cause unrelated predicate changes."""

    def test_no_new_relex_pairs(self, extractor):
        """No fixture should produce a Relex-sourced canonical predicate
        that is NOT in the p2_verb_prep set."""
        non_p2_canonical = []
        for fixture in FIXTURES:
            triples = _run_fixture(extractor, fixture)
            for t in triples:
                if t.predicate is not None and t.predicate not in _P2_PREDICATES:
                    non_p2_canonical.append(
                        (fixture["id"], t.subject_surface, t.predicate, t.object_surface)
                    )
        assert not non_p2_canonical, (
            f"Non-p2_verb_prep canonical predicates emitted: {non_p2_canonical}"
        )

    def test_no_cross_sentence_acceptance(self, extractor):
        """No fixture should produce a triple from a different sentence."""
        for fixture in FIXTURES:
            triples = _run_fixture(extractor, fixture)
            for t in triples:
                assert t.sentence_idx == 0, (
                    f"{fixture['id']}: triple from sentence {t.sentence_idx}, "
                    f"expected 0 (single-sentence fixture)"
                )

    def test_no_graph_eligibility_for_ambiguous(self, extractor):
        """Open relations (predicate=None) must not be graph-eligible."""
        for fixture in FIXTURES:
            triples = _run_fixture(extractor, fixture)
            for t in triples:
                if t.predicate is None:
                    assert t.graph_eligible is False, (
                        f"{fixture['id']}: open relation "
                        f"({t.subject_surface}, {t.predicate_lemma}, "
                        f"{t.object_surface}) is graph_eligible"
                    )
