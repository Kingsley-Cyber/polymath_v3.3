"""Pure fail-closed gates for the senior-authorized atomic B4 runner."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts import semantic_gateway_mark_atomic_b4 as runner


PACKET_SET_HASH = "sha256:" + "1" * 64
SELECTION_SET_HASH = "sha256:" + "2" * 64
PROMPT_HASH = "sha256:" + "3" * 64
REPAIR_HASH = "sha256:" + "4" * 64
SCHEMA_HASH = "sha256:" + "5" * 64


def _prepared() -> SimpleNamespace:
    selected = tuple(
        SimpleNamespace(
            row=SimpleNamespace(planned=SimpleNamespace(job_id=f"sha256:{index:064x}"))
        )
        for index in range(runner.TARGET_COUNT)
    )
    return SimpleNamespace(
        selected=selected,
        receipt={
            "packet_contract": {"packet_set_hash": PACKET_SET_HASH},
            "selection": {
                "selection_set_hash": SELECTION_SET_HASH,
                "unique_document_count": runner.TARGET_COUNT,
            },
            "provider_contract": {
                "route_id": runner.ROUTE_ID,
                "model_id": "openai/LongCat-2.0",
                "capability_tier": "tier3",
                "max_tokens": 8192,
                "temperature": 0,
                "thinking": "disabled",
                "prompt_hash": PROMPT_HASH,
                "repair_prompt_hash": REPAIR_HASH,
                "schema_hash": SCHEMA_HASH,
            },
            "cost_authority": {"selected_b4_ceiling_usd": 0.42995425},
        },
    )


def _assert_contract(prepared: SimpleNamespace) -> None:
    runner._assert_go_contract(
        prepared,
        authorization_reference=runner.GO_AUTHORIZATION_REFERENCE,
        expected_packet_set_hash=PACKET_SET_HASH,
        expected_selection_set_hash=SELECTION_SET_HASH,
        expected_prompt_hash=PROMPT_HASH,
        expected_repair_prompt_hash=REPAIR_HASH,
        expected_schema_hash=SCHEMA_HASH,
        max_authorized_cost_usd=Decimal("0.42995425"),
    )


def test_exact_senior_go_contract_is_accepted() -> None:
    _assert_contract(_prepared())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization", "wrong"),
        ("packet", "sha256:" + "a" * 64),
        ("selection", "sha256:" + "b" * 64),
        ("prompt", "sha256:" + "c" * 64),
        ("repair", "sha256:" + "d" * 64),
        ("schema", "sha256:" + "e" * 64),
        ("ceiling", Decimal("0.42995426")),
    ],
)
def test_any_go_identity_or_ceiling_drift_fails_closed(
    field: str,
    value: object,
) -> None:
    prepared = _prepared()
    kwargs = {
        "authorization_reference": runner.GO_AUTHORIZATION_REFERENCE,
        "expected_packet_set_hash": PACKET_SET_HASH,
        "expected_selection_set_hash": SELECTION_SET_HASH,
        "expected_prompt_hash": PROMPT_HASH,
        "expected_repair_prompt_hash": REPAIR_HASH,
        "expected_schema_hash": SCHEMA_HASH,
        "max_authorized_cost_usd": Decimal("0.42995425"),
    }
    mapping = {
        "authorization": "authorization_reference",
        "packet": "expected_packet_set_hash",
        "selection": "expected_selection_set_hash",
        "prompt": "expected_prompt_hash",
        "repair": "expected_repair_prompt_hash",
        "schema": "expected_schema_hash",
        "ceiling": "max_authorized_cost_usd",
    }
    kwargs[mapping[field]] = value
    with pytest.raises(runner.PaidPassError):
        runner._assert_go_contract(prepared, **kwargs)


def test_checkpoint_requires_nine_of_ten_complete_bounded_and_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "_canonical_store_census_receipt",
        lambda before, after: {"protected_exactly_unchanged": before == after},
    )
    rows = [
        {
            "status": runner.SUCCESS_STATUS,
            "actual_cost_usd": 0.01,
            "cost_complete": True,
        }
        for _ in range(9)
    ]
    rows.append(
        {
            "status": "dead_letter",
            "actual_cost_usd": None,
            "cost_complete": False,
            "unpriced_exposure_upper_bound_usd": (runner.UNPRICED_EXPOSURE_BOUND_USD),
            "cost_accounting_basis": runner.UNPRICED_EXPOSURE_BASIS,
        }
    )
    receipt = runner._checkpoint(
        rows,
        canonical_before={"x": 1},
        canonical_after={"x": 1},
        authorized_ceiling=Decimal("0.42995425"),
        stop_reason=None,
    )
    assert receipt["accepted_count"] == 9
    assert receipt["unpriced_exposure_count"] == 1
    assert receipt["budget_accounting_complete"] is True
    assert receipt["execution_green"] is True
    assert receipt["summary_faithfulness_review_pending"] is True


def test_checkpoint_fails_on_eight_accepts_cost_or_canonical_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "_canonical_store_census_receipt",
        lambda before, after: {"protected_exactly_unchanged": before == after},
    )
    rows = [
        {
            "status": runner.SUCCESS_STATUS,
            "actual_cost_usd": 0.01,
            "cost_complete": True,
        }
        for _ in range(8)
    ] + [
        {"status": "dead_letter", "actual_cost_usd": 0.01, "cost_complete": True}
        for _ in range(2)
    ]
    receipt = runner._checkpoint(
        rows,
        canonical_before={"x": 1},
        canonical_after={"x": 2},
        authorized_ceiling=Decimal("0.05"),
        stop_reason=None,
    )
    assert receipt["acceptance_green"] is False
    assert receipt["ceiling_green"] is False
    assert receipt["execution_green"] is False


def test_two_read_timeouts_are_the_exact_pause_threshold() -> None:
    rows = [
        {"transport_error_class": "ReadTimeout"},
        {"transport_error_class": "ReadTimeout"},
        {"transport_error_class": "ConnectTimeout"},
    ]
    assert runner._read_timeout_count(rows) == runner.MAX_READ_TIMEOUTS
