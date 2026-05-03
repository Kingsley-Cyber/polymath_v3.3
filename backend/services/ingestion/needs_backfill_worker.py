"""Background scanner for documents stuck in needs_backfill graph status.

Companion to GraphRepairWorker. While the repair worker drains the per-relation
graph_repair_queue, this worker scans the documents collection for any doc
whose write_state.graph_status is "needs_backfill" and triggers
backfill_failed_graph_chunks — which now includes the staging fallback for
documents where Ghost B extraction succeeded but post-extraction steps
(repair enqueue, graph vectors, or Neo4j write) crashed.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import datetime
from typing import Any

from config import get_settings
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


class NeedsBackfillWorker:
    """Periodically scans for documents with graph_status == needs_backfill
    and triggers graph backfill to recover them.

    Uses the same polling cadence as GraphRepairWorker so the two scanners
    share the same duty-cycle budget. Each backfill call is idempotent —
    the staging fallback in backfill_failed_graph_chunks handles the case
    where ghost_b_staging exists but ghost_b_failures is empty.
    """

    def __init__(
        self,
        *,
        db: AsyncIOMotorDatabase,
        qdrant_client: AsyncQdrantClient,
        neo4j_driver: Any,
    ) -> None:
        self._db = db
        self._qdrant = qdrant_client
        self._neo4j = neo4j_driver
        self._settings = get_settings()
        self._owner = f"backfill-worker:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        if not self._settings.GRAPH_REPAIR_WORKER_ENABLED:
            logger.info(
                "NeedsBackfillWorker: staying idle (GRAPH_REPAIR_WORKER_ENABLED=False)"
            )
            return
        self._task = asyncio.create_task(self._run(), name="needs-backfill-worker")
        logger.info("NeedsBackfillWorker started owner=%s", self._owner)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("NeedsBackfillWorker stopped owner=%s", self._owner)

    async def _run(self) -> None:
        poll_seconds = float(self._settings.GRAPH_REPAIR_WORKER_POLL_SECONDS)
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception as exc:
                logger.warning("NeedsBackfillWorker loop error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass  # poll interval elapsed — scan again

    async def _scan_once(self) -> None:
        """Find up to 20 docs with needs_backfill and attempt recovery."""
        if self._neo4j is None:
            return

        docs = (
            await self._db["documents"]
            .find({"write_state.graph_status": "needs_backfill"})
            .to_list(length=20)
        )

        if not docs:
            return

        logger.info(
            "NeedsBackfillWorker: found %d docs in needs_backfill",
            len(docs),
        )

        from services.ingestion.graph_backfill import backfill_failed_graph_chunks

        for doc in docs:
            doc_id = doc.get("doc_id", "")
            corpus_id = doc.get("corpus_id", "")
            user_id = doc.get("user_id", "system")
            if not doc_id or not corpus_id:
                continue
            try:
                result = await backfill_failed_graph_chunks(
                    db=self._db,
                    qdrant_client=self._qdrant,
                    neo4j_driver=self._neo4j,
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    user_id=user_id,
                )
                status = result.get("status", "unknown")
                recovered = int(result.get("recovered_chunks") or 0)
                logger.info(
                    "NeedsBackfillWorker: doc=%s corpus=%s status=%s recovered=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    status,
                    recovered,
                )
            except Exception as exc:
                logger.warning(
                    "NeedsBackfillWorker: doc=%s corpus=%s failed: %s",
                    doc_id[:12],
                    corpus_id[:8],
                    exc,
                )
