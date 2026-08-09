from backend.tests.extraction.test_graphify_predicate_compiler import _case
from models.graphify_contracts import AdaptedOpenIEArgumentV1, EntityTerminalState, OpenIEArgumentKind
from services.extraction.graphify_assertion_assembler import assemble_openie_assertions


def _args(candidate):
    output = []
    for role, argument_id, entity_id in (
        ("subject", candidate.subject_argument_id, "e1"),
        ("object", candidate.object_argument_id, "e2"),
    ):
        output.append(AdaptedOpenIEArgumentV1(
            argument_id=argument_id, proposition_id="p1", document_id="d1", unit_id="u1",
            role=role, surface=entity_id, kind=OpenIEArgumentKind.ENTITY,
            normalized_start=0 if role == "subject" else 10,
            normalized_end=2 if role == "subject" else 12,
            mention_id="m-" + role, entity_id=entity_id, entity_type="software",
            entity_state=EntityTerminalState.PROMOTED, normalized_value=entity_id,
            reasons=("test",), adapter_release="test",
        ))
    return output


def test_positive_mapped_entity_edge_enters_fact_lane():
    candidate = _case("uses", "use")
    result = assemble_openie_assertions([candidate], _args(candidate))
    assert result.assertions[0].lane == "FACT"
    assert result.report["wildcard_endpoints"] == 0


def test_qualified_candidate_cannot_enter_fact_lane():
    candidate = _case("uses", "use", qualified=True)
    result = assemble_openie_assertions([candidate], _args(candidate))
    assert result.assertions[0].lane == "QUALIFIED_CLAIM"
    assert result.report["qualified_promoted_to_fact"] == 0


def test_unmapped_candidate_enters_open_relation_lane():
    candidate = _case("powers", "power")
    result = assemble_openie_assertions([candidate], _args(candidate))
    assert result.assertions[0].lane == "OPEN_RELATION"


def test_wildcard_endpoint_is_rejected_before_qualification():
    candidate = _case("uses", "use", qualified=True)
    arguments = _args(candidate)
    arguments[0] = arguments[0].model_copy(update={
        "surface": "*",
        "kind": OpenIEArgumentKind.UNRESOLVED,
        "normalized_start": None,
        "normalized_end": None,
        "mention_id": None,
        "entity_id": None,
        "entity_type": None,
        "entity_state": None,
        "normalized_value": "",
    })
    result = assemble_openie_assertions([candidate], arguments)
    assert result.assertions[0].lane == "REJECT"
    assert "invalid_endpoint_sentinel" in result.assertions[0].reasons
    assert result.report["wildcard_non_reject_endpoints"] == 0
