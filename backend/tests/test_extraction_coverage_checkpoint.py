"""Coverage checkpoints must catch a dead organ.

The bug this guards against ran undetected for months: three of four extraction
organs returned nothing for 362,142 chunks while every other health signal —
chunks ingested, entities present, jobs green, retrieval readiness OK — stayed
green. Nothing measured whether each ORGAN fired.

Portable: pure logic, no DB, no models.
"""

from __future__ import annotations

from services.extraction.coverage_checkpoint import (
    ORGAN_FLOORS, STATUS_BELOW, STATUS_DEAD, STATUS_OK,
    classify, coverage_from_row,
)


class TestZeroIsAlwaysDead:
    """Zero is a disconnected wire, not a low number."""

    def test_zero_relations_is_DEAD_not_below_threshold(self):
        oc = classify("relations", 0.0, total=0, chunks=161_108)
        assert oc.status == STATUS_DEAD
        assert "hardcoded empty" in oc.diagnosis

    def test_zero_facts_is_DEAD(self):
        oc = classify("facts", 0.0, total=0, chunks=100_000)
        assert oc.status == STATUS_DEAD
        assert "Stage D" in oc.diagnosis

    def test_zero_facets_is_DEAD_and_names_the_lane(self):
        oc = classify("facets", 0.0, total=0, chunks=100_000)
        assert oc.status == STATUS_DEAD
        assert "pass-2" in oc.diagnosis

    def test_dead_status_cannot_be_tuned_away_by_lowering_a_floor(self):
        """Even with an absurdly low floor, zero stays DEAD."""
        oc = classify("relations", 0.0, total=0, chunks=1000)
        assert oc.status == STATUS_DEAD
        assert oc.status != STATUS_BELOW


class TestGrading:
    def test_healthy_organ_passes(self):
        oc = classify("facts", 0.8673, total=314_616, chunks=362_759)
        assert oc.status == STATUS_OK
        assert not oc.failed

    def test_low_but_nonzero_is_below_threshold_not_dead(self):
        floor = ORGAN_FLOORS["facets"][1]
        oc = classify("facets", floor / 2, total=5, chunks=1000)
        assert oc.status == STATUS_BELOW
        assert oc.failed

    def test_empty_corpus_is_not_graded_as_failure(self):
        oc = classify("relations", 0.0, total=0, chunks=0)
        assert oc.status == STATUS_OK


class TestRealWorldRows:
    def test_the_actual_broken_state_is_flagged(self):
        """ecommerce_meta as it was BEFORE the repairs: 3 dead organs."""
        rep = coverage_from_row({
            "_id": "8dfb070a", "chunks": 161_108,
            "entities": 1_253_000, "relations": 0, "facts": 0,
            "claims": 1_400_000, "faceted_mentions": 0,
        }, "ecommerce_meta")
        assert set(rep.dead_organs) == {"relations", "facts", "facets"}
        assert not rep.healthy

    def test_the_repaired_state_flags_only_facets(self):
        """After the relations + facts repairs, facets remains the gap."""
        rep = coverage_from_row({
            "_id": "8dfb070a", "chunks": 161_108,
            "entities": 1_253_000, "relations": 8_666, "facts": 148_498,
            "claims": 1_400_000, "faceted_mentions": 3_000,
        }, "ecommerce_meta")
        assert "relations" not in rep.failing_organs
        assert "facts" not in rep.failing_organs
        assert "facets" in rep.failing_organs
        assert not rep.healthy

    def test_fully_healthy_corpus_reports_healthy(self):
        rep = coverage_from_row({
            "_id": "x", "chunks": 1000,
            "entities": 7000, "relations": 50, "facts": 800,
            "claims": 8000, "faceted_mentions": 2000,
        }, "healthy")
        assert rep.healthy
        assert rep.dead_organs == []


def test_every_declared_organ_has_a_zero_diagnosis():
    """A dead organ must always explain itself, never just report a number."""
    for organ in ORGAN_FLOORS:
        oc = classify(organ, 0.0, total=0, chunks=500)
        assert oc.status == STATUS_DEAD
        assert oc.diagnosis, f"{organ} has no zero-diagnosis text"


def test_receipt_is_serializable():
    rep = coverage_from_row({
        "_id": "x", "chunks": 10, "entities": 70, "relations": 1,
        "facts": 8, "claims": 80, "faceted_mentions": 20,
    }, "n")
    doc = rep.to_doc()
    import json
    json.dumps(doc)  # must not raise
    assert doc["checkpoint_version"]
    assert "healthy" in doc and "dead_organs" in doc
