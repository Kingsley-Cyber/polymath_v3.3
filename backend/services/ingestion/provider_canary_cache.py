"""Secret-safe provider canary cache shared across corpora and pool roles."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

CANARY_CACHE_COLLECTION = "provider_canary_cache"
CANARY_CONTRACT_VERSION = "provider_canary.v1"


def provider_canary_fingerprint(entry: dict[str, Any]) -> str:
    """Hash the credential and route contract without retaining plaintext."""

    credential = str(entry.get("api_key") or "")
    payload = {
        "contract_version": CANARY_CONTRACT_VERSION,
        "credential_sha256": hashlib.sha256(credential.encode("utf-8")).hexdigest(),
        "provider": entry.get("provider_preset") or entry.get("provider"),
        "model": entry.get("model"),
        "base_url": str(entry.get("base_url") or "").rstrip("/"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def load_cached_canary(
    db: Any,
    *,
    entry: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any] | None:
    now = now or datetime.utcnow()
    fingerprint = provider_canary_fingerprint(entry)
    try:
        row = await db[CANARY_CACHE_COLLECTION].find_one(
            {"_id": fingerprint},
            {"_id": 0, "credential_sha256": 0},
        )
    except Exception:
        return None
    if not row:
        return None
    valid_until = row.get("valid_until")
    cooldown_until = row.get("cooldown_until")
    if row.get("ok") and isinstance(valid_until, datetime) and valid_until > now:
        return {**row, "cached": True}
    if not row.get("ok") and isinstance(cooldown_until, datetime) and cooldown_until > now:
        return {**row, "cached": True}
    return None


async def record_canary(
    db: Any,
    *,
    entry: dict[str, Any],
    ok: bool,
    status: int | None,
    latency_ms: int | None,
    error_class: str | None = None,
    ttl_seconds: int = 3600,
    cooldown_seconds: int = 60,
    now: datetime | None = None,
) -> None:
    now = now or datetime.utcnow()
    fingerprint = provider_canary_fingerprint(entry)
    credential_hash = hashlib.sha256(
        str(entry.get("api_key") or "").encode("utf-8")
    ).hexdigest()
    row = {
        "contract_version": CANARY_CONTRACT_VERSION,
        "credential_sha256": credential_hash,
        "provider": entry.get("provider_preset") or entry.get("provider"),
        "model": entry.get("model"),
        "base_url": str(entry.get("base_url") or "").rstrip("/"),
        "ok": bool(ok),
        "status": status,
        "latency_ms": latency_ms,
        "error_class": error_class,
        "checked_at": now,
        "valid_until": now + timedelta(seconds=max(30, int(ttl_seconds or 0)))
        if ok
        else None,
        "cooldown_until": now
        + timedelta(seconds=max(5, int(cooldown_seconds or 0)))
        if not ok
        else None,
        "updated_at": now,
    }
    try:
        await db[CANARY_CACHE_COLLECTION].update_one(
            {"_id": fingerprint},
            {"$set": row, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception:
        pass

