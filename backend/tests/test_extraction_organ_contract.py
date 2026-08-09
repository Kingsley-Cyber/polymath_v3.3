from services.control_plane.extraction_organs import (
    LANE_GRAPHIFY,
    ORGAN_BY_NAME,
    ORGAN_SPECS,
    compile_organ_contract,
    lane_coverage_gaps,
    organs_expected_for_lane,
)


def test_every_organ_has_a_concrete_repair_route():
    assert ORGAN_SPECS
    assert all(spec.repair_route.strip() for spec in ORGAN_SPECS)


def test_graphify_owns_entities_and_relations_only():
    assert set(organs_expected_for_lane(LANE_GRAPHIFY)) == {"entities", "relations"}


def test_facts_and_claims_are_not_falsely_claimed_by_graphify():
    assert ORGAN_BY_NAME["facts"].produced_by == ()
    assert ORGAN_BY_NAME["claims"].produced_by == ()
    assert not ORGAN_BY_NAME["facts"].required
    assert not ORGAN_BY_NAME["claims"].required


def test_contract_is_lane_specific_and_actionable():
    contract = compile_organ_contract(LANE_GRAPHIFY)
    assert contract["lane"] == LANE_GRAPHIFY
    assert contract["organs"]["entities"]["required"] is True
    assert contract["organs"]["relations"]["required"] is True
    assert contract["organs"]["facts"]["expected"] is False
    assert contract["organs"]["claims"]["expected"] is False


def test_lane_gap_report_names_every_unowned_organ():
    gaps = lane_coverage_gaps()
    assert set(gaps) == {LANE_GRAPHIFY}
    assert set(gaps[LANE_GRAPHIFY]) == {"facets", "facts", "claims"}
