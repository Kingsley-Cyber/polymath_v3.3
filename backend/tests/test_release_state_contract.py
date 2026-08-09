"""Contract tests for models/release_state.py (Slice 1 scaffolding).

Encodes the owner-mandated release policy: categorical state only,
deny-by-default graph writes, route- and artifact-aware readiness (never a
blanket hard gate, never purely advisory).
"""

import pytest

from models.release_state import (
    ROUTE_ARTIFACT_REQUIREMENTS,
    ReadinessDecision,
    ReleasePin,
    blocked_no_release_state,
    graph_write_allowed,
    missing_release_conditions,
)


def _fully_qualified_release() -> ReleasePin:
    return ReleasePin(
        release_id="extraction-core-v1.0.0",
        engineering_freeze="passed",
        recoverability="passed",
        git_reproducibility="passed",
        closed_world_annotation="passed",
        calibration="passed",
        held_out_qualification="passed",
        graph_write_promotion="passed",
        extractor_hash_matches=True,
        ontology_hash_matches=True,
        acceptance_policy_hash_matches=True,
        schema_hash_matches=True,
    )


# ---------------------------------------------------------------------------
# graph_write_allowed — deny by default
# ---------------------------------------------------------------------------


def test_graph_write_denied_without_release_bundle():
    assert graph_write_allowed(None) is False


def test_graph_write_denied_for_default_pending_release():
    assert graph_write_allowed(ReleasePin(release_id="r")) is False


def test_graph_write_allowed_only_when_every_condition_passes():
    assert graph_write_allowed(_fully_qualified_release()) is True


@pytest.mark.parametrize(
    "field",
    [
        "engineering_freeze",
        "recoverability",
        "git_reproducibility",
        "closed_world_annotation",
        "calibration",
        "held_out_qualification",
        "graph_write_promotion",
    ],
)
def test_single_pending_categorical_state_denies_writes(field):
    release = _fully_qualified_release()
    degraded = release.model_copy(update={field: "pending"})
    assert graph_write_allowed(degraded) is False
    assert field in missing_release_conditions(degraded)


@pytest.mark.parametrize(
    "field",
    [
        "extractor_hash_matches",
        "ontology_hash_matches",
        "acceptance_policy_hash_matches",
        "schema_hash_matches",
    ],
)
def test_single_hash_mismatch_denies_writes(field):
    release = _fully_qualified_release()
    degraded = release.model_copy(update={field: False})
    assert graph_write_allowed(degraded) is False
    assert field in missing_release_conditions(degraded)


def test_blocked_no_release_payload_shape():
    release = _fully_qualified_release().model_copy(
        update={
            "held_out_qualification": "pending",
            "graph_write_promotion": "pending",
        }
    )
    state = blocked_no_release_state(release)
    assert state["state"] == "blocked_no_release"
    assert state["retryable"] is True
    assert "held_out_qualification" in state["missing_conditions"]
    assert "graph_write_promotion" in state["missing_conditions"]
    # Owner-frozen shape: denial happens BEFORE any write is tried, and no
    # registry entry exists until the active release registry lands.
    assert state["write_attempted"] is False
    assert state["release_registry_entry"] is None
    assert set(state) == {
        "state",
        "retryable",
        "write_attempted",
        "missing_conditions",
        "release_registry_entry",
    }


# ---------------------------------------------------------------------------
# ReadinessDecision — route- and artifact-aware
# ---------------------------------------------------------------------------


def test_ingest_inspection_always_allowed():
    decision = ReadinessDecision.decide(
        corpus_id="c1", route="ingest_inspection", present_artifacts=set()
    )
    assert decision.allowed is True
    assert decision.mode == "full"
    assert decision.missing_artifacts == ()


def test_entity_lookup_requires_lexicon_only():
    blocked = ReadinessDecision.decide(
        corpus_id="c1", route="entity_lookup", present_artifacts=set()
    )
    assert blocked.allowed is False
    assert blocked.mode == "blocked"
    assert blocked.missing_artifacts == ("entity_lexicon",)

    ready = ReadinessDecision.decide(
        corpus_id="c1", route="entity_lookup", present_artifacts={"entity_lexicon"}
    )
    assert ready.allowed is True
    assert ready.mode == "full"


def test_partial_mode_preserves_legitimate_partial_access():
    # Corpus has child vectors but no parents yet: hybrid search is partial,
    # entity lookup on the same corpus is still fully allowed.
    present = {"entity_lexicon", "child_vectors"}
    hybrid = ReadinessDecision.decide(
        corpus_id="c1", route="hybrid_search", present_artifacts=present
    )
    assert hybrid.allowed is False
    assert hybrid.mode == "partial"
    assert "parent_records" in hybrid.missing_artifacts

    lookup = ReadinessDecision.decide(
        corpus_id="c1", route="entity_lookup", present_artifacts=present
    )
    assert lookup.allowed is True


def test_graph_write_route_requires_full_release_not_only_artifacts():
    # Even with every artifact present, the categorical release chain gates.
    artifacts = {"extraction_qualification", "graph_write_promotion"}
    denied = ReadinessDecision.decide(
        corpus_id="c1", route="graph_write", present_artifacts=artifacts
    )
    assert denied.allowed is False
    assert "release_bundle_absent" in denied.missing_artifacts

    allowed = ReadinessDecision.decide(
        corpus_id="c1",
        route="graph_write",
        present_artifacts=artifacts,
        release=_fully_qualified_release(),
    )
    assert allowed.allowed is True
    assert allowed.mode == "full"


def test_unknown_route_is_blocked_not_crashed():
    decision = ReadinessDecision.decide(
        corpus_id="c1", route="teleport", present_artifacts={"everything"}
    )
    assert decision.allowed is False
    assert decision.mode == "blocked"
    assert decision.reasons == ("unknown_route",)


def test_decision_carries_certificate_and_release_pins():
    decision = ReadinessDecision.decide(
        corpus_id="c1",
        route="entity_lookup",
        present_artifacts={"entity_lexicon"},
        certificate_id="cert-1",
        release_pins={"extractor_release": "extraction-core-v1.0.0"},
    )
    assert decision.certificate_id == "cert-1"
    assert decision.release_pins["extractor_release"] == "extraction-core-v1.0.0"


def test_route_gate_table_covers_all_documented_operations():
    expected_routes = {
        "ingest_inspection",
        "extraction_inspection",
        "entity_lookup",
        "vector_search",
        "hybrid_search",
        "curated_chat",
        "graph_read",
        "graph_write",
        "ontology_admin_write",
    }
    assert set(ROUTE_ARTIFACT_REQUIREMENTS) == expected_routes
