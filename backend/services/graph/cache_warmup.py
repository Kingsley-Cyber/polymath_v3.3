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

`services.graph.orchestrator.schedule_graph_discovery_cache_warm` is the
public trigger. It delegates here whenever the legacy discovery artifact is
missing, so post-ingest and manual warm paths both reach this tracked worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# A self-heal claim older than this (a crashed/failed warm) is reclaimable, so a
# stuck rebuild retries instead of wedging bridges off forever. Longer than a
# normal warm (clustering + label + metrics) but short enough to recover same
# session.
_WARM_CLAIM_TTL_SECONDS: float = 600.0

# Hard ceiling on a single rebuild — a wedge-breaker, not a tight SLA. With the
# metrics graph bounded to the top-N RELATES_TO hubs a real warm runs ~6.5 min
# (mostly LLM cluster-labeling + sampled betweenness), so this is set well above
# that; it only fires on a true hang, and the claim TTL then lets a later query
# retry instead of leaving a wedged task holding a Neo4j session forever.
_WARM_MAX_SECONDS: float = 600.0

# Per-corpus pending warmup tasks. Each ingest completion cancels any
# in-flight pending task and schedules a fresh one, so a 50-doc batch
# results in exactly one rebuild after the batch settles.
_PENDING_WARMUP_TASKS: dict[str, asyncio.Task[Any]] = {}

# Wait this long after the last ingest completion before kicking off
# the actual rebuild. Tuned to outlast typical batch-ingest spacing
# (5-15s per doc) but not so long that a user querying their fresh
# corpus has to wait.
_WARMUP_DEBOUNCE_SECONDS: float = 30.0


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def active_ingest_warmup_defer_seconds(fallback: float) -> float:
    raw = os.getenv("GRAPH_CACHE_WARMUP_ACTIVE_INGEST_DEFER_SECONDS")
    if not raw:
        return max(0.01, fallback)
    try:
        return max(0.01, float(raw))
    except ValueError:
        logger.warning(
            "auto-warm: invalid GRAPH_CACHE_WARMUP_ACTIVE_INGEST_DEFER_SECONDS=%r; "
            "using %.0fs",
            raw,
            fallback,
        )
        return max(0.01, fallback)


async def should_defer_warmup_for_active_ingest(db: Any, corpus_id: str) -> bool:
    """Return True when a corpus still has an actively running durable batch."""

    if not _env_flag("GRAPH_CACHE_WARMUP_SKIP_DURING_ACTIVE_INGEST"):
        return False
    if db is None:
        return False
    try:
        batch = await db["ingest_batches"].find_one(
            {"corpus_id": corpus_id, "status": "running"},
            {"_id": 1},
        )
    except Exception as exc:
        logger.warning(
            "auto-warm: active-ingest check failed corpus=%s: %s",
            corpus_id[:8],
            exc,
        )
        return False
    return batch is not None


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
            while await should_defer_warmup_for_active_ingest(db, corpus_id):
                defer_seconds = active_ingest_warmup_defer_seconds(
                    debounce_seconds
                )
                logger.info(
                    "auto-warm: deferring metrics rebuild corpus=%s; "
                    "durable ingest batch is still running",
                    corpus_id[:8],
                )
                await asyncio.sleep(defer_seconds)

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
            await asyncio.wait_for(
                emerge_domains(qdrant, neo4j_driver, db, corpus_id),
                timeout=_WARM_MAX_SECONDS,
            )
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


async def ensure_graph_metrics_fresh(corpus_id: str) -> str:
    """Self-healing graph-analytics cache — call this on the graph query path.

    The corpus_change_signature (sha256 of doc_ids + updated_at) is the universal
    freshness oracle: it changes on ANY mutation — ingest, delete, backfill,
    dedup, re-ingest. This checks the live signature against `graph_metrics_cache`
    and, on a miss (missing OR stale), atomically claims and schedules a
    background rebuild. It NEVER blocks the query — bridges simply appear on the
    next graph query once the rebuild lands.

    This is both PREVENTIVE (future staleness self-repairs on next use) and
    CORRECTIVE (an already-empty/stale cache repairs itself), replacing the
    fragile ingest-only, in-process, non-durable warm task as the sole producer.

    Durability + no-stampede: the "warming" claim is a Mongo doc
    (`graph_metrics_warm_state`), so it survives restarts and a claim older than
    `_WARM_CLAIM_TTL_SECONDS` (a crashed warm) is reclaimable. Concurrent queries
    / replicas can't double-warm. Best-effort: every failure is swallowed.

    Returns one of: "fresh" | "scheduled" | "in_flight" | "skipped" | "error".
    """
    try:
        from services.ingestion_service import ingestion_service
        from services.graph.analytics import compute_corpus_change_signature

        db = ingestion_service.db
        neo4j_driver = ingestion_service.neo4j_driver
        qdrant = ingestion_service.qdrant_client
        if db is None or neo4j_driver is None:
            return "skipped"

        sig = await compute_corpus_change_signature(db, corpus_id)
        fresh = await db["graph_metrics_cache"].find_one(
            {"corpus_id": corpus_id, "corpus_change_signature": sig}, {"_id": 1}
        )
        if fresh:
            return "fresh"

        # Already warming THIS signature, recently? (durable across restarts.)
        now = datetime.utcnow()
        state = await db["graph_metrics_warm_state"].find_one({"corpus_id": corpus_id})
        if (
            state
            and state.get("signature") == sig
            and isinstance(state.get("started_at"), datetime)
            and (now - state["started_at"]).total_seconds() < _WARM_CLAIM_TTL_SECONDS
        ):
            return "in_flight"

        # Claim it (the in-process debounce dedups concurrent same-process calls;
        # this marker dedups across restarts / replicas and drives retry-on-fail).
        await db["graph_metrics_warm_state"].update_one(
            {"corpus_id": corpus_id},
            {"$set": {
                "corpus_id": corpus_id,
                "corpus_change_signature": sig,
                "signature": sig,
                "status": "warming",
                "started_at": now,
            }},
            upsert=True,
        )
        schedule_metrics_warmup_after_ingest(
            qdrant=qdrant,
            neo4j_driver=neo4j_driver,
            db=db,
            corpus_id=corpus_id,
            debounce_seconds=0.5,  # near-immediate; this is a repair, not a batch
        )
        logger.info(
            "self-heal: graph-metrics cache stale/missing corpus=%s sig=%s — "
            "scheduled background rebuild",
            corpus_id[:8],
            sig[:8],
        )
        return "scheduled"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "self-heal: ensure_graph_metrics_fresh failed corpus=%s: %s",
            str(corpus_id)[:8],
            exc,
        )
        return "error"
