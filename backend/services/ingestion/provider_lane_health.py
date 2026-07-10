"""Durable provider-lane health for Ghost B extraction pools.

Ghost B already cools down a provider lane inside one batch when a 429 arrives.
This read model carries that signal across bounded repair/ingest runs so a
flapping provider does not keep re-entering the pool while healthy RTX/cloud
lanes continue working.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from services.extraction_provider_cards import resolve_extraction_provider_card

RATE_LIMIT_EVENTS = {"ghost_b_attempt_rate_limited"}
SUCCESS_EVENTS = {
    "ghost_b_attempt_succeeded",
    "ghost_b_attempt_succeeded_with_validation_rejections",
}
FAILED_EVENTS = {"ghost_b_attempt_failed"}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _lane_key(*, provider: str, model: str, lane: int | None = None) -> str:
    provider = str(provider or "unknown").strip().lower() or "unknown"
    model = str(model or "unknown").strip().lower() or "unknown"
    if lane is None:
        return f"{provider}|{model}"
    return f"{provider}|{model}|lane:{lane}"


def _event_provider(event: dict[str, Any]) -> str:
    card = event.get("provider_card") if isinstance(event.get("provider_card"), dict) else {}
    return str(card.get("provider") or event.get("provider") or "").strip().lower()


def _event_model(event: dict[str, Any]) -> str:
    card = event.get("provider_card") if isinstance(event.get("provider_card"), dict) else {}
    return str(event.get("model") or card.get("model") or "").strip()


def _is_rate_limited(event: dict[str, Any]) -> bool:
    if str(event.get("event") or "") in RATE_LIMIT_EVENTS:
        return True
    error_type = str(event.get("error_type") or "").lower()
    error_message = str(event.get("error_message") or "").lower()
    return "rate_limited" in error_type or "429" in error_message


def summarize_provider_lane_health(
    events: list[dict[str, Any]],
    *,
    min_rate_limit_events: int = 5,
    rate_limit_ratio: float = 0.50,
) -> dict[str, Any]:
    """Return API-key-free provider/lane health from recent audit events."""

    grouped: dict[str, dict[str, Any]] = {}
    aggregate_to_lane_keys: dict[str, set[str]] = {}
    for event in events:
        provider = _event_provider(event)
        model = _event_model(event)
        if not provider and not model:
            continue
        lane_value = event.get("lane")
        try:
            lane = int(lane_value) if lane_value is not None else None
        except (TypeError, ValueError):
            lane = None
        aggregate_key = _lane_key(provider=provider, model=model)
        keys = [aggregate_key]
        if lane is not None:
            lane_key = _lane_key(provider=provider, model=model, lane=lane)
            keys.append(lane_key)
            aggregate_to_lane_keys.setdefault(aggregate_key, set()).add(lane_key)
        event_name = str(event.get("event") or "")
        for key in keys:
            row = grouped.setdefault(
                key,
                {
                    "key": key,
                    "provider": provider or "unknown",
                    "model": model or "unknown",
                    "attempts": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "rate_limited": 0,
                    "status": "normal",
                    "reasons": [],
                },
            )
            if lane is not None and key.endswith(f"lane:{lane}"):
                row["lane"] = lane
            row["attempts"] += 1
            if event_name in SUCCESS_EVENTS:
                row["succeeded"] += 1
            elif event_name in FAILED_EVENTS:
                row["failed"] += 1
            if _is_rate_limited(event):
                row["rate_limited"] += 1

    min_rate_limit_events = max(1, int(min_rate_limit_events or 1))
    ratio_threshold = max(0.0, min(float(rate_limit_ratio or 0.50), 1.0))
    cooled: set[str] = set()
    for key, row in grouped.items():
        attempts = max(_int(row.get("attempts")), 1)
        rate_limited = _int(row.get("rate_limited"))
        succeeded = _int(row.get("succeeded"))
        row["rate_limit_ratio"] = round(rate_limited / attempts, 4)
        should_cool = rate_limited >= min_rate_limit_events and (
            succeeded == 0 or row["rate_limit_ratio"] >= ratio_threshold
        )
        if not should_cool:
            continue
        if "lane:" in key or not aggregate_to_lane_keys.get(key):
            row["status"] = "cooldown"
            row["reasons"] = ["provider_rate_limited"]
            cooled.add(key)
        else:
            row["status"] = "degraded"
            row["reasons"] = ["provider_has_rate_limited_lanes"]

    return {
        "status": "degraded" if cooled else "normal",
        "cooldown_keys": sorted(cooled),
        "lanes": [grouped[key] for key in sorted(grouped)],
    }


def _pool_lane_key(entry: dict[str, Any], lane: int) -> tuple[str, str]:
    card = resolve_extraction_provider_card(entry)
    provider_model = _lane_key(provider=card.provider, model=card.model or str(entry.get("model") or ""))
    specific = _lane_key(
        provider=card.provider,
        model=card.model or str(entry.get("model") or ""),
        lane=lane,
    )
    return provider_model, specific


def filter_extraction_pool_by_provider_health(
    pool: list[dict[str, Any]],
    health: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Remove only cooled-down provider lanes, never the whole pool."""

    if not pool or not health:
        return pool, []
    cooldown_keys = set(str(key) for key in health.get("cooldown_keys") or [])
    if not cooldown_keys:
        return pool, []

    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for lane, entry in enumerate(pool):
        provider_model, specific = _pool_lane_key(entry, lane)
        if provider_model in cooldown_keys or specific in cooldown_keys:
            card = resolve_extraction_provider_card(entry)
            skipped.append(
                {
                    "lane": lane,
                    "provider": card.provider,
                    "model": card.model,
                    "reason": "provider_rate_limited",
                }
            )
        else:
            kept.append(entry)

    if kept:
        return kept, skipped
    return pool, []


def adapt_extraction_pool_concurrency(
    pool: list[dict[str, Any]],
    health: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply conservative provider canary/rate-limit caps to lane budgets.

    Configured concurrency remains the operator ceiling. Prompt-only LongCat
    lanes must earn their way from a two-request canary to wider concurrency
    using recently accepted outputs; an explicit ``provider_canary_passed``
    flag is required before using the full configured ceiling.
    """

    if not pool:
        return pool, []
    rows = {
        str(row.get("key") or ""): row
        for row in ((health or {}).get("lanes") or [])
        if isinstance(row, dict)
    }
    adapted: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []
    for lane, original in enumerate(pool):
        entry = dict(original)
        extra = entry.get("extra_params") or {}
        if not isinstance(extra, dict):
            extra = {}
        card = resolve_extraction_provider_card(entry)
        configured = max(1, int(entry.get("max_concurrent") or 1))
        effective = configured
        aggregate_key, _ = _pool_lane_key(entry, lane)
        row = rows.get(aggregate_key) or {}
        successes = _int(row.get("succeeded"))
        rate_limited = _int(row.get("rate_limited"))
        rate_limit_ratio = float(row.get("rate_limit_ratio") or 0.0)
        reasons: list[str] = []

        if card.provider == "longcat" and not bool(extra.get("provider_canary_passed")):
            initial_cap = max(1, int(extra.get("canary_max_concurrent") or 2))
            if successes < 20:
                earned_cap = initial_cap
            elif successes < 100:
                earned_cap = max(initial_cap, 4)
            elif successes < 500:
                earned_cap = max(initial_cap, 8)
            else:
                earned_cap = max(initial_cap, 16)
            effective = min(effective, earned_cap)
            if effective < configured:
                reasons.append("provider_canary_ramp")

        if rate_limited > 0 and rate_limit_ratio >= 0.10:
            reduced = max(1, effective // 2)
            if reduced < effective:
                effective = reduced
                reasons.append("recent_rate_limit_backoff")

        if effective != configured:
            entry["max_concurrent"] = effective
            adjustments.append(
                {
                    "lane": lane,
                    "provider": card.provider,
                    "model": card.model,
                    "configured": configured,
                    "effective": effective,
                    "recent_successes": successes,
                    "recent_rate_limited": rate_limited,
                    "reasons": reasons,
                }
            )
        adapted.append(entry)
    return adapted, adjustments


async def load_recent_provider_lane_health(
    db: Any,
    *,
    corpus_id: str,
    window_minutes: int = 30,
    limit: int = 2000,
    min_rate_limit_events: int = 5,
    rate_limit_ratio: float = 0.50,
) -> dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(minutes=max(1, int(window_minutes or 30)))
    limit = max(1, min(int(limit or 2000), 10_000))
    rows = await db["ghost_b_error_events"].find(
        {
            "corpus_id": corpus_id,
            "created_at": {"$gte": cutoff},
            "event": {
                "$in": sorted(RATE_LIMIT_EVENTS | SUCCESS_EVENTS | FAILED_EVENTS)
            },
        },
        {
            "_id": 0,
            "event": 1,
            "provider": 1,
            "provider_card": 1,
            "model": 1,
            "lane": 1,
            "error_type": 1,
            "error_message": 1,
            "created_at": 1,
        },
    ).sort("created_at", -1).limit(limit).to_list(length=limit)
    health = summarize_provider_lane_health(
        rows,
        min_rate_limit_events=min_rate_limit_events,
        rate_limit_ratio=rate_limit_ratio,
    )
    health["window_minutes"] = max(1, int(window_minutes or 30))
    health["sample_size"] = len(rows)
    return health
