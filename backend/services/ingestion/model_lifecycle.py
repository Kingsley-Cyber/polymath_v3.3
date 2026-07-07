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


def _managed_entries(pool: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not pool:
        return []
    return [
        entry
        for entry in pool
        if entry.get("lifecycle_base_url") and entry.get("lifecycle_auto_start")
    ]


def _url(entry: dict[str, Any], path_field: str, default_path: str) -> str:
    base = str(entry.get("lifecycle_base_url") or "").strip().rstrip("/")
    path = str(entry.get(path_field) or default_path).strip() or default_path
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _headers(entry: dict[str, Any]) -> dict[str, str]:
    key = str(entry.get("lifecycle_api_key") or "").strip()
    return {"X-Api-Key": key} if key else {}


async def ensure_model_lifecycle_ready(
    pool: list[dict[str, Any]] | None,
    *,
    purpose: str,
) -> None:
    """Start and poll each managed runtime used by this pool."""

    entries = _managed_entries(pool)
    if not entries:
        return

    seen: set[tuple[str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            str(entry.get("lifecycle_base_url") or "").strip().rstrip("/"),
            str(entry.get("lifecycle_up_path") or "/up"),
            str(entry.get("lifecycle_status_path") or "/status"),
            str(entry.get("lifecycle_api_key") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    await asyncio.gather(*[_ensure_one_ready(entry, purpose=purpose) for entry in unique])


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
    """Best-effort stop for entries that explicitly opt into auto-stop."""

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

    async with httpx.AsyncClient(timeout=20.0) as client:
        for entry in unique:
            base = str(entry.get("lifecycle_base_url") or "").strip().rstrip("/")
            try:
                resp = await client.post(
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
