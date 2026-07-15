"""Pure fail-closed gates for the authorized sentence-hybrid v3 canary."""

from __future__ import annotations

import argparse
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts import semantic_gateway_mark_sentence_hybrid_canary as runner

PROMPT_HASH = "sha256:" + "1" * 64
REPAIR_HASH = "sha256:" + "2" * 64
DIGEST_SCHEMA_HASH = "sha256:" + "3" * 64


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
            "corpus": {
                "packet_ready_count": 793,
                "non_packet_ready_count": 2,
            },
            "packet_contract": {
                "packet_schema_version": "semantic_parent_packet.sentence_hybrid.v3",
                "packet_set_hash": runner.AUTHORIZED_PACKET_SET_HASH,
                "packet_schema_hash": runner.AUTHORIZED_PACKET_SCHEMA_HASH,
                "packet_max_utf8_bytes": 26_000,
                "over_20000_packet_count": 3,
                "over_26000_packet_count": 0,
                "dropped_sentence_count": 0,
            },
            "mapping_disclosure": {
                "source_sentence_count": 30_694,
                "mapped_sentence_count": 24_845,
                "context_only_sentence_count": 5_849,
                "context_only_units_uncitable": True,
                "expansion_is_unique_model_intent": False,
            },
            "selection": {
                "selection_set_hash": runner.AUTHORIZED_SELECTION_SET_HASH,
                "unique_document_count": runner.TARGET_COUNT,
                "long_packet_selected_count": 1,
            },
            "historical_ledger": {"fresh_packet_ready_count": 728},
            "provider_contract": {
                "route_id": runner.ROUTE_ID,
                "model_id": "openai/LongCat-2.0",
                "capability_tier": "tier3",
                "max_tokens": 8192,
                "temperature": 0,
                "thinking": "disabled",
                "prompt_hash": PROMPT_HASH,
                "repair_prompt_hash": REPAIR_HASH,
                "digest_schema_hash": DIGEST_SCHEMA_HASH,
            },
            "cost_authority": {
                "selected_governing_authority_usd": str(runner.AUTHORIZED_COST_USD),
                "selected_claim_reservation_sum_usd": str(runner.AUTHORIZED_COST_USD),
            },
        },
    )


def _contract_kwargs() -> dict[str, object]:
    return {
        "authorization_reference": runner.GO_AUTHORIZATION_REFERENCE,
        "expected_packet_set_hash": runner.AUTHORIZED_PACKET_SET_HASH,
        "expected_packet_schema_hash": runner.AUTHORIZED_PACKET_SCHEMA_HASH,
        "expected_selection_set_hash": runner.AUTHORIZED_SELECTION_SET_HASH,
        "expected_prompt_hash": PROMPT_HASH,
        "expected_repair_prompt_hash": REPAIR_HASH,
        "expected_digest_schema_hash": DIGEST_SCHEMA_HASH,
        "max_authorized_cost_usd": runner.AUTHORIZED_COST_USD,
    }


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        **_contract_kwargs(),
        "corpus_name": "mark",
        "expected_parent_count": 795,
        "expected_child_count": 3_493,
        "max_entities": 40,
        "out": "/tmp/not-written-by-run-test.json",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_exact_senior_go_contract_is_accepted() -> None:
    runner._assert_go_contract(_prepared(), **_contract_kwargs())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization_reference", "wrong"),
        ("expected_packet_set_hash", "sha256:" + "a" * 64),
        ("expected_packet_schema_hash", "sha256:" + "b" * 64),
        ("expected_selection_set_hash", "sha256:" + "c" * 64),
        ("expected_prompt_hash", "sha256:" + "d" * 64),
        ("expected_repair_prompt_hash", "sha256:" + "e" * 64),
        ("expected_digest_schema_hash", "sha256:" + "f" * 64),
        ("max_authorized_cost_usd", Decimal("0.78260931")),
    ],
)
def test_any_go_identity_or_authority_drift_fails_closed(
    field: str,
    value: object,
) -> None:
    kwargs = _contract_kwargs()
    kwargs[field] = value

    with pytest.raises(runner.PaidPassError):
        runner._assert_go_contract(_prepared(), **kwargs)


@pytest.mark.asyncio
async def test_cumulative_umbrella_accepts_exact_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basis = runner.CUMULATIVE_OWNER_UMBRELLA_USD - runner.AUTHORIZED_COST_USD

    async def cumulative(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "budget_accounting_complete": True,
            "ceiling_basis_usd": float(basis),
        }

    monkeypatch.setattr(runner, "_cumulative_cost", cumulative)

    receipt = await runner._global_umbrella_preflight(object(), corpus_id="mark")

    assert Decimal(str(receipt["remaining_before_canary_usd"])) == (
        runner.AUTHORIZED_COST_USD
    )


@pytest.mark.asyncio
async def test_cumulative_umbrella_rejects_one_quantum_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basis = (
        runner.CUMULATIVE_OWNER_UMBRELLA_USD
        - runner.AUTHORIZED_COST_USD
        + Decimal("0.00000001")
    )

    async def cumulative(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "budget_accounting_complete": True,
            "ceiling_basis_usd": str(basis),
        }

    monkeypatch.setattr(runner, "_cumulative_cost", cumulative)

    with pytest.raises(runner.PaidPassError):
        await runner._global_umbrella_preflight(object(), corpus_id="mark")


@pytest.mark.asyncio
async def test_bad_authorization_fails_before_settings_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def prepare(*args: object, **kwargs: object) -> SimpleNamespace:
        return _prepared()

    def forbidden_settings() -> object:
        raise AssertionError("settings must not be opened after a failed GO seal")

    async def forbidden_credential(*args: object, **kwargs: object) -> str:
        raise AssertionError("credentials must not be read after a failed GO seal")

    monkeypatch.setattr(runner, "_prepare", prepare)
    monkeypatch.setattr(runner, "get_settings", forbidden_settings)
    monkeypatch.setattr(
        runner.settings_service,
        "get_plaintext_key_any_user",
        forbidden_credential,
    )

    with pytest.raises(runner.PaidPassError):
        await runner.run(_args(authorization_reference="invalid"))
