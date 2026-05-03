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
from utils.tokens import count_tokens_messages, get_model_context_limit

logger = logging.getLogger(__name__)

# Reserve a small headroom under the model's reported context so off-by-one
# tokenizer estimates (we use tiktoken for cloud, char/4 fallback for local)
# don't push the request 1–2 tokens past the limit and trip a 400.
_SUMMARY_CONTEXT_SAFETY_MARGIN = 64
# Floor for the completion budget. If we can't get at least this many output
# tokens for the summary, the prompt is too large to summarize meaningfully —
# skip with a warning instead of sending a request that will either 400 or
# return a 1-token fragment.
_MIN_SUMMARY_OUTPUT_TOKENS = 32
# Substrings that indicate the provider rejected us for context-window reasons.
# Match against the lowercased response body so we can degrade gracefully on
# the first call and skip the parent rather than burning lane retries.
_CONTEXT_OVERFLOW_HINTS = (
    "context length",
    "maximum context",
    "input_tokens",
    "exceed",
    "too long",
)

_SYSTEM = (
    "You create retrieval summaries for book, article, and markdown parent "
    "chunks. Produce dense factual summaries that preserve key terms, proper "
    "nouns, technical language, dates, claims, and section context. Do not "
    "frame the content as a meeting or transcript. Do not add information not "
    "present in the context."
)

_USER = (
    "Use the following context to produce a dense factual parent-chunk summary "
    "for retrieval. The context is from a book, article, or markdown document "
    "parent chunk, not a meeting transcript. Keep it in {max_tokens} tokens "
    "or fewer. Focus on facts, entities, relationships, dates, claims, "
    "definitions, and terminology that would help answer later questions.\n\n"
    "CONTEXT:\n{text}\n\nDENSE FACTUAL SUMMARY:"
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


def _safe_summary_budget(
    *,
    messages: list[dict[str, str]],
    model: str,
    requested_tokens: int,
    context_limit_override: int | None = None,
) -> tuple[int | None, dict[str, int]]:
    """Compute the safe `max_tokens` for a summary call.

    Returns `(safe_max_tokens, budget)`. `safe_max_tokens is None` means the
    prompt is too large to leave room for a meaningful completion — caller
    should skip the parent with a warning rather than send a request that the
    provider will reject (or worse, that will succeed but truncate the output
    silently). `budget` is a structured dict for logging / metrics.

    `context_limit_override` lets callers pass the lane's authoritative context
    window (from the model_pool registration) instead of the
    `get_model_context_limit` registry guess. Local fine-tunes like
    `lfm2-summary` running at 12,288 are not in the registry and would
    otherwise default to 4,096.
    """
    context_limit = context_limit_override or get_model_context_limit(model)
    prompt_tokens = count_tokens_messages(messages, model)
    available = context_limit - prompt_tokens - _SUMMARY_CONTEXT_SAFETY_MARGIN
    budget = {
        "context_limit": context_limit,
        "prompt_tokens_estimate": prompt_tokens,
        "available_completion_tokens": available,
        "requested_completion_tokens": requested_tokens,
        "safety_margin_tokens": _SUMMARY_CONTEXT_SAFETY_MARGIN,
    }
    if available < _MIN_SUMMARY_OUTPUT_TOKENS:
        return None, budget
    return min(requested_tokens, available), budget


def _is_context_overflow_error(exc: Exception) -> bool:
    """Recognize provider 400s caused by exceeding the context window so we
    can demote them from fatal-lane signals to per-parent skips.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code != 400:
        return False
    text = provider_error_summary(exc).lower()
    return any(hint in text for hint in _CONTEXT_OVERFLOW_HINTS)


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
    task_queue: "asyncio.Queue[SummaryTask]" = asyncio.Queue()
    for t in tasks:
        task_queue.put_nowait(t)

    results_list: list[SummaryResult] = []
    _list_lock = asyncio.Lock()
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
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER.format(max_tokens=cap, text=task.text)},
        ]
        # Pre-flight token budget. The chunker honors the user-configured
        # parent_chunk_tokens range, which can exceed the summary model's
        # context window; without this guard the provider returns a 400 that
        # gets silently swallowed and the doc fails downstream.
        safe_max, budget = _safe_summary_budget(
            messages=messages,
            model=str(entry["model"]),
            requested_tokens=cap,
            context_limit_override=entry.get("context_length"),
        )
        if safe_max is None:
            logger.warning(
                "GHOST A skip parent_id=%s reason=token_budget model=%s "
                "prompt_tokens=%d context_limit=%d available=%d",
                task.parent_id,
                entry["model"],
                budget["prompt_tokens_estimate"],
                budget["context_limit"],
                budget["available_completion_tokens"],
            )
            return None
        payload: dict = {
            "model": entry["model"],
            "messages": messages,
            "temperature": 0,
            "max_tokens": safe_max,
        }
        if entry.get("base_url"):
            payload["api_base"] = entry["base_url"]
        if entry.get("api_key"):
            payload["api_key"] = entry["api_key"]
        for _k, _v in (entry.get("extra_params") or {}).items():
            if _k not in ("model", "messages", "max_tokens"):
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
            # Provider rejected the request because our token estimate was
            # off (tokenizer mismatch, prompt template overhead). Demote to a
            # per-parent skip — the lane is healthy, this single parent is
            # just too big to fit. Without this branch a single oversized
            # parent would trip SOFT_FATAL_DISABLE_STRIKES and disable the
            # whole lane mid-ingest.
            if _is_context_overflow_error(exc):
                logger.warning(
                    "GHOST A skip parent_id=%s reason=context_overflow_400 "
                    "model=%s prompt_tokens_estimate=%d context_limit=%d "
                    "safe_max=%d: %s",
                    task.parent_id,
                    entry["model"],
                    budget["prompt_tokens_estimate"],
                    budget["context_limit"],
                    safe_max,
                    provider_error_summary(exc),
                )
                return None
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
                    async with _list_lock:
                        results_list.append(result)
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

    await _run_enabled_workers()
    while not task_queue.empty():
        enabled_count = sum(
            1 for pool_idx in range(len(pool)) if pool_idx not in disabled_lanes
        )
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

    results = results_list
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
