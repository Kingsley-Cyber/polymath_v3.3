"""Phase 4 — debounced auto-warm of the graph analytics cache.

When an ingest finishes writing to Mongo + Qdrant + Neo4j, we want the
analytics.CorpusMetrics cache to refresh so subsequent Agent Search /
chat queries hit the elite path (Phases 1-3) instead of falling back
to naive Cypher.

The naive approach — call `emerge_domains()` synchronously at the end
of every ingest — has two problems:

  1. PageRank + betweenness + community detection are O(V + E) but
     can take 5-30s on a large corpus. Blocking the ingest response
     for that long defeats the async ingest pattern.

  2. Bulk ingests fire 50 ingest-completion events in rapid succession.
     Rebuilding the cache 50 times during a batch is pure waste — the
     signature changes each time but the user gets nothing useful
     until the batch settles.

This module's solution: a per-corpus **debounced** background task.
Each ingest completion cancels any pending warmup for the corpus and
re-arms a new one. The cache only rebuilds ~`_WARMUP_DEBOUNCE_SECONDS`
after the LAST ingest completion in a batch — so a 50-doc upload
results in exactly one rebuild.

The pre-existing `services.graph.orchestrator.schedule_graph_discovery_cache_warm`
is a no-op stub (its legacy `.pyc` is missing on most deployments).
This module replaces the dead-code path with one that actually works.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Per-corpus pending warmup tasks. Each ingest completion cancels any
# in-flight pending task and schedules a fresh one, so a 50-doc batch
# results in exactly one rebuild after the batch settles.
_PENDING_WARMUP_TASKS: dict[str, asyncio.Task[Any]] = {}

# Wait this long after the last ingest completion before kicking off
# the actual rebuild. Tuned to outlast typical batch-ingest spacing
# (5-15s per doc) but not so long that a user querying their fresh
# corpus has to wait.
_WARMUP_DEBOUNCE_SECONDS: float = 30.0


def schedule_metrics_warmup_after_ingest(
    *,
    qdrant: Any,
    neo4j_driver: Any,
    db: Any,
    corpus_id: str,
    debounce_seconds: float = _WARMUP_DEBOUNCE_SECONDS,
) -> None:
    """Schedule a debounced graph analytics cache rebuild for `corpus_id`.

    Called from `worker.py` after a successful ingest writes Mongo +
    Qdrant + Neo4j. The actual rebuild fires `debounce_seconds` later;
    any subsequent call for the same corpus cancels the pending task
    and re-arms a new one. Result: bulk ingests produce exactly one
    rebuild after the batch settles.

    Best-effort — every failure mode (cancelled, emerge_domains raises,
    asyncio loop issues, missing dependencies) is caught and logged.
    The ingest pipeline is never blocked or broken by this hook.

    Args:
        qdrant: AsyncQdrantClient instance (or None — emerge_domains
            tolerates).
        neo4j_driver: AsyncDriver instance (or None — emerge_domains
            tolerates).
        db: AsyncIOMotorDatabase instance. Required; without it
            emerge_domains has nowhere to write the cache row.
        corpus_id: Which corpus to warm.
        debounce_seconds: Delay before the actual rebuild fires.
            Defaults to `_WARMUP_DEBOUNCE_SECONDS`. Tests pass a
            shorter value.
    """
    if db is None:
        logger.debug(
            "auto-warm: db handle is None — skipping rebuild for corpus=%s",
            corpus_id[:8],
        )
        return

    # Cancel any pending warmup for this corpus — the most recent
    # ingest completion supersedes earlier scheduling. This is the
    # debounce mechanism: while ingests keep firing, the task gets
    # cancelled-and-replaced before its sleep ever completes.
    existing = _PENDING_WARMUP_TASKS.get(corpus_id)
    if existing is not None and not existing.done():
        existing.cancel()
        logger.debug(
            "auto-warm: superseding pending task for corpus=%s",
            corpus_id[:8],
        )

    async def _delayed_warmup() -> None:
        try:
            await asyncio.sleep(debounce_seconds)
        except asyncio.CancelledError:
            # Superseded by a newer ingest completion — exit cleanly.
            return
        try:
            # Local import — avoids pulling analytics' heavy
            # dependencies (NetworkX, etc.) at module load time. The
            # ingest worker runs in worker processes that don't need
            # those deps unless an actual warmup fires.
            from services.graph.analytics import emerge_domains
            logger.info(
                "auto-warm: starting metrics rebuild corpus=%s "
                "(triggered by ingest + %.0fs debounce)",
                corpus_id[:8],
                debounce_seconds,
            )
            await emerge_domains(qdrant, neo4j_driver, db, corpus_id)
            logger.info(
                "auto-warm: metrics rebuild complete corpus=%s",
                corpus_id[:8],
            )
        except asyncio.CancelledError:
            # Re-raise so the asyncio runtime cleans up properly.
            raise
        except Exception as exc:
            logger.warning(
                "auto-warm: metrics rebuild failed corpus=%s: %s",
                corpus_id[:8],
                exc,
            )
        finally:
            # Drop our tracking entry. New calls after this completes
            # spawn fresh tasks.
            _PENDING_WARMUP_TASKS.pop(corpus_id, None)

    try:
        task = asyncio.create_task(_delayed_warmup())
    except RuntimeError as exc:
        # asyncio.create_task requires a running loop. If the caller is
        # in a sync context (shouldn't happen — worker is async, but
        # defensive), log and bail.
        logger.warning(
            "auto-warm: cannot create task for corpus=%s (%s) — "
            "no running event loop",
            corpus_id[:8],
            exc,
        )
        return
    _PENDING_WARMUP_TASKS[corpus_id] = task


def is_warmup_pending(corpus_id: str) -> bool:
    """Diagnostic — return True iff a debounced warmup is currently
    scheduled (or in progress) for this corpus. Used by tests and
    optionally by status endpoints."""
    t = _PENDING_WARMUP_TASKS.get(corpus_id)
    return t is not None and not t.done()


def pending_corpus_ids() -> list[str]:
    """Diagnostic — return the corpus_ids that currently have a
    debounced warmup pending or in flight."""
    return [cid for cid, t in _PENDING_WARMUP_TASKS.items() if not t.done()]
