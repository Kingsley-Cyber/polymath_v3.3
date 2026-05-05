"""Mistral batch dispatch for Ghost A summaries and Ghost B extractions.

Sits between the worker and `services.mistral_batch.MistralBatchClient`.
Builds per-task .jsonl bodies using the same prompt builders the sync path
uses, submits one batch job per phase, polls until terminal, parses the
output file, and returns SummaryResult / ExtractionResult lists in the same
shape the sync path returns. Worker code that consumes the result lists
doesn't need to know which path was used.

Trade-off: batch is async (minutes-to-hours wall clock) but cheap (50% of
sync price) and high-throughput (Mistral's fleet parallelism dwarfs what
a single sync client can sustain). Activates only when the corpus toggles
extraction_batch_mode / summary_batch_mode to "mistral" AND a Mistral lane
is present in the relevant pool.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from config import get_settings
from services.ghost_a import (
    _SYSTEM as _GHOST_A_SYSTEM,
    _USER as _GHOST_A_USER_TEMPLATE,
    SummaryResult,
    SummaryTask,
)
from services.ghost_b import (
    ExtractionResult,
    ExtractionTask,
    SchemaContext,
    SchemaLens,
    _compact_system_prompt,
    _compact_user_prompt,
    _json_schema_response_format,
    _parse,
    build_target_extraction_json_schema,
)
from services.mistral_batch import (
    BatchJobStatus,
    MistralBatchClient,
    build_chat_jsonl,
)

logger = logging.getLogger(__name__)


class MistralBatchUnavailable(RuntimeError):
    """No Mistral lane configured / api_key missing — caller falls back to sync."""


def resolve_mistral_lane(pool: list[dict] | None) -> dict | None:
    """Find the first usable Mistral lane in a worker pool dict list.

    A "Mistral lane" is one whose provider_preset is "mistral" OR whose
    base_url points at api.mistral.ai. Returns None if no such lane exists
    or if the candidate has no api_key.

    The worker's _build_ghost_pool decrypts api_keys ahead of time, so the
    api_key here is plaintext and ready to drop into the Authorization
    header.
    """
    for entry in pool or []:
        if not isinstance(entry, dict):
            continue
        preset = (entry.get("provider_preset") or "").lower()
        base_url = (entry.get("base_url") or "").lower()
        if preset == "mistral" or "api.mistral.ai" in base_url:
            if not entry.get("api_key"):
                logger.warning(
                    "Mistral batch: lane found but api_key empty — falling back to sync"
                )
                continue
            return entry
    return None


def _bare_model(litellm_model: str) -> str:
    """Strip the LiteLLM provider prefix the chip composer added.

    Mistral's batch API expects native model names (`mistral-small-latest`),
    not LiteLLM-prefixed ones (`mistral/mistral-small-latest`).
    """
    return litellm_model.split("/", 1)[1] if "/" in litellm_model else litellm_model


def _on_tick_logger(phase_label: str, doc_id: str, corpus_id: str):
    """Closure that emits a structured progress log line on each poll tick."""
    last_pct: dict[str, int] = {"v": -1}

    def _tick(status: BatchJobStatus) -> None:
        pct = (
            int(status.completed_requests * 100 / max(status.total_requests, 1))
            if status.total_requests
            else 0
        )
        # Only log on >= 5pp progress jumps to keep noise down.
        if pct - last_pct["v"] < 5 and status.status == "RUNNING":
            return
        last_pct["v"] = pct
        logger.info(
            "phase=%s_batch_tick doc=%s corpus=%s status=%s "
            "completed=%d/%d succeeded=%d failed=%d",
            phase_label,
            doc_id[:12],
            corpus_id[:8],
            status.status,
            status.completed_requests,
            status.total_requests,
            status.succeeded_requests,
            status.failed_requests,
        )

    return _tick


# ── Ghost A — Summary batch ───────────────────────────────────────────


async def run_summary_via_mistral_batch(
    tasks: list[SummaryTask],
    *,
    lane: dict,
    max_summary_tokens: int,
    doc_id: str,
    corpus_id: str,
) -> list[SummaryResult]:
    """Submit Ghost A summary tasks as a single Mistral batch job.

    Returns one SummaryResult per task. Tasks whose Mistral row carried an
    error get status="skipped" + skip_reason="batch_error_<code>" so the
    existing partial-warning + parent_chunks[].summary_status logic kicks
    in unchanged.
    """
    if not tasks:
        return []
    settings = get_settings()
    model = _bare_model(str(lane["model"]))
    started = time.monotonic()

    # Build per-task bodies in the same shape the sync path uses, minus the
    # `model` field (Mistral batch sets it at job creation).
    rows: list[dict[str, Any]] = []
    by_id: dict[str, SummaryTask] = {}
    for t in tasks:
        body = {
            "custom_id": t.parent_id,
            "messages": [
                {"role": "system", "content": _GHOST_A_SYSTEM},
                {
                    "role": "user",
                    "content": _GHOST_A_USER_TEMPLATE.format(
                        max_tokens=max_summary_tokens, text=t.text
                    ),
                },
            ],
            "max_tokens": max_summary_tokens,
            "temperature": 0,
        }
        rows.append(body)
        by_id[t.parent_id] = t

    jsonl = build_chat_jsonl(rows)
    deadline_seconds = float(settings.MISTRAL_BATCH_DEADLINE_HOURS) * 3600

    async with MistralBatchClient(
        api_key=str(lane["api_key"]),
        base_url=str(lane.get("base_url") or settings.MISTRAL_BATCH_BASE_URL),
    ) as client:
        file_id = await client.upload(jsonl, filename=f"ghost_a_{doc_id[:12]}.jsonl")
        submit = await client.submit(
            input_file_ids=[file_id],
            model=model,
            endpoint="/v1/chat/completions",
            metadata={"phase": "ghost_a", "doc_id": doc_id, "corpus_id": corpus_id},
        )
        logger.info(
            "phase=ghost_a_batch_submit doc=%s corpus=%s job_id=%s tasks=%d model=%s",
            doc_id[:12], corpus_id[:8], submit.job_id, len(tasks), model,
        )

        final = await client.wait_until_done(
            submit.job_id,
            poll_seconds=int(settings.MISTRAL_BATCH_POLL_SECONDS),
            deadline_seconds=deadline_seconds,
            on_tick=_on_tick_logger("ghost_a", doc_id, corpus_id),
        )
        duration = time.monotonic() - started
        logger.info(
            "phase=ghost_a_batch_done doc=%s corpus=%s job_id=%s status=%s "
            "succeeded=%d failed=%d duration=%.1fs",
            doc_id[:12], corpus_id[:8], submit.job_id, final.status,
            final.succeeded_requests, final.failed_requests, duration,
        )

        results: list[SummaryResult] = []
        seen_ids: set[str] = set()

        if final.is_done and final.output_file:
            async for row in client.iter_output_lines(final.output_file):
                custom_id = row.get("custom_id")
                if not custom_id or custom_id not in by_id:
                    continue
                seen_ids.add(custom_id)
                t = by_id[custom_id]
                if row.get("error"):
                    err_code = (row["error"] or {}).get("code") or "unknown"
                    results.append(SummaryResult(
                        parent_id=t.parent_id, doc_id=t.doc_id, corpus_id=t.corpus_id,
                        source_tier=t.source_tier, summary="",
                        status="skipped", skip_reason=f"batch_error_{err_code}",
                    ))
                    continue
                try:
                    body = ((row.get("response") or {}).get("body") or {})
                    text = body["choices"][0]["message"]["content"].strip()
                except Exception:
                    results.append(SummaryResult(
                        parent_id=t.parent_id, doc_id=t.doc_id, corpus_id=t.corpus_id,
                        source_tier=t.source_tier, summary="",
                        status="skipped", skip_reason="batch_response_malformed",
                    ))
                    continue
                results.append(SummaryResult(
                    parent_id=t.parent_id, doc_id=t.doc_id, corpus_id=t.corpus_id,
                    source_tier=t.source_tier, summary=text, status="ok",
                ))

        # Any task missing from the output (timeout / cancelled / unparsed) is
        # an attempted-but-skipped — same contract the sync path uses so the
        # worker's resume-gate doesn't retry these on the next ingest.
        skip_reason = (
            "batch_terminal_" + final.status.lower()
            if not final.is_done
            else "batch_missing_output_row"
        )
        for t in tasks:
            if t.parent_id not in seen_ids:
                results.append(SummaryResult(
                    parent_id=t.parent_id, doc_id=t.doc_id, corpus_id=t.corpus_id,
                    source_tier=t.source_tier, summary="",
                    status="skipped", skip_reason=skip_reason,
                ))
        return results


# ── Ghost B — Extraction batch ────────────────────────────────────────


async def run_extraction_via_mistral_batch(
    tasks: list[ExtractionTask],
    *,
    lane: dict,
    schema: SchemaContext | None,
    schema_lens: SchemaLens | None,
    max_entities: int,
    max_relations: int,
    max_completion_tokens: int,
    extraction_mode: str,
    doc_id: str,
    corpus_id: str,
) -> list[ExtractionResult]:
    """Submit Ghost B extraction tasks as a single Mistral batch job.

    Each task gets its own JSON Schema response_format so vllm-style
    structured-output decoding works. Mistral's API enforces the schema
    server-side. Failed rows are skipped — the worker's existing
    partial-warning path turns this into a graph_partial state with the
    repair queue picking up the misses on the next ingest.
    """
    if not tasks:
        return []
    settings = get_settings()
    model = _bare_model(str(lane["model"]))
    started = time.monotonic()

    schema_ctx = schema or SchemaContext()
    eff_entity = (
        schema_ctx.entity_vocab if schema_ctx and schema_ctx.has_entity_schema else None
    )
    eff_relation = (
        schema_ctx.relation_vocab if schema_ctx and schema_ctx.has_relation_schema else None
    )

    rows: list[dict[str, Any]] = []
    by_id: dict[str, ExtractionTask] = {}
    for task in tasks:
        target_schema = build_target_extraction_json_schema(
            chunk_id=task.chunk_id,
            doc_id=task.doc_id,
            corpus_id=task.corpus_id,
            entity_vocab=eff_entity,
            relation_vocab=eff_relation,
            max_entities=max_entities,
            max_relations=max_relations,
        )
        body = {
            "custom_id": task.chunk_id,
            "messages": [
                {"role": "system", "content": _compact_system_prompt(recovery_mode=False)},
                {
                    "role": "user",
                    "content": _compact_user_prompt(
                        task=task,
                        max_entities=max_entities,
                        max_relations=max_relations,
                        compact_mode=extraction_mode == "compact",
                        schema_lens=schema_lens,
                    ),
                },
            ],
            "max_tokens": max_completion_tokens,
            "temperature": 0,
            "response_format": _json_schema_response_format(target_schema),
        }
        rows.append(body)
        by_id[task.chunk_id] = task

    jsonl = build_chat_jsonl(rows)
    deadline_seconds = float(settings.MISTRAL_BATCH_DEADLINE_HOURS) * 3600

    async with MistralBatchClient(
        api_key=str(lane["api_key"]),
        base_url=str(lane.get("base_url") or settings.MISTRAL_BATCH_BASE_URL),
    ) as client:
        file_id = await client.upload(jsonl, filename=f"ghost_b_{doc_id[:12]}.jsonl")
        submit = await client.submit(
            input_file_ids=[file_id],
            model=model,
            endpoint="/v1/chat/completions",
            metadata={"phase": "ghost_b", "doc_id": doc_id, "corpus_id": corpus_id},
        )
        logger.info(
            "phase=ghost_b_batch_submit doc=%s corpus=%s job_id=%s tasks=%d model=%s",
            doc_id[:12], corpus_id[:8], submit.job_id, len(tasks), model,
        )

        final = await client.wait_until_done(
            submit.job_id,
            poll_seconds=int(settings.MISTRAL_BATCH_POLL_SECONDS),
            deadline_seconds=deadline_seconds,
            on_tick=_on_tick_logger("ghost_b", doc_id, corpus_id),
        )
        duration = time.monotonic() - started
        logger.info(
            "phase=ghost_b_batch_done doc=%s corpus=%s job_id=%s status=%s "
            "succeeded=%d failed=%d duration=%.1fs",
            doc_id[:12], corpus_id[:8], submit.job_id, final.status,
            final.succeeded_requests, final.failed_requests, duration,
        )

        results: list[ExtractionResult] = []
        if final.is_done and final.output_file:
            async for row in client.iter_output_lines(final.output_file):
                custom_id = row.get("custom_id")
                if not custom_id or custom_id not in by_id:
                    continue
                if row.get("error"):
                    # Per-line error — leave for the repair queue. Worker's
                    # partial logic surfaces the gap as a graph_partial state.
                    continue
                try:
                    body = ((row.get("response") or {}).get("body") or {})
                    raw = body["choices"][0]["message"]["content"]
                except Exception as exc:
                    logger.warning(
                        "Mistral batch row malformed for chunk=%s: %s", custom_id, exc
                    )
                    continue
                task = by_id[custom_id]
                result = _parse(
                    raw,
                    task=task,
                    threshold=settings.ENTITY_CONFIDENCE_THRESHOLD,
                    schema=schema_ctx,
                )
                if result is None:
                    continue
                results.append(result)
        return results
