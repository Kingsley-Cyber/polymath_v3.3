"""Process-local ingest admission control.

Extracted from `routers/ingestion.py` so the MCP write surface
(`polymath_mcp/tools.py:_ingest_bytes`) can reuse the same gate.

Two surfaces ingest into Polymath:
  1. HTTP route POST /api/corpora/{cid}/ingest    (routers/ingestion.py)
  2. MCP tools `polymath_upload_document` / `polymath_ingest_from_url`
     (polymath_mcp/tools.py:_ingest_bytes)

Pre-fix (commit 0a47b8f only patched #1): the HTTP route acquired a
slot before reading the file body so a 500-file simultaneous burst
couldn't materialize 5 GB of bytes in RAM before being 429'd. The
MCP path called `ingestion_service.ingest()` DIRECTLY without ever
acquiring a slot. An agent looping over 500 files via MCP could
trigger the exact same OOM / worker-starvation pattern the HTTP fix
prevented.

This module hosts the primitives. Both surfaces now route through:
    acquire = await try_acquire_ingest_slot()
    if not acquire:
        # surface-specific reject (HTTP: 429, MCP: ValueError)
    try:
        ... do the work
    finally / on early-failure:
        await release_ingest_slot()

Process-local counter caveat: if the FastAPI backend and the MCP
sidecar run as separate processes, they each get an INDEPENDENT
counter. Combined ceiling = HTTP_LIMIT + MCP_LIMIT. Each process
is individually safe, but cross-process coordination would need a
Redis-backed counter. For the typical single-host docker-compose
deployment that's fine — each container's worker pool is bounded.
If multi-replica deployment ever lands, replace this module's
state with a distributed counter.
"""

from __future__ import annotations

import asyncio
import logging

from config import get_settings

logger = logging.getLogger(__name__)

# Read once at module load. Tests can override the LIMIT module-level
# attribute (same pattern routers/ingestion.py used pre-extraction).
INGEST_ACTIVE_LIMIT: int = max(1, int(get_settings().INGEST_MAX_ACTIVE_JOBS))

# Per-process count of currently-in-flight ingest jobs that have
# acquired a slot. Reset to 0 in tests via the fixture that imports
# this module.
_ingest_active_count: int = 0
_admission_lock = asyncio.Lock()


async def try_acquire_ingest_slot() -> bool:
    """Try to reserve an ingest slot.

    Returns True if the active count was below INGEST_ACTIVE_LIMIT
    and was incremented; False when the cap is already saturated.
    Callers MUST pair every True return with a matching
    release_ingest_slot() call (typically in a finally block or
    in the background worker task's finally).
    """
    global _ingest_active_count
    async with _admission_lock:
        if _ingest_active_count >= INGEST_ACTIVE_LIMIT:
            return False
        _ingest_active_count += 1
        return True


async def release_ingest_slot() -> None:
    """Release an ingest slot.

    Safe to call when the count is already 0 (defensive on early-
    failure paths that might race with the background worker task's
    own release). The max(0, ...) prevents double-release from
    pushing the counter negative.
    """
    global _ingest_active_count
    async with _admission_lock:
        _ingest_active_count = max(0, _ingest_active_count - 1)


def active_count() -> int:
    """Diagnostic — current in-flight ingest count. Used by status
    endpoints and tests. Not a coordination primitive; check-then-
    acquire across awaits without holding the lock is racy."""
    return _ingest_active_count


def _reset_for_tests() -> None:
    """Test-only hook — restore the counter to zero between cases.
    Production code never calls this."""
    global _ingest_active_count
    _ingest_active_count = 0
