"""Priority lease server for the Mac's shared Metal GPU.

EMBED leases have high priority.  RERANK leases are selected when no embed
is waiting, when the oldest rerank has waited past the starvation threshold,
or after a bounded embed burst.  The server never invokes model code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
import time
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

WorkloadClass = Literal["embed", "rerank"]
PERCENTILE_METHOD_VERSION = "nearest_rank.v1"


class AcquireRequest(BaseModel):
    workload_class: WorkloadClass
    client_id: str = Field(min_length=1, max_length=200)
    timeout_ms: int = Field(default=30_000, ge=1, le=120_000)
    hold_target_ms: int = Field(default=500, ge=1, le=10_000)


class ReleaseRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=100)
    client_id: str = Field(min_length=1, max_length=200)


@dataclass
class _Waiter:
    workload_class: WorkloadClass
    client_id: str
    sequence: int
    enqueued_at: float
    hold_target_ms: int
    future: asyncio.Future["_Lease"]


@dataclass(frozen=True)
class _Lease:
    lease_id: str
    workload_class: WorkloadClass
    client_id: str
    acquired_at: float
    wait_ms: float
    hold_target_ms: int


class PriorityLeaseScheduler:
    """One-device, two-class scheduler with bounded low-priority starvation."""

    def __init__(
        self,
        *,
        max_embed_burst: int = 1,
        rerank_starvation_seconds: float = 0.5,
        stale_lease_seconds: float = 75.0,
    ) -> None:
        self.max_embed_burst = max(1, max_embed_burst)
        self.rerank_starvation_seconds = max(0.001, rerank_starvation_seconds)
        self.stale_lease_seconds = max(1.0, stale_lease_seconds)
        self._lock = asyncio.Lock()
        self._waiters: list[_Waiter] = []
        self._sequence = 0
        self._active: _Lease | None = None
        self._embed_burst = 0
        self._grants = {"embed": 0, "rerank": 0}
        self._releases = {"embed": 0, "rerank": 0}
        self._over_target_count = 0
        self._stale_recovery_count = 0
        self._wait_ms = {"embed": [], "rerank": []}
        self._hold_ms = {"embed": [], "rerank": []}
        self._wait_sample_count = {"embed": 0, "rerank": 0}
        self._hold_sample_count = {"embed": 0, "rerank": 0}

    async def acquire(
        self,
        workload_class: WorkloadClass,
        client_id: str,
        timeout_ms: int,
        hold_target_ms: int,
    ) -> _Lease:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Lease] = loop.create_future()
        async with self._lock:
            self._sequence += 1
            waiter = _Waiter(
                workload_class=workload_class,
                client_id=client_id,
                sequence=self._sequence,
                enqueued_at=time.monotonic(),
                hold_target_ms=hold_target_ms,
                future=future,
            )
            self._waiters.append(waiter)
            self._dispatch_locked()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout_ms / 1000.0)
        except BaseException:
            async with self._lock:
                if (
                    future.done()
                    and not future.cancelled()
                    and self._active is not None
                    and self._active.lease_id == future.result().lease_id
                ):
                    # The deadline and dispatch crossed. The HTTP client will
                    # not receive this lease, so return it to the scheduler
                    # immediately instead of waiting for stale recovery.
                    self._active = None
                else:
                    self._waiters = [
                        item for item in self._waiters if item.future is not future
                    ]
                if not future.done():
                    future.cancel()
                self._dispatch_locked()
            raise

    async def release(self, lease_id: str, client_id: str) -> dict[str, float | bool]:
        async with self._lock:
            active = self._active
            if active is None:
                raise KeyError("no active lease")
            if active.lease_id != lease_id or active.client_id != client_id:
                raise PermissionError("lease ownership mismatch")
            hold_ms = (time.monotonic() - active.acquired_at) * 1000.0
            over_target = hold_ms > active.hold_target_ms
            self._hold_ms[active.workload_class].append(hold_ms)
            self._hold_ms[active.workload_class] = self._hold_ms[active.workload_class][
                -512:
            ]
            self._hold_sample_count[active.workload_class] += 1
            self._releases[active.workload_class] += 1
            if over_target:
                self._over_target_count += 1
            self._active = None
            self._dispatch_locked()
            return {"hold_ms": hold_ms, "over_target": over_target}

    async def recover_stale_lease(self) -> bool:
        async with self._lock:
            if self._active is None:
                return False
            age = time.monotonic() - self._active.acquired_at
            if age < self.stale_lease_seconds:
                return False
            self._active = None
            self._stale_recovery_count += 1
            self._dispatch_locked()
            return True

    def _dispatch_locked(self) -> None:
        if self._active is not None:
            return
        self._waiters = [item for item in self._waiters if not item.future.done()]
        if not self._waiters:
            return

        now = time.monotonic()
        embeds = [item for item in self._waiters if item.workload_class == "embed"]
        reranks = [item for item in self._waiters if item.workload_class == "rerank"]
        oldest_rerank = (
            min(reranks, key=lambda item: item.sequence) if reranks else None
        )
        rerank_aged = bool(
            oldest_rerank
            and now - oldest_rerank.enqueued_at >= self.rerank_starvation_seconds
        )
        force_rerank = bool(
            oldest_rerank and (rerank_aged or self._embed_burst >= self.max_embed_burst)
        )
        if embeds and not force_rerank:
            selected = min(embeds, key=lambda item: item.sequence)
        elif oldest_rerank is not None:
            selected = oldest_rerank
        else:
            selected = min(embeds, key=lambda item: item.sequence)

        self._waiters.remove(selected)
        wait_ms = (now - selected.enqueued_at) * 1000.0
        lease = _Lease(
            lease_id=uuid4().hex,
            workload_class=selected.workload_class,
            client_id=selected.client_id,
            acquired_at=now,
            wait_ms=wait_ms,
            hold_target_ms=selected.hold_target_ms,
        )
        self._active = lease
        self._grants[selected.workload_class] += 1
        self._wait_ms[selected.workload_class].append(wait_ms)
        self._wait_ms[selected.workload_class] = self._wait_ms[selected.workload_class][
            -512:
        ]
        self._wait_sample_count[selected.workload_class] += 1
        if selected.workload_class == "embed":
            self._embed_burst += 1
        else:
            self._embed_burst = 0
        selected.future.set_result(lease)

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(
            len(ordered) - 1,
            max(0, math.ceil(fraction * len(ordered)) - 1),
        )
        return ordered[index]

    async def snapshot(self) -> dict:
        async with self._lock:
            active = self._active
            return {
                "active": (
                    {
                        "lease_id": active.lease_id,
                        "workload_class": active.workload_class,
                        "client_id": active.client_id,
                        "hold_ms": round(
                            (time.monotonic() - active.acquired_at) * 1000.0, 3
                        ),
                        "hold_target_ms": active.hold_target_ms,
                    }
                    if active
                    else None
                ),
                "queued": {
                    "embed": sum(
                        item.workload_class == "embed" for item in self._waiters
                    ),
                    "rerank": sum(
                        item.workload_class == "rerank" for item in self._waiters
                    ),
                },
                "grants": dict(self._grants),
                "releases": dict(self._releases),
                "release_count": sum(self._releases.values()),
                "over_target": self._over_target_count,
                "stale_recoveries": self._stale_recovery_count,
                "wait_sample_count": dict(self._wait_sample_count),
                "hold_sample_count": dict(self._hold_sample_count),
                "wait_p95_ms": {
                    key: self._percentile(values, 0.95)
                    for key, values in self._wait_ms.items()
                },
                "wait_p50_ms": {
                    key: self._percentile(values, 0.50)
                    for key, values in self._wait_ms.items()
                },
                "hold_p95_ms": {
                    key: self._percentile(values, 0.95)
                    for key, values in self._hold_ms.items()
                },
                "hold_p50_ms": {
                    key: self._percentile(values, 0.50)
                    for key, values in self._hold_ms.items()
                },
                "percentile_method": PERCENTILE_METHOD_VERSION,
                "policy": {
                    "max_embed_burst": self.max_embed_burst,
                    "rerank_starvation_seconds": self.rerank_starvation_seconds,
                    "stale_lease_seconds": self.stale_lease_seconds,
                },
            }


scheduler = PriorityLeaseScheduler(
    max_embed_burst=int(os.environ.get("ARBITER_MAX_EMBED_BURST", "1")),
    rerank_starvation_seconds=float(
        os.environ.get("ARBITER_RERANK_STARVATION_SECONDS", "0.5")
    ),
    stale_lease_seconds=float(os.environ.get("ARBITER_STALE_LEASE_SECONDS", "75")),
)
app = FastAPI(title="Polymath Metal GPU Arbiter", version="1.0.0")
_reaper_task: asyncio.Task[None] | None = None


async def _stale_reaper() -> None:
    while True:
        await asyncio.sleep(1.0)
        await scheduler.recover_stale_lease()


@app.on_event("startup")
async def _startup() -> None:
    global _reaper_task
    _reaper_task = asyncio.create_task(_stale_reaper())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _reaper_task is not None:
        _reaper_task.cancel()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "scheduler": await scheduler.snapshot()}


@app.post("/v1/acquire")
async def acquire(req: AcquireRequest) -> dict:
    try:
        lease = await scheduler.acquire(
            req.workload_class,
            req.client_id,
            req.timeout_ms,
            req.hold_target_ms,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="arbiter acquisition timed out"
        ) from exc
    return {
        "lease_id": lease.lease_id,
        "workload_class": lease.workload_class,
        "wait_ms": round(lease.wait_ms, 3),
        "hold_target_ms": lease.hold_target_ms,
    }


@app.post("/v1/release")
async def release(req: ReleaseRequest) -> dict:
    try:
        telemetry = await scheduler.release(req.lease_id, req.client_id)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "released": True,
        "hold_ms": round(float(telemetry["hold_ms"]), 3),
        "over_target": bool(telemetry["over_target"]),
    }
