"""#7 — temporal/numeric as qualifiers, never predicates."""
from services.extraction.graphify_value_ir import (
    extract_temporal_qualifier,
    parse_typed_value,
)


def test_typed_values_parse_with_dimensions() -> None:
    assert parse_typed_value("420 ms") == {
        "value": 420.0, "unit": "ms", "dimension": "duration",
        "ir_release": "graphify-value-ir-v1",
    }
    assert parse_typed_value("74 seconds")["dimension"] == "duration"
    assert parse_typed_value("12 000")["dimension"] == "count"
    unknown = parse_typed_value("6.4 Citation Recall points")
    assert unknown["value"] == 6.4 and unknown["dimension"] == "unknown"
    assert parse_typed_value("ninety days") is None          # abstain, no guess
    assert parse_typed_value("the ledger") is None


def test_temporal_qualifiers_capture_operator_and_value() -> None:
    released = extract_temporal_qualifier("DeltaGraph 1.3, released on May 14, 2026, implements validation.")
    assert (released["operator"], released["value"]) == ("on", "May 14, 2026")
    deadline = extract_temporal_qualifier("Nikhil Rao must report its result by July 1, 2026.")
    assert (deadline["operator"], deadline["value"]) == ("by", "July 1, 2026")
    quarter = extract_temporal_qualifier("A separate experiment may evaluate DeltaGraph during Q4 2026.")
    assert (quarter["operator"], quarter["value"]) == ("during", "Q4 2026")
    weekday = extract_temporal_qualifier("Project Cedar deploys release 1.8 by Tuesday noon.")
    assert (weekday["operator"], weekday["value"]) == ("by", "Tuesday noon")
    assert extract_temporal_qualifier("Harbor uses MongoDB for storage.") is None


def test_qualifiers_ride_assertions_not_predicates() -> None:
    from models.graphify_contracts import OpenIEAssertionV1, SurfaceRelationV1

    assert "temporal" in OpenIEAssertionV1.model_fields
    assert "value_ir" in OpenIEAssertionV1.model_fields
    assert "temporal" in SurfaceRelationV1.model_fields
    # The frozen predicate inventory carries NO temporal predicate family.
    from services.extraction.graphify_relations import _CANONICAL_BY_LEMMA

    assert not any(
        "launch" in value or "occur" in value or "expire" in value
        for value in _CANONICAL_BY_LEMMA.values()
    )
