"""
GHOST A — Parent Summarization (Phase 3)

Runs when ingestion_config.chunk_summarization is True.
Summarizes parent chunks via LiteLLM. temperature=0 for determinism.
Bounded by SUMMARY_MAX_CONCURRENT semaphore.

Called by the ingestion worker AFTER tier chunking and stable ID assignment.
Returns SummaryResult list — worker persists to Mongo + Qdrant.

Tier routing for Qdrant writes (enforced by caller):
  polymath_naive  — always eligible
  polymath_hrag   — only if source_tier in {tier_a, tier_b}
  polymath_graph  — NEVER (summaries do NOT seed Entity nodes)
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from config import get_settings
from services.llm_lane_pool import (
    FatalLaneError,
    SOFT_FATAL_DISABLE_STRIKES,
    is_fatal_provider_error,
    provider_error_tier,
    provider_error_summary,
)

logger = logging.getLogger(__name__)
_SUMMARY_RETRY_ATTEMPTS = 2
_SUMMARY_RETRY_BACKOFF_SECONDS = 1.5

_SYSTEM = (
    "You are a precise document summarizer. Produce a factual, dense summary "
    "that preserves key terms, proper nouns, and technical language. "
    "Do not add information not present in the passage."
)

_USER = (
    "Summarize the following passage in {max_tokens} tokens or fewer.\n\n"
    "PASSAGE:\n{text}\n\nSUMMARY:"
)


@dataclass
class SummaryTask:
    parent_id: str
    doc_id: str
    corpus_id: str
    text: str
    source_tier: str  # tier_a | tier_b | tier_c | ocr_ast


@dataclass
class SummaryResult:
    parent_id: str
    doc_id: str
    corpus_id: str
    source_tier: str
    summary: str


async def summarize_parents(
    tasks: list[SummaryTask],
    max_summary_tokens: int | None = None,
    *,
    pool: list[dict] | None = None,
    model: str | None = None,
) -> list[SummaryResult]:
    """
    Summarize parent chunks in parallel, round-robining tasks across the
    `pool`. Each pool entry gets its own asyncio.Semaphore sized by
    max_concurrent, so overall throughput = sum(entry.max_concurrent).

    pool entries (already decrypted at the worker layer):
        {
          "model":          str,
          "base_url":       str | None,
          "api_key":        str | None,          # plaintext
          "max_concurrent": int,
          "extra_params":   dict,
        }

    If `pool` is empty/None and `model` is given, a one-entry default pool is
    synthesized (falls back to settings.SUMMARY_MAX_CONCURRENT). This keeps
    legacy callers working.

    Toggle gate: caller must verify ingestion_config.chunk_summarization.
    """
    if not tasks:
        return []

    settings = get_settings()
    cap = max_summary_tokens or settings.SUMMARY_MAX_TOKENS
    headers = {
        "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }

    if not pool:
        pool = [
            {
                "model": model or settings.DEFAULT_COMPLETION_MODEL,
                "base_url": None,
                "api_key": None,
                "max_concurrent": settings.SUMMARY_MAX_CONCURRENT,
                "extra_params": {},
            }
        ]

    # Phase K — WORK-STEALING POOL. See ghost_b.py for full rationale.
    # Shared task queue; one worker per lane slot; workers pull as fast
    # as they can → slow lanes don't bottleneck fast lanes' throughput.
    task_by_parent_id = {t.parent_id: t for t in tasks}
    task_queue: "asyncio.Queue[SummaryTask]" = asyncio.Queue()
    for t in tasks:
        task_queue.put_nowait(t)

    results_by_parent_id: dict[str, SummaryResult] = {}
    _results_lock = asyncio.Lock()
    disabled_lanes: set[int] = set()
    lane_fatal_strikes: dict[int, int] = {}
    _disabled_lock = asyncio.Lock()

    async def _lane_disable_ready(pool_idx: int, exc: Exception) -> bool:
        tier = provider_error_tier(exc)
        if tier == "hard":
            return True
        if tier != "soft":
            return False
        async with _disabled_lock:
            strikes = lane_fatal_strikes.get(pool_idx, 0) + 1
            lane_fatal_strikes[pool_idx] = strikes
        entry = pool[pool_idx]
        if strikes >= SOFT_FATAL_DISABLE_STRIKES:
            return True
        logger.warning(
            "GHOST A saw soft fatal provider signal for lane=%d model=%s "
            "strike=%d/%d; keeping lane active until repeated: %s",
            pool_idx,
            entry["model"],
            strikes,
            SOFT_FATAL_DISABLE_STRIKES,
            provider_error_summary(exc),
        )
        return False

    async def _clear_lane_strikes(pool_idx: int) -> None:
        async with _disabled_lock:
            lane_fatal_strikes.pop(pool_idx, None)

    async def _disable_lane(pool_idx: int, exc: Exception) -> None:
        async with _disabled_lock:
            if pool_idx in disabled_lanes:
                return
            disabled_lanes.add(pool_idx)
        entry = pool[pool_idx]
        logger.error(
            "GHOST A disabled summary lane=%d model=%s after fatal provider error: %s",
            pool_idx,
            entry["model"],
            provider_error_summary(exc),
        )

    async def _process_one(task: SummaryTask, pool_idx: int) -> SummaryResult | None:
        entry = pool[pool_idx]
        payload: dict = {
            "model": entry["model"],
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER.format(max_tokens=cap, text=task.text)},
            ],
            "temperature": 0,
            "max_tokens": cap,
        }
        if entry.get("base_url"):
            payload["api_base"] = entry["base_url"]
        if entry.get("api_key"):
            payload["api_key"] = entry["api_key"]
        for _k, _v in (entry.get("extra_params") or {}).items():
            if _k not in ("model", "messages"):
                payload[_k] = _v
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{settings.LITELLM_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                summary = resp.json()["choices"][0]["message"]["content"].strip()
                return SummaryResult(
                    parent_id=task.parent_id,
                    doc_id=task.doc_id,
                    corpus_id=task.corpus_id,
                    source_tier=task.source_tier,
                    summary=summary,
                )
        except Exception as exc:
            if is_fatal_provider_error(exc):
                raise FatalLaneError(exc) from exc
            logger.error(
                "GHOST A failed parent_id=%s via %s: %s",
                task.parent_id, entry["model"], exc,
            )
            return None

    async def _lane_worker(pool_idx: int) -> None:
        while True:
            if pool_idx in disabled_lanes:
                return
            try:
                task = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if pool_idx in disabled_lanes:
                    task_queue.put_nowait(task)
                    return
                try:
                    result = await _process_one(task, pool_idx)
                except FatalLaneError as exc:
                    task_queue.put_nowait(task)
                    if await _lane_disable_ready(pool_idx, exc.original):
                        await _disable_lane(pool_idx, exc.original)
                        logger.warning(
                            "GHOST A requeued parent_id=%s after disabling lane=%d",
                            task.parent_id,
                            pool_idx,
                        )
                    else:
                        logger.warning(
                            "GHOST A requeued parent_id=%s after soft fatal strike on lane=%d",
                            task.parent_id,
                            pool_idx,
                        )
                    return
                if result is not None:
                    await _clear_lane_strikes(pool_idx)
                    async with _results_lock:
                        results_by_parent_id[result.parent_id] = result
            finally:
                task_queue.task_done()

    async def _run_enabled_workers() -> None:
        workers: list[asyncio.Task] = []
        for pool_idx, entry in enumerate(pool):
            if pool_idx in disabled_lanes:
                continue
            slots = int(entry.get("max_concurrent") or 1) or 1
            for _ in range(slots):
                workers.append(asyncio.create_task(_lane_worker(pool_idx)))
        if workers:
            await asyncio.gather(*workers, return_exceptions=False)

    def _enabled_lane_count() -> int:
        return sum(
            1 for pool_idx in range(len(pool)) if pool_idx not in disabled_lanes
        )

    async def _drain_pending_queue() -> None:
        while not task_queue.empty():
            enabled_count = _enabled_lane_count()
            if enabled_count <= 0:
                logger.error(
                    "GHOST A stopped with %d parents still queued because all summary lanes were disabled",
                    task_queue.qsize(),
                )
                break
            pending_before = task_queue.qsize()
            disabled_before = len(disabled_lanes)
            await _run_enabled_workers()
            if task_queue.qsize() >= pending_before and len(disabled_lanes) == disabled_before:
                logger.warning(
                    "GHOST A stopped with %d parents still queued after retry drain made no progress",
                    task_queue.qsize(),
                )
                break

    def _clear_pending_queue() -> None:
        while not task_queue.empty():
            try:
                task_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            task_queue.task_done()

    def _missing_tasks() -> list[SummaryTask]:
        return [
            task_by_parent_id[parent_id]
            for parent_id in task_by_parent_id
            if parent_id not in results_by_parent_id
        ]

    await _run_enabled_workers()
    await _drain_pending_queue()

    for attempt in range(1, _SUMMARY_RETRY_ATTEMPTS + 1):
        missing = _missing_tasks()
        if not missing:
            break
        if _enabled_lane_count() <= 0:
            logger.error(
                "GHOST A cannot retry %d missing parents because all summary lanes are disabled",
                len(missing),
            )
            break
        _clear_pending_queue()
        if _SUMMARY_RETRY_BACKOFF_SECONDS > 0:
            await asyncio.sleep(_SUMMARY_RETRY_BACKOFF_SECONDS * attempt)
        logger.warning(
            "GHOST A retry %d/%d for %d missing parent summaries",
            attempt,
            _SUMMARY_RETRY_ATTEMPTS,
            len(missing),
        )
        for task in missing:
            task_queue.put_nowait(task)
        await _run_enabled_workers()
        await _drain_pending_queue()

    if not task_queue.empty():
        _clear_pending_queue()

    results = [
        results_by_parent_id[t.parent_id]
        for t in tasks
        if t.parent_id in results_by_parent_id
    ]
    if disabled_lanes:
        logger.warning(
            "GHOST A completed with disabled lanes: %s",
            ", ".join(
                f"{idx}:{pool[idx]['model']}" for idx in sorted(disabled_lanes)
            ),
        )
    logger.info(
        "GHOST A complete: %d/%d parents summarized across %d model(s) [%s]",
        len(results),
        len(tasks),
        len(pool),
        ", ".join(e["model"] for e in pool),
    )
    return results
