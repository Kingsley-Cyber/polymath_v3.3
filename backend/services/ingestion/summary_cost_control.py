"""Durable, fail-closed list-price ceilings for paid summary calls.

The controller reserves a conservative upper bound before provider dispatch,
settles the reservation from response usage, and keeps secret-free receipts.
Mongo stores integer nano-USD so concurrent workers never race on floats.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

from pymongo import ReturnDocument

from services.ingestion.paid_cost_reservation import (
    DEFAULT_SAFETY_MARGIN,
    worst_case_next_call_cost_usd,
)


RUNS_COLLECTION = "summary_cost_runs"
CALLS_COLLECTION = "summary_cost_call_receipts"
REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "registries"
    / "semantic_gateway_provider_prices.v1.json"
)
NANOS_PER_USD = Decimal("1000000000")
MAX_SUMMARY_COST_AUTHORITY_USD = Decimal("10000")
INPUT_TOKEN_OVERHEAD = 1024
EXPECTED_REGISTRY_SCHEMA = "polymath.semantic_gateway_provider_prices.v1"


class SummaryCostError(RuntimeError):
    """Base class for fail-closed summary cost-control failures."""


class SummaryCostAuthorityRequired(SummaryCostError):
    """A paid summary path was reached without explicit authority."""


class SummaryCostPriceCardError(SummaryCostError):
    """The exact provider route has no certified price card."""


class SummaryCostCeilingExceeded(SummaryCostError):
    """The next provider call cannot fit inside remaining authority."""


class SummaryCostSettlementError(SummaryCostError):
    """A provider call could not be settled without an accounting violation."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_authority_usd(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise SummaryCostAuthorityRequired(
            "summary_cost_authority_usd is required for provider-backed summaries"
        )
    try:
        authority = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalized to the public guard
        raise SummaryCostAuthorityRequired(
            "summary_cost_authority_usd must be a finite positive decimal"
        ) from exc
    if (
        not authority.is_finite()
        or authority <= 0
        or authority > MAX_SUMMARY_COST_AUTHORITY_USD
    ):
        raise SummaryCostAuthorityRequired(
            "summary_cost_authority_usd must be greater than 0 and no more than "
            f"{MAX_SUMMARY_COST_AUTHORITY_USD}"
        )
    return authority


def usd_to_nanos(value: Decimal, *, rounding: str = ROUND_CEILING) -> int:
    return int((value * NANOS_PER_USD).to_integral_value(rounding=rounding))


def nanos_to_usd(value: Any) -> str:
    nanos = max(0, int(value or 0))
    return format(Decimal(nanos) / NANOS_PER_USD, ".9f")


def normalize_api_base(value: Any) -> str:
    base = str(value or "").strip().rstrip("/")
    if base == "https://api.deepseek.com":
        return f"{base}/v1"
    return base


def normalize_model_id(value: Any) -> str:
    return str(value or "").strip()


def normalize_provider(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class SummaryPriceCard:
    route_id: str
    provider: str
    model_id: str
    api_base: str
    price_unit_tokens: int
    uncached_input_usd: Decimal
    cached_input_usd: Decimal
    output_usd: Decimal
    source_url: str
    price_tier: str


def load_summary_price_card(
    *,
    provider: Any,
    model: Any,
    api_base: Any,
    registry_path: Path = REGISTRY_PATH,
) -> SummaryPriceCard:
    """Resolve one exact normalized provider/model/base list-price route."""

    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - fail closed on registry damage
        raise SummaryCostPriceCardError(
            f"summary price registry unavailable: {type(exc).__name__}"
        ) from exc
    if payload.get("schema_version") != EXPECTED_REGISTRY_SCHEMA:
        raise SummaryCostPriceCardError("summary price registry schema mismatch")

    target = (
        normalize_provider(provider),
        normalize_model_id(model),
        normalize_api_base(api_base),
    )
    matches = [
        row
        for row in payload.get("routes") or []
        if (
            normalize_provider(row.get("provider")),
            normalize_model_id(row.get("model_id")),
            normalize_api_base(row.get("api_base")),
        )
        == target
    ]
    if len(matches) != 1:
        raise SummaryCostPriceCardError(
            "no unique certified summary price card for "
            f"provider={target[0]!r} model={target[1]!r} api_base={target[2]!r}"
        )
    row = matches[0]
    try:
        route_id = str(row["route_id"])
        source_url = str(row["source_url"])
        price_tier = str(row["price_tier"])
        unit = int(row["price_unit_tokens"])
        input_rate = Decimal(str(row["uncached_input_usd"]))
        cached_rate = Decimal(str(row["cached_input_usd"]))
        output_rate = Decimal(str(row["output_usd"]))
        if (
            not route_id
            or not source_url.startswith("https://")
            or row.get("currency") != "USD"
            or row.get("fallback_input_basis") != "uncached_input_conservative"
            or price_tier != "published_list_price"
            or unit <= 0
            or input_rate <= 0
            or cached_rate < 0
            or output_rate <= 0
        ):
            raise ValueError("nonpositive price card")
    except Exception as exc:  # noqa: BLE001 - fail closed on malformed rates
        raise SummaryCostPriceCardError("malformed summary price card") from exc
    return SummaryPriceCard(
        route_id=route_id,
        provider=target[0],
        model_id=target[1],
        api_base=target[2],
        price_unit_tokens=unit,
        uncached_input_usd=input_rate,
        cached_input_usd=cached_rate,
        output_usd=output_rate,
        source_url=source_url,
        price_tier=price_tier,
    )


def message_input_token_upper_bound(messages: list[dict[str, Any]]) -> int:
    """Bound provider prompt tokens by canonical UTF-8 bytes plus overhead."""

    encoded = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, len(encoded) + INPUT_TOKEN_OVERHEAD)


def list_price_nanos(
    card: SummaryPriceCard,
    *,
    input_tokens: int,
    output_tokens: int,
) -> int:
    input_count = max(0, int(input_tokens or 0))
    output_count = max(0, int(output_tokens or 0))
    cost = (
        Decimal(input_count) * card.uncached_input_usd
        + Decimal(output_count) * card.output_usd
    ) / Decimal(card.price_unit_tokens)
    return usd_to_nanos(cost)


@dataclass(frozen=True)
class SummaryCallReservation:
    reservation_id: str
    run_id: str
    card: SummaryPriceCard
    item_count: int
    input_token_upper_bound: int
    output_token_upper_bound: int
    reserved_nanos: int

    def telemetry_fields(self) -> dict[str, Any]:
        return {
            "summary_cost_run_id": self.run_id,
            "summary_cost_reservation_id": self.reservation_id,
            "summary_cost_route_id": self.card.route_id,
            "summary_cost_reserved_usd": nanos_to_usd(self.reserved_nanos),
            "summary_cost_reserved_nanos": self.reserved_nanos,
            "summary_cost_price_source_url": self.card.source_url,
        }


class SummaryCostController:
    """One durable authority shared by every concurrent call in a run."""

    def __init__(
        self,
        db: Any,
        *,
        run_id: str,
        corpus_id: str,
        user_id: str,
        authority_usd: Decimal,
    ) -> None:
        self.db = db
        self.run_id = str(run_id)
        self.corpus_id = str(corpus_id)
        self.user_id = str(user_id)
        self.authority_usd = authority_usd
        self.authorized_nanos = usd_to_nanos(authority_usd)

    @classmethod
    async def open(
        cls,
        db: Any,
        *,
        run_id: Any,
        corpus_id: Any,
        user_id: Any,
        authority_usd: Any,
    ) -> "SummaryCostController":
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise SummaryCostAuthorityRequired(
                "summary_cost_run_id is required for provider-backed summaries"
            )
        authority = parse_authority_usd(authority_usd)
        controller = cls(
            db,
            run_id=normalized_run_id,
            corpus_id=str(corpus_id or ""),
            user_id=str(user_id or ""),
            authority_usd=authority,
        )
        now = _utcnow()
        await db[RUNS_COLLECTION].update_one(
            {"_id": controller.run_id},
            {
                "$setOnInsert": {
                    "run_id": controller.run_id,
                    "corpus_id": controller.corpus_id,
                    "user_id": controller.user_id,
                    "authorized_nanos": controller.authorized_nanos,
                    "actual_nanos": 0,
                    "reported_usage_nanos": 0,
                    "conservative_charge_nanos": 0,
                    "reserved_nanos": 0,
                    "calls_reserved": 0,
                    "calls_completed": 0,
                    "calls_refused": 0,
                    "usage_missing_charged_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "routes": [],
                    "settled_reservation_ids": [],
                    "status": "open",
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        row = await db[RUNS_COLLECTION].find_one(
            {"_id": controller.run_id},
            {
                "_id": 0,
                "corpus_id": 1,
                "user_id": 1,
                "authorized_nanos": 1,
            },
        )
        expected = (
            controller.corpus_id,
            controller.user_id,
            controller.authorized_nanos,
        )
        actual = (
            str((row or {}).get("corpus_id") or ""),
            str((row or {}).get("user_id") or ""),
            int((row or {}).get("authorized_nanos") or 0),
        )
        if actual != expected:
            raise SummaryCostAuthorityRequired(
                "summary cost run identity/authority does not match its durable ledger"
            )
        return controller

    async def reserve(
        self,
        *,
        provider: Any,
        model: Any,
        api_base: Any,
        messages: list[dict[str, Any]],
        max_output_tokens: int,
        item_count: int,
    ) -> SummaryCallReservation:
        card = load_summary_price_card(
            provider=provider,
            model=model,
            api_base=api_base,
        )
        input_bound = message_input_token_upper_bound(messages)
        output_bound = max(1, int(max_output_tokens or 0))
        reserved_usd = worst_case_next_call_cost_usd(
            packet_input_token_upper_bound=input_bound,
            max_output_tokens=output_bound,
            uncached_input_usd=card.uncached_input_usd,
            output_usd=card.output_usd,
            price_unit_tokens=card.price_unit_tokens,
            max_provider_calls=1,
            safety_margin=DEFAULT_SAFETY_MARGIN,
        )
        reserved_nanos = usd_to_nanos(reserved_usd)
        reservation_id = str(uuid.uuid4())
        now = _utcnow()
        row = await self.db[RUNS_COLLECTION].find_one_and_update(
            {
                "_id": self.run_id,
                "status": "open",
                "$expr": {
                    "$lte": [
                        {
                            "$add": [
                                {"$ifNull": ["$actual_nanos", 0]},
                                {"$ifNull": ["$reserved_nanos", 0]},
                                reserved_nanos,
                            ]
                        },
                        "$authorized_nanos",
                    ]
                },
            },
            {
                "$inc": {
                    "reserved_nanos": reserved_nanos,
                    "calls_reserved": 1,
                },
                "$addToSet": {"routes": card.route_id},
                "$set": {"updated_at": now},
            },
            return_document=ReturnDocument.AFTER,
        )
        if row is None:
            await self.db[RUNS_COLLECTION].update_one(
                {"_id": self.run_id},
                {
                    "$inc": {"calls_refused": 1},
                    "$set": {
                        "status": "ceiling_exhausted",
                        "updated_at": now,
                        "last_refused_reservation_nanos": reserved_nanos,
                    },
                },
            )
            snapshot = await self.snapshot()
            raise SummaryCostCeilingExceeded(
                "summary cost ceiling refused provider dispatch: "
                f"run={self.run_id} reserved_usd={nanos_to_usd(reserved_nanos)} "
                f"remaining_usd={snapshot['remaining_authority_usd']}"
            )

        receipt = {
            "_id": reservation_id,
            "reservation_id": reservation_id,
            "run_id": self.run_id,
            "corpus_id": self.corpus_id,
            "user_id": self.user_id,
            "provider": card.provider,
            "model": card.model_id,
            "api_base": card.api_base,
            "route_id": card.route_id,
            "price_source_url": card.source_url,
            "price_tier": card.price_tier,
            "item_count": max(1, int(item_count or 1)),
            "input_token_upper_bound": input_bound,
            "output_token_upper_bound": output_bound,
            "reserved_nanos": reserved_nanos,
            "actual_nanos": None,
            "status": "reserved",
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self.db[CALLS_COLLECTION].insert_one(receipt)
        except Exception as exc:  # reservation intentionally remains outstanding
            raise SummaryCostSettlementError(
                "summary reservation receipt persistence failed; provider dispatch refused"
            ) from exc
        return SummaryCallReservation(
            reservation_id=reservation_id,
            run_id=self.run_id,
            card=card,
            item_count=receipt["item_count"],
            input_token_upper_bound=input_bound,
            output_token_upper_bound=output_bound,
            reserved_nanos=reserved_nanos,
        )

    async def settle(
        self,
        reservation: SummaryCallReservation,
        *,
        usage: dict[str, Any] | None,
        failure_class: str | None = None,
    ) -> dict[str, Any]:
        prompt_tokens = max(0, int((usage or {}).get("prompt_tokens") or 0))
        completion_tokens = max(0, int((usage or {}).get("completion_tokens") or 0))
        usage_complete = (
            bool(usage) and "prompt_tokens" in usage and "completion_tokens" in usage
        )
        computed_nanos = (
            list_price_nanos(
                reservation.card,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            )
            if usage_complete
            else reservation.reserved_nanos
        )
        actual_nanos = max(0, computed_nanos)
        overrun = actual_nanos > reservation.reserved_nanos
        now = _utcnow()

        # Idempotency lives on the run row. If a process dies after this update
        # but before the receipt update, a retry cannot decrement twice.
        updated = await self.db[RUNS_COLLECTION].update_one(
            {
                "_id": self.run_id,
                "settled_reservation_ids": {"$ne": reservation.reservation_id},
            },
            {
                "$inc": {
                    "reserved_nanos": -reservation.reserved_nanos,
                    "actual_nanos": actual_nanos,
                    "calls_completed": 1,
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "reported_usage_nanos": actual_nanos if usage_complete else 0,
                    "conservative_charge_nanos": (
                        0 if usage_complete else actual_nanos
                    ),
                    "usage_missing_charged_calls": 0 if usage_complete else 1,
                },
                "$addToSet": {
                    "settled_reservation_ids": reservation.reservation_id,
                    "routes": reservation.card.route_id,
                },
                "$set": {
                    "updated_at": now,
                    "last_failure_class": str(failure_class or "")[:120] or None,
                    **({"status": "settlement_overrun"} if overrun else {}),
                },
            },
        )
        if int(getattr(updated, "matched_count", 0) or 0) == 0:
            existing = await self.db[RUNS_COLLECTION].find_one(
                {
                    "_id": self.run_id,
                    "settled_reservation_ids": reservation.reservation_id,
                },
                {"_id": 1},
            )
            if not existing:
                raise SummaryCostSettlementError(
                    "summary reservation could not be settled against its run ledger"
                )

        receipt_update = await self.db[CALLS_COLLECTION].update_one(
            {
                "_id": reservation.reservation_id,
                "run_id": self.run_id,
            },
            {
                "$set": {
                    "status": "settlement_overrun" if overrun else "settled",
                    "actual_nanos": actual_nanos,
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "usage_complete": usage_complete,
                    "failure_class": str(failure_class or "")[:120] or None,
                    "updated_at": now,
                    "settled_at": now,
                }
            },
        )
        if int(getattr(receipt_update, "matched_count", 0) or 0) == 0:
            raise SummaryCostSettlementError(
                "summary call receipt is missing after durable run settlement"
            )
        fields = {
            **reservation.telemetry_fields(),
            "summary_cost_accounted_usd": nanos_to_usd(actual_nanos),
            "summary_cost_accounted_nanos": actual_nanos,
            "summary_cost_reported_usage_usd": (
                nanos_to_usd(actual_nanos) if usage_complete else None
            ),
            "summary_cost_reported_usage_nanos": (
                actual_nanos if usage_complete else None
            ),
            "summary_cost_usage_complete": usage_complete,
            "summary_cost_settlement_overrun": overrun,
        }
        if overrun:
            raise SummaryCostSettlementError(
                "summary provider usage exceeded its conservative pre-call reservation; "
                f"run={self.run_id} reservation={reservation.reservation_id}"
            )
        return fields

    async def snapshot(self) -> dict[str, Any]:
        row = await self.db[RUNS_COLLECTION].find_one(
            {"_id": self.run_id},
            {"_id": 0, "settled_reservation_ids": 0},
        )
        if row is None:
            raise SummaryCostSettlementError("summary cost run ledger is missing")
        authorized = int(row.get("authorized_nanos") or 0)
        actual = int(row.get("actual_nanos") or 0)
        reserved = int(row.get("reserved_nanos") or 0)
        reported_usage = int(row.get("reported_usage_nanos") or 0)
        conservative_charge = int(row.get("conservative_charge_nanos") or 0)
        return {
            "run_id": self.run_id,
            "corpus_id": str(row.get("corpus_id") or ""),
            "user_id": str(row.get("user_id") or ""),
            "status": str(row.get("status") or "unknown"),
            "authorized_usd": nanos_to_usd(authorized),
            "accounted_cost_usd": nanos_to_usd(actual),
            "reported_usage_list_price_usd": nanos_to_usd(reported_usage),
            "conservative_missing_usage_charge_usd": nanos_to_usd(conservative_charge),
            "outstanding_reserved_usd": nanos_to_usd(reserved),
            "ceiling_basis_usd": nanos_to_usd(actual + reserved),
            "remaining_authority_usd": nanos_to_usd(
                max(0, authorized - actual - reserved)
            ),
            "calls_reserved": int(row.get("calls_reserved") or 0),
            "calls_completed": int(row.get("calls_completed") or 0),
            "calls_refused": int(row.get("calls_refused") or 0),
            "usage_missing_charged_calls": int(
                row.get("usage_missing_charged_calls") or 0
            ),
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "routes": sorted(str(value) for value in (row.get("routes") or [])),
            "price_basis": "published_list_price_uncached_input_conservative",
        }


async def summary_cost_snapshot(db: Any, run_id: Any) -> dict[str, Any] | None:
    normalized = str(run_id or "").strip()
    if not normalized:
        return None
    row = await db[RUNS_COLLECTION].find_one(
        {"_id": normalized},
        {"_id": 0, "settled_reservation_ids": 0},
    )
    if row is None:
        return None
    controller = SummaryCostController(
        db,
        run_id=normalized,
        corpus_id=str(row.get("corpus_id") or ""),
        user_id=str(row.get("user_id") or ""),
        authority_usd=Decimal(int(row.get("authorized_nanos") or 0)) / NANOS_PER_USD,
    )
    return await controller.snapshot()
