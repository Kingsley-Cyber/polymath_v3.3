from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from services.llm_lane_pool import (
    is_fatal_provider_error,
    is_rate_limit_provider_error,
    rate_limit_retry_after_seconds,
    shared_provider_semaphore,
)


def _http_error(status_code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(
        status_code,
        request=request,
        headers=headers,
        text='{"error":{"type":"rate_limit_error"}}',
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=response,
    )


def test_429_is_rate_limited_not_fatal() -> None:
    exc = _http_error(429, retry_after="12")

    assert is_rate_limit_provider_error(exc) is True
    assert is_fatal_provider_error(exc) is False
    assert rate_limit_retry_after_seconds(exc) == 12


def test_retry_after_http_date_is_capped() -> None:
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    exc = _http_error(429, retry_after=format_datetime(future))

    assert rate_limit_retry_after_seconds(exc, max_seconds=30) == 30


def test_missing_retry_after_uses_default() -> None:
    exc = _http_error(429)

    assert rate_limit_retry_after_seconds(exc, default_seconds=7) == 7


def test_same_provider_credential_shares_one_budget(event_loop) -> None:
    async def resolve():
        first = shared_provider_semaphore(
            {
                "model": "openai/tencent/Hy3",
                "base_url": "https://api.siliconflow.example/v1",
                "api_key": "shared-secret",
            },
            lane=1,
            limit=8,
        )
        second = shared_provider_semaphore(
            {
                "model": "openai/tencent/Hy3",
                "base_url": "https://api.siliconflow.example/v1",
                "api_key": "shared-secret",
            },
            lane=1,
            limit=8,
        )
        other_key = shared_provider_semaphore(
            {
                "model": "openai/tencent/Hy3",
                "base_url": "https://api.siliconflow.example/v1",
                "api_key": "other-secret",
            },
            lane=2,
            limit=8,
        )
        return first, second, other_key

    first, second, other_key = event_loop.run_until_complete(resolve())

    assert first is second
    assert first is not other_key
