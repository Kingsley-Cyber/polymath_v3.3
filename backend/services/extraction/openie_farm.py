"""N-process warm triplet-extract farm — factory execution plane, station A.

Owner-ratified (2026-08-08): one warm OpenIE extractor per WORKER PROCESS,
loaded once and kept resident until the corpus completes; eligible units from
ANY document dispatch to any worker. Parallelism changes WHEN work happens,
never WHAT semantic output exists — results are keyed by unit_id and
reassembled in the caller's deterministic unit order, so the serial and
parallel proposition digests are identical by construction (verified by
scripts/verify_factory_equality.py).

Failure semantics carry the UNION invariant unchanged: a worker error on a
unit returns an explicit failure record for that unit — never a crash, never
a silent deterministic-only unit.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
from typing import Sequence

logger = logging.getLogger(__name__)

OPENIE_FARM_RELEASE = "graphify-openie-farm-v1"

_WORKER_EXTRACTOR = None  # per-process warm instance (worker side only)


def default_worker_count() -> int:
    raw = os.environ.get("GRAPHIFY_OPENIE_WORKERS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("GRAPHIFY_OPENIE_WORKERS=%r is not an int; using auto", raw)
    return max(1, min(4, (os.cpu_count() or 2) - 2))


def rendering_payload(rendering) -> dict:
    """One rendering as a plain dict — the single shape both the serial path
    and the farm emit, so proposition identity is engine-independent."""
    links = []
    for link in getattr(rendering, "asserter_links", None) or ():
        links.append(link.to_dict() if hasattr(link, "to_dict") else dict(link))
    return {
        "subject": str(rendering.subject),
        "relation": str(rendering.relation),
        "object": str(rendering.object),
        "confidence": float(getattr(rendering, "confidence", 1.0)),
        "from_clause_split": bool(getattr(rendering, "from_clause_split", False)),
        "from_entailment": bool(getattr(rendering, "from_entailment", False)),
        "entailment_score": float(getattr(rendering, "entailment_score", 1.0)),
        "asserter_chain": [str(value) for value in (getattr(rendering, "asserter_chain", None) or ())],
        "asserter_links": links,
    }


def _worker_extract(task: tuple[str, str]) -> tuple[str, str, list]:
    """Worker-side: warm-load once, then extract. Returns (unit_id, status, rows)."""
    unit_id, text = task
    global _WORKER_EXTRACTOR
    try:
        if _WORKER_EXTRACTOR is None:
            from triplet_extract import OpenIEExtractor

            _WORKER_EXTRACTOR = OpenIEExtractor(
                speed_preset="balanced",
                deep_search=False,
                resolve_coref=False,
                preserve_latex=False,
            )
        rows = [
            rendering_payload(rendering)
            for rendering in _WORKER_EXTRACTOR.extract_triplet_objects(text)
        ]
        return unit_id, "ok", rows
    except Exception as exc:  # noqa: BLE001 — failure is data
        return unit_id, "error", [type(exc).__name__, str(exc)[:300]]


class OpenIEFarm:
    """Persistent spawn-based worker pool; models stay warm across calls."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self._pool: mp.pool.Pool | None = None
        self._lock = threading.Lock()

    def _ensure_pool(self) -> mp.pool.Pool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    context = mp.get_context("spawn")
                    self._pool = context.Pool(processes=self.workers)
                    logger.info("openie farm: %d warm worker processes", self.workers)
        return self._pool

    def extract_all(
        self, tasks: Sequence[tuple[str, str]],
    ) -> dict[str, tuple[str, list]]:
        """Dispatch (unit_id, text) tasks; return {unit_id: (status, rows)}.

        Arrival order is irrelevant — callers reassemble in their own
        deterministic unit order.
        """
        pool = self._ensure_pool()
        results: dict[str, tuple[str, list]] = {}
        for unit_id, status, rows in pool.imap_unordered(
            _worker_extract, list(tasks), chunksize=1,
        ):
            results[unit_id] = (status, rows)
        return results

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.terminate()
                self._pool.join()
                self._pool = None


_FARM: OpenIEFarm | None = None
_FARM_LOCK = threading.Lock()


def get_openie_farm(workers: int | None = None) -> OpenIEFarm:
    global _FARM
    size = workers or default_worker_count()
    if _FARM is None or _FARM.workers != size:
        with _FARM_LOCK:
            if _FARM is None or _FARM.workers != size:
                if _FARM is not None:
                    _FARM.close()
                _FARM = OpenIEFarm(size)
    return _FARM
