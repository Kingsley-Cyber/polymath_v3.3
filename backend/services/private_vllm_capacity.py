"""Capacity planning helpers for a private vLLM extraction server."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class PrivateVllmCapacity:
    ready: bool
    gpu_vram_total_gb: float | None = None
    gpu_vram_used_gb: float | None = None
    gpu_vram_free_gb: float | None = None
    usable_vram_gb: float | None = None
    reserved_vram_gb: float | None = None
    estimated_vram_per_request_gb: float | None = None
    recommended_concurrency: int | None = None
    running_requests: int = 0
    waiting_requests: int = 0
    source: str = "status"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _int_or_none(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def parse_private_vllm_status(payload: dict[str, Any] | None) -> PrivateVllmCapacity:
    data = payload or {}
    nested_capacity = data.get("capacity")
    capacity = nested_capacity if isinstance(nested_capacity, dict) else {}
    nested_gpu = data.get("gpu_memory")
    gpu = nested_gpu if isinstance(nested_gpu, dict) else {}
    total = (
        _float_or_none(data.get("gpu_vram_total_gb"))
        or _float_or_none(data.get("vram_total_gb"))
        or _float_or_none(data.get("total_vram_gb"))
        or _float_or_none(capacity.get("gpu_vram_total_gb"))
        or _float_or_none(capacity.get("vram_total_gb"))
        or _float_or_none(capacity.get("total_vram_gb"))
        or _float_or_none(gpu.get("total_gb"))
    )
    used = (
        _float_or_none(data.get("gpu_vram_used_gb"))
        or _float_or_none(data.get("vram_used_gb"))
        or _float_or_none(data.get("used_vram_gb"))
        or _float_or_none(capacity.get("gpu_vram_used_gb"))
        or _float_or_none(capacity.get("vram_used_gb"))
        or _float_or_none(capacity.get("used_vram_gb"))
        or _float_or_none(gpu.get("used_gb"))
    )
    free = (
        _float_or_none(data.get("gpu_vram_free_gb"))
        or _float_or_none(data.get("vram_free_gb"))
        or _float_or_none(data.get("free_vram_gb"))
        or _float_or_none(capacity.get("gpu_vram_free_gb"))
        or _float_or_none(capacity.get("vram_free_gb"))
        or _float_or_none(capacity.get("free_vram_gb"))
        or _float_or_none(gpu.get("free_gb"))
    )
    if free is None and total is not None and used is not None:
        free = max(0.0, total - used)
    if used is None and total is not None and free is not None:
        used = max(0.0, total - free)
    recommended = (
        _int_or_none(data.get("recommended_concurrency"))
        or _int_or_none(capacity.get("recommended_concurrency"))
        or _int_or_none(capacity.get("adaptive_limit"))
    )
    usable = (
        _float_or_none(data.get("usable_vram_gb"))
        or _float_or_none(capacity.get("usable_vram_gb"))
    )
    reserved = (
        _float_or_none(data.get("reserved_vram_gb"))
        or _float_or_none(capacity.get("reserved_vram_gb"))
    )
    estimated_per_request = (
        _float_or_none(data.get("estimated_vram_per_request_gb"))
        or _float_or_none(capacity.get("estimated_vram_per_request_gb"))
    )

    return PrivateVllmCapacity(
        ready=bool(data.get("ready")),
        gpu_vram_total_gb=total,
        gpu_vram_used_gb=used,
        gpu_vram_free_gb=free,
        usable_vram_gb=usable,
        reserved_vram_gb=reserved,
        estimated_vram_per_request_gb=estimated_per_request,
        recommended_concurrency=recommended,
        running_requests=(
            _int_or_none(data.get("running_requests"))
            or _int_or_none(data.get("active_requests"))
            or _int_or_none(capacity.get("running_requests"))
            or _int_or_none(capacity.get("active_requests"))
            or 0
        ),
        waiting_requests=(
            _int_or_none(data.get("waiting_requests"))
            or _int_or_none(data.get("queue_depth"))
            or _int_or_none(capacity.get("waiting_requests"))
            or _int_or_none(capacity.get("queue_depth"))
            or 0
        ),
        source=str(data.get("source") or "status"),
    )


def plan_private_vllm_concurrency(
    requested_concurrency: int,
    capacity: PrivateVllmCapacity,
    *,
    safety_ratio: float = 0.85,
    per_request_vram_gb: float | None = None,
    min_concurrency: int = 1,
) -> tuple[int, dict[str, Any]]:
    """Return an effective concurrency cap and an audit payload.

    Prefer the server's own recommended_concurrency when present because vLLM
    knows its KV cache layout better than the Mac backend. Also always apply
    the caller/controller VRAM budget when a per-request estimate is available:
    effective <= floor(available_vram * safety_ratio / per_request_vram_gb).
    """

    requested = max(1, int(requested_concurrency or 1))
    safety = min(0.95, max(0.10, float(safety_ratio or 0.85)))
    minimum = max(1, int(min_concurrency or 1))
    limits = [requested]
    reason = "requested"

    if capacity.recommended_concurrency is not None:
        limits.append(max(minimum, capacity.recommended_concurrency))
        reason = "server_recommended"

    per_request = (
        float(per_request_vram_gb)
        if per_request_vram_gb and per_request_vram_gb > 0
        else capacity.estimated_vram_per_request_gb
    )
    vram_limit: int | None = None
    if per_request and per_request > 0:
        if capacity.usable_vram_gb is not None:
            budget = max(0.0, capacity.usable_vram_gb)
        elif capacity.gpu_vram_free_gb is not None:
            budget = max(0.0, capacity.gpu_vram_free_gb * safety)
        else:
            budget = 0.0
        if budget > 0:
            vram_limit = max(minimum, int(budget // float(per_request)))
            limits.append(vram_limit)

    effective = min(limits)
    if vram_limit is not None and effective == vram_limit:
        reason = (
            "server_recommended_and_vram_budget"
            if capacity.recommended_concurrency is not None
            and capacity.recommended_concurrency == vram_limit
            else "vram_budget"
        )

    if not capacity.ready:
        effective = min(effective, minimum)
        reason = "not_ready"

    meta = {
        "requested_concurrency": requested,
        "effective_concurrency": effective,
        "reason": reason,
        "safety_ratio": safety,
        "per_request_vram_gb": per_request,
        "vram_budget_limit": vram_limit,
        "capacity": capacity.to_dict(),
    }
    return effective, meta


async def fetch_private_vllm_capacity(
    lifecycle_base_url: str,
    *,
    api_key: str | None = None,
    status_path: str = "/status",
    timeout_s: float = 10.0,
) -> PrivateVllmCapacity:
    base = str(lifecycle_base_url or "").strip().rstrip("/")
    path = status_path if status_path.startswith("/") else f"/{status_path}"
    headers = {"X-Api-Key": api_key} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(base + path, headers=headers)
        resp.raise_for_status()
        return parse_private_vllm_status(resp.json())
