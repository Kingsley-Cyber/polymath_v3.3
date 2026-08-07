"""Adversarial conjunction-resolution regression tests.

Validates that the FrameExtractor handles coordinated argument structures
correctly: expanding shared subjects/objects within a clause, but NOT
performing Cartesian expansion across independent clause boundaries.

Fixtures: tests/extraction/fixtures/conjunction_adversarial.yaml
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from services.extraction.dep_path_extractor import EntitySpan
from services.extraction.frame_extractor import FrameExtractor

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "conjunction_adversarial.yaml"


def _load_fixtures() -> list[dict]:
    with open(FIXTURE_PATH) as f:
        data = yaml.safe_load(f)
    return data["fixtures"]


FIXTURES = _load_fixtures()


@pytest.fixture(scope="module")
def extractor() -> FrameExtractor:
    return FrameExtractor()


def _extract(extractor: FrameExtractor, fixture: dict) -> list[tuple[str, str, str]]:
    """Run extraction and return (subject, predicate_lemma, object) tuples."""
    entities = [
        EntitySpan(
            surface=e["surface"],
            start_char=e["start"],
            end_char=e["end"],
            entity_type=e["type"],
        )
        for e in fixture["entities"]
    ]
    triples = extractor.extract(
        fixture["sentence"],
        entities,
        chunk_id=fixture["id"],
    )
    return [
        (t.subject_surface, t.predicate_lemma, t.object_surface)
        for t in triples
    ]


class TestConjunctionExpansion:
    """Shared-subject and shared-object conjunctions expand correctly."""

    @pytest.mark.parametrize(
        "fixture",
        [f for f in FIXTURES if f["id"].startswith("conj_shared")],
        ids=[f["id"] for f in FIXTURES if f["id"].startswith("conj_shared")],
    )
    def test_expected_triples_present(self, extractor, fixture):
        """All expected triples must be extracted."""
        results = _extract(extractor, fixture)
        for expected in fixture["expected_triples"]:
            subj, pred, obj = expected
            found = any(
                subj.lower() in r[0].lower()
                and pred.lower() in r[1].lower()
                and obj.lower() in r[2].lower()
                for r in results
            )
            assert found, (
                f"{fixture['id']}: expected ({subj}, {pred}, {obj}) "
                f"not found in {results}"
            )


class TestConjunctionContainment:
    """Independent clauses and parentheticals do NOT cross-pollinate."""

    @pytest.mark.parametrize(
        "fixture",
        [f for f in FIXTURES if f.get("forbidden_triples")],
        ids=[f["id"] for f in FIXTURES if f.get("forbidden_triples")],
    )
    def test_forbidden_triples_absent(self, extractor, fixture):
        """Forbidden cross-clause triples must NOT appear."""
        results = _extract(extractor, fixture)
        for forbidden in fixture["forbidden_triples"]:
            subj, pred, obj = forbidden
            found = any(
                subj.lower() in r[0].lower()
                and pred.lower() in r[1].lower()
                and obj.lower() in r[2].lower()
                for r in results
            )
            assert not found, (
                f"{fixture['id']}: forbidden ({subj}, {pred}, {obj}) "
                f"was extracted — Cartesian expansion across clause boundary. "
                f"All results: {results}"
            )


class TestConjunctionMetadata:
    """Direction metadata is populated for conjunction-expanded triples."""

    @pytest.mark.parametrize("fixture", FIXTURES, ids=[f["id"] for f in FIXTURES])
    def test_metadata_populated(self, extractor, fixture):
        """Every extracted triple must have non-empty direction metadata."""
        entities = [
            EntitySpan(
                surface=e["surface"],
                start_char=e["start"],
                end_char=e["end"],
                entity_type=e["type"],
            )
            for e in fixture["entities"]
        ]
        triples = extractor.extract(
            fixture["sentence"],
            entities,
            chunk_id=fixture["id"],
        )
        for t in triples:
            assert t.subject_dependency_role, (
                f"{fixture['id']}: subject_dependency_role is empty for "
                f"({t.subject_surface}, {t.predicate_lemma}, {t.object_surface})"
            )
            assert t.object_dependency_role, (
                f"{fixture['id']}: object_dependency_role is empty for "
                f"({t.subject_surface}, {t.predicate_lemma}, {t.object_surface})"
            )
            assert t.direction_confidence, (
                f"{fixture['id']}: direction_confidence is empty for "
                f"({t.subject_surface}, {t.predicate_lemma}, {t.object_surface})"
            )
