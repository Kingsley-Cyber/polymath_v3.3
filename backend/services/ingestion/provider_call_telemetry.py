"""Secret-free provider call accounting for ingestion phases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


COLLECTION = "ingest_provider_call_metrics"


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def provider_family(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    if "longcat" in text:
        return "longcat"
    if "silicon" in text or "hy3" in text or "tencent" in text:
        return "siliconflow"
    if "polymath-extract" in text or "vllm" in text or "rtx" in text:
        return "rtx_vllm"
    if "local" in text or "mlx" in text:
        return "local"
    return text[:80] or "unknown"


async def record_provider_call(db: Any, event: dict[str, Any]) -> None:
    """Persist only bounded accounting fields; never prompts, outputs, or keys."""
    family = provider_family(event.get("provider") or event.get("model"))
    item_count = max(1, _int(event.get("item_count")))
    accepted = min(item_count, _int(event.get("accepted_count")))
    rejected = max(_int(event.get("rejected_count")), item_count - accepted)
    doc = {
        "corpus_id": str(event.get("corpus_id") or ""),
        "phase": str(event.get("phase") or "unknown")[:40],
        "provider_family": family,
        "model": str(event.get("model") or "unknown")[:200],
        "local_compute": family in {"local", "rtx_vllm"},
        "billable_provider": family not in {"local", "rtx_vllm"},
        "attempts": max(1, _int(event.get("attempts")) or 1),
        "item_count": item_count,
        "accepted_count": accepted,
        "rejected_count": rejected,
        "latency_ms": round(_float(event.get("latency_ms")), 2),
        "input_tokens": _int(event.get("input_tokens")),
        "output_tokens": _int(event.get("output_tokens")),
        "retries": _int(event.get("retries")),
        "rate_limited": bool(event.get("rate_limited")),
        "failure_class": str(event.get("failure_class") or "")[:120] or None,
        "created_at": event.get("created_at") or datetime.now(timezone.utc),
    }
    if not doc["corpus_id"]:
        return
    await db[COLLECTION].insert_one(doc)


async def provider_efficiency_snapshot(
    db: Any,
    *,
    corpus_id: str,
    window_hours: int = 24,
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))
    rows = await db[COLLECTION].find(
        {"corpus_id": corpus_id, "created_at": {"$gte": since}},
        {"_id": 0},
    ).limit(50_000).to_list(length=50_000)
    calls = len(rows)
    accepted = sum(_int(row.get("accepted_count")) for row in rows)
    attempted_items = sum(_int(row.get("item_count")) for row in rows)
    input_tokens = sum(_int(row.get("input_tokens")) for row in rows)
    output_tokens = sum(_int(row.get("output_tokens")) for row in rows)
    retries = sum(_int(row.get("retries")) for row in rows)
    rate_limits = sum(1 for row in rows if row.get("rate_limited"))
    billable_calls = sum(1 for row in rows if row.get("billable_provider"))
    local_calls = calls - billable_calls
    by_provider: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("provider_family") or "unknown")
        bucket = by_provider.setdefault(
            key,
            {"calls": 0, "accepted": 0, "input_tokens": 0, "output_tokens": 0},
        )
        bucket["calls"] += 1
        bucket["accepted"] += _int(row.get("accepted_count"))
        bucket["input_tokens"] += _int(row.get("input_tokens"))
        bucket["output_tokens"] += _int(row.get("output_tokens"))
    return {
        "window_hours": max(1, window_hours),
        "calls": calls,
        "billable_calls": billable_calls,
        "local_calls": local_calls,
        "attempted_items": attempted_items,
        "accepted_artifacts": accepted,
        "calls_per_artifact": round(calls / accepted, 4) if accepted else None,
        "tokens_per_artifact": (
            round((input_tokens + output_tokens) / accepted, 2) if accepted else None
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "retries": retries,
        "rate_limits": rate_limits,
        "providers": by_provider,
    }


async def record_ghost_b_event(db: Any, event: dict[str, Any]) -> None:
    name = str(event.get("event") or "")
    if name == "ghost_b_microbatch_call":
        await record_provider_call(
            db,
            {
                "corpus_id": event.get("corpus_id"),
                "phase": "extraction",
                "provider": (event.get("provider_card") or {}).get("provider"),
                "model": event.get("model"),
                "item_count": event.get("item_count"),
                "accepted_count": event.get("accepted_count"),
                "rejected_count": event.get("rejected_count"),
                "latency_ms": _float(event.get("duration_seconds")) * 1000,
                "input_tokens": event.get("prompt_tokens"),
                "output_tokens": event.get("completion_tokens"),
                "attempts": 1,
                "rate_limited": event.get("rate_limited"),
                "failure_class": event.get("error_type"),
            },
        )
        return
    if event.get("provider_call_accounted"):
        return
    if not name.startswith("ghost_b_attempt_"):
        return
    succeeded = name.startswith("ghost_b_attempt_succeeded")
    await record_provider_call(
        db,
        {
            "corpus_id": event.get("corpus_id"),
            "phase": "extraction",
            "provider": (event.get("provider_card") or {}).get("provider"),
            "model": event.get("model"),
            "item_count": 1,
            "accepted_count": 1 if succeeded else 0,
            "rejected_count": 0 if succeeded else 1,
            "latency_ms": _float(event.get("duration_seconds")) * 1000,
            "input_tokens": event.get("prompt_tokens") or event.get("input_tokens"),
            "output_tokens": event.get("completion_tokens"),
            "attempts": 1,
            "retries": max(0, _int(event.get("attempt")) - 1),
            "rate_limited": name == "ghost_b_attempt_rate_limited",
            "failure_class": event.get("error_type"),
        },
    )
