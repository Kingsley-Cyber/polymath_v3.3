"""Deterministic entity quality gate — behavior locks.

Every case below is a real mention taken from the judged gate samples, not a
hypothetical. MEASURED effect on HELD-OUT data (60 unseen surfaces):
    baseline precision 0.300 -> 0.625   recall 0.833   F1 0.714

The in-sample figure was 0.947, and the gap between the two IS the tuning that
went into the curated generic list. The held-out number is the honest one.

Portable: pure logic, no DB, no models.
"""

from __future__ import annotations

import pytest

from services.extraction.entity_quality import (
    LABEL_TO_ONTOLOGY, NOISE_LABELS, filter_entities, judge_entity,
    new_reject_counters,
)


class TestDeterminism:
    def test_same_input_always_same_verdict(self):
        """A production gate cannot be stochastic."""
        args = ("Amazon RDS", "PRODUCT")
        first = judge_entity(*args, confidence=0.54)
        for _ in range(50):
            v = judge_entity(*args, confidence=0.54)
            assert v.keep == first.keep
            assert v.ontology_type == first.ontology_type
            assert v.reason == first.reason

    def test_every_rejection_increments_a_named_counter(self):
        """Repo law: never drop silently."""
        c = new_reject_counters()
        for surface, etype, conf in [
            ("you", "PERSON", 0.9), ("good", "QUALITY", 0.9),
            ("Page 135", "DOCUMENT", 0.9), ("customer", "PERSON", 0.9),
            ("x", "CONCEPT", 0.9), ("This pillow", "PRODUCT", 0.9),
        ]:
            judge_entity(surface, etype, confidence=conf, counters=c)
        assert sum(c.values()) >= 6, "a rejection went uncounted"


class TestPronounsAndFunctionWords:
    @pytest.mark.parametrize("surface", ["I", "you", "we", "they", "me", "It",
                                         "this", "anyone", "there"])
    def test_pronouns_rejected(self, surface):
        """35 of 135 sampled mentions were PERSON-typed pronouns."""
        assert not judge_entity(surface, "PERSON", confidence=0.99).keep


class TestDocumentArtifacts:
    @pytest.mark.parametrize("surface", [
        "Page 135", "Page 522", "Figure 5.1", "Table 1", "Chapter 7",
        "ch04", "Practice 2", "Section 3",
    ])
    def test_locators_rejected(self, surface):
        assert not judge_entity(surface, "DOCUMENT", confidence=0.99).keep

    def test_machine_identifiers_rejected(self):
        """A raw epub filename was emitted as a SYSTEM entity."""
        assert not judge_entity(
            "Lume_9780307763662_epub_c05_r1", "SYSTEM", confidence=0.9
        ).keep


class TestNoiseLabels:
    @pytest.mark.parametrize("label", ["QUALITY", "GROUP", "PROCESS", "METRIC",
                                       "CONDITION", "INTERVENTION", "BEHAVIOR"])
    def test_ontological_categories_rejected(self, label):
        """Measured graph-worthiness ~0.00-0.11. These are categories, not types."""
        assert label in NOISE_LABELS
        assert not judge_entity("anything at all", label, confidence=0.99).keep

    def test_person_is_filtered_not_dropped(self):
        """PERSON scored 0.11 but is the RIGHT label for a real name."""
        assert "PERSON" not in NOISE_LABELS
        assert judge_entity("Glorianna Davenport", "PERSON", confidence=0.95).keep
        assert not judge_entity("customer", "PERSON", confidence=0.95).keep


class TestOrthographySplitConfidence:
    """A flat confidence floor destroyed 13 of 14 good entities."""

    @pytest.mark.parametrize("surface,conf", [
        ("AWS", 0.49), ("Amazon", 0.54), ("CloudFront", 0.40),
        ("CloudWatch", 0.48), ("Amazon RDS", 0.54), ("Andromeda", 0.40),
    ])
    def test_short_proper_nouns_survive_low_model_confidence(self, surface, conf):
        """GLiNER under-scores short proper nouns; orthography is independent
        evidence its score does not carry."""
        assert judge_entity(surface, "ORGANIZATION", confidence=conf).keep

    def test_low_confidence_common_nouns_still_rejected(self):
        assert not judge_entity("workload", "CONCEPT", confidence=0.42).keep


class TestNamedRequiredTypes:
    """A location that is not a name is a common noun wearing a label."""

    @pytest.mark.parametrize("surface", ["block", "campus", "flea market",
                                         "barren place", "stations", "desert"])
    def test_unnamed_places_rejected(self, surface):
        assert not judge_entity(surface, "PLACE", confidence=0.85).keep

    def test_named_places_kept(self):
        assert judge_entity("Los Angeles", "PLACE", confidence=0.90).keep


class TestDeictics:
    @pytest.mark.parametrize("surface", ["This pillow", "that company",
                                         "each customer", "such systems"])
    def test_deictic_references_rejected(self, surface):
        assert not judge_entity(surface, "PRODUCT", confidence=0.9).keep


class TestCorpusFrequency:
    """The general mechanism, so the curated list can stay short."""

    def test_high_corpus_frequency_rejects_a_generic(self):
        v = judge_entity("architecture", "CONCEPT", confidence=0.9,
                         corpus_doc_frequency=0.05, max_doc_frequency=0.002)
        assert not v.keep
        assert "corpus" in v.reason or "common noun" in v.reason

    def test_strong_names_are_exempt_from_frequency(self):
        """AWS is frequent in an AWS corpus and still a real entity."""
        assert judge_entity("AWS", "ORGANIZATION", confidence=0.9,
                            corpus_doc_frequency=0.9,
                            max_doc_frequency=0.002).keep

    def test_lone_capitalised_word_is_NOT_exempt(self):
        """Sentence-initial common nouns capitalise; 'Space' must not escape."""
        assert not judge_entity("Space", "CONCEPT", confidence=0.9,
                                corpus_doc_frequency=0.05,
                                max_doc_frequency=0.002).keep


class TestOntologyRemap:
    def test_types_are_mapped_onto_ontology(self):
        """100% of sampled mentions carried a type outside ontology.yaml,
        which made the allowed_pairs gate decorative."""
        assert judge_entity("Facebook", "ORGANIZATION",
                            confidence=0.9).ontology_type == "Organization"
        assert judge_entity("ChatGPT", "SYSTEM",
                            confidence=0.9).ontology_type == "Software"

    def test_every_mapped_type_targets_a_real_ontology_value(self):
        ontology = {
            "Person", "Organization", "Location", "Event", "Concept", "Method",
            "Product", "Software", "Document", "Standard", "Rule", "Law",
            "Artifact", "TimeReference", "other",
        }
        for src, dst in LABEL_TO_ONTOLOGY.items():
            assert dst in ontology, f"{src} maps to non-ontology {dst}"


class TestFilterEntities:
    def test_survivors_preserve_the_original_type(self):
        """The remap must be auditable, never lossy."""
        out = filter_entities([{
            "surface_form": "Facebook", "canonical_name": "facebook",
            "entity_type": "ORGANIZATION", "confidence": 0.9,
        }])
        assert len(out) == 1
        assert out[0]["entity_type"] == "Organization"
        assert out[0]["source_entity_type"] == "ORGANIZATION"
        assert out[0]["entity_quality_version"]

    def test_realistic_chunk_drops_the_noise_and_keeps_the_signal(self):
        ents = [
            {"surface_form": "I", "entity_type": "PERSON", "confidence": 0.98},
            {"surface_form": "good", "entity_type": "QUALITY", "confidence": 0.9},
            {"surface_form": "Page 12", "entity_type": "DOCUMENT", "confidence": 0.9},
            {"surface_form": "customer", "entity_type": "PERSON", "confidence": 0.9},
            {"surface_form": "AWS", "entity_type": "ORGANIZATION", "confidence": 0.49},
            {"surface_form": "Amazon RDS", "entity_type": "PRODUCT", "confidence": 0.54},
        ]
        out = filter_entities(ents)
        assert {e["surface_form"] for e in out} == {"AWS", "Amazon RDS"}
