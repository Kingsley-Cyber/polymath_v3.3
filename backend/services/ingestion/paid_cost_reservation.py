"""Fail-closed pre-claim cost reservation for every paid provider lane."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, ROUND_CEILING


USD_QUANTUM = Decimal("0.00000001")
DEFAULT_SAFETY_MARGIN = Decimal("1.10")
DEFAULT_MAX_PROVIDER_CALLS_PER_CLAIM = 2


def _positive_decimal(value: int | float | Decimal, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be numeric, not bool")
    decimal = Decimal(str(value))
    if not decimal.is_finite() or decimal <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return decimal


def worst_case_next_call_cost_usd(
    *,
    packet_input_token_upper_bound: int,
    max_output_tokens: int,
    uncached_input_usd: int | float | Decimal,
    output_usd: int | float | Decimal,
    price_unit_tokens: int,
    max_provider_calls: int = DEFAULT_MAX_PROVIDER_CALLS_PER_CLAIM,
    safety_margin: int | float | Decimal = DEFAULT_SAFETY_MARGIN,
) -> Decimal:
    """Return a ceiling-rounded reservation for one durable gateway claim.

    A gateway claim may make the initial provider call plus one repair call.
    Both calls reserve the full packet input-token upper bound and full output
    cap. The caller must reserve this amount before atomically claiming the
    durable row; actual complete-or-bounded telemetry replaces the reservation
    after the row terminalizes.
    """

    return worst_case_authority_usd(
        packet_input_token_upper_bounds=[packet_input_token_upper_bound],
        max_output_tokens=max_output_tokens,
        uncached_input_usd=uncached_input_usd,
        output_usd=output_usd,
        price_unit_tokens=price_unit_tokens,
        max_provider_calls=max_provider_calls,
        safety_margin=safety_margin,
    )


def worst_case_authority_usd(
    *,
    packet_input_token_upper_bounds: Iterable[int],
    max_output_tokens: int,
    uncached_input_usd: int | float | Decimal,
    output_usd: int | float | Decimal,
    price_unit_tokens: int,
    max_provider_calls: int = DEFAULT_MAX_PROVIDER_CALLS_PER_CLAIM,
    safety_margin: int | float | Decimal = DEFAULT_SAFETY_MARGIN,
) -> Decimal:
    """Return the two-attempt, margin-inclusive authority for a packet set."""

    packet_bounds = [
        _positive_decimal(value, label="packet input token upper bound")
        for value in packet_input_token_upper_bounds
    ]
    if not packet_bounds:
        raise ValueError("packet input token upper bounds must not be empty")
    output_bound = _positive_decimal(max_output_tokens, label="max output tokens")
    input_rate = _positive_decimal(uncached_input_usd, label="uncached input rate")
    output_rate = _positive_decimal(output_usd, label="output rate")
    price_unit = _positive_decimal(price_unit_tokens, label="price unit tokens")
    call_count = _positive_decimal(max_provider_calls, label="max provider calls")
    margin = _positive_decimal(safety_margin, label="safety margin")
    raw = sum(
        (
            ((packet_bound * input_rate) + (output_bound * output_rate))
            / price_unit
            * call_count
        )
        for packet_bound in packet_bounds
    )
    raw *= margin
    return raw.quantize(USD_QUANTUM, rounding=ROUND_CEILING)


def cost_reservation_allows_claim(
    *,
    current_ceiling_basis_usd: int | float | Decimal,
    max_call_cost_usd: int | float | Decimal,
    authorized_ceiling_usd: int | float | Decimal,
) -> bool:
    """Claim only when current basis plus reserved exposure is within authority."""

    if isinstance(current_ceiling_basis_usd, bool):
        raise TypeError("current ceiling basis must be numeric, not bool")
    basis = Decimal(str(current_ceiling_basis_usd))
    if not basis.is_finite() or basis < 0:
        raise ValueError("current ceiling basis must be finite and nonnegative")
    max_call = _positive_decimal(max_call_cost_usd, label="max call cost")
    authority = _positive_decimal(
        authorized_ceiling_usd,
        label="authorized ceiling",
    )
    return basis + max_call <= authority
