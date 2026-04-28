"""Shared helpers for ingestion model-pool lane circuit breakers."""

from __future__ import annotations

from typing import Literal

import httpx


FatalErrorTier = Literal["hard", "soft"]

HARD_FATAL_STATUS_CODES = {401, 402}
SOFT_FATAL_STATUS_CODES = {403}
SOFT_FATAL_DISABLE_STRIKES = 2

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
