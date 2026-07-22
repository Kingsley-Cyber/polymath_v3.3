"""Bounded lane orchestration for research workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable, Literal

LaneStatus = Literal["success", "fallback", "failed"]


@dataclass(frozen=True)
class ResearchLaneSpec:
    name: str
    run: Callable[[], Awaitable[Any]]
    fallback: Callable[[], Awaitable[Any]] | None = None
    max_attempts: int = 1
    timeout_seconds: float | None = None
    required: bool = False


@dataclass(frozen=True)
class ResearchLaneOutcome:
    name: str
    status: LaneStatus
    attempts: int
    duration_ms: int
    result: Any
    error_type: str | None = None
    error: str | None = None
    required: bool = False

    def receipt(self) -> dict[str, Any]:
        return {
            "lane": self.name,
            "status": self.status,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "item_count": _item_count(self.result),
            "error_type": self.error_type,
            "error": self.error,
            "required": self.required,
        }


def _item_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 1


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return text[:500]


async def _run_one_lane(spec: ResearchLaneSpec, semaphore: asyncio.Semaphore) -> ResearchLaneOutcome:
    started = perf_counter()
    attempts = 0
    last_error: BaseException | None = None
    max_attempts = max(1, int(spec.max_attempts or 1))
    async with semaphore:
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                if spec.timeout_seconds and spec.timeout_seconds > 0:
                    result = await asyncio.wait_for(spec.run(), timeout=spec.timeout_seconds)
                else:
                    result = await spec.run()
                return ResearchLaneOutcome(
                    name=spec.name,
                    status="success",
                    attempts=attempts,
                    duration_ms=int((perf_counter() - started) * 1000),
                    result=result,
                    required=spec.required,
                )
            except Exception as exc:  # noqa: BLE001 - lane isolation boundary
                last_error = exc
        if spec.fallback is not None:
            try:
                if spec.timeout_seconds and spec.timeout_seconds > 0:
                    result = await asyncio.wait_for(spec.fallback(), timeout=spec.timeout_seconds)
                else:
                    result = await spec.fallback()
                return ResearchLaneOutcome(
                    name=spec.name,
                    status="fallback",
                    attempts=attempts,
                    duration_ms=int((perf_counter() - started) * 1000),
                    result=result,
                    error_type=type(last_error).__name__ if last_error else None,
                    error=_safe_error(last_error) if last_error else None,
                    required=spec.required,
                )
            except Exception as fallback_exc:  # noqa: BLE001
                last_error = fallback_exc
        return ResearchLaneOutcome(
            name=spec.name,
            status="failed",
            attempts=attempts,
            duration_ms=int((perf_counter() - started) * 1000),
            result=None,
            error_type=type(last_error).__name__ if last_error else None,
            error=_safe_error(last_error) if last_error else None,
            required=spec.required,
        )


async def run_research_lanes(
    lanes: list[ResearchLaneSpec],
    *,
    max_concurrency: int = 2,
) -> list[ResearchLaneOutcome]:
    """Run independent research lanes with bounded concurrency.

    Results preserve lane input order so downstream receipts are stable.
    """
    if not lanes:
        return []
    semaphore = asyncio.Semaphore(max(1, int(max_concurrency or 1)))
    return await asyncio.gather(*[_run_one_lane(lane, semaphore) for lane in lanes])
