"""Fail-open client for the host-local Metal GPU arbiter.

The arbiter changes admission order only.  Callers execute their existing
model function inside :meth:`GpuArbiterClient.lease`; if the service is not
available, the context manager yields immediately and logs the named
``gpu_arbiter_unavailable`` alert.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
import threading
import time
from typing import Iterator

import httpx

ALERT_NAME = "gpu_arbiter_unavailable"


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LeaseResult:
    """Admission telemetry returned to a sidecar."""

    lease_id: str | None
    wait_ms: float
    fail_open: bool


class GpuArbiterClient:
    """Small synchronous client intended for model worker threads."""

    def __init__(
        self,
        workload_class: str,
        *,
        enabled: bool | None = None,
        base_url: str | None = None,
        acquire_timeout_seconds: float | None = None,
        hold_target_ms: int | None = None,
        logger: logging.Logger | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if workload_class not in {"embed", "rerank"}:
            raise ValueError(f"unsupported arbiter workload class: {workload_class!r}")
        self.workload_class = workload_class
        self.enabled = (
            _env_enabled("ARBITER_ENABLED", False) if enabled is None else enabled
        )
        host = os.environ.get("ARBITER_HOST", "127.0.0.1")
        port = os.environ.get("ARBITER_PORT", "8085")
        self.base_url = (base_url or f"http://{host}:{port}").rstrip("/")
        self.acquire_timeout_seconds = (
            float(os.environ.get("ARBITER_ACQUIRE_TIMEOUT_SECONDS", "30"))
            if acquire_timeout_seconds is None
            else acquire_timeout_seconds
        )
        default_target = 500 if workload_class == "rerank" else 2000
        target_env = (
            "ARBITER_RERANK_HOLD_TARGET_MS"
            if workload_class == "rerank"
            else "ARBITER_EMBED_HOLD_TARGET_MS"
        )
        self.hold_target_ms = (
            int(os.environ.get(target_env, str(default_target)))
            if hold_target_ms is None
            else hold_target_ms
        )
        self._logger = logger or logging.getLogger(f"gpu_arbiter.{workload_class}")
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(
                connect=0.25,
                read=max(0.25, self.acquire_timeout_seconds + 1.0),
                write=0.5,
                pool=0.5,
            )
        )
        self._client_id = f"{workload_class}:{os.getpid()}"
        self._last_alert_at = {"acquire": 0.0, "release": 0.0}
        self._alert_lock = threading.Lock()

    def _alert_unavailable(self, operation: str, exc: BaseException) -> None:
        """Rate-limit the required named alert without hiding first failure."""
        now = time.monotonic()
        with self._alert_lock:
            if now - self._last_alert_at.get(operation, 0.0) < 5.0:
                return
            self._last_alert_at[operation] = now
        self._logger.warning(
            "%s workload=%s operation=%s error=%s",
            ALERT_NAME,
            self.workload_class,
            operation,
            f"{type(exc).__name__}: {exc}",
        )

    def acquire(self) -> LeaseResult:
        if not self.enabled:
            return LeaseResult(lease_id=None, wait_ms=0.0, fail_open=False)
        started = time.monotonic()
        try:
            response = self._http.post(
                f"{self.base_url}/v1/acquire",
                json={
                    "workload_class": self.workload_class,
                    "client_id": self._client_id,
                    "timeout_ms": int(self.acquire_timeout_seconds * 1000),
                    "hold_target_ms": self.hold_target_ms,
                },
            )
            response.raise_for_status()
            payload = response.json()
            lease_id = str(payload["lease_id"])
            return LeaseResult(
                lease_id=lease_id,
                wait_ms=float(payload.get("wait_ms", 0.0)),
                fail_open=False,
            )
        except Exception as exc:
            self._alert_unavailable("acquire", exc)
            return LeaseResult(
                lease_id=None,
                wait_ms=(time.monotonic() - started) * 1000.0,
                fail_open=True,
            )

    def release(self, lease_id: str | None) -> None:
        if not self.enabled or not lease_id:
            return
        try:
            response = self._http.post(
                f"{self.base_url}/v1/release",
                json={"lease_id": lease_id, "client_id": self._client_id},
                timeout=1.0,
            )
            response.raise_for_status()
        except Exception as exc:
            # The model work is already complete.  The server's stale-lease
            # watchdog is the recovery path; computation must not be retried.
            self._alert_unavailable("release", exc)

    @contextmanager
    def lease(self) -> Iterator[LeaseResult]:
        """Acquire a scheduling lease, or yield direct in named fail-open mode."""
        result = self.acquire()
        try:
            yield result
        finally:
            self.release(result.lease_id)
