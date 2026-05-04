"""VRAM idle watcher — auto-stop vllm containers when the queue is idle.

When no ingestion batch has been active for VLLM_IDLE_UNLOAD_SECONDS, this
watcher stops the configured vllm container set (vllm-summary, vllm-extract
by default), freeing the ~45 GB of VRAM they hold. On the next batch
enqueue, `ensure_started()` brings them back up and waits for `/health`
to flip healthy before letting the worker claim items.

Talks to the Docker daemon over the Unix socket at /var/run/docker.sock —
docker-compose.yml mounts that socket read-write into the backend
container. No `docker` CLI required in the image.

Concurrency model:
  * Single watcher task per backend process. Idempotent across restarts;
    if backend dies mid-stop, next batch's ensure_started() always starts.
  * `_lock` serializes start vs stop so a slow start doesn't race a fast
    "queue is idle" decision.
  * `paused` batches are NOT considered idle — operator may resume them.

Knobs (config.py):
  VLLM_IDLE_UNLOAD_ENABLED   default True
  VLLM_IDLE_UNLOAD_SECONDS   default 300
  VLLM_IDLE_CONTAINERS       default polymath_v33-vllm-summary-1,polymath_v33-vllm-extract-1
  VLLM_IDLE_POLL_SECONDS     default 30
  VLLM_IDLE_HEALTHY_TIMEOUT  default 180  (max wait after start)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from config import get_settings

logger = logging.getLogger(__name__)

_DOCKER_SOCK = "/var/run/docker.sock"

# Match the strings the worker / scheduler treat as "queue still has work".
# Keep in sync with batch_queue.NON_TERMINAL_ITEM_STATUSES, plus the
# batch-level "paused" status which the operator can resume.
_BUSY_BATCH_STATUSES = {"running", "queued", "paused"}


class VllmIdleWatcher:
    """Auto-stops idle vllm containers; auto-starts them on demand."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db
        self._settings = get_settings()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # `True` means containers are currently stopped by us. We don't
        # touch containers we didn't stop — if the operator stopped them
        # manually before us, we leave them alone until enqueue time.
        self._unloaded = False
        self._last_active_at = time.monotonic()

    @property
    def container_names(self) -> list[str]:
        raw = str(self._settings.VLLM_IDLE_CONTAINERS or "")
        return [n.strip() for n in raw.split(",") if n.strip()]

    @property
    def enabled(self) -> bool:
        return bool(self._settings.VLLM_IDLE_UNLOAD_ENABLED) and bool(self.container_names)

    # ── Docker socket helpers ────────────────────────────────────────────

    async def _docker(self) -> httpx.AsyncClient:
        # Returns an httpx client bound to the Docker UDS. Caller must close.
        # Docker exposes its REST API on the same socket the CLI uses.
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK),
            base_url="http://localhost",
            timeout=httpx.Timeout(60.0, connect=5.0),
        )

    async def _container_running(self, name: str) -> bool:
        try:
            async with await self._docker() as client:
                resp = await client.get(f"/containers/{name}/json")
                if resp.status_code == 404:
                    return False
                resp.raise_for_status()
                return bool((resp.json().get("State") or {}).get("Running"))
        except Exception as exc:
            logger.debug("container state probe failed for %s: %s", name, exc)
            return False

    async def _container_health(self, name: str) -> str | None:
        """Returns "healthy", "starting", "unhealthy", "none", or None on probe failure."""
        try:
            async with await self._docker() as client:
                resp = await client.get(f"/containers/{name}/json")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                state = resp.json().get("State") or {}
                health = (state.get("Health") or {}).get("Status")
                return health or "none"
        except Exception:
            return None

    async def _stop_one(self, name: str) -> None:
        async with await self._docker() as client:
            # t=10: graceful SIGTERM with 10s before SIGKILL.
            resp = await client.post(f"/containers/{name}/stop", params={"t": "10"})
            if resp.status_code in (204, 304):  # 304 = already stopped
                return
            if resp.status_code == 404:
                logger.warning("vllm idle: container %s not found, skipping stop", name)
                return
            resp.raise_for_status()

    async def _start_one(self, name: str) -> None:
        async with await self._docker() as client:
            resp = await client.post(f"/containers/{name}/start")
            if resp.status_code in (204, 304):  # 304 = already started
                return
            if resp.status_code == 404:
                logger.error("vllm idle: container %s not found, cannot start", name)
                raise RuntimeError(f"container {name} missing — cannot start")
            resp.raise_for_status()

    async def _wait_healthy(self, name: str, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            health = await self._container_health(name)
            if health == "healthy":
                return True
            if health == "unhealthy":
                # Crash loop — fail fast rather than wait the full timeout.
                logger.error("vllm idle: container %s reported unhealthy", name)
                return False
            await asyncio.sleep(3)
        logger.warning(
            "vllm idle: container %s did not become healthy within %ds",
            name, int(timeout_seconds),
        )
        return False

    # ── Public surface ──────────────────────────────────────────────────

    async def mark_active(self) -> None:
        """Reset the idle clock. Called from batch_queue when work happens."""
        self._last_active_at = time.monotonic()

    async def ensure_started(self) -> None:
        """Bring the configured containers up and wait for them to be healthy.

        Called from batch_queue.create_batch BEFORE admitting work. If the
        watcher had stopped them, this restarts them and blocks until each
        is healthy. Idempotent — no-op if everything is already running.
        """
        if not self.enabled:
            return
        async with self._lock:
            self._last_active_at = time.monotonic()
            if not self._unloaded:
                # Defensive: even if our internal flag says everything is
                # up, a manual `docker stop` could have happened. Probe
                # each container and start any that aren't running.
                for name in self.container_names:
                    if not await self._container_running(name):
                        logger.warning(
                            "vllm idle: container %s was stopped externally; restarting",
                            name,
                        )
                        await self._start_one(name)
                # No global wait — assume the operator stopped them
                # intentionally and they'll be ready by the time the
                # worker actually dispatches.
                return
            timeout = float(self._settings.VLLM_IDLE_HEALTHY_TIMEOUT)
            for name in self.container_names:
                logger.info("vllm idle: starting %s for new batch", name)
                await self._start_one(name)
            for name in self.container_names:
                ok = await self._wait_healthy(name, timeout)
                if not ok:
                    raise RuntimeError(
                        f"vllm idle: {name} failed to become healthy within {int(timeout)}s "
                        "after auto-start"
                    )
                logger.info("vllm idle: %s is healthy", name)
            self._unloaded = False

    async def _maybe_stop(self) -> None:
        """Single tick of the idle loop — stop containers if all batches are
        in terminal state and the idle window has elapsed.
        """
        if self._unloaded:
            return
        # Are any batches non-terminal in Mongo?
        try:
            busy = await self._db["ingestion_batches"].count_documents(
                {"status": {"$in": list(_BUSY_BATCH_STATUSES)}},
                limit=1,
            )
        except Exception as exc:
            logger.warning("vllm idle: batch count query failed: %s", exc)
            return
        if busy:
            self._last_active_at = time.monotonic()
            return
        idle_for = time.monotonic() - self._last_active_at
        threshold = float(self._settings.VLLM_IDLE_UNLOAD_SECONDS)
        if idle_for < threshold:
            return
        async with self._lock:
            if self._unloaded:
                return
            logger.info(
                "vllm idle: stopping %s after %ds of idleness (threshold=%ds)",
                ", ".join(self.container_names), int(idle_for), int(threshold),
            )
            failed: list[str] = []
            for name in self.container_names:
                try:
                    await self._stop_one(name)
                    logger.info("vllm idle: stopped %s, VRAM released", name)
                except Exception as exc:
                    logger.error("vllm idle: failed to stop %s: %s", name, exc)
                    failed.append(name)
            # Mark unloaded only if EVERY container we manage stopped — a
            # partial stop is worse than no stop because the running ones
            # still hold VRAM but we'd think we'd freed it.
            if not failed:
                self._unloaded = True

    async def loop(self) -> None:
        if not self.enabled:
            logger.info("vllm idle: disabled (VLLM_IDLE_UNLOAD_ENABLED=false or no containers configured)")
            return
        poll = float(self._settings.VLLM_IDLE_POLL_SECONDS)
        logger.info(
            "vllm idle: watcher started, threshold=%ds, poll=%ds, containers=%s",
            int(self._settings.VLLM_IDLE_UNLOAD_SECONDS),
            int(poll),
            self.container_names,
        )
        while True:
            try:
                await self._maybe_stop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("vllm idle: loop tick raised: %s", exc)
            await asyncio.sleep(poll)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.loop(), name="vllm_idle_watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("vllm idle: watcher task raised on shutdown")
        self._task = None


# Module-level singleton — created lazily by ingestion_service.connect.
_singleton: VllmIdleWatcher | None = None


def get_watcher() -> VllmIdleWatcher | None:
    return _singleton


def init_watcher(db: AsyncIOMotorDatabase) -> VllmIdleWatcher:
    global _singleton
    if _singleton is None:
        _singleton = VllmIdleWatcher(db)
    return _singleton
