"""Shared helpers for ingestion model-pool lane circuit breakers."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx


FatalErrorTier = Literal["hard", "soft"]

HARD_FATAL_STATUS_CODES = {401, 402}
SOFT_FATAL_STATUS_CODES = {403}
SOFT_FATAL_DISABLE_STRIKES = 2

_SHARED_PROVIDER_SEMAPHORES: dict[tuple[int, str, str], asyncio.Semaphore] = {}


def shared_provider_semaphore(
    entry: dict[str, Any],
    *,
    lane: int,
    limit: int,
) -> asyncio.Semaphore:
    """Return one event-loop-local budget for a provider credential.

    Ghost A and Ghost B can use the same provider card concurrently. Their
    lane-local worker counts must not be added together or one key configured
    for eight calls can receive sixteen. A hashed credential identity lets the
    two pipelines share capacity without retaining or logging the secret.
    """

    normalized_limit = max(1, int(limit or 1))
    raw_key = str(entry.get("api_key") or "").strip()
    if raw_key:
        credential_id = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    else:
        credential_id = f"lane:{lane}:{entry.get('model') or ''}"
    endpoint = str(
        entry.get("base_url")
        or entry.get("api_base")
        or entry.get("provider_preset")
        or entry.get("provider")
        or "litellm"
    ).rstrip("/")
    key = (id(asyncio.get_running_loop()), endpoint, credential_id)
    semaphore = _SHARED_PROVIDER_SEMAPHORES.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(normalized_limit)
        _SHARED_PROVIDER_SEMAPHORES[key] = semaphore
    return semaphore

HARD_FATAL_ERROR_MARKERS = (
    "insufficient balance",
    "insufficient credits",
    "insufficient credit",
    "not enough balance",
    "not enough credits",
    "no credits",
    "out of credits",
    "payment required",
    "billing",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "unauthorized",
    "account disabled",
    "account_deactivated",
)

SOFT_FATAL_ERROR_MARKERS = (
    "forbidden",
    "quota exceeded",
    "quota_exceeded",
    "daily quota",
    "free-models-per-day",
    "free usage limit",
    "no endpoints for this model",
    "model not found",
)


class FatalLaneError(Exception):
    """Raised when a pool entry should be disabled for this batch."""

    def __init__(self, original: Exception):
        self.original = original
        super().__init__(provider_error_summary(original))


class RateLimitedLaneError(Exception):
    """Raised when a pool entry should cool down without failing the chunk."""

    def __init__(self, original: Exception, *, retry_after_seconds: float):
        self.original = original
        self.retry_after_seconds = max(0.5, float(retry_after_seconds or 0.5))
        super().__init__(provider_error_summary(original))


def _response_text(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.text or ""
    except Exception:
        return ""


def provider_error_summary(exc: Exception, *, max_chars: int = 500) -> str:
    """Compact provider/LiteLLM error string safe for logs and audit rows."""

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        text = _response_text(exc).strip()
        if text:
            return f"HTTP {status}: {text[:max_chars]}"
        return f"HTTP {status}: {exc}"
    return str(exc)[:max_chars]


def is_fatal_provider_error(exc: Exception) -> bool:
    """True when retrying the same pool entry is expected to keep failing.

    This intentionally does not treat generic 429 rate limits as fatal. Daily
    caps, balance exhaustion, bad keys, disabled accounts, and unavailable
    models are fatal for the current ingest batch and should be rerouted.
    """

    return provider_error_tier(exc) is not None


def is_rate_limit_provider_error(exc: Exception) -> bool:
    """True for provider throttling that should be retried after cooldown."""

    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response is not None
        and exc.response.status_code == 429
    )


def rate_limit_retry_after_seconds(
    exc: Exception,
    *,
    default_seconds: float = 5.0,
    max_seconds: float = 60.0,
) -> float:
    """Resolve Retry-After for 429s, capped so other lanes keep making progress."""

    if not isinstance(exc, httpx.HTTPStatusError) or exc.response is None:
        return default_seconds
    retry_after = exc.response.headers.get("retry-after")
    if not retry_after:
        return default_seconds
    retry_after = retry_after.strip()
    try:
        return min(max_seconds, max(0.5, float(retry_after)))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(retry_after)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return min(
            max_seconds,
            max(0.5, (dt - datetime.now(timezone.utc)).total_seconds()),
        )
    except Exception:
        return default_seconds


def provider_error_tier(exc: Exception) -> FatalErrorTier | None:
    """Classify provider errors conservatively for lane circuit breaking.

    Hard fatal errors are clear account/key/payment failures. Soft fatal errors
    look lane-specific but can sometimes be transient provider behavior, so the
    caller should require repeated strikes before disabling that lane.
    """

    text = provider_error_summary(exc).lower()
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in HARD_FATAL_STATUS_CODES:
            return "hard"
        if status in SOFT_FATAL_STATUS_CODES:
            return "soft"
    if any(marker in text for marker in HARD_FATAL_ERROR_MARKERS):
        return "hard"
    if any(marker in text for marker in SOFT_FATAL_ERROR_MARKERS):
        return "soft"
    return None
