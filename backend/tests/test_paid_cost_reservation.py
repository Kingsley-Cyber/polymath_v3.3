"""Standing hard-ceiling reservation gates for every paid provider claim."""

from decimal import Decimal

import pytest

from services.ingestion.paid_cost_reservation import (
    cost_reservation_allows_claim,
    worst_case_authority_usd,
    worst_case_next_call_cost_usd,
)


def test_two_attempt_claim_reservation_is_ceiling_rounded() -> None:
    assert worst_case_next_call_cost_usd(
        packet_input_token_upper_bound=20_000,
        max_output_tokens=8_192,
        uncached_input_usd=Decimal("0.75"),
        output_usd=Decimal("2.95"),
        price_unit_tokens=1_000_000,
    ) == Decimal("0.08616608")


def test_two_attempt_batch_authority_rounds_after_aggregation() -> None:
    assert worst_case_authority_usd(
        packet_input_token_upper_bounds=[20_000, 19_000],
        max_output_tokens=8_192,
        uncached_input_usd=Decimal("0.75"),
        output_usd=Decimal("2.95"),
        price_unit_tokens=1_000_000,
    ) == Decimal("0.17068216")


def test_exact_reservation_boundary_is_allowed() -> None:
    authority = Decimal("0.42995425")
    max_call = Decimal("0.08616608")
    assert cost_reservation_allows_claim(
        current_ceiling_basis_usd=authority - max_call,
        max_call_cost_usd=max_call,
        authorized_ceiling_usd=authority,
    )


def test_basis_within_one_call_of_authority_cannot_claim() -> None:
    authority = Decimal("0.42995425")
    max_call = Decimal("0.08616608")
    assert not cost_reservation_allows_claim(
        current_ceiling_basis_usd=authority - max_call + Decimal("0.00000001"),
        max_call_cost_usd=max_call,
        authorized_ceiling_usd=authority,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"packet_input_token_upper_bound": 0},
        {"max_output_tokens": -1},
        {"uncached_input_usd": 0},
        {"output_usd": True},
        {"price_unit_tokens": 0},
        {"max_provider_calls": 0},
        {"safety_margin": 0},
    ],
)
def test_invalid_reservation_inputs_fail_closed(kwargs: dict[str, object]) -> None:
    values = {
        "packet_input_token_upper_bound": 20_000,
        "max_output_tokens": 8_192,
        "uncached_input_usd": Decimal("0.75"),
        "output_usd": Decimal("2.95"),
        "price_unit_tokens": 1_000_000,
    }
    values.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        worst_case_next_call_cost_usd(**values)


def test_empty_batch_authority_fails_closed() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        worst_case_authority_usd(
            packet_input_token_upper_bounds=[],
            max_output_tokens=8_192,
            uncached_input_usd=Decimal("0.75"),
            output_usd=Decimal("2.95"),
            price_unit_tokens=1_000_000,
        )


def test_negative_or_boolean_basis_fails_closed() -> None:
    for basis in (Decimal("-0.00000001"), True):
        with pytest.raises((TypeError, ValueError)):
            cost_reservation_allows_claim(
                current_ceiling_basis_usd=basis,
                max_call_cost_usd=Decimal("0.01"),
                authorized_ceiling_usd=Decimal("1"),
            )
