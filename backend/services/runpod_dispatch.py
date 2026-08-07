"""Runpod queue dispatch — submit/retry/poll/cancel plus multi-account routing.

Extracted verbatim from the retired GLiNER-Relex flash-extraction adapter
(2026-08-07 legacy purge): the queue plumbing was the only part of that module
any live lane still used — the embedding lane routes batches through it. It
carries no extraction semantics.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

import httpx

from models.schemas import RunpodFlashAccount

logger = logging.getLogger(__name__)

RUNPOD_API_BASE = "https://api.runpod.ai/v2"
TERMINAL_FAILURES = {"FAILED", "CANCELLED", "TIMED_OUT"}


def _safe_error(value: Any) -> str:
    text = str(value or "runpod request failed").replace("\n", " ")
    return text[:1000]


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return max(0.25, min(float(raw), 30.0))
        except ValueError:
            pass
    return min(float(2**attempt), 8.0)


def _extract_output(payload: dict[str, Any]) -> dict[str, Any]:
    output: Any = payload.get("output", payload)
    if isinstance(output, dict) and isinstance(output.get("output"), dict):
        output = output["output"]
    if not isinstance(output, dict):
        raise RuntimeError("Runpod job returned a non-object output")
    if output.get("success") is False:
        raise RuntimeError(
            f"Runpod worker rejected the job: {_safe_error(output.get('error'))}"
        )
    return output


async def _cancel_job(
    client: httpx.AsyncClient,
    endpoint_id: str,
    job_id: str,
    headers: dict[str, str],
) -> None:
    try:
        await client.post(
            f"{RUNPOD_API_BASE}/{endpoint_id}/cancel/{job_id}", headers=headers
        )
    except Exception:  # noqa: BLE001 - cancellation is best effort
        logger.debug(
            "Runpod cancellation failed endpoint=%s job=%s", endpoint_id, job_id
        )


async def _submit_and_wait(
    client: httpx.AsyncClient,
    *,
    endpoint_id: str,
    api_key: str,
    request: dict[str, Any],
    timeout_seconds: int,
    poll_interval_seconds: float,
    job_event_sink: Any = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    submit_url = f"{RUNPOD_API_BASE}/{endpoint_id}/run"
    response: httpx.Response | None = None
    for attempt in range(3):
        # The generated handler calls ``fn(**job_input)`` with one named
        # argument (payload), so direct REST calls must preserve that
        # keyword envelope.
        response = await client.post(
            submit_url,
            headers=headers,
            json={"input": {"payload": request}},
        )
        if response.status_code < 500 and response.status_code != 429:
            break
        if attempt < 2:
            await asyncio.sleep(_retry_delay(response, attempt))
    assert response is not None
    response.raise_for_status()
    submitted = response.json()
    job_id = str(submitted.get("id") or "")
    if not job_id:
        raise RuntimeError("Runpod submission returned no job id")
    if job_event_sink is not None:
        emitted = job_event_sink(
            {
                "event": "submitted",
                "endpoint_id": endpoint_id,
                "batch_id": str(request.get("batch_id") or ""),
                "job_id": job_id,
            }
        )
        if inspect.isawaitable(emitted):
            await emitted

    deadline = time.monotonic() + timeout_seconds
    status_url = f"{RUNPOD_API_BASE}/{endpoint_id}/status/{job_id}"
    try:
        while True:
            if time.monotonic() >= deadline:
                if job_event_sink is not None:
                    emitted = job_event_sink(
                        {
                            "event": "terminal",
                            "endpoint_id": endpoint_id,
                            "batch_id": str(request.get("batch_id") or ""),
                            "job_id": job_id,
                            "status": "CLIENT_TIMEOUT",
                        }
                    )
                    if inspect.isawaitable(emitted):
                        await emitted
                await _cancel_job(client, endpoint_id, job_id, headers)
                raise TimeoutError(f"Runpod job exceeded {timeout_seconds}s timeout")
            status_response: httpx.Response | None = None
            for attempt in range(3):
                status_response = await client.get(status_url, headers=headers)
                if (
                    status_response.status_code < 500
                    and status_response.status_code != 429
                ):
                    break
                if attempt < 2:
                    await asyncio.sleep(_retry_delay(status_response, attempt))
            assert status_response is not None
            status_response.raise_for_status()
            body = status_response.json()
            status = str(body.get("status") or "").upper()
            if status == "COMPLETED":
                if job_event_sink is not None:
                    emitted = job_event_sink(
                        {
                            "event": "terminal",
                            "endpoint_id": endpoint_id,
                            "batch_id": str(request.get("batch_id") or ""),
                            "job_id": job_id,
                            "status": status,
                            "delay_time_ms": body.get("delayTime"),
                            "execution_time_ms": body.get("executionTime"),
                        }
                    )
                    if inspect.isawaitable(emitted):
                        await emitted
                output = _extract_output(body)
                output["_runpod_job"] = {
                    "job_id": job_id,
                    "delay_time_ms": body.get("delayTime"),
                    "execution_time_ms": body.get("executionTime"),
                }
                return output
            if status in TERMINAL_FAILURES:
                if job_event_sink is not None:
                    emitted = job_event_sink(
                        {
                            "event": "terminal",
                            "endpoint_id": endpoint_id,
                            "batch_id": str(request.get("batch_id") or ""),
                            "job_id": job_id,
                            "status": status,
                            "delay_time_ms": body.get("delayTime"),
                            "execution_time_ms": body.get("executionTime"),
                        }
                    )
                    if inspect.isawaitable(emitted):
                        await emitted
                raise RuntimeError(
                    f"Runpod job {status.lower()}: {_safe_error(body.get('error'))}"
                )
            await asyncio.sleep(poll_interval_seconds)
    except asyncio.CancelledError:
        await _cancel_job(client, endpoint_id, job_id, headers)
        raise


class _AccountState:
    """Mutable dispatch state for one Runpod account (P2.7c)."""

    __slots__ = (
        "account",
        "api_key",
        "semaphore",
        "in_flight",
        "batches",
        "failures",
        "failovers",
    )

    def __init__(self, account: RunpodFlashAccount, api_key: str) -> None:
        self.account = account
        self.api_key = api_key
        self.semaphore = asyncio.Semaphore(account.request_concurrency)
        # Batches assigned and not yet finished (queued-on-semaphore included).
        self.in_flight = 0
        # batches = dispatch attempts routed here (failover retries included);
        # failures = attempts that failed here; failovers = batches this
        # account accepted after they failed on a different account.
        self.batches = 0
        self.failures = 0
        self.failovers = 0


class _AccountDispatcher:
    """Least-in-flight batch router across Runpod accounts (P2.7c).

    Selection is synchronous: the winner is the enabled account with the
    fewest assigned-and-unfinished batches; ties go to the higher weight,
    then to the alphabetically first name. Per-account concurrency is
    enforced by each account's own semaphore, sized from its
    ``request_concurrency``.
    """

    def __init__(self, accounts: list[tuple[RunpodFlashAccount, str]]) -> None:
        self._states = [_AccountState(account, key) for account, key in accounts]

    @property
    def size(self) -> int:
        return len(self._states)

    def select(self, exclude: frozenset[str] = frozenset()) -> _AccountState | None:
        candidates = [
            state for state in self._states if state.account.name not in exclude
        ]
        if not candidates:
            return None
        chosen = min(
            candidates,
            key=lambda state: (
                state.in_flight,
                -state.account.weight,
                state.account.name,
            ),
        )
        chosen.in_flight += 1
        chosen.batches += 1
        return chosen

    @staticmethod
    def release(state: _AccountState) -> None:
        state.in_flight -= 1

    def summary(self) -> dict[str, dict[str, int]]:
        return {
            state.account.name: {
                "batches": state.batches,
                "failures": state.failures,
                "failovers": state.failovers,
            }
            for state in sorted(self._states, key=lambda item: item.account.name)
        }
