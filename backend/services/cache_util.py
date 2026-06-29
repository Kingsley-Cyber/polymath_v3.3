"""Small dependency-free TTL caches for hot RAG paths.

In-process only (per worker) — deliberately simple: no Redis round-trip on the
hot path, no new dependency. Used for query embeddings and assembled retrieval
results, both of which are deterministic for a fixed (text, model) / (query,
corpus, tier) and therefore safe to memoize for a short window.
"""

from __future__ import annotations

import hashlib
from time import monotonic
from typing import Any, Hashable, Optional


class TTLCache:
    """Tiny TTL + size-bounded cache. Not thread-safe across threads, but the
    app runs a single asyncio loop per worker and these ops are synchronous, so
    there is no await between get/set — concurrent coroutines can't interleave a
    partial update."""

    def __init__(self, *, maxsize: int = 4096, ttl_seconds: float = 300.0) -> None:
        self._maxsize = max(1, int(maxsize))
        self._ttl = float(ttl_seconds)
        self._store: "dict[Hashable, tuple[float, Any]]" = {}

    def get(self, key: Hashable) -> Optional[Any]:
        item = self._store.get(key)
        if item is None:
            return None
        ts, value = item
        if (monotonic() - ts) >= self._ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: Hashable, value: Any) -> None:
        if len(self._store) >= self._maxsize and key not in self._store:
            # Evict the oldest ~10% by insertion order (dicts preserve it).
            for stale in list(self._store.keys())[: max(1, self._maxsize // 10)]:
                self._store.pop(stale, None)
        self._store[key] = (monotonic(), value)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


def hash_key(*parts: Any) -> str:
    """Stable content-addressed key from arbitrary parts."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8", "ignore"))
        digest.update(b"\x00")
    return digest.hexdigest()
