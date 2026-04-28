"""
Modal deploy / destroy / status API (Phase 22).

  POST /api/infrastructure/modal/deploy          — kick off a blocking deploy
  POST /api/infrastructure/modal/destroy         — tear the app down
  GET  /api/infrastructure/modal/status          — is anything deployed?
  GET  /api/infrastructure/modal/deploy/stream   — SSE progress for the last
                                                   in-flight deploy request
                                                   for the caller's user_id

Named `modal_ops` (not `modal`) to avoid shadowing the SDK module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from routers.auth import get_current_user
from services.modal_deployer import (
    ModalDeployError,
    deploy_app as deployer_deploy,
    destroy_app as deployer_destroy,
    get_app_status as deployer_status,
    warm_up as deployer_warm_up,
)
from services.settings import settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/infrastructure/modal", tags=["modal-ops"])


# ── Rate limiter — 1 deploy per 5 minutes per user ─────────────────────────
# In-memory only; if we need multi-worker later, swap to Redis. Key is
# user_id so a second admin can still kick off their own deploy.

_DEPLOY_WINDOW_SECONDS = 300
_last_deploy_at: dict[str, float] = {}


def _check_rate_limit(user_id: str) -> None:
    now = time.time()
    last = _last_deploy_at.get(user_id, 0.0)
    wait = int(_DEPLOY_WINDOW_SECONDS - (now - last))
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "reason": f"Retry in {wait}s. Only one deploy per {_DEPLOY_WINDOW_SECONDS}s per user.",
                "retry_after_seconds": wait,
            },
            headers={"Retry-After": str(wait)},
        )
    _last_deploy_at[user_id] = now


# ── SSE progress queues, keyed by user_id ──────────────────────────────────
# When POST /deploy runs, it drops progress events into the caller's queue.
# GET /deploy/stream drains the queue as an SSE response.

_progress_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}


def _queue_for(user_id: str) -> asyncio.Queue[dict[str, Any]]:
    q = _progress_queues.get(user_id)
    if q is None:
        q = asyncio.Queue()
        _progress_queues[user_id] = q
    return q


async def _push_progress(user_id: str, event: dict[str, Any]) -> None:
    await _queue_for(user_id).put(event)


# ── Audit log ───────────────────────────────────────────────────────────────


async def _audit(
    *,
    user_id: str,
    action: str,
    success: bool,
    gpu_tier: str | None = None,
    max_containers: int | None = None,
    app_name: str | None = None,
    url: str | None = None,
    app_id: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Best-effort insert into `modal_deploy_audit`. Never raises."""
    try:
        db = settings_service._db  # type: ignore[attr-defined]
        if db is None:
            return
        await db["modal_deploy_audit"].insert_one({
            "user_id": user_id,
            "timestamp": time.time(),
            "action": action,
            "success": success,
            "gpu_tier": gpu_tier,
            "max_containers": max_containers,
            "app_name": app_name,
            "url": url,
            "app_id": app_id,
            "error": error,
            "duration_ms": duration_ms,
        })
    except Exception as exc:
        logger.warning("modal_deploy_audit insert failed: %s", exc)


async def _resolve_tokens(user_id: str) -> tuple[str, str]:
    """Pull Modal tokens from the user's saved API keys. Raises 400 if
    either is missing — better than a confusing 401 from the SDK."""
    saved = await settings_service.get_plaintext_keys_for_llm(user_id)
    token_id = (saved.get("modal_token_id") or "").strip()
    token_secret = (saved.get("modal_token_secret") or "").strip()
    if not token_id or not token_secret:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "modal_tokens_missing",
                "reason": (
                    "Save modal_token_id and modal_token_secret under "
                    "Settings → Infrastructure → Modal before deploying."
                ),
            },
        )
    return token_id, token_secret


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/deploy", status_code=200)
async def deploy_endpoint(
    body: dict[str, Any] | None = Body(default=None),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Build and deploy the polymath-embedder app. Blocks until ready.
    Clients wanting progress updates should open the SSE stream at
    /deploy/stream BEFORE issuing this POST — the two endpoints share the
    caller's per-user progress queue."""
    user_id = current_user["user_id"]
    _check_rate_limit(user_id)
    body = body or {}

    token_id, token_secret = await _resolve_tokens(user_id)
    gpu_tier = (body.get("gpu_tier") or "T4").strip()
    max_containers = int(body.get("max_containers") or 10)
    min_containers = int(body.get("min_containers") or 0)
    idle_timeout = int(body.get("idle_timeout") or 300)
    app_name = (body.get("app_name") or "polymath-embedder").strip()
    concurrency = int(body.get("concurrency") or 4)

    async def _progress(evt: dict[str, Any]) -> None:
        await _push_progress(user_id, evt)

    try:
        result = await deployer_deploy(
            token_id=token_id,
            token_secret=token_secret,
            gpu_tier=gpu_tier,
            max_containers=max_containers,
            min_containers=min_containers,
            idle_timeout=idle_timeout,
            concurrency=concurrency,
            app_name=app_name,
            progress_callback=_progress,
        )
    except ModalDeployError as exc:
        # Deploy failure — do NOT flip settings.modal.enabled. Emit terminal
        # failure event so any in-flight SSE stream drains cleanly.
        await _push_progress(user_id, {
            "phase": "failed",
            "at_phase": exc.at_phase,
            "error": str(exc),
        })
        await _audit(
            user_id=user_id, action="deploy", success=False,
            gpu_tier=gpu_tier, max_containers=max_containers,
            app_name=app_name, error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "deploy_failed",
                "at_phase": exc.at_phase,
                "reason": str(exc),
            },
        ) from exc

    # Success — persist runtime wiring so the embedder dispatcher picks
    # up the new URL on the next ingest (no backend restart).
    await settings_service.update_system_modal(
        user_id,
        enabled=True,
        embedder_url=result["url"] or "",
    )
    # Best-effort warm-up so the first real ingest skips a cold start.
    asyncio.create_task(deployer_warm_up(result["url"]))

    await _audit(
        user_id=user_id, action="deploy", success=True,
        gpu_tier=gpu_tier, max_containers=max_containers,
        app_name=app_name, url=result["url"], app_id=result["app_id"],
        duration_ms=result["duration_ms"],
    )
    return {
        "ok": True,
        "url": result["url"],
        "app_id": result["app_id"],
        "deployed_at": result["deployed_at"],
        "duration_ms": result["duration_ms"],
    }


@router.post("/destroy", status_code=200)
async def destroy_endpoint(
    body: dict[str, Any] | None = Body(default=None),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = current_user["user_id"]
    body = body or {}
    token_id, token_secret = await _resolve_tokens(user_id)
    app_name = (body.get("app_name") or "polymath-embedder").strip()

    try:
        result = await deployer_destroy(
            token_id=token_id,
            token_secret=token_secret,
            app_name=app_name,
        )
    except ModalDeployError as exc:
        await _audit(
            user_id=user_id, action="destroy", success=False,
            app_name=app_name, error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "destroy_failed",
                "at_phase": exc.at_phase,
                "reason": str(exc),
            },
        ) from exc

    # Flip runtime off. Keep the stored URL/workspace for "easy redeploy".
    await settings_service.update_system_modal(
        user_id,
        enabled=False,
        embedder_url="",
    )
    await _audit(
        user_id=user_id, action="destroy", success=True,
        app_name=app_name, app_id=result.get("app_id"),
    )
    return {"ok": True, **result}


@router.get("/status")
async def status_endpoint(
    app_name: str = "polymath-embedder",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = current_user["user_id"]
    token_id, token_secret = await _resolve_tokens(user_id)
    return await deployer_status(
        token_id=token_id, token_secret=token_secret, app_name=app_name,
    )


@router.get("/deploy/stream")
async def deploy_stream(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """SSE event stream for the current user's in-flight deploy.
    Events have shape `{phase, message, estimated_seconds?, ...}`.
    Terminal events are `phase=ready` or `phase=failed`."""
    user_id = current_user["user_id"]
    queue = _queue_for(user_id)

    async def _generate():
        # 1s heartbeat so the client can detect a stalled connection.
        idle_timeout = 1.0
        terminal = {"ready", "failed"}
        while True:
            if await request.is_disconnected():
                break
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                # Heartbeat — SSE comment, silently dropped by EventSource.
                yield ": heartbeat\n\n"
                continue
            payload = json.dumps(evt)
            yield f"data: {payload}\n\n"
            if evt.get("phase") in terminal:
                break

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer
        },
    )
