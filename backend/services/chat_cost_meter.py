"""Request-scoped, secret-free cost accounting for the ``/api/chat`` lane.

The meter is deliberately additive. When the feature flag is disabled no
request field or SSE frame changes. When enabled, LLMService records only
numeric usage, route identity, and price arithmetic in this request-local
ledger; prompts, outputs, credentials, and provider response bodies never
cross the seam.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncGenerator, Iterable
from urllib.parse import urlsplit
from uuid import uuid4

from models.schemas import ChatChunk
from utils.streaming import build_sse_chunk


CHAT_COST_LEDGER_VERSION = "polymath.chat_cost_ledger.v1"
CHAT_COST_RUN_LEDGER_VERSION = "polymath.chat_cost_run_ledger.v1"
CHAT_COST_TRACE_TITLE = "Chat synthesis cost ledger"
_PRICE_REGISTRY = (
    Path(__file__).resolve().parents[1] / "registries" / "chat_provider_prices.v1.json"
)
_CURRENT_LEDGER: ContextVar[ChatCostLedger | None] = ContextVar(
    "polymath_chat_cost_ledger",
    default=None,
)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001 - untrusted provider/registry value
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _safe_route_base(value: Any) -> str | None:
    """Return scheme/host/path only; query strings can contain credentials."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


@lru_cache(maxsize=1)
def _price_registry() -> tuple[dict[str, Any], str]:
    raw = _PRICE_REGISTRY.read_bytes()
    payload = json.loads(raw)
    if payload.get("schema_version") != "polymath.chat_provider_prices.v1":
        raise ValueError("chat provider price registry schema drifted")
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("chat provider price registry has no routes")
    return payload, hashlib.sha256(raw).hexdigest()


def _route_for(model: str, api_base: str | None) -> tuple[dict[str, Any] | None, str]:
    registry, registry_sha256 = _price_registry()
    normalized_model = str(model or "").strip().lower()
    safe_base = _safe_route_base(api_base)
    for route in registry["routes"]:
        exact = str(route.get("model_id") or "").strip().lower()
        prefix = str(route.get("model_prefix") or "").strip().lower()
        if exact and normalized_model != exact:
            continue
        if prefix and not normalized_model.startswith(prefix):
            continue
        required_base = _safe_route_base(route.get("api_base_prefix"))
        if required_base and not (safe_base or "").startswith(required_base):
            continue
        if exact or prefix:
            return route, registry_sha256
    return None, registry_sha256


def _usage_from(telemetry: dict[str, Any] | None) -> dict[str, int | None]:
    raw = (telemetry or {}).get("usage")
    raw = raw if isinstance(raw, dict) else {}
    input_tokens = _nonnegative_int(raw.get("prompt_tokens", raw.get("input_tokens")))
    output_tokens = _nonnegative_int(
        raw.get("completion_tokens", raw.get("output_tokens"))
    )
    total_tokens = _nonnegative_int(raw.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _metered_call_row(
    *,
    row_id: str,
    call_kind: str,
    model: str,
    api_base: str | None,
    provider_telemetry: dict[str, Any] | None,
    transport_attempts: int,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    attempts = max(1, int(transport_attempts or 1))
    usage = _usage_from(provider_telemetry)
    route, registry_sha256 = _route_for(model, api_base)
    input_tokens = usage["input_tokens"]
    output_tokens = usage["output_tokens"]
    usage_complete = input_tokens is not None and output_tokens is not None

    price: dict[str, Any] | None = None
    arithmetic: dict[str, Any] | None = None
    computed_cost: Decimal | None = None
    if route is not None:
        unit = _nonnegative_int(route.get("price_unit_tokens"))
        input_rate = _nonnegative_decimal(route.get("uncached_input_usd"))
        output_rate = _nonnegative_decimal(route.get("output_usd"))
        if (
            unit is not None
            and unit > 0
            and input_rate is not None
            and output_rate is not None
        ):
            price = {
                "route_id": route["route_id"],
                "currency": route.get("currency") or "USD",
                "price_unit_tokens": unit,
                "input_usd_per_unit": _decimal_text(input_rate),
                "output_usd_per_unit": _decimal_text(output_rate),
                "input_basis": route.get("input_basis"),
                "price_tier": route.get("price_tier"),
                "source_receipt": route.get("source_receipt"),
                "registry_sha256": registry_sha256,
            }
            if usage_complete:
                input_component = Decimal(input_tokens) * input_rate / Decimal(unit)
                output_component = Decimal(output_tokens) * output_rate / Decimal(unit)
                computed_cost = input_component + output_component
                arithmetic = {
                    "formula": (
                        "(input_tokens * input_usd_per_unit + "
                        "output_tokens * output_usd_per_unit) / price_unit_tokens"
                    ),
                    "input_cost_usd": _decimal_text(input_component),
                    "output_cost_usd": _decimal_text(output_component),
                    "computed_cost_usd": _decimal_text(computed_cost),
                }

    current_complete = bool(usage_complete and price is not None and arithmetic)
    prior_unmetered_attempts = attempts - 1
    unmetered_calls = prior_unmetered_attempts + (0 if current_complete else 1)
    provider_reported_cost = _nonnegative_decimal(
        (provider_telemetry or {}).get("actual_cost_usd")
    )
    reason = failure_reason
    if reason is None and not usage_complete:
        reason = "usage_missing"
    if reason is None and price is None:
        reason = "price_route_missing"
    if reason is None and prior_unmetered_attempts:
        reason = "prior_transport_attempt_unmetered"

    return {
        "call_id": row_id,
        "call_kind": str(call_kind or "unknown")[:80],
        "model": str(model or "unknown")[:200],
        "transport_attempts": attempts,
        "metered_synthesis_calls": 1 if current_complete else 0,
        "unmetered_synthesis_calls": unmetered_calls,
        "accounting_state": (
            "CLOSED" if current_complete and unmetered_calls == 0 else "OPEN"
        ),
        "failure_reason": str(reason)[:160] if reason else None,
        **usage,
        "price": price,
        "arithmetic": arithmetic,
        "computed_cost_usd": (
            _decimal_text(computed_cost) if computed_cost is not None else None
        ),
        "provider_reported_cost_usd": (
            _decimal_text(provider_reported_cost)
            if provider_reported_cost is not None
            else None
        ),
        "provider_reported_cost_source": (provider_telemetry or {}).get("cost_source"),
    }


@dataclass
class ChatCostLedger:
    """Mutable request-local ledger; ``snapshot`` returns a JSON-safe receipt."""

    ledger_id: str = field(default_factory=lambda: f"chat-cost-{uuid4().hex}")
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        call_kind: str,
        model: str,
        api_base: str | None,
        provider_telemetry: dict[str, Any] | None,
        transport_attempts: int = 1,
        failure_reason: str | None = None,
    ) -> None:
        self.calls.append(
            _metered_call_row(
                row_id=f"call-{len(self.calls) + 1}",
                call_kind=call_kind,
                model=model,
                api_base=api_base,
                provider_telemetry=provider_telemetry,
                transport_attempts=transport_attempts,
                failure_reason=failure_reason,
            )
        )

    def snapshot(self) -> dict[str, Any]:
        metered = sum(int(row["metered_synthesis_calls"]) for row in self.calls)
        unmetered = sum(int(row["unmetered_synthesis_calls"]) for row in self.calls)
        synthesis_calls = metered + unmetered
        input_tokens = sum(
            int(row["input_tokens"] or 0)
            for row in self.calls
            if row.get("input_tokens") is not None
        )
        output_tokens = sum(
            int(row["output_tokens"] or 0)
            for row in self.calls
            if row.get("output_tokens") is not None
        )
        known_cost = sum(
            (
                Decimal(str(row["computed_cost_usd"]))
                for row in self.calls
                if row.get("computed_cost_usd") is not None
            ),
            Decimal("0"),
        )
        closed = unmetered == 0
        return {
            "schema_version": CHAT_COST_LEDGER_VERSION,
            "ledger_id": self.ledger_id,
            "accounting_state": "CLOSED" if closed else "OPEN",
            "trace_reproduces_arithmetic": closed,
            "zero_unmetered_synthesis_calls": unmetered == 0,
            "trace_row_count": len(self.calls),
            "synthesis_call_count": synthesis_calls,
            "metered_synthesis_call_count": metered,
            "unmetered_synthesis_call_count": unmetered,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "known_cost_subtotal_usd": _decimal_text(known_cost),
            "computed_cost_usd": _decimal_text(known_cost) if closed else None,
            "calls": list(self.calls),
        }


@contextmanager
def chat_cost_scope() -> Iterable[ChatCostLedger]:
    ledger = ChatCostLedger()
    token = _CURRENT_LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _CURRENT_LEDGER.reset(token)


def current_chat_cost_ledger() -> ChatCostLedger | None:
    return _CURRENT_LEDGER.get()


def record_chat_provider_call(
    *,
    call_kind: str,
    model: str,
    api_base: str | None,
    provider_telemetry: dict[str, Any] | None,
    transport_attempts: int = 1,
    failure_reason: str | None = None,
) -> None:
    ledger = current_chat_cost_ledger()
    if ledger is None:
        return
    ledger.record(
        call_kind=call_kind,
        model=model,
        api_base=api_base,
        provider_telemetry=provider_telemetry,
        transport_attempts=transport_attempts,
        failure_reason=failure_reason,
    )


def _sse_payload(frame: str) -> dict[str, Any] | None:
    for line in str(frame or "").splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def build_chat_cost_trace(
    ledger: ChatCostLedger,
    *,
    conversation_id: str | None,
) -> str:
    snapshot = ledger.snapshot()
    trace_event = {
        "id": f"trace-{ledger.ledger_id}",
        "lane": "model_call",
        "title": CHAT_COST_TRACE_TITLE,
        "status": "done" if snapshot["accounting_state"] == "CLOSED" else "error",
        "content": (
            "Chat synthesis accounting "
            f"{snapshot['accounting_state'].lower()}: "
            f"calls={snapshot['synthesis_call_count']} "
            f"input_tokens={snapshot['input_tokens']} "
            f"output_tokens={snapshot['output_tokens']} "
            f"cost_usd={snapshot['computed_cost_usd'] or 'unknown'}."
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "metadata": {"chat_cost_ledger": snapshot},
    }
    return build_sse_chunk(
        ChatChunk(
            type="trace_event",
            trace_event=trace_event,
            conversation_id=conversation_id,
        )
    )


async def meter_chat_sse_stream(
    source: AsyncGenerator[str, None],
    *,
    enabled: bool,
) -> AsyncGenerator[str, None]:
    """Insert exactly one terminal cost trace before ``done`` when enabled."""
    if not enabled:
        async for frame in source:
            yield frame
        return

    with chat_cost_scope() as ledger:
        emitted = False
        conversation_id: str | None = None
        async for frame in source:
            payload = _sse_payload(frame)
            if payload and payload.get("conversation_id"):
                conversation_id = str(payload["conversation_id"])
            if payload and payload.get("type") == "done" and not emitted:
                yield build_chat_cost_trace(
                    ledger,
                    conversation_id=conversation_id,
                )
                emitted = True
            yield frame
        if not emitted:
            yield build_chat_cost_trace(
                ledger,
                conversation_id=conversation_id,
            )


def aggregate_chat_cost_ledgers(
    ledgers: Iterable[dict[str, Any] | None],
) -> dict[str, Any]:
    """Aggregate request ledgers for one eval run without inventing usage."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ledger in ledgers:
        if not isinstance(ledger, dict):
            continue
        if ledger.get("schema_version") != CHAT_COST_LEDGER_VERSION:
            continue
        ledger_id = str(ledger.get("ledger_id") or "")
        if not ledger_id or ledger_id in seen:
            continue
        seen.add(ledger_id)
        unique.append(ledger)

    metered = sum(int(row.get("metered_synthesis_call_count") or 0) for row in unique)
    unmetered = sum(
        int(row.get("unmetered_synthesis_call_count") or 0) for row in unique
    )
    known_cost = sum(
        (Decimal(str(row.get("known_cost_subtotal_usd") or "0")) for row in unique),
        Decimal("0"),
    )
    closed = (
        bool(unique)
        and unmetered == 0
        and all(row.get("accounting_state") == "CLOSED" for row in unique)
    )
    return {
        "schema_version": CHAT_COST_RUN_LEDGER_VERSION,
        "accounting_state": "CLOSED" if closed else "OPEN",
        "request_ledger_count": len(unique),
        "synthesis_call_count": metered + unmetered,
        "metered_synthesis_call_count": metered,
        "unmetered_synthesis_call_count": unmetered,
        "zero_unmetered_synthesis_calls": bool(unique) and unmetered == 0,
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in unique),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in unique),
        "known_cost_subtotal_usd": _decimal_text(known_cost),
        "computed_cost_usd": _decimal_text(known_cost) if closed else None,
        "request_ledger_ids": [row["ledger_id"] for row in unique],
    }
