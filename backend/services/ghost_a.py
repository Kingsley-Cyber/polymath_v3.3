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
import json
import logging
import re
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
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_STOPWORDS = {
    "about", "after", "again", "also", "because", "before", "being",
    "between", "could", "during", "every", "following", "from", "have",
    "here", "into", "like", "more", "most", "only", "other", "over",
    "same", "should", "some", "such", "than", "that", "their", "there",
    "these", "they", "this", "through", "using", "what", "when", "where",
    "which", "while", "with", "would", "your", "and", "the", "for",
}

# Fixed domain taxonomy — mirrors scripts/backfill_parent_domains_llm.py so
# ingest-time tags and any later backfill share one controlled vocabulary.
_DOMAIN_TAXONOMY = [
    "generative_ai", "machine_learning", "deep_learning", "nlp",
    "computer_vision", "software_engineering", "web_development",
    "data_engineering", "devops_cloud", "cybersecurity", "game_development",
    "creative_coding", "product_design", "ux_design", "psychology",
    "business_strategy", "research_methods", "mathematics", "other",
]

_SYSTEM = (
    "You are a precise document summarizer and classifier. First produce a "
    "factual, dense parent-level retrieval replacement summary that preserves key terms, proper nouns, and "
    "technical language; do not add information not present in the passage. Then "
    "classify the passage into exactly one domain from the taxonomy, name its "
    "semantic chunk type, and extract key terms and mechanisms.\nTaxonomy: " + ", ".join(_DOMAIN_TAXONOMY) + "\n"
    "__SEMANTIC_INSTRUCTION__"
)
from services.ingestion.summary_semantics import (  # noqa: E402
    SEMANTIC_SUMMARY_INSTRUCTION as _SEM_INSTR,
    canonical_parent_summary_fields,
    parse_semantic_summary,
)
_SYSTEM = _SYSTEM.replace("__SEMANTIC_INSTRUCTION__", _SEM_INSTR)
# NOTE (bridge retrieval B1, 2026-07-02): mechanisms extraction is done by
# scripts/backfill_mechanisms.py over existing parent SUMMARIES (independent of
# this live prompt, so the ingest pipeline is unchanged). Fold mechanisms into
# this _SYSTEM prompt + SummaryResult only after the backfill validates the
# vocabulary — see CONTINUITY/BRIDGE_RETRIEVAL_DESIGN.md.

_USER = (
    "Create a parent_summary.v1 compiler artifact for the following parent "
    "passage in {max_tokens} tokens or fewer, then classify it. Use only the "
    "parent passage. Do not invent IDs or source metadata. Do not summarize "
    "each child separately; summarize the parent as one coherent unit. Every "
    "key_point must cite supporting_child_ids from source_child_ids. Return "
    "only the JSON object.\n\nsource_child_ids: {source_child_ids}\n\n"
    "PARENT PASSAGE:\n{text}\n\nCHILD BOUNDARIES:\n{child_boundaries}"
)


@dataclass
class SummaryTask:
    parent_id: str
    doc_id: str
    corpus_id: str
    text: str
    source_tier: str  # tier_a | tier_b | tier_c | ocr_ast
    source_child_ids: list[str] | None = None
    child_boundaries: str = ""


@dataclass
class SummaryResult:
    parent_id: str
    doc_id: str
    corpus_id: str
    source_tier: str
    summary: str
    domain: str | None = None
    topics: list[str] | None = None  # DEPRECATED (retirement step 1: no longer written)
    semantic_chunk_type: str | None = None
    key_terms: list[str] | None = None
    mechanisms: list[str] | None = None
    schema_version: str | None = None
    summary_type: str | None = None
    central_claim: str | None = None
    key_points: list[dict] | None = None
    main_mechanism: str | None = None
    concept_tags: list[str] | None = None
    entity_hints: list[str] | None = None
    retrieval_uses: list[str] | None = None
    abstraction_level: str | None = None
    source_child_ids: list[str] | None = None
    summary_id: str | None = None
    source_hash: str | None = None
    summary_model: str | None = None
    summary_created_at: str | None = None
    validation_status: str | None = None
    repair_status: str | None = None
    quality_score: float | None = None
    quality_flags: list[str] | None = None
    retrieval_text: str | None = None


def _parse_summary_json(raw: str) -> tuple[str, str | None, list[str] | None]:
    """Lenient parse of Ghost A's JSON output ({summary, domain, topics}).

    Falls back to treating the whole string as the summary (domain/topics None)
    so a model that ignores the JSON instruction never breaks the summary
    pipeline — the summary tier keeps working, just untagged for that parent.
    """
    text = (raw or "").strip()
    if not text:
        return "", None, None
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            summary = str(obj.get("summary") or "").strip()
            if summary:
                domain = str(obj.get("domain") or "").strip().lower().replace(" ", "_")
                domain = domain if domain in _DOMAIN_TAXONOMY else ("other" if domain else None)
                topics = [
                    str(t).strip().lower()
                    for t in (obj.get("topics") or [])
                    if str(t).strip()
                ][:4]
                return summary, domain, (topics or None)
        except Exception:
            pass
    return text, None, None


def _extractive_fallback_summary(
    raw: str,
    max_summary_tokens: int,
) -> tuple[str, str, list[str]]:
    """Create a deterministic local summary when model generation fails.

    This is intentionally simple and dependency-free. It keeps parent-summary
    retrieval usable on fresh installs, during provider outages, or when a
    reasoning model returns empty assistant content. The fallback is not meant
    to be elegant prose; it is meant to preserve searchable evidence.
    """

    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return "", "other", []

    max_words = max(40, min(180, int(max_summary_tokens or 175)))
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    selected: list[str] = []
    word_count = 0
    for sentence in sentences[:8]:
        words = sentence.split()
        if not words:
            continue
        if selected and word_count + len(words) > max_words:
            break
        selected.append(sentence)
        word_count += len(words)
        if word_count >= max_words:
            break
    if not selected:
        selected = [" ".join(text.split()[:max_words])]

    summary = " ".join(selected).strip()
    if len(summary.split()) > max_words:
        summary = " ".join(summary.split()[:max_words]).rstrip(" ,;:") + "."

    tokens: dict[str, tuple[int, int]] = {}
    for idx, token in enumerate(_TOKEN_RE.findall(text.lower())):
        if token in _STOPWORDS or len(token) < 3:
            continue
        count, first_idx = tokens.get(token, (0, idx))
        tokens[token] = (count + 1, first_idx)
    topics = [
        token
        for token, (_count, _first_idx) in sorted(
            tokens.items(),
            key=lambda kv: (-kv[1][0], kv[1][1], kv[0]),
        )
        [:4]
    ]
    return summary, "other", topics


async def summarize_parents(
    tasks: list[SummaryTask],
    max_summary_tokens: int | None = None,
    *,
    pool: list[dict] | None = None,
    model: str | None = None,
    global_max_concurrent: int | None = None,
) -> list[SummaryResult]:
    """
    Summarize parent chunks in parallel, round-robining tasks across the
    `pool`. Each pool entry gets worker slots sized by max_concurrent. When
    ``global_max_concurrent`` is provided, the total active slots across all
    lanes are capped by that system budget.

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
                "model": model
                or getattr(settings, "GHOST_A_DEFAULT_MODEL", "")
                or settings.DEFAULT_COMPLETION_MODEL,
                "base_url": None,
                "api_key": None,
                "max_concurrent": settings.SUMMARY_MAX_CONCURRENT,
                "extra_params": {},
            }
        ]

    from services.ingestion.model_lifecycle import (
        ensure_model_lifecycle_ready,
        shutdown_model_lifecycle,
    )

    await ensure_model_lifecycle_ready(pool, purpose="ghost_a")

    cap_logged = False

    def _lane_slot_plan(*, log_cap: bool = False) -> list[int]:
        nonlocal cap_logged
        requested = [max(1, int(entry.get("max_concurrent") or 1)) for entry in pool]
        for idx in disabled_lanes:
            if 0 <= idx < len(requested):
                requested[idx] = 0
        if not global_max_concurrent:
            return requested
        cap_total = max(1, int(global_max_concurrent))
        requested_total = sum(requested)
        if requested_total <= cap_total:
            return requested

        plan = [0 for _ in requested]
        remaining = cap_total
        while remaining > 0 and any(plan[i] < requested[i] for i in range(len(requested))):
            for idx in range(len(requested)):
                if remaining <= 0:
                    break
                if plan[idx] < requested[idx]:
                    plan[idx] += 1
                    remaining -= 1
        if log_cap and not cap_logged:
            cap_logged = True
            logger.info(
                "GHOST A concurrency capped requested=%d active=%d lanes=%d",
                requested_total,
                sum(plan),
                len(pool),
            )
        return plan

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
                {"role": "user", "content": _USER.format(
                    max_tokens=cap,
                    text=task.text,
                    source_child_ids=json.dumps(task.source_child_ids or []),
                    child_boundaries=task.child_boundaries or "",
                )},
            ],
            "temperature": 0,
            "max_tokens": cap,
        }
        if entry.get("base_url"):
            payload["api_base"] = entry["base_url"]
        if entry.get("api_key"):
            payload["api_key"] = entry["api_key"]
        # Same filter as ghost_b: internal chip flags (supports_json_schema,
        # managed_vllm, …) never reach providers, and response_format joins
        # model/messages as caller-owned (Groq 400s on unknown keys).
        from services.ingestion.extraction_contract import provider_payload_extras

        payload.update(provider_payload_extras(entry.get("extra_params")))
        # DeepSeek v4 models default to THINKING mode, which returns EMPTY
        # content at bounded max_tokens — 156/156 and 128/148 parent
        # summaries came back blank on real ingests (2026-07-04). Disabling
        # thinking measured 1.6-2.0s with valid §10.1 JSON via the same
        # litellm hop. Injected only for deepseek-v4* and only when the
        # chip's extra_params didn't already set it.
        _mdl = str(entry.get("model") or "").lower()
        if "v4-flash" in _mdl or "v4-pro" in _mdl or "deepseek-v4" in _mdl:
            payload.setdefault("thinking", {"type": "disabled"})
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{settings.LITELLM_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                _sem = parse_semantic_summary(
                    raw,
                    source_child_ids=task.source_child_ids or [],
                    source_text=task.text,
                )
                _artifact = canonical_parent_summary_fields(
                    _sem,
                    parent_id=task.parent_id,
                    doc_id=task.doc_id,
                    corpus_id=task.corpus_id,
                    source_text=task.text,
                    source_child_ids=task.source_child_ids or [],
                    summary_model=entry["model"],
                    repair_status=_sem.get("repair_status"),
                )
                summary = _artifact["summary"]
                _dom = _sem["domain"]
                domain = _dom if _dom in _DOMAIN_TAXONOMY else ("other" if _dom else None)
                topics = None  # retired
                if not summary or _artifact.get("validation_status") != "valid":
                    return None
                return SummaryResult(
                    parent_id=task.parent_id,
                    doc_id=task.doc_id,
                    corpus_id=task.corpus_id,
                    source_tier=task.source_tier,
                    summary=summary,
                    domain=domain,
                    topics=topics,
                    semantic_chunk_type=_sem["semantic_chunk_type"],
                    key_terms=_sem["key_terms"] or None,
                    mechanisms=_sem["mechanisms"] or None,
                    schema_version=_artifact["schema_version"],
                    summary_type=_artifact["summary_type"],
                    central_claim=_artifact["central_claim"],
                    key_points=_artifact["key_points"] or None,
                    main_mechanism=_artifact["main_mechanism"],
                    concept_tags=_artifact["concept_tags"] or None,
                    entity_hints=_artifact["entity_hints"] or None,
                    retrieval_uses=_artifact["retrieval_uses"] or None,
                    abstraction_level=_artifact["abstraction_level"],
                    source_child_ids=_artifact["source_child_ids"] or None,
                    summary_id=_artifact["summary_id"],
                    source_hash=_artifact["source_hash"],
                    summary_model=_artifact["summary_model"],
                    summary_created_at=_artifact["summary_created_at"],
                    validation_status=_artifact["validation_status"],
                    repair_status=_artifact["repair_status"],
                    quality_score=_artifact["quality_score"],
                    quality_flags=_artifact["quality_flags"],
                    retrieval_text=_artifact["retrieval_text"],
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
        lane_slots = _lane_slot_plan(log_cap=True)
        for pool_idx, entry in enumerate(pool):
            if pool_idx in disabled_lanes:
                continue
            slots = lane_slots[pool_idx] if pool_idx < len(lane_slots) else 0
            for _ in range(slots):
                workers.append(asyncio.create_task(_lane_worker(pool_idx)))
        if workers:
            await asyncio.gather(*workers, return_exceptions=False)

    def _enabled_lane_count() -> int:
        lane_slots = _lane_slot_plan()
        return sum(
            1
            for pool_idx in range(len(pool))
            if pool_idx not in disabled_lanes
            and pool_idx < len(lane_slots)
            and lane_slots[pool_idx] > 0
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

    try:
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
    finally:
        await shutdown_model_lifecycle(pool, purpose="ghost_a")

    if not task_queue.empty():
        _clear_pending_queue()

    missing_after_retries = _missing_tasks()
    if missing_after_retries:
        logger.warning(
            "GHOST A using extractive fallback for %d missing parent summaries",
            len(missing_after_retries),
        )
        for task in missing_after_retries:
            summary, domain, topics = _extractive_fallback_summary(task.text, cap)
            if not summary:
                continue
            _sem = parse_semantic_summary(
                summary,
                source_child_ids=task.source_child_ids or [],
                source_text=task.text,
            )
            _artifact = canonical_parent_summary_fields(
                _sem,
                parent_id=task.parent_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                source_text=task.text,
                source_child_ids=task.source_child_ids or [],
                summary_model="extractive_fallback",
                repair_status="regenerated",
            )
            if not _artifact["summary"] or _artifact["validation_status"] != "valid":
                continue
            results_by_parent_id[task.parent_id] = SummaryResult(
                parent_id=task.parent_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                source_tier=task.source_tier,
                summary=_artifact["summary"],
                domain=domain,
                topics=topics,
                schema_version=_artifact["schema_version"],
                summary_type=_artifact["summary_type"],
                central_claim=_artifact["central_claim"],
                key_points=_artifact["key_points"] or None,
                main_mechanism=_artifact["main_mechanism"],
                concept_tags=(_artifact["concept_tags"] or topics or None),
                entity_hints=_artifact["entity_hints"] or None,
                retrieval_uses=_artifact["retrieval_uses"] or None,
                abstraction_level=_artifact["abstraction_level"],
                source_child_ids=_artifact["source_child_ids"] or None,
                summary_id=_artifact["summary_id"],
                source_hash=_artifact["source_hash"],
                summary_model=_artifact["summary_model"],
                summary_created_at=_artifact["summary_created_at"],
                validation_status=_artifact["validation_status"],
                repair_status=_artifact["repair_status"],
                quality_score=_artifact["quality_score"],
                quality_flags=_artifact["quality_flags"],
                retrieval_text=_artifact["retrieval_text"],
            )

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
