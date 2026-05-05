"""Cross-document Mistral batch dispatcher (pre-flight).

Aggregates Ghost B extraction tasks from MANY documents into a single
Mistral batch job, then stages results into each document's
`ghost_b_staging` field so the existing locked pipeline (worker.py) can
finalize each doc via the standard skip-on-resume path (GOTCHA #70).

Design summary
==============
Today the worker processes one document at a time:

    parse → chunk → [ghost_a ∥ ghost_b] → mongo → embed → qdrant → neo4j

If `extraction_batch_mode="mistral"` the per-doc Ghost B call goes to
Mistral Batch. Each doc submits its own ~30-50 row batch — far too small
to amortize Mistral's 5-15 minute queue floor.

Pre-flight changes the dispatch order for batches that opt in:

    PHASE 1 (this module):
      for each doc in batch:
        parse + chunk → in-memory parents/children
        run Ghost A (sync, cheap, ~5-10 calls per doc)
        collect Ghost B tasks into one shared list
      submit ONE Mistral batch job (15K-25K rows for a 500-file corpus)
      poll until done
      for each doc:
        stage parents/children/summaries/ghost_b_staging via _write_mongo_all
        flip mongo_written = True

    PHASE 2 (existing worker, unchanged):
      worker pool consumes each doc:
        parse + chunk runs again (idempotent, same content_hash → same chunks)
        Ghost A resume gate hits — summaries already in mongo, skip
        Ghost B resume gate hits — staging already in mongo, skip (#70)
        embed + qdrant + neo4j run as normal

The worker requires zero changes — the resume gates that already exist
for Ghost A (#69) and Ghost B (#70) make the staging path transparent.

Wiring (not yet done; see preflight_dispatch.md):
  • batch_queue.create_batch() accepts extraction_dispatch="preflight"
  • batch_queue._run_batch() runs run_preflight_extraction_batch() before
    spawning the per-doc worker pool
  • frontend exposes the toggle on multi-file batch uploads

This file ships the orchestrator only; the queue wiring is intentionally
deferred so reviewers can sign off on the seam before the pipeline state
machine is touched.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

from config import get_settings
from models.schemas import IngestionConfig, SourceTier, WriteState
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient
from services.ghost_a import SummaryTask, summarize_parents
from services.ghost_b import (
    ExtractionResult,
    ExtractionTask,
    SchemaContext,
)
from services.ingestion import docling_adapter, tier_chunker
from services.ingestion.mistral_batch_runner import (
    resolve_mistral_lane,
    run_extraction_via_mistral_batch,
)

logger = logging.getLogger(__name__)


class PreflightUnavailable(RuntimeError):
    """Raised when pre-flight cannot run (no Mistral lane, missing inputs)."""


async def run_preflight_extraction_batch(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    docs: list[dict[str, Any]],
    corpus_config: IngestionConfig,
    pool: list[dict[str, Any]],
    schema_ctx: SchemaContext | None,
    model: str,
) -> dict[str, list[ExtractionResult]]:
    """Run Ghost B extraction across many documents in one Mistral batch.

    Args:
        docs: list of {doc_id, corpus_id, user_id, file_id, filename,
              source_mime, content_bytes}. Caller is responsible for
              dedup / content-hash assignment of doc_ids ahead of time
              (same idempotency rules as the worker).
        corpus_config: effective ingestion config (build_effective_config)
        pool: pre-built ghost pool with decrypted api_keys
        schema_ctx: optional SchemaContext for vocab constraints
        model: fallback model name for non-batch lanes (unused here)

    Returns:
        Per-doc extraction results: {doc_id: [ExtractionResult, ...]}.
        Caller writes these into mongo via _write_mongo_all so the worker
        sees ghost_b_staging on resume.

    Raises:
        PreflightUnavailable: no Mistral lane in pool or corpus toggle off.
    """
    if (corpus_config.extraction_batch_mode or "off") != "mistral":
        raise PreflightUnavailable(
            "extraction_batch_mode is not 'mistral' — pre-flight requires "
            "the corpus toggle. Use per-doc dispatch instead."
        )
    lane = resolve_mistral_lane(pool)
    if lane is None:
        raise PreflightUnavailable(
            "No Mistral lane found in extraction pool. Add a Mistral chip "
            "with provider_preset='mistral' and an api_key."
        )

    settings = get_settings()
    started = time.monotonic()
    batch_size = len(docs)
    if batch_size == 0:
        return {}

    # ── PHASE 1A — parse + chunk every doc, collect tasks ────────────────
    all_tasks: list[ExtractionTask] = []
    doc_chunk_map: dict[str, list[str]] = {}  # doc_id → ordered chunk_ids
    parse_failures: list[str] = []

    for d in docs:
        doc_id = d["doc_id"]
        corpus_id = d["corpus_id"]
        try:
            parse_result = await docling_adapter.parse_document(
                d["content_bytes"],
                d["filename"],
                d.get("source_mime") or "application/octet-stream",
                do_ocr=False,
            )
            _parents, children, _injected = tier_chunker.chunk(
                parse_result, doc_id=doc_id, corpus_id=corpus_id
            )
        except Exception as exc:
            logger.exception(
                "phase=preflight_parse_failed doc=%s err=%s", doc_id[:12], exc
            )
            parse_failures.append(doc_id)
            continue

        chunk_ids: list[str] = []
        for c in children:
            task = ExtractionTask(
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                corpus_id=c.corpus_id,
                text=c.text,
                source_tier=c.source_tier,
                heading_path=getattr(c, "heading_path", None),
                chunk_kind=getattr(c, "chunk_kind", None),
            )
            all_tasks.append(task)
            chunk_ids.append(c.chunk_id)
        doc_chunk_map[doc_id] = chunk_ids

    if not all_tasks:
        logger.warning(
            "phase=preflight_empty docs=%d parse_failures=%d — nothing to batch",
            batch_size, len(parse_failures),
        )
        return {}

    logger.info(
        "phase=preflight_collected docs=%d parse_failures=%d total_tasks=%d "
        "duration=%.1fs",
        batch_size, len(parse_failures), len(all_tasks),
        time.monotonic() - started,
    )

    # ── PHASE 1B — submit ONE Mistral batch covering every chunk ─────────
    # Use the corpus's extraction policy values. We pull max_entities /
    # max_relations / max_completion_tokens from settings since
    # GhostBExtractionPolicy is per-doc; for cross-doc batching we use
    # corpus-wide defaults.
    results = await run_extraction_via_mistral_batch(
        all_tasks,
        lane=lane,
        schema=schema_ctx,
        schema_lens=None,  # cross-doc lens is meaningless; per-doc lens
                           # would require a much larger refactor
        max_entities=int(settings.GHOST_B_MAX_ENTITIES_PER_CHUNK),
        max_relations=int(settings.GHOST_B_MAX_RELATIONS_PER_CHUNK),
        max_completion_tokens=int(settings.EXTRACTION_MAX_TOKENS),
        extraction_mode="full",
        doc_id=f"preflight_batch_{batch_size}docs",
        corpus_id=docs[0]["corpus_id"],
    )

    # ── PHASE 1C — group results back per-doc ────────────────────────────
    by_chunk: dict[str, ExtractionResult] = {r.chunk_id: r for r in results}
    per_doc: dict[str, list[ExtractionResult]] = {}
    for doc_id, chunk_ids in doc_chunk_map.items():
        per_doc[doc_id] = [
            by_chunk[c] for c in chunk_ids if c in by_chunk
        ]

    duration = time.monotonic() - started
    logger.info(
        "phase=preflight_done docs=%d total_tasks=%d returned=%d duration=%.1fs",
        batch_size, len(all_tasks), len(results), duration,
    )
    return per_doc
