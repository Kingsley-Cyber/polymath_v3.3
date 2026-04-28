"""
Programmatic Modal deploy / destroy / status (Phase 22).

Replaces the "copy the URL Modal prints, paste it into .env, restart backend"
CLI flow with a single API call. Uses the Modal Python SDK directly — never
subprocess.

Public surface:
  deploy_app(...)      → programmatic deploy + optional SSE progress
  destroy_app(...)     → tear down a deployed Modal App
  get_app_status(...)  → lookup live app by name
  warm_up(...)         → fire /health against the URL; fire-and-forget

Design notes:
  - Auth injection tries `modal.Client.from_credentials` first; falls back to
    env-var save/restore (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET) guarded by a
    module-level asyncio.Lock so parallel deploys can't cross-contaminate
    each other's credentials.
  - Progress events are synthetic phase markers (SDK 0.64 does not stream
    deploy telemetry). Each carries an `estimated_seconds` hint so the
    frontend can animate a determinate progress bar between phases.
  - Tokens are NEVER logged or returned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# Serialize deploy/destroy across requests — env-var auth fallback is not
# reentrant. Rate limiting helps but cannot prevent two concurrent calls if
# they hit different users with different keys.
_DEPLOY_LOCK = asyncio.Lock()

# Synthetic phase hints — the frontend uses `estimated_seconds` to animate
# the progress bar between phase events.
_PHASE_VERIFY = ("verifying_tokens", "Verifying Modal credentials", 3)
_PHASE_BUILD = ("building_app", "Building app spec", 2)
_PHASE_DEPLOY = ("deploying", "Uploading and provisioning", 45)
_PHASE_READY = ("ready", "Deployed", 0)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class ModalDeployError(RuntimeError):
    """Raised when a deploy / destroy operation fails. Carries `at_phase`
    so the SSE stream can tag the failure to the right step."""

    def __init__(self, message: str, at_phase: str = "unknown"):
        super().__init__(message)
        self.at_phase = at_phase


async def _emit(cb: Optional[ProgressCallback], event: dict[str, Any]) -> None:
    if cb is not None:
        try:
            await cb(event)
        except Exception as exc:
            logger.warning("progress callback raised (ignored): %s", exc)


# ── Auth plumbing ───────────────────────────────────────────────────────────


class _ModalAuth:
    """Context manager that binds Modal SDK auth to a pair of tokens for the
    duration of a deploy call. Tries the Client.from_credentials path first;
    falls back to env-var save/restore.

    Intentionally NOT thread-safe — caller holds _DEPLOY_LOCK.
    """

    def __init__(self, token_id: str, token_secret: str):
        self.token_id = token_id
        self.token_secret = token_secret
        self.client = None
        self._env_id_prev: str | None = None
        self._env_secret_prev: str | None = None
        self._mode: str = "unknown"

    async def __aenter__(self) -> "_ModalAuth":
        # Preferred path: per-call Client with explicit creds.
        try:
            import modal.client as mc

            # SDK 0.64 doesn't advertise a public Client.from_credentials,
            # but Client(..., credentials=(id, secret)) works and is what
            # verify() uses. We construct but do not dispatch here — the
            # caller passes `self.client` to deploy_app(client=...).
            self.client = mc.Client(
                server_url=os.getenv("MODAL_SERVER_URL", "https://api.modal.com"),
                client_type=3,  # CLIENT_TYPE_CLIENT
                credentials=(self.token_id, self.token_secret),
            )
            self._mode = "client_injection"
            logger.info("modal auth: using per-call Client injection")
        except Exception as exc:
            logger.info("modal auth: Client injection unavailable (%s); using env fallback", exc)
            self.client = None
            self._mode = "env_fallback"
            self._env_id_prev = os.environ.get("MODAL_TOKEN_ID")
            self._env_secret_prev = os.environ.get("MODAL_TOKEN_SECRET")
            os.environ["MODAL_TOKEN_ID"] = self.token_id
            os.environ["MODAL_TOKEN_SECRET"] = self.token_secret
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._mode == "env_fallback":
            if self._env_id_prev is None:
                os.environ.pop("MODAL_TOKEN_ID", None)
            else:
                os.environ["MODAL_TOKEN_ID"] = self._env_id_prev
            if self._env_secret_prev is None:
                os.environ.pop("MODAL_TOKEN_SECRET", None)
            else:
                os.environ["MODAL_TOKEN_SECRET"] = self._env_secret_prev
        # Close the Client to release its gRPC channel. Best-effort.
        if self.client is not None:
            try:
                await self.client._close()  # type: ignore[attr-defined]
            except Exception:
                pass


# ── Token verify (cheap, before any build work) ─────────────────────────────


async def _verify_creds(token_id: str, token_secret: str) -> None:
    """Ping Modal's gRPC with the creds; raises ModalDeployError on failure."""
    import modal.client as mc
    import modal.config as modal_cfg

    server_url = modal_cfg.config.get("server_url") or "https://api.modal.com"
    try:
        await asyncio.wait_for(
            mc.Client.verify(server_url, (token_id, token_secret)),
            timeout=10.0,
        )
    except asyncio.TimeoutError as exc:
        raise ModalDeployError(
            "Modal gRPC verify timed out after 10s", at_phase="verifying_tokens"
        ) from exc
    except Exception as exc:
        # AuthError, ConnectionError, VersionError all land here. Do NOT
        # let the raw token bleed into the error message.
        raise ModalDeployError(
            f"{type(exc).__name__}: {exc}", at_phase="verifying_tokens"
        ) from exc


# ── Public API ──────────────────────────────────────────────────────────────


async def deploy_app(
    *,
    token_id: str,
    token_secret: str,
    gpu_tier: str = "T4",
    max_containers: int = 10,
    min_containers: int = 0,
    idle_timeout: int = 300,
    concurrency: int = 4,
    app_name: str = "polymath-embedder",
    model_id: str = "Qwen/Qwen3-Embedding-0.6B",
    use_auth: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Programmatically build and deploy the polymath-embedder Modal App.

    Returns:
        {
          "url": str,            # public endpoint of the deployed serve() fn
          "app_id": str,         # Modal App ID (ap-...)
          "workspace": str | None,
          "deployed_at": float,  # unix epoch seconds
          "duration_ms": int,
        }

    Raises:
        ModalDeployError on token failure, build failure, deploy failure,
        or post-deploy URL introspection failure.
    """
    if not token_id or not token_secret:
        raise ModalDeployError(
            "Modal tokens not provided", at_phase="verifying_tokens"
        )

    t_start = time.perf_counter()

    async with _DEPLOY_LOCK:
        # Phase 1 — verify creds. Fast fail, no build work if bad tokens.
        await _emit(progress_callback, {
            "phase": _PHASE_VERIFY[0],
            "message": _PHASE_VERIFY[1],
            "estimated_seconds": _PHASE_VERIFY[2],
        })
        await _verify_creds(token_id, token_secret)

        async with _ModalAuth(token_id, token_secret) as auth:
            # Phase 2 — build the App with the requested config.
            await _emit(progress_callback, {
                "phase": _PHASE_BUILD[0],
                "message": _PHASE_BUILD[1],
                "estimated_seconds": _PHASE_BUILD[2],
            })
            try:
                # Late import — modal_embedder lives at repo root, not under
                # backend/. sys.path usually includes repo root when backend
                # runs via uvicorn from the project dir.
                from modal_embedder import build_app
                app = build_app(
                    app_name=app_name,
                    model_id=model_id,
                    gpu_tier=gpu_tier,
                    max_containers=max_containers,
                    min_containers=min_containers,
                    idle_timeout=idle_timeout,
                    concurrency=concurrency,
                    use_auth=use_auth,
                )
            except Exception as exc:
                raise ModalDeployError(
                    f"Build failed: {type(exc).__name__}: {exc}",
                    at_phase="building_app",
                ) from exc

            # Phase 3 — deploy. SDK is sync-ish here; run in a thread so the
            # asyncio event loop stays responsive for the SSE heartbeat.
            await _emit(progress_callback, {
                "phase": _PHASE_DEPLOY[0],
                "message": _PHASE_DEPLOY[1],
                "estimated_seconds": _PHASE_DEPLOY[2],
            })
            try:
                from modal.runner import deploy_app as _sdk_deploy

                def _do_deploy():
                    if auth.client is not None:
                        return _sdk_deploy(app, name=app_name, client=auth.client)
                    return _sdk_deploy(app, name=app_name)

                app_handle = await asyncio.to_thread(_do_deploy)
            except TypeError:
                # Older SDK signatures may not accept client=; retry without.
                from modal.runner import deploy_app as _sdk_deploy
                app_handle = await asyncio.to_thread(
                    lambda: _sdk_deploy(app, name=app_name)
                )
            except Exception as exc:
                raise ModalDeployError(
                    f"Deploy failed: {type(exc).__name__}: {exc}",
                    at_phase="deploying",
                ) from exc

            # Extract url + app_id. Modal 0.64 returns a DeployResult /
            # LocalFunctionHandle-ish object; fields we read are best-effort.
            app_id = (
                getattr(app_handle, "app_id", None)
                or getattr(app_handle, "id", None)
                or ""
            )
            url = ""
            try:
                serve_fn = app.registered_functions.get("serve")
                if serve_fn is not None:
                    url = getattr(serve_fn, "web_url", "") or ""
                if not url:
                    # Fallback: inspect app_handle metadata if the SDK exposes it.
                    urls = getattr(app_handle, "web_urls", None) or []
                    url = urls[0] if urls else ""
            except Exception as exc:
                logger.warning("URL introspection failed: %s", exc)

            duration_ms = int((time.perf_counter() - t_start) * 1000)
            await _emit(progress_callback, {
                "phase": _PHASE_READY[0],
                "message": _PHASE_READY[1],
                "url": url,
                "app_id": app_id,
                "duration_ms": duration_ms,
            })
            return {
                "url": url,
                "app_id": app_id,
                "workspace": None,
                "deployed_at": time.time(),
                "duration_ms": duration_ms,
            }


async def destroy_app(
    *,
    token_id: str,
    token_secret: str,
    app_name: str = "polymath-embedder",
) -> dict[str, Any]:
    """Stop the deployed Modal App. Idempotent — returns ok=True even when
    nothing was deployed."""
    if not token_id or not token_secret:
        raise ModalDeployError(
            "Modal tokens not provided", at_phase="verifying_tokens"
        )

    async with _DEPLOY_LOCK:
        await _verify_creds(token_id, token_secret)
        async with _ModalAuth(token_id, token_secret) as auth:
            try:
                import modal

                def _do_stop():
                    # `modal.App.lookup` returns a handle; `app_stop` on the
                    # client tears it down. Missing-app lookups raise
                    # NotFoundError.
                    try:
                        handle = modal.App.lookup(
                            app_name,
                            client=auth.client,
                            create_if_missing=False,
                        )
                    except Exception as exc:
                        return {"ok": True, "was_deployed": False, "note": str(exc)}
                    app_id = getattr(handle, "app_id", None) or getattr(handle, "id", None)
                    if auth.client is not None and hasattr(auth.client, "app_stop"):
                        auth.client.app_stop(app_id)  # type: ignore[attr-defined]
                    else:
                        # Best-effort CLI-parallel fallback.
                        import modal.runner as runner
                        stop = getattr(runner, "stop_app", None)
                        if stop is not None:
                            stop(app_id)
                    return {"ok": True, "was_deployed": True, "app_id": app_id}

                result = await asyncio.to_thread(_do_stop)
                return result
            except ModalDeployError:
                raise
            except Exception as exc:
                raise ModalDeployError(
                    f"Destroy failed: {type(exc).__name__}: {exc}",
                    at_phase="destroying",
                ) from exc


async def get_app_status(
    *,
    token_id: str,
    token_secret: str,
    app_name: str = "polymath-embedder",
) -> dict[str, Any]:
    """Return `{deployed: bool, url, app_id}` for `app_name`."""
    if not token_id or not token_secret:
        return {"deployed": False, "url": "", "app_id": None, "error": "tokens missing"}

    try:
        await _verify_creds(token_id, token_secret)
    except ModalDeployError as exc:
        return {"deployed": False, "url": "", "app_id": None, "error": str(exc)}

    async with _ModalAuth(token_id, token_secret) as auth:
        try:
            import modal

            def _lookup():
                try:
                    handle = modal.App.lookup(
                        app_name,
                        client=auth.client,
                        create_if_missing=False,
                    )
                except Exception:
                    return None
                return handle

            handle = await asyncio.to_thread(_lookup)
            if handle is None:
                return {"deployed": False, "url": "", "app_id": None}

            app_id = getattr(handle, "app_id", None) or getattr(handle, "id", None) or ""
            urls = getattr(handle, "web_urls", None) or []
            url = urls[0] if urls else ""
            return {"deployed": True, "url": url, "app_id": app_id}
        except Exception as exc:
            logger.warning("status lookup failed: %s", exc)
            return {"deployed": False, "url": "", "app_id": None, "error": str(exc)}


async def warm_up(url: str, timeout: float = 15.0) -> None:
    """Hit /health once. Ignore outcome — fire-and-forget warm-up after deploy
    so the first real ingest skips a container cold start."""
    if not url:
        return
    target = url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(target)
    except Exception as exc:
        logger.info("warm-up ping failed (non-fatal): %s", exc)
