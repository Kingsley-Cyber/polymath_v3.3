"""extraction_validation.py — deploy-readiness probes for extraction endpoints.

Settings → Ingestion lets users point extraction at any machine on their
network (RTX box, the Mac sidecar, a future Linux box). Before trusting an
ingest run to that configuration, the user needs proof — from the BACKEND'S
network position, which is what the worker actually uses (host.docker.internal
resolves differently inside the container than `localhost` does on the host) —
that each endpoint is reachable, healthy, and really running the lane it
claims (an "ONNX GPU" endpoint that silently fell back to CPU shows up here,
not mid-backfill).

Checklist semantics per endpoint:
  reachable     GET {url}/health answered at all
  healthy       response says status == "ok"
  warm          models resident (cold endpoints still serve; first call is slow)
  model_loaded  gliner singleton constructed
  gpu_active    onnx -> CUDAExecutionProvider in active providers;
                torch -> device is cuda/mps. None when indeterminable.
  version_match endpoint pipeline_version == this backend's (None when either
                side is unknown)

state: "fail" when not reachable+healthy; "warning" when serving but any
quality check is false; "ready" when everything is green.
deploy_ready: at least one ENABLED endpoint is fully "ready".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 5.0


def _local_pipeline_version() -> str | None:
    try:
        from services.ghost_b_local import _ensure_local_ghost_b_on_path
        return str(_ensure_local_ghost_b_on_path().PIPELINE_VERSION)
    except Exception:  # noqa: BLE001 — version compare degrades to None
        return None


def _evaluate(health: dict[str, Any], local_version: str | None) -> dict[str, Any]:
    """Turn a /health payload into the checklist + info dicts."""
    gliner = health.get("gliner") or {}
    backend = gliner.get("backend") or "unknown"
    device = str(gliner.get("device") or "")
    providers = list(gliner.get("providers") or [])
    remote_version = health.get("pipeline_version")

    healthy = health.get("status") == "ok"
    warm = bool(health.get("warm"))
    loaded = bool(gliner.get("loaded"))

    gpu_active: bool | None
    if backend == "onnx":
        gpu_active = "CUDAExecutionProvider" in providers if providers else (
            device == "cuda" or None)
    elif backend == "torch":
        gpu_active = (device.startswith("cuda") or device.startswith("mps")) if device else None
    else:
        gpu_active = None

    version_match: bool | None = None
    if local_version and remote_version and remote_version != "unknown":
        version_match = remote_version == local_version

    checks = {
        "reachable": True,
        "healthy": healthy,
        "warm": warm,
        "model_loaded": loaded,
        "gpu_active": gpu_active,
        "version_match": version_match,
    }
    if not healthy:
        state, detail = "fail", "endpoint responded but reports unhealthy"
    elif not (warm and loaded) or gpu_active is False or version_match is False:
        state = "warning"
        problems = []
        if not warm:
            problems.append("cold (first request pays model load)")
        if not loaded:
            problems.append("model not loaded yet")
        if gpu_active is False:
            problems.append("no GPU active for this lane")
        if version_match is False:
            problems.append(f"pipeline {remote_version} != backend {local_version}")
        detail = "; ".join(problems)
    else:
        state, detail = "ready", ""
    info = {
        "backend": backend,
        "device": device or str(health.get("device") or ""),
        "model": gliner.get("model") or "",
        "pipeline_version": remote_version,
        "providers": providers,
    }
    return {"checks": checks, "info": info, "state": state, "detail": detail}


async def _probe(client: httpx.AsyncClient, ep: dict[str, Any],
                 local_version: str | None) -> dict[str, Any]:
    base = {
        "label": ep.get("label") or "",
        "url": ep.get("url") or "",
        "enabled": bool(ep.get("enabled")),
    }
    url = base["url"].rstrip("/")
    if not url:
        return {**base, "checks": {"reachable": False}, "info": {},
                "state": "fail", "detail": "no URL configured"}
    try:
        resp = await client.get(f"{url}/health")
        resp.raise_for_status()
        health = resp.json()
    except Exception as exc:  # noqa: BLE001 — any failure = unreachable
        return {**base,
                "checks": {"reachable": False, "healthy": False, "warm": False,
                           "model_loaded": False, "gpu_active": None,
                           "version_match": None},
                "info": {}, "state": "fail",
                "detail": f"unreachable from backend: {type(exc).__name__}"}
    return {**base, **_evaluate(health, local_version)}


async def validate_endpoints(endpoints: list[dict[str, Any]]) -> dict[str, Any]:
    """Probe all configured endpoints concurrently; return the full report."""
    local_version = _local_pipeline_version()
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
        results = await asyncio.gather(
            *(_probe(client, ep, local_version) for ep in endpoints))
    enabled = [r for r in results if r["enabled"]]
    enabled_ready = sum(1 for r in enabled if r["state"] == "ready")
    report = {
        "endpoints": list(results),
        "backend_pipeline_version": local_version,
        "enabled_total": len(enabled),
        "enabled_ready": enabled_ready,
        "deploy_ready": enabled_ready >= 1,
    }
    logger.info("extraction validate: %d/%d enabled endpoints ready (deploy_ready=%s)",
                enabled_ready, len(enabled), report["deploy_ready"])
    return report
