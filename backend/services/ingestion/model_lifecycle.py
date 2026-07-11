"""Managed remote model lifecycle helpers.

Model-pool entries can optionally point at a control plane that warms a remote
runtime before LiteLLM sends chat/completion traffic to its OpenAI-compatible
base_url. Secrets stay in the already-decrypted in-memory pool entry and are
sent only as X-Api-Key to the lifecycle service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_IDLE_SHUTDOWN_SECONDS = 600
DEFAULT_FAILURE_COOLDOWN_SECONDS = 60

LifecycleKey = tuple[str, str, str]
LifecycleStartKey = tuple[str, str, str, str]

_shutdown_tasks: dict[LifecycleKey, asyncio.Task] = {}
_shutdown_generations: dict[LifecycleKey, int] = {}
_lifecycle_holds: dict[LifecycleKey, set[str]] = {}
_failure_cooldowns: dict[LifecycleStartKey, float] = {}


def _managed_entries(pool: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not pool:
        return []
    return [
        entry
        for entry in pool
        if entry.get("lifecycle_base_url") and entry.get("lifecycle_auto_start")
    ]


def _lifecycle_entries(pool: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not pool:
        return []
    return [
        entry
        for entry in pool
        if entry.get("lifecycle_base_url")
        and (entry.get("lifecycle_auto_start") or entry.get("lifecycle_auto_stop"))
    ]


def _unique_entries_by_lifecycle_key(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[LifecycleKey] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        key = _lifecycle_key(entry)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _url(entry: dict[str, Any], path_field: str, default_path: str) -> str:
    base = str(entry.get("lifecycle_base_url") or "").strip().rstrip("/")
    path = str(entry.get(path_field) or default_path).strip() or default_path
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _headers(entry: dict[str, Any]) -> dict[str, str]:
    key = str(entry.get("lifecycle_api_key") or "").strip()
    return {"X-Api-Key": key} if key else {}


def _lifecycle_key(entry: dict[str, Any]) -> LifecycleKey:
    down_path = str(entry.get("lifecycle_down_path") or "/down").strip() or "/down"
    if not down_path.startswith("/"):
        down_path = "/" + down_path
    return (
        str(entry.get("lifecycle_base_url") or "").strip().rstrip("/"),
        down_path,
        str(entry.get("lifecycle_api_key") or ""),
    )


def _lifecycle_start_key(entry: dict[str, Any]) -> LifecycleStartKey:
    up_path = str(entry.get("lifecycle_up_path") or "/up").strip() or "/up"
    status_path = (
        str(entry.get("lifecycle_status_path") or "/status").strip() or "/status"
    )
    if not up_path.startswith("/"):
        up_path = "/" + up_path
    if not status_path.startswith("/"):
        status_path = "/" + status_path
    return (
        str(entry.get("lifecycle_base_url") or "").strip().rstrip("/"),
        up_path,
        status_path,
        str(entry.get("lifecycle_api_key") or ""),
    )


def _failure_cooldown_seconds(entry: dict[str, Any]) -> int:
    extra = entry.get("extra_params") or {}
    if not isinstance(extra, dict):
        extra = {}
    raw = entry.get("lifecycle_failure_cooldown_seconds")
    if raw is None:
        raw = extra.get("lifecycle_failure_cooldown_seconds")
    if raw is None:
        return DEFAULT_FAILURE_COOLDOWN_SECONDS
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_FAILURE_COOLDOWN_SECONDS


def _idle_shutdown_seconds(entry: dict[str, Any]) -> int:
    extra = entry.get("extra_params") or {}
    if not isinstance(extra, dict):
        extra = {}
    raw = (
        entry.get("lifecycle_idle_shutdown_seconds")
        if entry.get("lifecycle_idle_shutdown_seconds") is not None
        else extra.get("lifecycle_idle_shutdown_seconds")
    )
    if raw is None:
        raw = extra.get("idle_shutdown_seconds")
    if raw is None:
        raw = extra.get("idle_auto_stop_seconds")
    if raw is None:
        return DEFAULT_IDLE_SHUTDOWN_SECONDS
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_IDLE_SHUTDOWN_SECONDS


def _cancel_pending_shutdown(entry: dict[str, Any], *, purpose: str) -> None:
    key = _lifecycle_key(entry)
    _shutdown_generations[key] = _shutdown_generations.get(key, 0) + 1
    task = _shutdown_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
        logger.info(
            "model lifecycle idle shutdown cancelled purpose=%s control=%s",
            purpose,
            key[0],
        )


async def _post_down(
    entry: dict[str, Any],
    *,
    purpose: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    base = str(entry.get("lifecycle_base_url") or "").strip().rstrip("/")

    async def _send(cli: httpx.AsyncClient) -> None:
        try:
            resp = await cli.post(
                _url(entry, "lifecycle_down_path", "/down"),
                headers=_headers(entry),
            )
            if resp.status_code >= 400:
                logger.warning(
                    "model lifecycle stop failed purpose=%s control=%s http=%d",
                    purpose,
                    base,
                    resp.status_code,
                )
            else:
                logger.info(
                    "model lifecycle stopped purpose=%s control=%s",
                    purpose,
                    base,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model lifecycle stop unavailable purpose=%s control=%s: %s",
                purpose,
                base,
                exc,
            )

    if client is not None:
        await _send(client)
        return
    async with httpx.AsyncClient(timeout=20.0) as owned:
        await _send(owned)


async def _shutdown_after_idle(
    entry: dict[str, Any],
    *,
    purpose: str,
    key: LifecycleKey,
    generation: int,
    delay_seconds: int,
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        if _shutdown_generations.get(key) != generation:
            return
        await _post_down(entry, purpose=f"{purpose}:idle")
    except asyncio.CancelledError:
        return
    finally:
        if _shutdown_generations.get(key) == generation:
            _shutdown_tasks.pop(key, None)


async def ensure_model_lifecycle_ready(
    pool: list[dict[str, Any]] | None,
    *,
    purpose: str,
) -> list[dict[str, Any]]:
    """Start managed runtimes and quarantine only unavailable lifecycle lanes.

    Cloud providers in the same pool do not depend on a managed local runtime.
    Returning a filtered pool keeps those independent lanes usable when one
    lifecycle control plane is offline. If no lane remains, the original
    lifecycle error is raised so callers can record a real provider failure.
    """

    original_pool = list(pool or [])

    entries = _managed_entries(original_pool)
    if not entries:
        return original_pool

    seen: set[LifecycleStartKey] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        key = _lifecycle_start_key(entry)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    now = time.monotonic()
    failed_keys: set[LifecycleStartKey] = set()
    failures: list[BaseException] = []
    candidates: list[dict[str, Any]] = []
    for entry in unique:
        key = _lifecycle_start_key(entry)
        retry_at = _failure_cooldowns.get(key, 0.0)
        if retry_at > now:
            failed_keys.add(key)
            logger.info(
                "model lifecycle lane remains quarantined purpose=%s control=%s retry_in_s=%d",
                purpose,
                key[0],
                max(1, int(retry_at - now)),
            )
            continue
        _cancel_pending_shutdown(entry, purpose=purpose)
        candidates.append(entry)

    if candidates:
        results = await asyncio.gather(
            *[_ensure_one_ready(entry, purpose=purpose) for entry in candidates],
            return_exceptions=True,
        )
        for entry, result in zip(candidates, results, strict=True):
            key = _lifecycle_start_key(entry)
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                cooldown = _failure_cooldown_seconds(entry)
                _failure_cooldowns[key] = time.monotonic() + cooldown
                failed_keys.add(key)
                failures.append(result)
                logger.warning(
                    "model lifecycle lane quarantined purpose=%s control=%s "
                    "failure_class=%s cooldown_s=%d",
                    purpose,
                    key[0],
                    type(result).__name__,
                    cooldown,
                )
            else:
                _failure_cooldowns.pop(key, None)

    filtered_pool = [
        entry
        for entry in original_pool
        if not (
            entry.get("lifecycle_base_url")
            and entry.get("lifecycle_auto_start")
            and _lifecycle_start_key(entry) in failed_keys
        )
    ]
    if failed_keys and not filtered_pool:
        error = RuntimeError(
            "all model lanes unavailable after lifecycle quarantine "
            f"(purpose={purpose}, failed_controls={len(failed_keys)})"
        )
        if failures:
            raise error from failures[0]
        raise error
    if failed_keys:
        logger.warning(
            "model lifecycle failover purpose=%s quarantined_controls=%d "
            "usable_lanes=%d",
            purpose,
            len(failed_keys),
            len(filtered_pool),
        )
    return filtered_pool


async def acquire_model_lifecycle_hold(
    pool: list[dict[str, Any]] | None,
    *,
    purpose: str,
    hold_id: str,
    ensure_ready: bool = True,
) -> None:
    """Keep managed runtimes hot across a larger unit of work.

    Per-document callers still schedule their normal idle shutdown, but a batch
    hold defers those shutdowns until the batch releases the hold. This avoids
    RTX/vLLM cold-load thrash between files when parsing, summaries, embedding,
    or graph writes take longer than the per-call idle lease.
    """

    entries = _unique_entries_by_lifecycle_key(_lifecycle_entries(pool))
    if not entries:
        return

    acquired: list[LifecycleKey] = []
    for entry in entries:
        key = _lifecycle_key(entry)
        _cancel_pending_shutdown(entry, purpose=purpose)
        _lifecycle_holds.setdefault(key, set()).add(str(hold_id))
        acquired.append(key)
        logger.info(
            "model lifecycle hold acquired purpose=%s hold=%s control=%s active_holds=%d",
            purpose,
            hold_id,
            key[0],
            len(_lifecycle_holds.get(key, set())),
        )

    if not ensure_ready:
        return
    try:
        await ensure_model_lifecycle_ready(pool, purpose=purpose)
    except Exception:
        for key in acquired:
            holds = _lifecycle_holds.get(key)
            if holds:
                holds.discard(str(hold_id))
                if not holds:
                    _lifecycle_holds.pop(key, None)
        raise


async def release_model_lifecycle_hold(
    pool: list[dict[str, Any]] | None,
    *,
    purpose: str,
    hold_id: str,
) -> None:
    entries = _unique_entries_by_lifecycle_key(_lifecycle_entries(pool))
    if not entries:
        return

    for entry in entries:
        key = _lifecycle_key(entry)
        holds = _lifecycle_holds.get(key)
        if holds:
            holds.discard(str(hold_id))
            if not holds:
                _lifecycle_holds.pop(key, None)
        logger.info(
            "model lifecycle hold released purpose=%s hold=%s control=%s active_holds=%d",
            purpose,
            hold_id,
            key[0],
            len(_lifecycle_holds.get(key, set())),
        )

    await shutdown_model_lifecycle(pool, purpose=purpose)


async def _ensure_one_ready(entry: dict[str, Any], *, purpose: str) -> None:
    base = str(entry.get("lifecycle_base_url") or "").strip().rstrip("/")
    timeout_s = max(5, int(entry.get("lifecycle_ready_timeout_seconds") or 360))
    headers = _headers(entry)
    up_url = _url(entry, "lifecycle_up_path", "/up")
    status_url = _url(entry, "lifecycle_status_path", "/status")
    started = time.monotonic()

    async with httpx.AsyncClient(timeout=20.0) as client:
        logger.info("model lifecycle start purpose=%s control=%s", purpose, base)
        resp = await client.post(up_url, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"model lifecycle start failed for {base}: HTTP {resp.status_code}"
            )

        while True:
            status = await client.get(status_url, headers=headers)
            if status.status_code >= 400:
                raise RuntimeError(
                    f"model lifecycle status failed for {base}: HTTP {status.status_code}"
                )
            try:
                payload = status.json()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"model lifecycle status for {base} did not return JSON"
                ) from exc
            if bool(payload.get("ready")):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "model lifecycle ready purpose=%s control=%s elapsed_ms=%d",
                    purpose,
                    base,
                    elapsed_ms,
                )
                return
            if (time.monotonic() - started) >= timeout_s:
                raise TimeoutError(
                    f"model lifecycle {base} was not ready after {timeout_s}s"
                )
            await asyncio.sleep(5)


async def shutdown_model_lifecycle(
    pool: list[dict[str, Any]] | None,
    *,
    purpose: str,
) -> None:
    """Best-effort idle stop for entries that explicitly opt into auto-stop.

    Managed RTX/vLLM servers are expensive to keep hot, but immediate shutdown
    after every document causes cold-load thrash. Auto-stop therefore means
    "stop after the configured idle lease" by default. A new extraction call
    cancels and refreshes the pending lease before posting /up.
    """

    entries = [
        entry
        for entry in (pool or [])
        if entry.get("lifecycle_base_url") and entry.get("lifecycle_auto_stop")
    ]
    if not entries:
        return

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        down_path = str(entry.get("lifecycle_down_path") or "/down").strip() or "/down"
        if not down_path.startswith("/"):
            down_path = "/" + down_path
        key = (
            str(entry.get("lifecycle_base_url") or "").strip().rstrip("/"),
            down_path,
            str(entry.get("lifecycle_api_key") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    immediate: list[dict[str, Any]] = []
    for entry in unique:
        key = _lifecycle_key(entry)
        holds = _lifecycle_holds.get(key)
        if holds:
            logger.info(
                "model lifecycle idle shutdown deferred purpose=%s control=%s active_holds=%d",
                purpose,
                key[0],
                len(holds),
            )
            continue
        delay = _idle_shutdown_seconds(entry)
        if delay <= 0:
            immediate.append(entry)
            continue
        _cancel_pending_shutdown(entry, purpose=purpose)
        generation = _shutdown_generations.get(key, 0) + 1
        _shutdown_generations[key] = generation
        task = asyncio.create_task(
            _shutdown_after_idle(
                dict(entry),
                purpose=purpose,
                key=key,
                generation=generation,
                delay_seconds=delay,
            )
        )
        _shutdown_tasks[key] = task
        logger.info(
            "model lifecycle idle shutdown scheduled purpose=%s control=%s idle_seconds=%d",
            purpose,
            key[0],
            delay,
        )

    if immediate:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for entry in immediate:
                await _post_down(entry, purpose=purpose, client=client)
