"""Backend-owned Redis cache for live web search.

This cache is separate from LiteLLM's Redis cache. LiteLLM can cache model
responses; this module caches external web I/O after Polymath has normalized
it into safe, bounded JSON values.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)


class BackendWebCache:
    """Tiny async Redis JSON cache with fail-open behavior."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._disabled_until = 0.0

    async def _get_client(self) -> Any | None:
        if time.monotonic() < self._disabled_until:
            return None
        if self._client is not None:
            return self._client
        try:
            import redis.asyncio as aioredis

            settings = get_settings()
            self._client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            return self._client
        except Exception as exc:
            logger.debug("live web Redis cache unavailable: %s", exc)
            self._disabled_until = time.monotonic() + 30.0
            self._client = None
            return None

    async def get_json(self, key: str) -> dict[str, Any] | None:
        client = await self._get_client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
            if not raw:
                return None
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logger.debug("live web Redis cache get failed: %s", exc)
            self._disabled_until = time.monotonic() + 30.0
            self._client = None
            return None

    async def set_json(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> bool:
        if ttl_seconds <= 0:
            return False
        client = await self._get_client()
        if client is None:
            return False
        try:
            await client.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl_seconds)
            return True
        except Exception as exc:
            logger.debug("live web Redis cache set failed: %s", exc)
            self._disabled_until = time.monotonic() + 30.0
            self._client = None
            return False


web_cache = BackendWebCache()
