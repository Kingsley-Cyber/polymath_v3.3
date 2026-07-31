"""The control plane must know extraction is four accountable organs.

Before this, `compile_document_contract` declared extraction as ONE boolean, so
a document could be certified "extracted" while relations, facts and facets all
produced nothing — which is precisely what happened for 362,142 chunks.

Portable: pure logic, no DB, no models.
"""

from __future__ import annotations

from services.control_plane.extraction_organs import (
    LANE_LOCAL, LANE_POD, ORGAN_BY_NAME, ORGAN_SPECS,
    compile_organ_contract, lane_coverage_gaps, organs_expected_for_lane,
)


class TestEveryStageIsAccountable:
    def test_every_organ_names_a_concrete_repair_route(self):
        """A stage that cannot name its repair is a wish, not a stage."""
        for spec in ORGAN_SPECS:
            assert spec.repair_route.strip(), f"{spec.organ} has no repair route"
            assert len(spec.repair_route) > 12, (
                f"{spec.organ} repair route is too vague: {spec.repair_route!r}"
            )

    def test_every_organ_declares_an_owner_lane(self):
        for spec in ORGAN_SPECS:
            assert spec.produced_by, f"{spec.organ} has no owning lane"

    def test_every_organ_declares_its_repair_cost(self):
        """needs_model_pass separates a free recompute from a GPU run."""
        for spec in ORGAN_SPECS:
            assert isinstance(spec.needs_model_pass, bool)

    def test_free_recompute_organs_are_the_ones_we_backfilled_cheaply(self):
        assert ORGAN_BY_NAME["relations"].needs_model_pass is False
        assert ORGAN_BY_NAME["facts"].needs_model_pass is False
        # Facets genuinely need a model pass — that is why it is still open.
        assert ORGAN_BY_NAME["facets"].needs_model_pass is True


class TestLaneAsymmetryIsExplicit:
    """Neither lane runs all four organs. That must be declared, not folklore."""

    def test_pod_lane_does_not_produce_facets(self):
        assert "facets" not in organs_expected_for_lane(LANE_POD)

    def test_local_lane_does_not_produce_claims(self):
        assert "claims" not in organs_expected_for_lane(LANE_LOCAL)

    def test_the_asymmetry_is_reported_both_ways(self):
        gaps = lane_coverage_gaps()
        assert "facets" in gaps[LANE_POD]
        assert "claims" in gaps[LANE_LOCAL]

    def test_neither_lane_covers_everything(self):
        all_organs = {s.organ for s in ORGAN_SPECS}
        assert set(organs_expected_for_lane(LANE_POD)) != all_organs
        assert set(organs_expected_for_lane(LANE_LOCAL)) != all_organs


class TestContractShape:
    def test_pod_contract_marks_facets_not_expected(self):
        c = compile_organ_contract(LANE_POD)
        assert c["organs"]["facets"]["expected"] is False
        assert c["organs"]["facets"]["required"] is False
        assert "facets" in c["not_produced_by_this_lane"]

    def test_pod_contract_still_requires_relations_and_facts(self):
        """The organs that were hardcoded empty must be REQUIRED on this lane."""
        c = compile_organ_contract(LANE_POD)
        assert c["organs"]["relations"]["required"] is True
        assert c["organs"]["facts"]["required"] is True

    def test_local_contract_requires_facets_path_but_not_claims(self):
        c = compile_organ_contract(LANE_LOCAL)
        assert c["organs"]["facets"]["expected"] is True
        assert c["organs"]["claims"]["expected"] is False

    def test_contract_carries_repair_routes_for_the_reconciler(self):
        c = compile_organ_contract(LANE_POD)
        for organ, decl in c["organs"].items():
            assert decl["repair_route"], f"{organ} lost its repair route"

    def test_contract_is_versioned(self):
        assert compile_organ_contract(LANE_POD)["organ_contract_version"]


def test_document_contract_now_embeds_the_organ_contract():
    """The single extraction_required boolean is no longer the whole story."""
    from services.control_plane.desired_state import compile_document_contract

    # local_extraction_v1 is only a valid wire contract alongside
    # extraction_engine=runpod_flash — the IngestionConfig validator enforces
    # that pairing, so the fixture has to satisfy it.
    doc = {"ingestion_config": {
        "runpod_wire_contract": "local_extraction_v1",
        "extraction_engine": "runpod_flash",
        "runpod_endpoint_id_override": "ep-test",
        "runpod_account_name_override": "acct-test",
    }}
    contract = compile_document_contract(doc, {})
    assert "organ_contract" in contract, (
        "document contract still declares extraction as one opaque step"
    )
    assert contract["organ_contract"]["lane"] == LANE_POD
    assert contract["organ_contract"]["organs"]["facets"]["expected"] is False
