"""
Ingestion pipeline worker — staged production pipeline:

  1. Parse     → docling_adapter.parse_document
  2. Chunk     → tier_chunker.chunk (parents + children)
  3. Ghost A   → parent summaries under the model-phase semaphore.
                 Required summaries still run before Mongo because they are
                 embedded into parent chunks.
  4. Mongo     → ONE write pass: documents (summaries INLINE on parent_chunks)
                 + chunks. Flip mongo_written.
  5. Embed     → one embed_batch call over children+summary texts.
                 mode / dim / model-id come from ingestion_config.
  6. Qdrant    → children → naive / hrag (tier-filtered) / graph,
                 summaries → naive + hrag only. Flip qdrant_written and
                 vector_ready.
  7. Ghost B   → async-style graph enrichment lane after vector readiness.
  8. Neo4j     → write_document_graph incrementally. Flip neo4j_written.
                 Skipped entirely when use_neo4j=False.

Ghost A failure is a hard abort because parent summaries feed retrieval.
Ghost B failure/partial extraction is a graph-status warning after vector_ready:
Mongo/Qdrant stay usable, Neo4j keeps full chunk coverage when it can write, and
failed graph chunks are recoverable through backfill.
Resume logic (Decision D) reuses existing Mongo summaries and probes Neo4j for
MENTIONS so we never pay the LLM twice for work already persisted.
"""

import asyncio
import hashlib
import inspect
import logging
import mimetypes
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Any, Callable

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse, SourceTier, WriteState
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient
from services.embedder import embed_batch
from services.ghost_a import SummaryResult, SummaryTask, summarize_parents
from services.ghost_b import (
    CandidateFactItem,
    EntityItem,
    ExtractionBatchReport,
    ExtractionFailureItem,
    ExtractionResult,
    ExtractionTask,
    RelationItem,
    RelationRepairCandidate,
    SchemaContext,
    extract_entities,
)
from services.ingestion.cancellation import IngestCancelled
from services.ingestion import docling_adapter, repair_queue, tier_chunker
from services.ingestion.schema_lens import get_or_create_schema_lens
from services.ingestion.section_classifier import ChunkKind, should_skip_ghost_b
from services.secrets import decrypt as _decrypt_api_key
from services.storage import mongo_reader, mongo_writer, qdrant_writer
from services.storage.qdrant_writer import retrieve_schema_for_chunk

logger = logging.getLogger(__name__)
settings = get_settings()
_PARSE_SEMAPHORE = asyncio.Semaphore(max(1, settings.INGEST_MAX_PARSE_JOBS))
_LOCAL_MODEL_PHASE_SEMAPHORE = asyncio.Semaphore(
    max(1, settings.INGEST_MAX_MODEL_PHASE_DOCS)
)
_CLOUD_MODEL_PHASE_SEMAPHORE = asyncio.Semaphore(
    max(1, settings.INGEST_MAX_CLOUD_MODEL_PHASE_DOCS)
)
_GRAPH_MODEL_PHASE_SEMAPHORE = asyncio.Semaphore(
    max(1, settings.INGEST_MAX_GRAPH_MODEL_PHASE_DOCS)
)
_AUTO_BACKFILL_SEMAPHORE = asyncio.Semaphore(1)
GRAPH_PENDING = "graph_pending"
GRAPH_EXTRACTING = "graph_extracting"
GRAPH_PARTIAL = "graph_partial"
GRAPH_READY = "graph_ready"
GRAPH_NEEDS_BACKFILL = "needs_backfill"
GRAPH_RETRY_SCHEDULED = "graph_retry_scheduled"
GRAPH_SKIPPED = "graph_skipped"
GRAPH_FAILED_TOKEN_BUDGET = "graph_failed_token_budget"


def _canonical_embed_mode(value: object) -> str:
    aliases = {
        "local_st": "local",
        "modal_tei": "modal",
        "siliconflow": "api",
    }
    key = str(value or "local")
    return aliases.get(key, key)


def _pre_vector_model_semaphore(config: IngestionConfig) -> asyncio.Semaphore:
    """Route local and cloud pre-vector model phases through separate caps."""
    if _canonical_embed_mode(getattr(config, "embed_mode", "local")) == "local":
        return _LOCAL_MODEL_PHASE_SEMAPHORE
    return _CLOUD_MODEL_PHASE_SEMAPHORE


async def _raise_if_cancelled(cancel_check, *, doc_id: str | None = None) -> None:
    if cancel_check is None:
        return
    result = cancel_check()
    if inspect.isawaitable(result):
        result = await result
    if result:
        suffix = f" for doc {doc_id[:12]}" if doc_id else ""
        raise IngestCancelled(f"Ingest cancelled{suffix}")


class GhostAFailure(RuntimeError):
    """Ghost A produced fewer results than tasks — abort the document."""


class GhostBFailure(RuntimeError):
    """Ghost B failed catastrophically before returning usable extraction."""


@dataclass
class GhostRunResult:
    """Result envelope for the Ghost A/Ghost B model phase.

    Iteration intentionally yields only `(summaries, ghost_b_out)` to preserve
    older tests and callers that unpacked the pre-metrics two-tuple. New code
    should read the named attributes for warnings, failures, and metrics.
    """

    summaries: list[SummaryResult] | None
    ghost_b_out: list[ExtractionResult] | None
    warnings: list[str] = field(default_factory=list)
    ghost_b_failures: list[ExtractionFailureItem] = field(default_factory=list)
    ghost_b_metrics: dict | None = None
    relation_repair_candidates: list[RelationRepairCandidate] = field(
        default_factory=list
    )

    def __iter__(self):
        yield self.summaries
        yield self.ghost_b_out


_HRAG_TIERS = (
    SourceTier.tier_a.value,
    SourceTier.tier_b.value,
    SourceTier.tier_b_plus.value,
)
_DUPLICATE_DOC_THRESHOLD = 0.90
_DUPLICATE_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_'-]{2,}")
_DUPLICATE_STOP_WORDS = {
    "and",
    "are",
    "but",
    "for",
    "from",
    "have",
    "into",
    "not",
    "the",
    "that",
    "this",
    "with",
    "you",
    "your",
    "their",
    "there",
    "then",
    "than",
    "was",
    "were",
    "will",
    "would",
    "could",
    "should",
    "about",
    "which",
}


def _normalize_document_text_for_id(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _chunk_parse_result(
    *,
    parse_result: docling_adapter.DoclingParseResult,
    doc_id: str,
    corpus_id: str,
    config: IngestionConfig,
):
    return tier_chunker.chunk(
        parse_result=parse_result,
        doc_id=doc_id,
        corpus_id=corpus_id,
        config=config,
    )


def _is_vectorized_child(chunk) -> bool:
    kind = getattr(chunk, "chunk_kind", None) or ChunkKind.BODY
    return not should_skip_ghost_b(kind)


def _merge_warnings(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Append warnings once while preserving their original order."""
    merged: list[str] = []
    for warning in [*(existing or []), *(new or [])]:
        text = str(warning or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _graph_enabled(config: IngestionConfig) -> bool:
    return bool(config.use_neo4j and settings.NEO4J_ENABLED)


def _graph_counts(
    *,
    ghost_b_out: list[ExtractionResult] | None = None,
    ghost_b_failures: list[ExtractionFailureItem] | None = None,
    ghost_b_metrics: dict | None = None,
) -> tuple[int, int]:
    metrics = ghost_b_metrics or {}
    extracted = int(metrics.get("extracted_chunks") or len(ghost_b_out or []))
    failed = int(
        metrics.get("failed_chunk_count")
        or metrics.get("failed_chunks")
        or len(ghost_b_failures or [])
    )
    return extracted, failed


def _graph_status_after_extraction(
    *,
    ghost_b_out: list[ExtractionResult] | None,
    ghost_b_failures: list[ExtractionFailureItem] | None,
    ghost_b_metrics: dict | None,
) -> str:
    metrics = ghost_b_metrics or {}
    extracted, failed = _graph_counts(
        ghost_b_out=ghost_b_out,
        ghost_b_failures=ghost_b_failures,
        ghost_b_metrics=metrics,
    )
    requested = int(metrics.get("requested_chunks") or (extracted + failed))
    if failed:
        error_counts = metrics.get("error_counts") or {}
        deterministic_failures = sum(
            int(error_counts.get(key) or 0)
            for key in ("token_budget", "bad_request", "unprocessable_entity")
        )
        if deterministic_failures and not int(
            metrics.get("retryable_failed_chunks") or 0
        ):
            return GRAPH_FAILED_TOKEN_BUDGET
        if metrics.get("graph_retry_after"):
            return GRAPH_RETRY_SCHEDULED
        return GRAPH_PARTIAL
    if int(
        metrics.get("relation_repair_pending_count")
        or metrics.get("relation_repair_queued_count")
        or 0
    ):
        return GRAPH_PARTIAL
    if requested and extracted < requested:
        return GRAPH_PARTIAL
    return GRAPH_READY


def _refresh_derived_write_state(
    ws: WriteState, *, config: IngestionConfig
) -> WriteState:
    """Fill new staged-ingest status fields for legacy documents."""
    ws.vector_ready = bool(ws.vector_ready or (ws.mongo_written and ws.qdrant_written))
    if not _graph_enabled(config):
        ws.graph_status = ws.graph_status or GRAPH_SKIPPED
        ws.graph_completeness = ws.graph_completeness or "graph-skipped"
    elif ws.neo4j_written:
        ws.graph_status = ws.graph_status or GRAPH_READY
        ws.graph_completeness = ws.graph_completeness or "graph-complete"
    elif ws.mongo_written and ws.qdrant_written:
        # A crashed extraction can leave an old "extracting" marker. On resume,
        # make it pending so the worker can safely pick the graph lane back up.
        if ws.graph_status in (None, GRAPH_EXTRACTING):
            ws.graph_status = GRAPH_PENDING
    else:
        ws.graph_status = ws.graph_status or GRAPH_PENDING
    return ws


def _chunk_kind_counts(chunks: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        kind = str(getattr(chunk, "chunk_kind", None) or ChunkKind.BODY)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _low_value_chunk_summary(chunks: list) -> tuple[int, dict[str, int]]:
    skipped: dict[str, int] = {}
    body_count = 0
    for chunk in chunks:
        kind = str(getattr(chunk, "chunk_kind", None) or ChunkKind.BODY)
        if should_skip_ghost_b(kind):
            skipped[kind] = skipped.get(kind, 0) + 1
        else:
            body_count += 1
    return body_count, skipped


def _file_profile(parse_result, source_mime: str, filename: str) -> str:
    source_format = str(getattr(parse_result, "source_format", "") or "")
    mime = (source_mime or "").lower()
    name = (filename or "").lower()
    if "pdf" in mime or name.endswith(".pdf") or source_format == "pypdf_fast_text":
        if source_format == "pypdf_fast_text":
            return "digital_pdf"
        return "pdf"
    if source_format.startswith("local_markdown") or name.endswith(
        (".md", ".markdown")
    ):
        return "markdown"
    if source_format.startswith("local_text") or name.endswith((".txt", ".text")):
        return "plain_text"
    if source_format.startswith("local_html") or name.endswith((".html", ".htm")):
        return "html"
    return source_format or "document"


def _structure_quality(parse_result) -> str:
    if getattr(parse_result, "has_structure", False):
        heading_count = int(getattr(parse_result, "h1_count", 0) or 0) + int(
            getattr(parse_result, "h2_count", 0) or 0
        )
        if heading_count >= 3 or getattr(parse_result, "sections", None):
            return "high"
        return "medium"
    if getattr(parse_result, "pages", None):
        return "page_text"
    return "low"


def _trace_reasons(
    *,
    parse_result,
    chunking_config: dict,
    policy: "GhostBExtractionPolicy | None",
    low_value_count: int,
    config: IngestionConfig,
    ws: WriteState,
) -> list[str]:
    reasons: list[str] = []
    parent_strategy = str(chunking_config.get("parent_strategy") or "")
    if parent_strategy == "pdf_page_grouped":
        reasons.append(
            "PDF pages were grouped into token-sized parents with page ranges preserved."
        )
    elif parent_strategy.startswith("heading_bound"):
        reasons.append("Document headings were preserved as parent chunk boundaries.")
    elif parent_strategy == "token_window":
        reasons.append(
            "Weak document structure triggered token-window parent chunking."
        )
    if chunking_config.get("requested_child_strategy") != chunking_config.get(
        "child_strategy"
    ):
        reasons.append(
            "Requested child splitting was resolved to the safest implemented strategy."
        )
    if low_value_count:
        reasons.append(
            f"{low_value_count} low-value chunk(s) were skipped for graph extraction."
        )
    if not config.use_neo4j:
        reasons.append("Graph extraction is disabled for this ingest configuration.")
    else:
        if policy is not None and policy.extraction_strategy == "compact_large_doc":
            reasons.append("Large body chunk count triggered compact graph extraction.")
        elif policy is not None and policy.extraction_strategy == "full_ontology":
            reasons.append(
                "Document stayed within the full ontology extraction budget."
            )
    if ws.vector_ready:
        reasons.append("Mongo and Qdrant are ready for vector/chat retrieval.")
    if not reasons:
        reasons.append("Default auto ingestion policy was applied.")
    return reasons


def _build_decision_trace(
    *,
    parse_result,
    source_mime: str,
    filename: str,
    source_tier: SourceTier,
    chunking_config: dict,
    parents: list,
    children: list,
    config: IngestionConfig,
    ws: WriteState,
    ghost_b_metrics: dict | None = None,
) -> dict[str, Any]:
    body_children, low_value_kinds = _low_value_chunk_summary(children)
    policy: GhostBExtractionPolicy | None = None
    if config.use_neo4j and settings.NEO4J_ENABLED:
        policy = _select_ghost_b_extraction_policy(
            config,
            total_children=len(children),
            body_children=body_children,
            skipped_low_value_by_kind=low_value_kinds,
        )
    budgets = chunking_config.get("token_budgets") or {}
    metrics = ghost_b_metrics or {}
    graph_strategy = str(metrics.get("extraction_strategy") or "") or (
        policy.extraction_strategy if policy else "graph_disabled"
    )
    graph_mode = str(metrics.get("extraction_mode") or "") or (
        policy.extraction_mode if policy else "none"
    )
    graph_completeness = str(metrics.get("graph_completeness") or "") or (
        policy.graph_completeness if policy else "graph-skipped"
    )
    graph_engine = str(
        metrics.get("graph_extraction_engine_used")
        or _graph_extraction_engine(config)
        or "llm"
    )
    warnings: list[str] = []
    if chunking_config.get("semantic_split_reason"):
        warnings.append(str(chunking_config["semantic_split_reason"]))
    if _structure_quality(parse_result) == "low":
        warnings.append(
            "Parser found weak structure; chunking used fallback boundaries."
        )

    trace = {
        "file_profile": _file_profile(parse_result, source_mime, filename),
        "source_mime": source_mime,
        "source_tier": source_tier.value,
        "parser_strategy": str(
            getattr(parse_result, "source_format", "") or "docling_sidecar"
        ),
        "structure_quality": _structure_quality(parse_result),
        "page_count": int(getattr(parse_result, "num_pages", 1) or 1),
        "has_structure": bool(getattr(parse_result, "has_structure", False)),
        "chunking_strategy": str(chunking_config.get("parent_strategy") or "unknown"),
        "child_strategy": str(chunking_config.get("child_strategy") or "unknown"),
        "requested_child_strategy": str(
            chunking_config.get("requested_child_strategy") or "unknown"
        ),
        "parent_count": len(parents),
        "child_count": len(children),
        "parent_target_tokens": int(budgets.get("parent_target") or 0),
        "child_target_tokens": int(budgets.get("child_target") or 0),
        "max_child_tokens": max(
            [int(getattr(c, "token_count", 0) or 0) for c in children] or [0]
        ),
        "max_parent_tokens": max(
            [
                tier_chunker._count_tokens(str(getattr(p, "text", "") or ""))
                for p in parents
            ]
            or [0]
        ),
        "hard_token_split_enabled": True,
        "chunk_overlap": int(chunking_config.get("chunk_overlap") or 0),
        "page_ranges_preserved": bool(chunking_config.get("page_ranges_preserved")),
        "low_value_chunk_count": sum(low_value_kinds.values()),
        "low_value_chunk_kinds": low_value_kinds,
        "chunk_kind_counts": _chunk_kind_counts(children),
        "vector_strategy": "dense_sparse:" + ",".join(config.target_qdrant_collections),
        "vector_ready": bool(ws.vector_ready),
        "graph_status": ws.graph_status,
        "graph_strategy": graph_strategy,
        "graph_mode": graph_mode,
        "graph_extraction_engine": graph_engine,
        "graph_completeness": graph_completeness,
        "graph_requested_chunks": int(metrics.get("requested_chunks") or body_children),
        "graph_extracted_chunks": int(metrics.get("extracted_chunks") or 0),
        "graph_failed_chunks": int(
            metrics.get("failed_chunk_count") or metrics.get("failed_chunks") or 0
        ),
        "reasons": _trace_reasons(
            parse_result=parse_result,
            chunking_config=chunking_config,
            policy=policy,
            low_value_count=sum(low_value_kinds.values()),
            config=config,
            ws=ws,
        ),
        "warnings": warnings,
    }
    return trace


def _decision_trace_summary(trace: dict | None) -> str:
    if not trace:
        return "auto ingestion policy"
    chunking = str(trace.get("chunking_strategy") or "auto chunking").replace("_", " ")
    graph = str(trace.get("graph_strategy") or "graph policy").replace("_", " ")
    skipped = int(trace.get("low_value_chunk_count") or 0)
    parts = [chunking, graph]
    if skipped:
        parts.append(f"{skipped} low-value chunks skipped")
    return " - ".join(parts)


def _set_if_present(updates: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    updates[key] = value


async def _update_graph_write_state(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    ws: WriteState,
    status: str,
    ghost_b_out: list[ExtractionResult] | None = None,
    ghost_b_failures: list[ExtractionFailureItem] | None = None,
    ghost_b_metrics: dict | None = None,
    started_at: datetime | None | bool = False,
    finished_at: datetime | None | bool = False,
) -> None:
    extracted, failed = _graph_counts(
        ghost_b_out=ghost_b_out,
        ghost_b_failures=ghost_b_failures,
        ghost_b_metrics=ghost_b_metrics,
    )
    metrics = ghost_b_metrics or {}
    ws.graph_status = status
    ws.graph_extracted_chunk_count = extracted
    ws.graph_failed_chunk_count = failed
    if metrics.get("extraction_strategy"):
        ws.graph_extraction_strategy = str(metrics.get("extraction_strategy"))
    if metrics.get("graph_completeness"):
        ws.graph_completeness = str(metrics.get("graph_completeness"))
    elif status == GRAPH_SKIPPED:
        ws.graph_completeness = "graph-skipped"
    elif status == GRAPH_FAILED_TOKEN_BUDGET:
        ws.graph_completeness = "graph-token-budget-failed"
    elif status in (GRAPH_PARTIAL, GRAPH_NEEDS_BACKFILL, GRAPH_RETRY_SCHEDULED):
        ws.graph_completeness = "needs-backfill"
    elif status == GRAPH_READY:
        ws.graph_completeness = ws.graph_completeness or "graph-complete"
    retry_after_raw = metrics.get("graph_retry_after")
    retry_after = None
    if retry_after_raw:
        if isinstance(retry_after_raw, datetime):
            retry_after = retry_after_raw
        else:
            try:
                retry_after = datetime.fromisoformat(str(retry_after_raw))
            except Exception:
                retry_after = None
    ws.graph_retry_after = retry_after
    ws.graph_retryable_failed_chunk_count = int(
        metrics.get("retryable_failed_chunks") or 0
    )
    flags: dict[str, Any] = {
        "graph_status": ws.graph_status,
        "graph_extracted_chunk_count": ws.graph_extracted_chunk_count,
        "graph_failed_chunk_count": ws.graph_failed_chunk_count,
        "graph_extraction_strategy": ws.graph_extraction_strategy,
        "graph_completeness": ws.graph_completeness,
        "graph_retry_after": ws.graph_retry_after,
        "graph_retryable_failed_chunk_count": ws.graph_retryable_failed_chunk_count,
    }
    if started_at is not False:
        ws.graph_extraction_started_at = started_at
        flags["graph_extraction_started_at"] = started_at
    if finished_at is not False:
        ws.graph_extraction_finished_at = finished_at
        flags["graph_extraction_finished_at"] = finished_at
    await mongo_writer.update_write_state(
        db,
        doc_id,
        corpus_id=corpus_id,
        **flags,
    )
    trace_updates: dict[str, Any] = {
        "decision_trace.graph_status": ws.graph_status,
        "decision_trace.graph_extracted_chunks": ws.graph_extracted_chunk_count,
        "decision_trace.graph_failed_chunks": ws.graph_failed_chunk_count,
        "decision_trace.graph_retry_after": ws.graph_retry_after,
        "decision_trace.graph_retryable_failed_chunks": ws.graph_retryable_failed_chunk_count,
        "decision_trace.vector_ready": ws.vector_ready,
        "updated_at": datetime.utcnow(),
    }
    _set_if_present(
        trace_updates,
        "decision_trace.graph_strategy",
        ws.graph_extraction_strategy,
    )
    _set_if_present(
        trace_updates,
        "decision_trace.graph_completeness",
        ws.graph_completeness,
    )
    _set_if_present(
        trace_updates,
        "decision_trace.graph_extraction_engine",
        metrics.get("graph_extraction_engine_used"),
    )
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"$set": trace_updates},
    )


async def _persist_graph_extraction(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    ghost_b_out: list[ExtractionResult] | None,
    ghost_b_failures: list[ExtractionFailureItem] | None,
    ghost_b_metrics: dict | None,
    warnings: list[str],
) -> None:
    trace_updates: dict[str, Any] = {
        "write_state.warnings": warnings,
        "ghost_b_staging": [asdict(r) for r in (ghost_b_out or [])],
        "ghost_b_failures": [asdict(f) for f in (ghost_b_failures or [])],
        "ghost_b_metrics": ghost_b_metrics or {},
        "schema_lens": (ghost_b_metrics or {}).get("schema_lens"),
        "decision_trace.graph_failed_chunks": (
            (ghost_b_metrics or {}).get("failed_chunk_count")
            or (ghost_b_metrics or {}).get("failed_chunks")
            or len(ghost_b_failures or [])
        ),
        "updated_at": datetime.utcnow(),
    }
    for field_name, metric_name in (
        ("decision_trace.graph_strategy", "extraction_strategy"),
        ("decision_trace.graph_mode", "extraction_mode"),
        ("decision_trace.graph_extraction_engine", "graph_extraction_engine_used"),
        ("decision_trace.graph_completeness", "graph_completeness"),
        ("decision_trace.graph_requested_chunks", "requested_chunks"),
        ("decision_trace.graph_extracted_chunks", "extracted_chunks"),
        # Top-level health metrics. Surface these as targeted $set keys so
        # the UI / aggregations don't have to walk the nested ghost_b_metrics
        # blob, while keeping the full audit blob intact above.
        ("ghost_b_total_chunks", "ghost_b_total_chunks"),
        ("ghost_b_skipped_chunks", "ghost_b_skipped_chunks"),
        ("ghost_b_truncated_count", "ghost_b_truncated_count"),
        ("ghost_b_recovered_count", "ghost_b_recovered_count"),
        ("ghost_b_failed_count", "ghost_b_failed_count"),
    ):
        _set_if_present(
            trace_updates, field_name, (ghost_b_metrics or {}).get(metric_name)
        )
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"$set": trace_updates},
    )


async def _enqueue_relation_repairs_for_doc(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    candidates: list[RelationRepairCandidate],
    config: IngestionConfig,
) -> dict[str, Any]:
    if not candidates:
        return {
            "status": "not_needed",
            "counts": {
                "total": 0,
                "pending": 0,
                "succeeded": 0,
                "discarded": 0,
                "terminal_failed": 0,
            },
        }
    await repair_queue.enqueue_relation_repairs(
        db,
        candidates,
        max_attempts=getattr(config, "graph_repair_max_attempts", 3),
    )
    return await repair_queue.refresh_document_repair_state(
        db,
        corpus_id=corpus_id,
        doc_id=doc_id,
    )


def _ghost_b_partial_warning(
    *,
    extracted: int,
    total: int,
) -> str:
    skipped = max(total - extracted, 0)
    return (
        f"Ghost B graph extraction partial: {extracted}/{total} chunks produced "
        f"entities/relations; {skipped} chunks remain available for vector RAG "
        "but have no extracted graph entities."
    )


def _ghost_a_partial_warning(
    *,
    summarized: int,
    total: int,
) -> str:
    """Surface partial-coverage from Ghost A in the document UI without
    aborting the doc. Parents that fail Ghost A keep their inline parent text
    in Mongo; only the synthetic parent-summary embedding is missing for
    those parents. Vector + naive RAG are unaffected for those chunks.
    """
    skipped = max(total - summarized, 0)
    return (
        f"Ghost A parent summarization partial: {summarized}/{total} parents "
        f"summarized; {skipped} parents skipped (most often: parent text "
        "exceeded the summary model's context window). Affected parents are "
        "still indexed for vector RAG via their child chunks; only the "
        "parent-summary tier is missing for those parents."
    )


def _ghost_b_metrics_for_skipped(results: list[ExtractionResult] | None) -> dict | None:
    if results is None:
        return None
    relation_count = sum(len(r.relations) for r in results)
    related_to_count = sum(
        1 for r in results for rel in r.relations if rel.predicate == "related_to"
    )
    predicate_confidences = [
        float(rel.predicate_confidence)
        for r in results
        for rel in r.relations
        if rel.predicate_confidence is not None
    ]
    lens_ids = sorted({r.schema_lens_id for r in results if r.schema_lens_id})
    return {
        "requested_chunks": len(results),
        "extracted_chunks": len(results),
        "failed_chunks": 0,
        "failed_chunk_count": 0,
        "success_rate": 1.0,
        "ghost_b_success_rate": 1.0,
        "attempt_count": 0,
        "json_recovery_count": 0,
        "json_recovery_rate": 0.0,
        "json_recovery_attempt_rate": 0.0,
        "ghost_b_total_chunks": len(results),
        "ghost_b_skipped_chunks": 0,
        "ghost_b_truncated_count": 0,
        "ghost_b_truncated_rate": 0.0,
        "ghost_b_recovered_count": 0,
        "ghost_b_failed_count": 0,
        "models": [],
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost_tokens": 0,
        "avg_prompt_tokens_per_chunk": 0.0,
        "total_duration_seconds": 0.0,
        "entity_count": sum(len(r.entities) for r in results),
        "relation_count": relation_count,
        "candidate_fact_count": sum(len(r.candidate_facts) for r in results),
        "related_to_count": related_to_count,
        "related_to_ratio": round(related_to_count / relation_count, 4)
        if relation_count
        else 0.0,
        "predicate_confidence_avg": (
            round(sum(predicate_confidences) / len(predicate_confidences), 4)
            if predicate_confidences
            else 0.0
        ),
        "entity_remap_count": sum(r.entity_remap_count for r in results),
        "relation_remap_count": sum(r.relation_remap_count for r in results),
        "domain_range_remap_count": sum(r.domain_range_remap_count for r in results),
        "domain_range_warn_count": sum(r.domain_range_warn_count for r in results),
        "endpoint_completion_count": sum(r.endpoint_completion_count for r in results),
        "evidence_cue_repair_count": sum(r.evidence_cue_repair_count for r in results),
        "direction_repair_count": sum(r.direction_repair_count for r in results),
        "review_relation_count": sum(
            1
            for r in results
            for rel in r.relations
            if rel.review_status
            or "review_required" in str(rel.validation_status or "")
        ),
        "entity_drop_count": sum(r.entity_drop_count for r in results),
        "relation_drop_count": sum(r.relation_drop_count for r in results),
        "schema_lens_ids": lens_ids,
        "error_counts": {},
        "extraction_strategy": "staging_reused",
        "graph_completeness": "graph-complete",
        "skipped_low_value_chunks": 0,
        "compact_extraction_chunks": 0,
        "deep_extraction_chunks": 0,
        "full_extraction_chunks": len(results),
    }


@dataclass(frozen=True)
class GhostBExtractionPolicy:
    """Per-document Ghost B extraction budget.

    The policy is deliberately a budget selector, not a schema selector. Full
    and compact modes both keep the same ontology and deterministic compiler;
    compact mode only lowers JSON output pressure for huge documents.
    """

    extraction_strategy: str
    extraction_mode: str
    graph_completeness: str
    reason: str
    total_children: int
    body_children: int
    skipped_low_value_chunks: int
    skipped_low_value_by_kind: dict[str, int]
    full_extraction_chunks: int
    compact_extraction_chunks: int
    deep_extraction_chunks: int
    max_entities_per_chunk: int
    max_relations_per_chunk: int
    max_completion_tokens: int
    large_doc_child_threshold: int
    full_extract_max_children: int
    deep_pass_enabled: bool
    deep_pass_max_chunks: int

    def metrics(self) -> dict:
        return asdict(self)


def _config_int(config: IngestionConfig, name: str, default: int) -> int:
    try:
        return int(getattr(config, name, default))
    except Exception:
        return default


def _select_ghost_b_extraction_policy(
    config: IngestionConfig,
    *,
    total_children: int,
    body_children: int,
    skipped_low_value_by_kind: dict[str, int] | None = None,
) -> GhostBExtractionPolicy:
    """Choose full vs compact Ghost B extraction for this document."""
    large_threshold = max(1, _config_int(config, "large_doc_child_threshold", 600))
    full_cap = max(1, _config_int(config, "full_extract_max_children", large_threshold))
    compact_entities = max(
        1,
        min(
            _config_int(config, "compact_mode_max_entities", 8),
            settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK,
        ),
    )
    compact_relations = max(
        0,
        min(
            _config_int(config, "compact_mode_max_relations", 8),
            settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK,
        ),
    )
    deep_enabled = bool(getattr(config, "deep_pass_enabled", False))
    deep_max = max(0, _config_int(config, "deep_pass_max_chunks", 80))
    skipped_by_kind = dict(skipped_low_value_by_kind or {})
    skipped_total = sum(int(v or 0) for v in skipped_by_kind.values())

    is_large = body_children >= large_threshold or body_children > full_cap
    if is_large:
        return GhostBExtractionPolicy(
            extraction_strategy="compact_large_doc",
            extraction_mode="compact",
            graph_completeness="graph-compact",
            reason=(
                f"body_children={body_children} exceeds large_doc_child_threshold="
                f"{large_threshold} or full_extract_max_children={full_cap}"
            ),
            total_children=total_children,
            body_children=body_children,
            skipped_low_value_chunks=skipped_total,
            skipped_low_value_by_kind=skipped_by_kind,
            full_extraction_chunks=0,
            compact_extraction_chunks=body_children,
            deep_extraction_chunks=0,
            max_entities_per_chunk=compact_entities,
            max_relations_per_chunk=compact_relations,
            max_completion_tokens=min(settings.EXTRACTION_MAX_TOKENS, 2048),
            large_doc_child_threshold=large_threshold,
            full_extract_max_children=full_cap,
            deep_pass_enabled=deep_enabled,
            deep_pass_max_chunks=deep_max,
        )

    return GhostBExtractionPolicy(
        extraction_strategy="full_ontology",
        extraction_mode="full",
        graph_completeness="graph-complete",
        reason="body_children within full extraction budget",
        total_children=total_children,
        body_children=body_children,
        skipped_low_value_chunks=skipped_total,
        skipped_low_value_by_kind=skipped_by_kind,
        full_extraction_chunks=body_children,
        compact_extraction_chunks=0,
        deep_extraction_chunks=0,
        max_entities_per_chunk=settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK,
        max_relations_per_chunk=settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK,
        max_completion_tokens=settings.EXTRACTION_MAX_TOKENS,
        large_doc_child_threshold=large_threshold,
        full_extract_max_children=full_cap,
        deep_pass_enabled=deep_enabled,
        deep_pass_max_chunks=deep_max,
    )


def _graph_extraction_engine(config: IngestionConfig) -> str:
    engine = str(getattr(config, "graph_extraction_engine", "llm") or "llm").strip()
    return "llm" if engine != "llm" else engine


_HIGH_SIGNAL_ENTITY_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9_./+-]{2,}|[A-Z]{2,})\b")
_HIGH_SIGNAL_RELATION_RE = re.compile(
    r"\b("
    r"uses?|depends?|requires?|implements?|produces?|stores?|extracts?|detects?|"
    r"classifies?|runs?\s+on|trained\s+on|supports?|references?|defines?|measures?|"
    r"tests?|applied\s+to|causes?|motivates?|reinforces?|undermines?"
    r")\b",
    re.IGNORECASE,
)


def _high_signal_score(chunk, schema_lens: Any | None = None) -> float:
    """Cheap signal score for optional deep extraction.

    It is intentionally lexical and bounded. No embeddings or graph analytics
    run here, so query-time and ingest-time costs stay predictable.
    """
    text = str(getattr(chunk, "text", "") or "")
    if not text.strip():
        return 0.0
    sample = text[:2400]
    score = 0.0
    token_count = int(getattr(chunk, "token_count", 0) or 0)
    if token_count >= 250:
        score += 0.75
    heading_path = getattr(chunk, "heading_path", None) or []
    if heading_path and not re.match(r"^pages?_\d", str(heading_path[0]).lower()):
        score += 1.0
    entity_hits = len(_HIGH_SIGNAL_ENTITY_RE.findall(sample))
    relation_hits = len(_HIGH_SIGNAL_RELATION_RE.findall(sample))
    score += min(entity_hits, 12) * 0.18
    score += min(relation_hits, 8) * 0.35
    lens = schema_lens if hasattr(schema_lens, "preferred_relations") else None
    if lens:
        lower = sample.lower()
        score += sum(
            0.4
            for term in lens.preferred_relations[:10]
            if term.replace("_", " ") in lower
        )
        score += sum(
            0.25 for term in lens.object_kinds[:10] if str(term).lower() in lower
        )
        score += sum(
            0.25
            for term in lens.canonical_families[:10]
            if str(term).replace("_", " ").lower() in lower
        )
    return score


def _select_high_signal_children(
    children: list,
    *,
    schema_lens: Any | None = None,
    limit: int,
) -> list:
    if limit <= 0:
        return []
    scored = [
        (
            _high_signal_score(child, schema_lens=schema_lens),
            str(getattr(child, "chunk_id", "")),
            child,
        )
        for child in children
    ]
    selected = [
        child
        for score, _chunk_id, child in sorted(
            scored, key=lambda item: (-item[0], item[1])
        )
        if score >= 2.0
    ]
    return selected[:limit]


def _build_ghost_pool(refs) -> list[dict]:
    """
    Turn a list[ModelProfileRef] (Pydantic) or list[dict] into the plain-dict
    pool that ghost_a / ghost_b accept. Decrypts each entry's api_key exactly
    once here so the ghost layers stay ignorant of the secret format.

    `context_length` is passed through verbatim. When None, the ghost helpers
    fall back to `utils.tokens.get_model_context_limit` — that registry only
    knows public/cloud models, so local fine-tunes default to 4096 (a
    conservative under-estimate) and large parents will be skipped with a
    warning rather than killing the document.
    """
    if not refs:
        return []
    out: list[dict] = []
    for ref in refs:
        data = ref.model_dump() if hasattr(ref, "model_dump") else dict(ref)
        ct = data.get("api_key")
        if ct:
            pt = _decrypt_api_key(ct)
            data["api_key"] = pt if pt is not None else ct
        ctx_len = data.get("context_length")
        try:
            ctx_len_int = int(ctx_len) if ctx_len is not None else None
        except (TypeError, ValueError):
            ctx_len_int = None
        out.append(
            {
                "model": data.get("model"),
                "base_url": data.get("base_url") or None,
                "api_key": data.get("api_key") or None,
                "max_concurrent": int(data.get("max_concurrent") or 1) or 1,
                "extra_params": data.get("extra_params") or {},
                "context_length": ctx_len_int,
            }
        )
    return out


def _dataclass_from_staged(cls, row: dict, *, context: str):
    """Rehydrate persisted staging rows while ignoring stale/extra keys."""
    if not isinstance(row, dict):
        return None
    allowed = {item.name for item in dataclass_fields(cls)}
    try:
        return cls(**{key: value for key, value in row.items() if key in allowed})
    except (TypeError, ValueError) as exc:
        logger.warning("phase=ghost_b_rehydrate_skip context=%s error=%s", context, exc)
        return None


def _normalize_staged_relation_object_kind(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    if key in {
        "literal",
        "value",
        "scalar",
        "string",
        "number",
        "date",
        "literal_value",
    }:
        return "literal"
    if key in {"", "entity", "node", "named_entity", "canonical_entity"}:
        return "entity"
    return "entity"


def _rehydrate_ghost_b_staging(staged: list[dict]) -> list[ExtractionResult]:
    """Reconstruct ExtractionResult dataclasses from Mongo-stored staging rows."""
    out: list[ExtractionResult] = []
    for r in staged:
        if not isinstance(r, dict):
            continue
        entities = [
            item
            for item in (
                _dataclass_from_staged(EntityItem, e, context="entity")
                for e in r.get("entities", [])
            )
            if item is not None
        ]
        candidate_facts = [
            item
            for item in (
                _dataclass_from_staged(CandidateFactItem, f, context="candidate_fact")
                for f in r.get("candidate_facts", [])
            )
            if item is not None
        ]
        relations = []
        for x in r.get("relations", []):
            item = _dataclass_from_staged(RelationItem, x, context="relation")
            if item is None:
                continue
            item.object_kind = _normalize_staged_relation_object_kind(item.object_kind)
            relations.append(item)
        result = _dataclass_from_staged(
            ExtractionResult,
            {
                **r,
                "schema_version": r.get("schema_version", "polymath.extract.v1"),
                "entities": entities,
                "candidate_facts": candidate_facts,
                "relations": relations,
                "entity_remap_count": r.get("entity_remap_count", 0),
                "entity_drop_count": r.get("entity_drop_count", 0),
                "relation_remap_count": r.get("relation_remap_count", 0),
                "relation_drop_count": r.get("relation_drop_count", 0),
                "domain_range_remap_count": r.get("domain_range_remap_count", 0),
                "domain_range_warn_count": r.get("domain_range_warn_count", 0),
                "endpoint_completion_count": r.get("endpoint_completion_count", 0),
                "evidence_cue_repair_count": r.get("evidence_cue_repair_count", 0),
                "direction_repair_count": r.get("direction_repair_count", 0),
            },
            context="result",
        )
        if result is not None:
            out.append(result)
    return out


def _reconstruct_summaries_from_mongo(
    parents, existing_parent_chunks: list[dict]
) -> list[SummaryResult]:
    """Rebuild SummaryResult list from Mongo-stored parent_chunks[].

    Resume path: every parent must have either a non-empty summary or a
    `summary_status="skipped"` marker; the resume gate enforces that
    invariant before calling this function.

    Skipped parents are reconstructed with summary="" and status="skipped"
    so they propagate through the embed/qdrant phases without the embedder
    seeing an empty string (the embedder filters by status downstream when
    summary_texts are built — see _embed_batch_for_doc).
    """
    by_id = {ep["parent_id"]: ep for ep in existing_parent_chunks}
    out: list[SummaryResult] = []
    for p in parents:
        ep = by_id.get(p.parent_id)
        if not ep:
            continue
        summary = (ep.get("summary") or "").strip()
        status = ep.get("summary_status") or ("ok" if summary else None)
        if status is None:
            # Genuinely unattempted parent — exclude from the resume list so
            # the caller treats coverage as partial and re-runs Ghost A.
            continue
        out.append(
            SummaryResult(
                parent_id=p.parent_id,
                doc_id=p.doc_id,
                corpus_id=p.corpus_id,
                source_tier=p.source_tier,
                summary=summary,
                status=status,
                skip_reason=ep.get("summary_skip_reason"),
            )
        )
    return out


def _doc_token_set(texts: list[str]) -> set[str]:
    """Compact lexical fingerprint for near-duplicate document detection."""
    tokens: set[str] = set()
    for text in texts:
        for match in _DUPLICATE_TOKEN_RE.finditer(str(text or "").lower()):
            token = match.group(0).strip("'_-")
            if token and token not in _DUPLICATE_STOP_WORDS:
                tokens.add(token)
    return tokens


async def _find_near_duplicate_documents(
    *,
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    doc_id: str,
    parent_texts: list[str],
    threshold: float = _DUPLICATE_DOC_THRESHOLD,
    limit: int = 3,
) -> list[dict]:
    """Find existing corpus documents with high lexical overlap.

    This is intentionally deterministic and cheap: it runs once per ingest
    after chunking and before the new document is written. It does not block
    ingestion; it stores a quality warning so RAG audits can explain why a
    corpus is overweighting repeated concepts.
    """
    incoming = _doc_token_set(parent_texts)
    if len(incoming) < 24:
        return []

    candidates: list[dict] = []
    cursor = db["documents"].find(
        {"corpus_id": corpus_id, "doc_id": {"$ne": doc_id}},
        {"doc_id": 1, "filename": 1, "parent_chunks.text": 1},
    )
    async for doc in cursor:
        existing_texts = [
            str(p.get("text") or "")
            for p in (doc.get("parent_chunks") or [])
            if isinstance(p, dict)
        ]
        existing = _doc_token_set(existing_texts)
        if not existing:
            continue
        union = incoming | existing
        if not union:
            continue
        similarity = len(incoming & existing) / len(union)
        if similarity >= threshold:
            candidates.append(
                {
                    "doc_id": doc.get("doc_id"),
                    "filename": doc.get("filename") or "",
                    "similarity": round(float(similarity), 3),
                }
            )

    candidates.sort(key=lambda c: float(c.get("similarity") or 0.0), reverse=True)
    return candidates[:limit]


async def _run_ghosts_parallel(
    *,
    config: IngestionConfig,
    parents: list,
    children: list,
    doc_id: str,
    corpus_id: str,
    model: str,
    filename: str | None = None,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver,
    existing_doc: dict | None,
    ws: WriteState,
    include_ghost_a: bool = True,
    include_ghost_b: bool = True,
) -> GhostRunResult:
    """Run GHOST A and/or GHOST B. Either branch may be disabled
    by config OR skipped via resume gates (Decision D).

    Failure semantics (post-context-budget-guard):
      • Ghost A: per-parent skips (token-budget infeasible OR provider
        context-overflow 400) are surfaced as a partial warning; the doc
        commits with whichever summaries succeeded. Total outage (zero
        summaries with non-zero tasks) still raises GhostAFailure.
      • Ghost B: partials surface as a warning; the doc commits with whichever
        chunks produced extraction.
    """
    warnings: list[str] = []
    ghost_b_failures: list[ExtractionFailureItem] = []
    ghost_b_metrics: dict | None = None
    relation_repair_candidates: list[RelationRepairCandidate] = []
    summary_llm_calls = 0
    # ── GHOST A path decisions ────────────────────────────────────────────
    existing_parent_chunks: list[dict] = (existing_doc or {}).get("parent_chunks") or []
    summaries_from_mongo: list[SummaryResult] | None = None
    need_ghost_a = bool(include_ghost_a and config.chunk_summarization)

    if need_ghost_a and ws.qdrant_written:
        # Summaries already embedded into Qdrant on a prior run; nothing to do.
        need_ghost_a = False
    elif need_ghost_a and ws.mongo_written and existing_parent_chunks:
        # Resume gate: a parent counts as "Ghost A attempted" when it has
        # EITHER a non-empty `summary` (success) OR `summary_status="skipped"`
        # (Ghost A pre-flight ruled the parent infeasible against the
        # configured summary model — token-budget overflow). Skipped parents
        # must not be retried because they fail deterministically. Without
        # this marker, the resume gate retried them and wasted LLM calls.
        def _attempted(pc: dict) -> bool:
            if (pc.get("summary") or "").strip():
                return True
            return pc.get("summary_status") == "skipped"

        all_filled = all(_attempted(p) for p in existing_parent_chunks)
        if all_filled:
            summaries_from_mongo = _reconstruct_summaries_from_mongo(
                parents, existing_parent_chunks
            )
            if len(summaries_from_mongo) == len(parents):
                need_ghost_a = False
                logger.info(
                    "Ghost A skipped (resume) doc=%s corpus=%s parents=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    len(parents),
                )
            else:
                summaries_from_mongo = None  # partial reconstruct → rerun

    # ── GHOST B path decisions ────────────────────────────────────────────
    need_ghost_b = (
        include_ghost_b
        and config.use_neo4j
        and settings.NEO4J_ENABLED
        and not ws.neo4j_written
    )
    ghost_b_from_staging: list[ExtractionResult] | None = None
    if need_ghost_b and neo4j_driver is None:
        need_ghost_b = False
    elif need_ghost_b and ws.mongo_written:
        staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
        if staged:
            ghost_b_from_staging = _rehydrate_ghost_b_staging(staged)
            need_ghost_b = False
            logger.info(
                "phase=ghost_b_skip reason=staging_found doc=%s corpus=%s entries=%d",
                doc_id[:12],
                corpus_id[:8],
                len(ghost_b_from_staging),
            )
        elif ws.qdrant_written:
            # Pre-feature document: Qdrant done, Neo4j not, no staging on
            # disk → only possible for docs ingested before this change.
            logger.info(
                "phase=ghost_b_rerun reason=staging_missing_legacy_doc doc=%s corpus=%s",
                doc_id[:12],
                corpus_id[:8],
            )

    # ── Branch coroutines ────────────────────────────────────────────────
    async def _a_branch() -> list[SummaryResult] | None:
        nonlocal summary_llm_calls
        if not need_ghost_a:
            return summaries_from_mongo  # None unless resume-reconstructed
        # Skip non-body parents (TOC, bibliography, index, appendix, …).
        # Each summary call is an LLM round-trip and the resulting summary
        # also gets embedded → skipping noisy parents both saves LLM spend
        # and reduces GPU pressure on the embed phase. Backwards-compat:
        # parents without `chunk_kind` (legacy data, or rehydrated from
        # earlier ingest) are treated as body and flow through unchanged.
        skipped_kinds_a: dict[str, int] = {}
        body_parents = []
        for p in parents:
            kind = getattr(p, "chunk_kind", None) or ChunkKind.BODY
            if should_skip_ghost_b(kind):  # same skip set for both ghosts
                skipped_kinds_a[kind] = skipped_kinds_a.get(kind, 0) + 1
            else:
                body_parents.append(p)
        if skipped_kinds_a:
            logger.info(
                "phase=ghost_a_skip_kinds doc=%s corpus=%s skipped=%s body=%d/%d",
                doc_id[:12],
                corpus_id[:8],
                skipped_kinds_a,
                len(body_parents),
                len(parents),
            )
        tasks = [
            SummaryTask(
                parent_id=p.parent_id,
                doc_id=p.doc_id,
                corpus_id=p.corpus_id,
                text=p.text,
                source_tier=p.source_tier,
            )
            for p in body_parents
        ]
        pool = _build_ghost_pool(config.summary_models)
        logger.info(
            "Ghost A start doc=%s corpus=%s parents=%d pool=%d",
            doc_id[:12],
            corpus_id[:8],
            len(tasks),
            len(pool) or 1,
        )
        results = await summarize_parents(
            tasks,
            max_summary_tokens=config.max_summary_tokens,
            pool=pool,
            model=model,
        )
        summary_llm_calls = len(tasks)
        # With Phase 21 skip-markers, ghost_a returns one entry per task —
        # success entries have status="ok", infeasible parents have
        # status="skipped". Total outage = nothing returned at all (every
        # task hit a fatal lane error before producing even a marker).
        # Partial coverage = at least one "skipped" entry. Mirror Ghost B's
        # soft-warning policy so a few oversized parents don't kill the
        # document — vector RAG still works via the child chunks of skipped
        # parents; only the parent-summary embedding is missing.
        ok_results = [r for r in results if getattr(r, "status", "ok") == "ok"]
        skipped_results = [r for r in results if getattr(r, "status", None) == "skipped"]
        if not results and tasks:
            raise GhostAFailure(
                f"Ghost A produced 0/{len(tasks)} summaries — "
                "treating as provider outage and aborting document."
            )
        partial_total = len(ok_results)
        if partial_total < len(tasks):
            skip_reason_counts: dict[str, int] = {}
            for r in skipped_results:
                key = r.skip_reason or "unknown"
                skip_reason_counts[key] = skip_reason_counts.get(key, 0) + 1
            warnings.append(
                _ghost_a_partial_warning(
                    summarized=partial_total,
                    total=len(tasks),
                )
            )
            logger.warning(
                "phase=ghost_a_partial doc=%s corpus=%s summarized=%d total=%d "
                "skipped=%d skip_reasons=%s",
                doc_id[:12],
                corpus_id[:8],
                partial_total,
                len(tasks),
                len(skipped_results),
                skip_reason_counts,
            )
        return results

    async def _b_branch() -> list[ExtractionResult] | None:
        nonlocal ghost_b_metrics
        if not need_ghost_b:
            # Either Ghost B is disabled / already done, or staging already
            # rehydrated the previous run's output. Return staging (None
            # otherwise) so the caller can still push to Neo4j.
            return ghost_b_from_staging
        # Skip non-body chunks (TOC, bibliography, index, appendix, …) — the
        # extraction LLM call dominates per-chunk ingest cost, so dropping
        # noise here is the biggest single win. Default-body chunks and any
        # legacy chunk without a `chunk_kind` flow through unchanged.
        skipped_kinds: dict[str, int] = {}
        body_children = []
        for c in children:
            kind = getattr(c, "chunk_kind", None) or ChunkKind.BODY
            if should_skip_ghost_b(kind):
                skipped_kinds[kind] = skipped_kinds.get(kind, 0) + 1
            else:
                body_children.append(c)
        if skipped_kinds:
            logger.info(
                "phase=ghost_b_skip_kinds doc=%s corpus=%s skipped=%s body=%d/%d",
                doc_id[:12],
                corpus_id[:8],
                skipped_kinds,
                len(body_children),
                len(children),
            )
        policy = _select_ghost_b_extraction_policy(
            config,
            total_children=len(children),
            body_children=len(body_children),
            skipped_low_value_by_kind=skipped_kinds,
        )
        tasks = [
            ExtractionTask(
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                corpus_id=c.corpus_id,
                text=c.text,
                document_title=filename
                or (existing_doc or {}).get("filename")
                or doc_id,
                heading_path=getattr(c, "heading_path", None),
                chunk_kind=getattr(c, "chunk_kind", ChunkKind.BODY),
            )
            for c in body_children
        ]
        schema_ctx = SchemaContext(
            entity_schema=config.entity_schema,
            relation_schema=config.relation_schema,
            strict=config.schema_strict,
        )
        if config.models_linked or not config.extraction_models:
            pool = _build_ghost_pool(config.summary_models)
        else:
            pool = _build_ghost_pool(config.extraction_models)
        repair_pool = _build_ghost_pool(
            getattr(config, "extraction_repair_models", None) or []
        )
        # Exclude noisy parents/children from the schema lens — letting
        # bibliography page entries (publishers, ISBNs, citation bric-a-brac)
        # influence which schema terms get retrieved would erode entity
        # extraction quality on body content.
        body_parents_for_lens = [
            p
            for p in parents
            if not should_skip_ghost_b(getattr(p, "chunk_kind", None) or ChunkKind.BODY)
        ]
        body_children_for_lens = [
            c
            for c in children
            if not should_skip_ghost_b(getattr(c, "chunk_kind", None) or ChunkKind.BODY)
        ]
        schema_lens = await get_or_create_schema_lens(
            db=db,
            corpus_id=corpus_id,
            filename=filename or (existing_doc or {}).get("filename") or doc_id,
            parents=body_parents_for_lens or parents,  # fall back if all noisy
            children=body_children_for_lens or children,
            entity_schema=config.entity_schema,
            relation_schema=config.relation_schema,
            pool=pool,
            model=model,
        )

        async def _schema_resolver(
            kind: str, query_vec: list[float], top_k: int
        ) -> list[str]:
            return await retrieve_schema_for_chunk(
                qdrant_client, corpus_id, kind, query_vec, top_k
            )

        # Locked pipeline: embeddings don't exist yet when Ghost B runs. For
        # schemas with vocab ≤ SCHEMA_INLINE_LIMIT the full vocab is inlined
        # (no resolver call). For larger vocabs the resolver cannot use real
        # chunk vectors and resolve_chunk_vocab falls back to the first N
        # terms — this is the documented degraded mode (GOTCHA #42).
        if ws.vector_ready and ws.graph_status == GRAPH_EXTRACTING:
            reason = "graph_enrichment"
        else:
            reason = (
                "fresh_ingest" if not ws.mongo_written else "staging_missing_legacy_doc"
            )
        logger.info(
            "phase=ghost_b_run reason=%s doc=%s corpus=%s children=%d pool=%d strict=%s strategy=%s mode=%s max_entities=%d max_relations=%d max_tokens=%d",
            reason,
            doc_id[:12],
            corpus_id[:8],
            len(tasks),
            len(pool) or 1,
            schema_ctx.strict,
            policy.extraction_strategy,
            policy.extraction_mode,
            policy.max_entities_per_chunk,
            policy.max_relations_per_chunk,
            policy.max_completion_tokens,
        )
        if not tasks:
            metrics = dict(_ghost_b_metrics_for_skipped([]) or {})
            metrics.update(policy.metrics())
            metrics["schema_lens"] = schema_lens.to_dict()
            ghost_b_metrics = metrics
            return []
        llm_kwargs = {
            "schema": schema_ctx,
            "schema_lens": schema_lens,
            "chunk_vectors": None,
            "schema_resolver": _schema_resolver,
            "pool": pool,
            "repair_pool": repair_pool,
            "model": model,
            "return_report": True,
            "extraction_mode": policy.extraction_mode,
            "max_entities_per_chunk": policy.max_entities_per_chunk,
            "max_relations_per_chunk": policy.max_relations_per_chunk,
            "max_completion_tokens_override": policy.max_completion_tokens,
            "per_chunk_max_attempts": getattr(
                config, "graph_per_chunk_max_attempts", 2
            ),
            "per_doc_max_failed_chunks_before_pause": getattr(
                config, "graph_per_doc_max_failed_chunks_before_pause", 50
            ),
            "per_lane_max_consecutive_failures": getattr(
                config, "graph_per_lane_max_consecutive_failures", 2
            ),
            "per_lane_cooldown_seconds": getattr(
                config, "graph_per_lane_cooldown_seconds", 300
            ),
            "metrics_context": {
                **policy.metrics(),
                "graph_extraction_engine_requested": _graph_extraction_engine(config),
            },
            "defer_triple_repair": bool(
                getattr(config, "deferred_graph_repair_enabled", True)
            ),
        }
        report = await extract_entities(tasks, **llm_kwargs)
        if not isinstance(report, ExtractionBatchReport):
            results = report
            failures: list[ExtractionFailureItem] = []
            metrics = _ghost_b_metrics_for_skipped(results)
        else:
            results = report.results
            failures = report.failures
            metrics = report.metrics
            relation_repair_candidates.extend(report.relation_repairs or [])
        metrics = dict(metrics or {})
        if (
            policy.extraction_mode == "compact"
            and policy.deep_pass_enabled
            and policy.deep_pass_max_chunks > 0
            and results
        ):
            deep_children = _select_high_signal_children(
                body_children,
                schema_lens=schema_lens,
                limit=policy.deep_pass_max_chunks,
            )
            already_failed = {failure.chunk_id for failure in failures}
            deep_tasks = [
                ExtractionTask(
                    chunk_id=c.chunk_id,
                    doc_id=c.doc_id,
                    corpus_id=c.corpus_id,
                    text=c.text,
                    document_title=filename
                    or (existing_doc or {}).get("filename")
                    or doc_id,
                    heading_path=getattr(c, "heading_path", None),
                    chunk_kind=getattr(c, "chunk_kind", ChunkKind.BODY),
                )
                for c in deep_children
                if c.chunk_id not in already_failed
            ]
            if deep_tasks:
                logger.info(
                    "phase=ghost_b_deep_pass doc=%s corpus=%s selected=%d limit=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    len(deep_tasks),
                    policy.deep_pass_max_chunks,
                )
                deep_report = await extract_entities(
                    deep_tasks,
                    schema=schema_ctx,
                    schema_lens=schema_lens,
                    chunk_vectors=None,
                    schema_resolver=_schema_resolver,
                    pool=pool,
                    repair_pool=repair_pool,
                    model=model,
                    return_report=True,
                    extraction_mode="full",
                    per_chunk_max_attempts=getattr(
                        config, "graph_per_chunk_max_attempts", 2
                    ),
                    per_doc_max_failed_chunks_before_pause=getattr(
                        config, "graph_per_doc_max_failed_chunks_before_pause", 50
                    ),
                    per_lane_max_consecutive_failures=getattr(
                        config, "graph_per_lane_max_consecutive_failures", 2
                    ),
                    per_lane_cooldown_seconds=getattr(
                        config, "graph_per_lane_cooldown_seconds", 300
                    ),
                    defer_triple_repair=bool(
                        getattr(config, "deferred_graph_repair_enabled", True)
                    ),
                    metrics_context={
                        "extraction_strategy": "deep_pass_high_signal",
                        "graph_completeness": "graph-compact",
                    },
                )
                if isinstance(deep_report, ExtractionBatchReport):
                    deep_results = deep_report.results
                    relation_repair_candidates.extend(
                        deep_report.relation_repairs or []
                    )
                    result_by_chunk = {r.chunk_id: r for r in results}
                    for item in deep_results:
                        result_by_chunk[item.chunk_id] = item
                    results = list(result_by_chunk.values())
                    deep_metrics = deep_report.metrics
                    metrics["deep_extraction_chunks"] = len(deep_tasks)
                    metrics["deep_extracted_chunks"] = len(deep_results)
                    metrics["deep_failed_chunks"] = len(deep_report.failures)
                    metrics["deep_total_tokens"] = int(
                        deep_metrics.get("total_tokens") or 0
                    )
                    metrics["deep_prompt_tokens"] = int(
                        deep_metrics.get("prompt_tokens") or 0
                    )
                    metrics["deep_completion_tokens"] = int(
                        deep_metrics.get("completion_tokens") or 0
                    )
                    metrics["total_tokens"] = (
                        int(metrics.get("total_tokens") or 0)
                        + metrics["deep_total_tokens"]
                    )
                    metrics["prompt_tokens"] = (
                        int(metrics.get("prompt_tokens") or 0)
                        + metrics["deep_prompt_tokens"]
                    )
                    metrics["completion_tokens"] = (
                        int(metrics.get("completion_tokens") or 0)
                        + metrics["deep_completion_tokens"]
                    )
                    metrics["estimated_cost_tokens"] = int(
                        metrics.get("total_tokens") or 0
                    )
                    metrics["attempt_count"] = int(
                        metrics.get("attempt_count") or 0
                    ) + int(deep_metrics.get("attempt_count") or 0)
                    metrics["json_recovery_count"] = int(
                        metrics.get("json_recovery_count") or 0
                    ) + int(deep_metrics.get("json_recovery_count") or 0)
                    metrics["graph_completeness"] = "graph-compact"
                    metrics["extraction_strategy"] = "compact_large_doc_with_deep_pass"
                    metrics["entity_count"] = sum(len(r.entities) for r in results)
                    metrics["relation_count"] = sum(len(r.relations) for r in results)
                    metrics["candidate_fact_count"] = sum(
                        len(r.candidate_facts) for r in results
                    )
                    metrics["related_to_count"] = sum(
                        1
                        for r in results
                        for rel in r.relations
                        if rel.predicate == "related_to"
                    )
                    relation_count = int(metrics.get("relation_count") or 0)
                    metrics["related_to_ratio"] = (
                        round(
                            int(metrics.get("related_to_count") or 0) / relation_count,
                            4,
                        )
                        if relation_count
                        else 0.0
                    )
                    requested = int(metrics.get("requested_chunks") or len(tasks))
                    attempts = int(metrics.get("attempt_count") or 0)
                    recoveries = int(metrics.get("json_recovery_count") or 0)
                    metrics["json_recovery_rate"] = (
                        round(recoveries / requested, 4) if requested else 0.0
                    )
                    metrics["json_recovery_attempt_rate"] = (
                        round(recoveries / attempts, 4) if attempts else 0.0
                    )
        metrics["schema_lens"] = schema_lens.to_dict()
        metrics["relation_repair_queued_count"] = len(relation_repair_candidates)
        metrics["relation_repair_pending_count"] = len(relation_repair_candidates)
        ghost_b_failures.extend(failures)
        ghost_b_metrics = metrics
        if len(results) < len(tasks):
            missing_ids = sorted(
                {t.chunk_id for t in tasks} - {r.chunk_id for r in results}
            )
            warning = _ghost_b_partial_warning(
                extracted=len(results),
                total=len(tasks),
            )
            warnings.append(warning)
            logger.warning(
                "phase=ghost_b_partial doc=%s corpus=%s extracted=%d total=%d missing_sample=%s",
                doc_id[:12],
                corpus_id[:8],
                len(results),
                len(tasks),
                missing_ids[:5],
            )
        return results

    # Keep these branches sequential inside a document. User-configured
    # summary/extraction pool concurrency already fans out within each branch;
    # running both branches at once doubles provider pressure and makes
    # high-throughput API settings unsafe during batch ingest.
    summaries = await _a_branch()
    ghost_b_out = await _b_branch()
    if ghost_b_metrics is None:
        ghost_b_metrics = _ghost_b_metrics_for_skipped(ghost_b_out)
    ghost_b_metrics = dict(ghost_b_metrics or {})
    ghost_b_metrics.setdefault("summary_llm_calls", summary_llm_calls)
    ghost_b_metrics.setdefault("llm_graph_calls", 0)
    return GhostRunResult(
        summaries=summaries,
        ghost_b_out=ghost_b_out,
        warnings=warnings,
        ghost_b_failures=ghost_b_failures,
        ghost_b_metrics=ghost_b_metrics,
        relation_repair_candidates=relation_repair_candidates,
    )


def _build_parent_dicts(parents, summaries: list[SummaryResult] | None) -> list[dict]:
    """Assemble the parent_chunks[] array for the Mongo document record.

    Each parent dict carries:
      - `summary` — the Ghost A output text, or None if Ghost A wasn't run
        for this parent
      - `summary_status` — one of "ok" | "skipped" | None
          * None     → Ghost A was never attempted for this parent (e.g. Ghost
                       A globally disabled, or the worker pre-empted before
                       this parent)
          * "ok"     → Ghost A succeeded; `summary` holds real text
          * "skipped" → Ghost A attempted, parent infeasible (token-budget /
                        context overflow). On resume we DO NOT retry these.
                        `summary` is empty string, `summary_skip_reason` says
                        why.
    """
    by_parent = {s.parent_id: s for s in (summaries or [])}
    out: list[dict] = []
    for p in parents:
        s = by_parent.get(p.parent_id)
        if s is None:
            summary_text: str | None = None
            summary_status: str | None = None
            skip_reason: str | None = None
        else:
            summary_status = getattr(s, "status", "ok")
            skip_reason = getattr(s, "skip_reason", None)
            # Persist empty string for skipped parents so resume can tell
            # "attempted-and-skipped" (status="skipped", summary="") apart
            # from "never-attempted" (status=None, summary=None).
            summary_text = s.summary
        out.append(
            {
                "parent_id": p.parent_id,
                "doc_id": p.doc_id,
                "corpus_id": p.corpus_id,
                "text": p.text,
                "heading_path": p.heading_path,
                "source_tier": p.source_tier,
                "page_start": getattr(p, "page_start", None),
                "page_end": getattr(p, "page_end", None),
                "summary": summary_text,
                "summary_status": summary_status,
                "summary_skip_reason": skip_reason,
                "child_ids": [c.chunk_id for c in p.children],
            }
        )
    return out


async def _write_mongo_all(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    file_id: str,
    filename: str,
    source_tier: SourceTier,
    source_mime: str,
    ingestion_config: IngestionConfig,
    chunking_config: dict,
    parents,
    children,
    summaries: list[SummaryResult] | None,
    ghost_b_out: list[ExtractionResult] | None,
    ghost_b_failures: list[ExtractionFailureItem] | None,
    ghost_b_metrics: dict | None,
    decision_trace: dict | None,
    ws: WriteState,
    content_hash: str | None = None,
) -> None:
    """Single Mongo write pass: documents + chunks. Summaries go INLINE on
    parent_chunks[].summary and Ghost B output goes INLINE on
    documents.ghost_b_staging — one atomic write, no post-insert UPDATE.
    The staging list is authoritative for Ghost B resume gating and is
    retained as provenance after neo4j_written flips (never cleared).
    """
    parent_dicts = _build_parent_dicts(parents, summaries)
    duplicate_candidates = await _find_near_duplicate_documents(
        db=db,
        corpus_id=corpus_id,
        doc_id=doc_id,
        parent_texts=[p.get("text") or "" for p in parent_dicts],
    )
    child_dicts = [
        {
            "chunk_id": c.chunk_id,
            "parent_id": c.parent_id,
            "doc_id": c.doc_id,
            "corpus_id": c.corpus_id,
            "user_id": user_id,
            "text": c.text,
            "heading_path": c.heading_path,
            "source_tier": c.source_tier,
            "token_count": c.token_count,
            "page_start": getattr(c, "page_start", None),
            "page_end": getattr(c, "page_end", None),
            "chunk_kind": getattr(c, "chunk_kind", ChunkKind.BODY),
        }
        for c in children
    ]
    ghost_b_staging = [asdict(r) for r in ghost_b_out] if ghost_b_out else None
    ghost_b_failure_rows = (
        [asdict(f) for f in ghost_b_failures] if ghost_b_failures else []
    )
    # Phase 21 — snapshot FROZEN fields only. Mutable fields (embed_*, pools,
    # concurrency knobs) are always read live from the corpus at ingest time;
    # persisting them onto the doc record would create two sources of truth.
    from services.ingestion_service import freeze_snapshot

    now = datetime.utcnow()
    doc_record = {
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "file_id": file_id,
        "filename": filename,
        "content_hash": content_hash,
        "source_mime": source_mime,
        "source_tier": source_tier.value,
        "ingestion_config": freeze_snapshot(ingestion_config),
        "chunking_config": chunking_config,
        "write_state": ws.model_dump(),
        "chunk_count": len(children),
        "parent_count": len(parents),
        "parent_chunks": parent_dicts,
        "ghost_b_staging": ghost_b_staging,
        "ghost_b_failures": ghost_b_failure_rows,
        "ghost_b_metrics": ghost_b_metrics or {},
        "decision_trace": decision_trace or {},
        "decision_trace_summary": _decision_trace_summary(decision_trace),
        "schema_lens": (ghost_b_metrics or {}).get("schema_lens"),
        "is_near_duplicate": bool(duplicate_candidates),
        "near_duplicate_candidates": duplicate_candidates,
        "created_at": now,
        "updated_at": now,
    }
    if duplicate_candidates:
        logger.warning(
            "phase=duplicate_check doc=%s corpus=%s filename=%s candidates=%s",
            doc_id[:12],
            corpus_id[:8],
            filename,
            duplicate_candidates,
        )
    # BSON pre-flight. Mongo's hard limit is 16 MB per document; we get a
    # confusing pymongo error several layers deep if we ship over it. The
    # culprit is virtually always parent_chunks[].text on book-sized docs
    # (the 6700-chunk PBR ingest hits this). At 12 MB we warn; at 15 MB we
    # demote heavy fields to per-chunk Mongo writes and trim the inline copy
    # so the doc record fits.
    BSON_SOFT_LIMIT = 12 * 1024 * 1024
    BSON_HARD_LIMIT = 15 * 1024 * 1024
    estimated_size = _estimate_bson_size(doc_record)
    if estimated_size > BSON_HARD_LIMIT:
        logger.error(
            "phase=mongo_bson_overflow doc=%s corpus=%s estimated_bytes=%d "
            "parent_count=%d chunk_count=%d filename=%s — trimming inline "
            "parent_chunks[].text to keep doc under 16MB; full text still "
            "available via the chunks collection.",
            doc_id[:12], corpus_id[:8], estimated_size,
            len(parent_dicts), len(child_dicts), filename,
        )
        for parent in parent_dicts:
            parent["text_truncated"] = True
            parent["text_byte_count"] = len(str(parent.get("text") or "").encode("utf-8"))
            parent["text"] = (str(parent.get("text") or "")[:1000] or "")
        ws.warnings = _merge_warnings(
            ws.warnings,
            [
                f"Mongo BSON overflow: parent_chunks[].text trimmed to keep "
                f"document under 16MB. Full child-chunk text remains in the "
                f"chunks collection (estimated_bytes={estimated_size})."
            ],
        )
        doc_record["write_state"] = ws.model_dump()
    elif estimated_size > BSON_SOFT_LIMIT:
        logger.warning(
            "phase=mongo_bson_warn doc=%s corpus=%s estimated_bytes=%d "
            "parent_count=%d — approaching 16MB BSON limit",
            doc_id[:12], corpus_id[:8], estimated_size, len(parent_dicts),
        )
    await mongo_writer.upsert_document(db, doc_record)
    await mongo_writer.upsert_chunks(db, child_dicts)


def _estimate_bson_size(doc: dict) -> int:
    """Cheap upper-bound estimate for how big a Mongo document will be on disk.

    Pymongo encodes each Python value to a BSON byte sequence on insert; the
    full encode is O(N) but allocates and stalls. For pre-flight we only need
    a fast estimate that's never under-counts. Strategy: walk the dict, sum
    UTF-8 byte length of every string + 8 bytes per scalar slot for keys and
    type bytes. Lists / dicts add a small constant per entry.

    Always over-estimates — that's the right direction for a "refuse if over
    threshold" gate.
    """
    if doc is None:
        return 0
    if isinstance(doc, str):
        return len(doc.encode("utf-8")) + 6
    if isinstance(doc, (int, float, bool)):
        return 9
    if isinstance(doc, dict):
        total = 5
        for k, v in doc.items():
            total += len(str(k).encode("utf-8")) + 2
            total += _estimate_bson_size(v)
        return total
    if isinstance(doc, (list, tuple)):
        total = 5
        for v in doc:
            total += _estimate_bson_size(v) + 4
        return total
    if isinstance(doc, (bytes, bytearray)):
        return len(doc) + 6
    if isinstance(doc, datetime):
        return 9
    return len(str(doc).encode("utf-8")) + 6


async def _ensure_progress_document(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    file_id: str,
    filename: str,
    source_tier: SourceTier,
    source_mime: str,
    ingestion_config: IngestionConfig,
    chunking_config: dict,
    parents,
    decision_trace: dict | None,
    ws: WriteState,
    content_hash: str | None = None,
) -> None:
    """Create a minimal document row so SSE has something to poll early."""
    from services.ingestion_service import freeze_snapshot

    now = datetime.utcnow()
    await mongo_writer.upsert_document(
        db,
        {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "user_id": user_id,
            "file_id": file_id,
            "filename": filename,
            "content_hash": content_hash,
            "source_mime": source_mime,
            "source_tier": source_tier.value,
            "ingestion_config": freeze_snapshot(ingestion_config),
            "chunking_config": chunking_config,
            "write_state": ws.model_dump(),
            "chunk_count": int((decision_trace or {}).get("child_count") or 0),
            "parent_count": len(parents),
            "parent_chunks": _build_parent_dicts(parents, None),
            "decision_trace": decision_trace or {},
            "decision_trace_summary": _decision_trace_summary(decision_trace),
            "ghost_b_staging": None,
            "ghost_b_failures": [],
            "ghost_b_metrics": {},
            "created_at": now,
            "updated_at": now,
        },
    )


async def _embed_batch_for_doc(
    *,
    children,
    summaries: list[SummaryResult] | None,
    config: IngestionConfig,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Combine child + summary text into ONE embed_batch call.

    Pulls per-corpus provider wiring from `config` (Phase 21): `embed_mode`,
    `embed_base_url`, `embed_api_key` (Fernet ciphertext — decrypted here),
    `embed_max_concurrent`, `modal_containers`. None values fall through to
    provider defaults / global env inside embedder.py.

    Returns:
        (child_vectors_by_chunk_id, summary_vectors_by_parent_id)
    """
    vector_children = [c for c in children if _is_vectorized_child(c)]
    skipped_children = len(children) - len(vector_children)
    if skipped_children:
        logger.info(
            "phase=embed_skip_kinds skipped=%d body=%d/%d",
            skipped_children,
            len(vector_children),
            len(children),
        )
    child_texts = [c.text for c in vector_children]
    summary_list = summaries or []
    # Filter out skipped parents (empty summary by design — they failed Ghost A
    # for token-budget reasons). Embedding empty strings either raises in some
    # backends or produces a degenerate vector that pollutes recall.
    embedded_summaries = [
        s for s in summary_list
        if getattr(s, "status", "ok") == "ok" and (s.summary or "").strip()
    ]
    summary_texts = [s.summary for s in embedded_summaries]
    all_texts = [*child_texts, *summary_texts]
    if not all_texts:
        return {}, {}

    all_vectors = await embed_batch(
        all_texts,
        mode=getattr(config, "embed_mode", "local"),
        expected_dim=getattr(config, "embedding_dimension", 1024),
        expected_model_id=getattr(config, "embedding_model_id", None),
        base_url=getattr(config, "embed_base_url", None),
        api_key=_plaintext_embed_api_key(config),
        max_concurrent=getattr(config, "embed_max_concurrent", None),
        modal_containers=getattr(config, "modal_containers", None),
        api_pool=_build_ghost_pool(getattr(config, "embedding_models", None)),
    )
    split = len(child_texts)
    child_vecs = all_vectors[:split]
    summary_vecs = all_vectors[split:]
    vec_map = {c.chunk_id: v for c, v in zip(vector_children, child_vecs)}
    summary_vec_map = {s.parent_id: v for s, v in zip(embedded_summaries, summary_vecs)}
    return vec_map, summary_vec_map


def _plaintext_embed_api_key(config: IngestionConfig) -> str | None:
    # Decrypt embed_api_key once per ingest; embed_batch sees plaintext only.
    raw_key = getattr(config, "embed_api_key", None)
    plaintext_key = _decrypt_api_key(raw_key) if raw_key else None
    if raw_key and plaintext_key is None:
        # Value stored but couldn't decrypt — most likely plaintext passed
        # through a migration shim. Pass as-is rather than failing the embed.
        plaintext_key = raw_key
    return plaintext_key


def _append_unique(items: list, value) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text and text not in items:
        items.append(text)


def _build_graph_vector_payloads(
    *,
    corpus_id: str,
    ghost_b_out: list[ExtractionResult] | None,
) -> tuple[list[dict], list[dict]]:
    """Build Qdrant graph-vector payloads from the same extraction structs Neo4j uses."""
    if not ghost_b_out:
        return [], []

    from services.ghost_b import normalize_relation_predicate_alias
    from services.graph.neo4j_writer import (
        canonicalize_entity_name,
        entity_id_from_name,
        relation_family_for_predicate,
        resolve_primary_entity_type,
    )

    entity_groups: dict[str, dict] = {}
    for result in ghost_b_out:
        for entity in result.entities:
            canonical = canonicalize_entity_name(entity.canonical_name)
            if not canonical:
                continue
            group = entity_groups.setdefault(
                canonical,
                {
                    "canonical_name": canonical,
                    "display_name": entity.surface_form or entity.canonical_name,
                    "observed_entity_types": [],
                    "aliases": [],
                    "descriptions": [],
                    "doc_ids": [],
                    "chunk_ids": [],
                    "confidence": 0.0,
                },
            )
            _append_unique(group["observed_entity_types"], entity.entity_type)
            for alias in getattr(entity, "aliases", []) or []:
                _append_unique(group["aliases"], alias)
            _append_unique(group["descriptions"], getattr(entity, "description", ""))
            _append_unique(group["doc_ids"], result.doc_id)
            _append_unique(group["chunk_ids"], result.chunk_id)
            confidence = float(getattr(entity, "confidence", 0.0) or 0.0)
            if confidence > float(group["confidence"] or 0.0):
                group["confidence"] = confidence
                group["display_name"] = entity.surface_form or entity.canonical_name

    entity_payloads: list[dict] = []
    for canonical, group in entity_groups.items():
        observed_types = list(group["observed_entity_types"])
        primary_type = resolve_primary_entity_type(canonical, observed_types)
        embedding_id = qdrant_writer.entity_embedding_id(corpus_id, canonical)
        entity_payloads.append(
            {
                "embedding_id": embedding_id,
                "entity_id": entity_id_from_name(canonical, primary_type),
                "canonical_name": canonical,
                "display_name": group["display_name"],
                "type": primary_type,
                "entity_type": primary_type,
                "aliases": group["aliases"],
                "description": max(group["descriptions"], key=len)
                if group["descriptions"]
                else "",
                "doc_ids": group["doc_ids"],
                "chunk_ids": group["chunk_ids"],
                "confidence": group["confidence"],
            }
        )

    relation_payloads: list[dict] = []
    for result in ghost_b_out:
        for relation in result.relations:
            if relation.object_kind != "entity":
                continue
            source_predicate = relation.source_predicate or relation.predicate
            normalized_source_predicate, reverse_relation = (
                normalize_relation_predicate_alias(source_predicate)
            )
            subject_name = relation.object if reverse_relation else relation.subject
            object_name = relation.subject if reverse_relation else relation.object
            subject_canonical = canonicalize_entity_name(subject_name)
            object_canonical = canonicalize_entity_name(object_name)
            if not subject_canonical or not object_canonical:
                continue
            predicate = relation.predicate
            predicate_family = getattr(
                relation, "predicate_family", None
            ) or relation_family_for_predicate(predicate)
            source_sentence = (
                getattr(relation, "source_sentence", "")
                or relation.evidence_phrase
                or relation.atomic_fact
                or ""
            )
            embedding_predicate = normalized_source_predicate or predicate
            embedding_id = qdrant_writer.relation_embedding_id(
                corpus_id,
                result.doc_id,
                result.chunk_id,
                subject_canonical,
                embedding_predicate,
                object_canonical,
            )
            relation_payloads.append(
                {
                    "embedding_id": embedding_id,
                    "relation_id": embedding_id,
                    "subject": subject_canonical,
                    "predicate": predicate,
                    "predicate_family": predicate_family,
                    "object": object_canonical,
                    "qualifier": getattr(relation, "qualifier", "") or "",
                    "confidence": float(getattr(relation, "confidence", 0.0) or 0.0),
                    "source_sentence": source_sentence,
                    "chunk_id": result.chunk_id,
                    "doc_id": result.doc_id,
                    "extraction_model": getattr(relation, "extraction_model", "") or "",
                    "repaired": bool(getattr(relation, "repaired", False)),
                }
            )

    return entity_payloads, relation_payloads


async def _write_graph_vectors_for_doc(
    *,
    qdrant_client: AsyncQdrantClient,
    corpus_id: str,
    ghost_b_out: list[ExtractionResult] | None,
    config: IngestionConfig,
) -> None:
    """Embed and write entity/relation vectors into the graph collection."""
    if "graph" not in config.target_qdrant_collections:
        return
    entity_payloads, relation_payloads = _build_graph_vector_payloads(
        corpus_id=corpus_id,
        ghost_b_out=ghost_b_out,
    )
    texts = [
        *[
            qdrant_writer.build_entity_vector_text(payload)
            for payload in entity_payloads
        ],
        *[
            qdrant_writer.build_relation_vector_text(payload)
            for payload in relation_payloads
        ],
    ]
    if not texts:
        return

    vectors = await embed_batch(
        texts,
        mode=getattr(config, "embed_mode", "local"),
        expected_dim=getattr(config, "embedding_dimension", 1024),
        expected_model_id=getattr(config, "embedding_model_id", None),
        base_url=getattr(config, "embed_base_url", None),
        api_key=_plaintext_embed_api_key(config),
        max_concurrent=getattr(config, "embed_max_concurrent", None),
        modal_containers=getattr(config, "modal_containers", None),
        api_pool=_build_ghost_pool(getattr(config, "embedding_models", None)),
    )
    split = len(entity_payloads)
    entity_vectors = vectors[:split]
    relation_vectors = vectors[split:]
    entity_count = await qdrant_writer.upsert_graph_entities(
        qdrant_client,
        corpus_id,
        entity_payloads,
        entity_vectors,
    )
    relation_count = await qdrant_writer.upsert_graph_relations(
        qdrant_client,
        corpus_id,
        relation_payloads,
        relation_vectors,
    )
    logger.info(
        "phase=graph_vectors corpus=%s entities=%d relations=%d",
        corpus_id[:8],
        entity_count,
        relation_count,
    )


async def _write_qdrant_for_doc(
    *,
    qdrant_client: AsyncQdrantClient,
    corpus_id: str,
    user_id: str,
    parents,
    children,
    vec_map: dict[str, list[float]],
    summaries: list[SummaryResult] | None,
    summary_vec_map: dict[str, list[float]],
    config: IngestionConfig,
    child_sparse_map: dict[str, Any] | None = None,
    summary_sparse_map: dict[str, Any] | None = None,
) -> None:
    """Write children + summaries to per-corpus Qdrant collections.

    Children: naive (always) / hrag (tier-filtered) / graph (all) — gated
    against the corpus's `target_qdrant_collections` list to preserve
    existing semantics for corpora that opt out of any kind.
    Summaries: naive + hrag only (qdrant_writer.upsert_summaries also
    enforces this defensively).

    Sparse vectors (BM25) are passed through verbatim. New per-corpus
    collections store them under a "sparse" named-vector slot; legacy
    collections silently drop them.
    """
    target_cols = config.target_qdrant_collections
    child_sparse_map = child_sparse_map or {}
    summary_sparse_map = summary_sparse_map or {}
    vector_children = [c for c in children if c.chunk_id in vec_map]

    def _as_payload(c) -> dict:
        return {
            "chunk_id": c.chunk_id,
            "parent_id": c.parent_id,
            "doc_id": c.doc_id,
            "corpus_id": c.corpus_id,
            "user_id": user_id,
            "text": c.text,
            "source_tier": c.source_tier,
            "heading_path": c.heading_path,
            "page_start": getattr(c, "page_start", None),
            "page_end": getattr(c, "page_end", None),
            "chunk_kind": getattr(c, "chunk_kind", ChunkKind.BODY),
        }

    if "naive" in target_cols:
        dicts = [_as_payload(c) for c in vector_children]
        vecs = [vec_map[c.chunk_id] for c in vector_children]
        sparse = [child_sparse_map.get(c.chunk_id) for c in vector_children]
        await qdrant_writer.upsert_children(
            qdrant_client,
            corpus_id,
            dicts,
            vecs,
            ["naive"],
            sparse_vectors=sparse,
        )

    hrag_eligible = [c for c in vector_children if c.source_tier in _HRAG_TIERS]
    if "hrag" in target_cols and hrag_eligible:
        dicts = [_as_payload(c) for c in hrag_eligible]
        vecs = [vec_map[c.chunk_id] for c in hrag_eligible]
        sparse = [child_sparse_map.get(c.chunk_id) for c in hrag_eligible]
        await qdrant_writer.upsert_children(
            qdrant_client,
            corpus_id,
            dicts,
            vecs,
            ["hrag"],
            sparse_vectors=sparse,
        )

    if "graph" in target_cols:
        dicts = [_as_payload(c) for c in vector_children]
        vecs = [vec_map[c.chunk_id] for c in vector_children]
        sparse = [child_sparse_map.get(c.chunk_id) for c in vector_children]
        await qdrant_writer.upsert_children(
            qdrant_client,
            corpus_id,
            dicts,
            vecs,
            ["graph"],
            sparse_vectors=sparse,
        )

    if summaries:
        hp_map = {p.parent_id: p.heading_path for p in parents}
        kind_map = {
            p.parent_id: getattr(p, "chunk_kind", ChunkKind.BODY) for p in parents
        }
        summary_payloads = [
            {
                "parent_id": s.parent_id,
                "doc_id": s.doc_id,
                "corpus_id": s.corpus_id,
                "source_tier": s.source_tier,
                "summary": s.summary,
                "heading_path": hp_map.get(s.parent_id),
                "user_id": user_id,
                "chunk_kind": kind_map.get(s.parent_id, ChunkKind.BODY),
            }
            for s in summaries
        ]
        summary_vecs = [summary_vec_map[s.parent_id] for s in summaries]
        summary_sparse = [summary_sparse_map.get(s.parent_id) for s in summaries]
        summary_kinds = [k for k in target_cols if k in ("naive", "hrag")]
        await qdrant_writer.upsert_summaries(
            qdrant_client,
            corpus_id,
            summary_payloads,
            summary_vecs,
            summary_kinds,
            sparse_vectors=summary_sparse,
        )


async def _write_neo4j_for_doc(
    *,
    neo4j_driver,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    file_id: str,
    children,
    ghost_b_out: list[ExtractionResult] | None,
    summaries: list[SummaryResult] | None = None,
) -> None:
    """Delegate to neo4j_writer.write_document_graph with Ghost B output."""
    from services.graph.neo4j_writer import write_document_graph

    await write_document_graph(
        driver=neo4j_driver,
        doc_id=doc_id,
        corpus_id=corpus_id,
        extraction_results=ghost_b_out or [],
        user_id=user_id,
        file_id=file_id,
        all_chunk_ids=[c.chunk_id for c in children],
        summaries=summaries,
    )


async def recover_vector_from_mongo(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    corpus_id: str,
    doc_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Finish Qdrant/vector readiness for a document already stored in Mongo.

    This is the MONGO_ONLY recovery lane: no parse, no rechunk, no Ghost B,
    and no semantic retries. It reuses stored chunks/parents and idempotently
    upserts vectors into Qdrant.
    """
    doc = await mongo_reader.get_document(db, doc_id, corpus_id=corpus_id)
    if not doc:
        raise ValueError("Document not found")
    ws = WriteState(**(doc.get("write_state") or {}))
    if not ws.mongo_written:
        raise RuntimeError("Document is not Mongo-ready; cannot recover vectors")
    if ws.qdrant_written or ws.vector_ready:
        return {
            "status": "noop",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "vector_ready": True,
            "qdrant_written": True,
        }

    from services.ingestion.tier_chunker import ChildChunk, ParentChunk
    from services.ingestion_service import build_effective_config

    corpus = await mongo_reader.get_corpus(db, corpus_id)
    live_cfg = (corpus or {}).get("default_ingestion_config") or {}
    config = build_effective_config(
        frozen_base=doc.get("ingestion_config") or live_cfg,
        live_corpus=live_cfg,
        ingest_overrides=None,
    )
    parent_rows = doc.get("parent_chunks") or []
    chunk_rows = (
        await db["chunks"]
        .find(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"_id": 0},
        )
        .to_list(length=None)
    )
    if not chunk_rows:
        raise RuntimeError("No stored child chunks found for vector recovery")

    children = [
        ChildChunk(
            chunk_id=str(row.get("chunk_id") or ""),
            parent_id=str(row.get("parent_id") or ""),
            doc_id=doc_id,
            corpus_id=corpus_id,
            text=str(row.get("text") or ""),
            heading_path=row.get("heading_path"),
            source_tier=str(row.get("source_tier") or SourceTier.TIER_C.value),
            token_count=int(row.get("token_count") or 0),
            page_start=row.get("page_start"),
            page_end=row.get("page_end"),
            chunk_kind=str(row.get("chunk_kind") or ChunkKind.BODY),
        )
        for row in chunk_rows
        if row.get("chunk_id")
    ]
    child_by_parent: dict[str, list[ChildChunk]] = {}
    for child in children:
        child_by_parent.setdefault(child.parent_id, []).append(child)
    parents = [
        ParentChunk(
            parent_id=str(row.get("parent_id") or ""),
            doc_id=doc_id,
            corpus_id=corpus_id,
            text=str(row.get("text") or ""),
            heading_path=row.get("heading_path"),
            source_tier=str(row.get("source_tier") or SourceTier.TIER_C.value),
            children=child_by_parent.get(str(row.get("parent_id") or ""), []),
            page_start=row.get("page_start"),
            page_end=row.get("page_end"),
            chunk_kind=str(row.get("chunk_kind") or ChunkKind.BODY),
        )
        for row in parent_rows
        if row.get("parent_id")
    ]
    summaries = _reconstruct_summaries_from_mongo(parents, parent_rows)

    async with _pre_vector_model_semaphore(config):
        vec_map, summary_vec_map = await _embed_batch_for_doc(
            children=children,
            summaries=summaries,
            config=config,
        )

    from services.storage.sparse_encoder import encode_text as _bm25_encode

    child_sparse_map = {
        child.chunk_id: _bm25_encode(child.text)
        for child in children
        if child.chunk_id in vec_map
    }
    summary_sparse_map = {
        summary.parent_id: _bm25_encode(summary.summary) for summary in summaries
    }
    await _write_qdrant_for_doc(
        qdrant_client=qdrant_client,
        corpus_id=corpus_id,
        user_id=user_id,
        parents=parents,
        children=children,
        vec_map=vec_map,
        summaries=summaries,
        summary_vec_map=summary_vec_map,
        config=config,
        child_sparse_map=child_sparse_map,
        summary_sparse_map=summary_sparse_map,
    )
    now = datetime.utcnow()
    await mongo_writer.update_write_state(
        db,
        doc_id,
        corpus_id=corpus_id,
        qdrant_written=True,
        vector_ready=True,
    )
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "decision_trace.vector_ready": True,
                "decision_trace.vector_recovered_at": now,
                "updated_at": now,
            },
            "$addToSet": {
                "write_state.warnings": "Vector recovery completed from stored Mongo chunks without reparsing."
            },
        },
    )
    return {
        "status": "done",
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "vector_ready": True,
        "qdrant_written": True,
        "children_vectorized": len(vec_map),
        "summaries_vectorized": len(summary_vec_map),
    }


async def _auto_backfill_graph_failures_once(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    ws: WriteState,
) -> WriteState:
    """Run one controlled Ghost B backfill pass before the ingest job completes.

    `partial` is a valid internal recovery state, but production ingestion
    should not make the user manually schedule retry work. This uses the
    existing persisted `ghost_b_failures` list, retries only those chunks, and
    patches Neo4j incrementally. Remaining failures stay visible for audit.
    """
    if neo4j_driver is None:
        return ws

    doc = await mongo_reader.get_document(db, doc_id, corpus_id=corpus_id)
    failures = (doc or {}).get("ghost_b_failures") or []
    if not failures:
        return ws
    retry_after_values: list[datetime] = []
    for row in failures:
        raw = row.get("retry_after") if isinstance(row, dict) else None
        if isinstance(raw, datetime):
            retry_after_values.append(raw)
        elif raw:
            try:
                retry_after_values.append(datetime.fromisoformat(str(raw)))
            except Exception:
                pass
    if retry_after_values:
        retry_after = min(retry_after_values)
        if retry_after > datetime.utcnow():
            await mongo_writer.update_write_state(
                db,
                doc_id,
                corpus_id=corpus_id,
                graph_status=GRAPH_RETRY_SCHEDULED,
                graph_retry_after=retry_after,
                graph_retryable_failed_chunk_count=len(failures),
            )
            ws.graph_status = GRAPH_RETRY_SCHEDULED
            ws.graph_retry_after = retry_after
            ws.graph_retryable_failed_chunk_count = len(failures)
            logger.info(
                "phase=ghost_b_auto_backfill_skip reason=retry_after doc=%s corpus=%s retry_after=%s failures=%d",
                doc_id[:12],
                corpus_id[:8],
                retry_after.isoformat(),
                len(failures),
            )
            return ws

    async with _AUTO_BACKFILL_SEMAPHORE:
        # Another job or manual endpoint may have recovered this document while
        # we waited for the global backfill lane.
        latest = await mongo_reader.get_document(db, doc_id, corpus_id=corpus_id)
        if not ((latest or {}).get("ghost_b_failures") or []):
            if latest and latest.get("write_state"):
                return WriteState(**latest["write_state"])
            return ws

        from services.ingestion.graph_backfill import backfill_failed_graph_chunks

        t0 = time.monotonic()
        try:
            result = await backfill_failed_graph_chunks(
                db=db,
                qdrant_client=qdrant_client,
                neo4j_driver=neo4j_driver,
                corpus_id=corpus_id,
                doc_id=doc_id,
                user_id=user_id,
            )
            logger.info(
                "phase=ghost_b_auto_backfill duration=%.2fs doc=%s corpus=%s retried=%d recovered=%d remaining=%d",
                time.monotonic() - t0,
                doc_id[:12],
                corpus_id[:8],
                int(result.get("retried_chunks") or 0),
                int(result.get("recovered_chunks") or 0),
                int(result.get("remaining_failed_chunks") or 0),
            )
        except Exception as exc:
            logger.exception(
                "phase=ghost_b_auto_backfill_failed doc=%s corpus=%s: %s",
                doc_id[:12],
                corpus_id[:8],
                exc,
            )
            warnings = _merge_warnings(
                list(ws.warnings or []),
                [f"Ghost B auto-backfill failed: {str(exc)[:500]}"],
            )
            await mongo_writer.update_write_state(
                db,
                doc_id,
                corpus_id=corpus_id,
                warnings=warnings,
                graph_status=GRAPH_NEEDS_BACKFILL,
            )
            ws.warnings = warnings
            ws.graph_status = GRAPH_NEEDS_BACKFILL
            return ws

        refreshed = await mongo_reader.get_document(db, doc_id, corpus_id=corpus_id)
        if refreshed and refreshed.get("write_state"):
            return WriteState(**refreshed["write_state"])
    return ws


async def run_ingest_job(
    job_id: str,
    data: bytes,
    filename: str,
    corpus_id: str,
    user_id: str,
    ingestion_config: IngestionConfig,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver,
    model: str,
    ingest_overrides: dict | None = None,
    source_mime: str | None = None,
    cancel_check=None,
    # Phase K — called with the resolved doc_id as soon as docling parse
    # completes, BEFORE the expensive ghost + embed + write phases run.
    # The HTTP endpoint uses this to return {doc_id, status: "queued"} in
    # under ~2s even when the full pipeline will run for 30+ minutes.
    on_doc_id: "Callable[[str], None] | None" = None,
) -> IngestJobResponse:
    """Run the locked ingestion pipeline for a single document.

    Idempotent: re-running a completed job is a no-op; partial state resumes
    from the first incomplete phase (Decision D).

    Late-bound config resolution (Phase 21):
      1. Load live corpus from Mongo.
      2. Structural identity (FROZEN) comes from the doc snapshot on resume,
         else from the corpus's current default config.
      3. Operational wiring (MUTABLE: embed_*, pools, concurrency) comes
         from the live corpus record — NEVER from the doc snapshot.
      4. `ingest_overrides` (ephemeral) is applied last, shadowing corpus
         values for this ingest only. Not persisted.
    """

    cid8 = corpus_id[:8]
    content_hash = hashlib.sha256(data).hexdigest()

    # Load live corpus + build effective config (Phase 21). The `corpus` doc
    # carries unmasked ciphertext for embed_api_key / pool api_keys; worker
    # downstream helpers decrypt at dispatch time.
    from services.ingestion_service import build_effective_config, freeze_snapshot

    corpus_doc = await mongo_reader.get_corpus(db, corpus_id)
    if corpus_doc is None:
        raise ValueError(f"Corpus not found: {corpus_id}")
    live_corpus_cfg = corpus_doc.get("default_ingestion_config") or {}
    # `ingestion_config` passed in by the caller already has per-request
    # frozen-field overrides (use_neo4j/chunk_summarization form params);
    # treat it as the frozen baseline. The live corpus supplies mutable
    # fields; ingest_overrides layers on top.
    frozen_base = ingestion_config.model_dump()
    effective_config = build_effective_config(
        frozen_base=frozen_base,
        live_corpus=live_corpus_cfg,
        ingest_overrides=ingest_overrides,
    )
    # Rebind the name so all downstream reads use the effective config.
    ingestion_config = effective_config

    # ── Phase 1: Parse ───────────────────────────────────────────────────
    await _raise_if_cancelled(cancel_check)
    async with _PARSE_SEMAPHORE:
        t0 = time.monotonic()
        mime_hint = str(source_mime or "").split(";", 1)[0].strip().lower()
        if not mime_hint:
            mime_hint, _ = mimetypes.guess_type(filename)
        parse_result = await docling_adapter.parse_document(
            data,
            filename=filename,
            mime=mime_hint or "application/octet-stream",
            do_ocr=False,
        )
    _norm = await asyncio.to_thread(
        _normalize_document_text_for_id,
        parse_result.markdown or parse_result.text or "",
    )
    doc_id = hashlib.sha256(_norm.encode("utf-8")).hexdigest()
    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
    source_tier = parse_result.source_tier
    source_mime = mime_hint or "application/octet-stream"
    logger.info(
        "phase=parse duration=%.2fs doc=%s corpus=%s tier=%s",
        time.monotonic() - t0,
        doc_id[:12],
        cid8,
        source_tier.value,
    )

    # ── Phase 2: Chunk ───────────────────────────────────────────────────
    t0 = time.monotonic()
    parents, children, injected_headers = await asyncio.to_thread(
        _chunk_parse_result,
        parse_result=parse_result,
        doc_id=doc_id,
        corpus_id=corpus_id,
        config=ingestion_config,
    )
    chunking_config = tier_chunker.describe_chunking(parse_result, ingestion_config)
    if injected_headers:
        chunking_config["injected_headers"] = [
            {
                "line_no": h.line_no,
                "level": h.level,
                "pattern": h.pattern,
                "original_line": h.original_line,
            }
            for h in injected_headers
        ]
    logger.info(
        "phase=chunk duration=%.2fs doc=%s corpus=%s parents=%d children=%d injected=%d",
        time.monotonic() - t0,
        doc_id[:12],
        cid8,
        len(parents),
        len(children),
        len(injected_headers),
    )

    # ── Resume: existing write_state ─────────────────────────────────────
    existing_doc = await mongo_reader.get_document(db, doc_id, corpus_id=corpus_id)
    if existing_doc and existing_doc.get("write_state"):
        ws = WriteState(**existing_doc["write_state"])
    else:
        ws = WriteState()
    ws = _refresh_derived_write_state(ws, config=ingestion_config)
    decision_trace = _build_decision_trace(
        parse_result=parse_result,
        source_mime=source_mime,
        filename=filename,
        source_tier=source_tier,
        chunking_config=chunking_config,
        parents=parents,
        children=children,
        config=ingestion_config,
        ws=ws,
        ghost_b_metrics=(existing_doc or {}).get("ghost_b_metrics")
        if existing_doc
        else None,
    )
    file_id = (
        existing_doc.get("file_id", str(uuid.uuid4()))
        if existing_doc
        else str(uuid.uuid4())
    )
    if existing_doc is None:
        await _ensure_progress_document(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            file_id=file_id,
            filename=filename,
            content_hash=content_hash,
            source_tier=source_tier,
            source_mime=source_mime,
            ingestion_config=ingestion_config,
            chunking_config=chunking_config,
            parents=parents,
            decision_trace=decision_trace,
            ws=ws,
        )
    elif not existing_doc.get("decision_trace"):
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "$set": {
                    "decision_trace": decision_trace,
                    "decision_trace_summary": _decision_trace_summary(decision_trace),
                    "content_hash": existing_doc.get("content_hash") or content_hash,
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    # Phase K — signal the HTTP endpoint only after a progress row exists, so
    # the frontend/SSE never observes a real running job as "not found".
    if on_doc_id is not None:
        try:
            on_doc_id(doc_id)
        except Exception as _exc:
            logger.debug("on_doc_id callback raised: %s", _exc)

    # ── Phase 3: Ghost A summary lane ───────────────────────────────────
    # Ghost B intentionally does not run here. Mongo/Qdrant must become
    # usable for vector RAG before graph enrichment starts.
    try:
        async with _pre_vector_model_semaphore(ingestion_config):
            t0 = time.monotonic()
            ghost_result = await _run_ghosts_parallel(
                config=ingestion_config,
                parents=parents,
                children=children,
                doc_id=doc_id,
                corpus_id=corpus_id,
                filename=filename,
                model=model,
                db=db,
                qdrant_client=qdrant_client,
                neo4j_driver=neo4j_driver,
                existing_doc=existing_doc,
                ws=ws,
                include_ghost_a=True,
                include_ghost_b=False,
            )
        if isinstance(ghost_result, GhostRunResult):
            summaries = ghost_result.summaries
            ingest_warnings = ghost_result.warnings
        else:
            # Backward-compatible path for older tests/mocks that still return
            # the pre-metrics two-tuple.
            ghost_tuple = tuple(ghost_result)
            summaries = ghost_tuple[0] if len(ghost_tuple) > 0 else None
            ingest_warnings = ghost_tuple[2] if len(ghost_tuple) > 2 else []
    except GhostAFailure as exc:
        # Document record exists from _ensure_progress_document above.
        # Save the warning so the UI shows why this doc failed, then
        # re-raise so the batch queue marks the item as failed.
        ws.warnings = _merge_warnings(
            ws.warnings,
            [f"Ghost A summarization failed: {str(exc)[:500]}"],
        )
        await mongo_writer.update_write_state(
            db,
            doc_id,
            corpus_id=corpus_id,
            warnings=ws.warnings,
        )
        logger.exception(
            "phase=ghost_a_failed doc=%s corpus=%s",
            doc_id[:12],
            cid8,
        )
        raise
    ghost_b_out: list[ExtractionResult] | None = None
    ghost_b_failures: list[ExtractionFailureItem] = []
    ghost_b_metrics: dict | None = None
    relation_repair_candidates: list[RelationRepairCandidate] = []
    ws.warnings = _merge_warnings(ws.warnings, ingest_warnings)
    logger.info(
        "phase=ghost_a duration=%.2fs doc=%s corpus=%s status=%s warnings=%d",
        time.monotonic() - t0,
        doc_id[:12],
        cid8,
        "ok" if summaries is not None else "skipped",
        len(ingest_warnings),
    )

    # ── Phase 4: Mongo (ONE write pass, inline summaries) ────────────────
    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
    if not ws.mongo_written:
        t0 = time.monotonic()
        await _write_mongo_all(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            file_id=file_id,
            filename=filename,
            content_hash=content_hash,
            source_tier=source_tier,
            source_mime=source_mime,
            ingestion_config=ingestion_config,
            chunking_config=chunking_config,
            parents=parents,
            children=children,
            summaries=summaries,
            ghost_b_out=ghost_b_out,
            ghost_b_failures=ghost_b_failures,
            ghost_b_metrics=ghost_b_metrics,
            decision_trace=decision_trace,
            ws=ws,
        )
        await mongo_writer.update_write_state(
            db,
            doc_id,
            corpus_id=corpus_id,
            mongo_written=True,
            warnings=ws.warnings,
        )
        ws.mongo_written = True
        logger.info(
            "phase=mongo duration=%.2fs doc=%s corpus=%s parents=%d children=%d summaries=%d",
            time.monotonic() - t0,
            doc_id[:12],
            cid8,
            len(parents),
            len(children),
            len(summaries or []),
        )
    elif ingest_warnings or ghost_b_failures or ghost_b_metrics:
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "$set": {
                    "write_state.warnings": ws.warnings,
                    "ghost_b_failures": [asdict(f) for f in ghost_b_failures],
                    "ghost_b_metrics": ghost_b_metrics or {},
                    "schema_lens": (ghost_b_metrics or {}).get("schema_lens"),
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    # ── Phase 5: Embed + Phase 6: Qdrant ─────────────────────────────────
    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
    if not ws.qdrant_written:
        async with _pre_vector_model_semaphore(ingestion_config):
            t0 = time.monotonic()
            vec_map, summary_vec_map = await _embed_batch_for_doc(
                children=children,
                summaries=summaries,
                config=ingestion_config,
            )
        logger.info(
            "phase=embed duration=%.2fs doc=%s corpus=%s mode=%s children=%d summaries=%d",
            time.monotonic() - t0,
            doc_id[:12],
            cid8,
            getattr(ingestion_config, "embed_mode", "local_st"),
            len(vec_map),
            len(summary_vec_map),
        )

        # Sparse vectors for Qdrant hybrid search. Pure-Python BM25 with
        # server-side IDF — no GPU, no model load. New corpora store these
        # alongside the dense vector under the "sparse" named slot;
        # legacy corpora's collections silently drop the sparse field at
        # upsert time and keep the Mongo $text fallback in place.
        from services.storage.sparse_encoder import encode_text as _bm25_encode

        t0 = time.monotonic()
        child_sparse_map = {
            c.chunk_id: _bm25_encode(c.text) for c in children if c.chunk_id in vec_map
        }
        summary_sparse_map = {
            s.parent_id: _bm25_encode(s.summary) for s in (summaries or [])
        }
        logger.info(
            "phase=sparse_encode duration=%.2fs doc=%s corpus=%s children=%d summaries=%d",
            time.monotonic() - t0,
            doc_id[:12],
            cid8,
            len(child_sparse_map),
            len(summary_sparse_map),
        )

        t0 = time.monotonic()
        await _write_qdrant_for_doc(
            qdrant_client=qdrant_client,
            corpus_id=corpus_id,
            user_id=user_id,
            parents=parents,
            children=children,
            vec_map=vec_map,
            summaries=summaries,
            summary_vec_map=summary_vec_map,
            config=ingestion_config,
            child_sparse_map=child_sparse_map,
            summary_sparse_map=summary_sparse_map,
        )
        try:
            await _raise_if_cancelled(cancel_check, doc_id=doc_id)
        except IngestCancelled:
            await qdrant_writer.delete_points_by_doc(qdrant_client, corpus_id, doc_id)
            raise
        await mongo_writer.update_write_state(
            db, doc_id, corpus_id=corpus_id, qdrant_written=True, vector_ready=True
        )
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "$set": {
                    "decision_trace.vector_ready": True,
                    "decision_trace.graph_status": ws.graph_status,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        ws.qdrant_written = True
        ws.vector_ready = True
        logger.info(
            "phase=qdrant duration=%.2fs doc=%s corpus=%s targets=%s",
            time.monotonic() - t0,
            doc_id[:12],
            cid8,
            ",".join(ingestion_config.target_qdrant_collections),
        )
    else:
        ws.vector_ready = bool(
            ws.vector_ready or (ws.mongo_written and ws.qdrant_written)
        )

    # ── Phase 7: Graph enrichment lane (optional) ────────────────────────
    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
    if not _graph_enabled(ingestion_config):
        if ws.graph_status != GRAPH_SKIPPED:
            await _update_graph_write_state(
                db=db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                ws=ws,
                status=GRAPH_SKIPPED,
                finished_at=datetime.utcnow(),
            )
    elif not ws.neo4j_written:
        if neo4j_driver is None:
            ws.warnings = _merge_warnings(
                ws.warnings,
                [
                    "Neo4j enabled but graph driver is unavailable; graph extraction is pending."
                ],
            )
            await mongo_writer.update_write_state(
                db,
                doc_id,
                corpus_id=corpus_id,
                warnings=ws.warnings,
                graph_status=GRAPH_PENDING,
            )
            ws.graph_status = GRAPH_PENDING
            logger.warning(
                "phase=graph_enrichment doc=%s corpus=%s status=driver_missing",
                doc_id[:12],
                cid8,
            )
        else:
            started = datetime.utcnow()
            await _update_graph_write_state(
                db=db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                ws=ws,
                status=GRAPH_EXTRACTING,
                started_at=started,
            )
            try:
                async with _GRAPH_MODEL_PHASE_SEMAPHORE:
                    t0 = time.monotonic()
                    graph_result = await _run_ghosts_parallel(
                        config=ingestion_config,
                        parents=parents,
                        children=children,
                        doc_id=doc_id,
                        corpus_id=corpus_id,
                        filename=filename,
                        model=model,
                        db=db,
                        qdrant_client=qdrant_client,
                        neo4j_driver=neo4j_driver,
                        existing_doc=existing_doc,
                        ws=ws,
                        include_ghost_a=False,
                        include_ghost_b=True,
                    )
                if isinstance(graph_result, GhostRunResult):
                    ghost_b_out = graph_result.ghost_b_out
                    graph_warnings = graph_result.warnings
                    ghost_b_failures = graph_result.ghost_b_failures
                    ghost_b_metrics = graph_result.ghost_b_metrics
                    relation_repair_candidates = graph_result.relation_repair_candidates
                else:
                    graph_tuple = tuple(graph_result)
                    ghost_b_out = graph_tuple[1] if len(graph_tuple) > 1 else None
                    graph_warnings = graph_tuple[2] if len(graph_tuple) > 2 else []
                    ghost_b_failures = graph_tuple[3] if len(graph_tuple) > 3 else []
                    ghost_b_metrics = (
                        graph_tuple[4]
                        if len(graph_tuple) > 4
                        else _ghost_b_metrics_for_skipped(ghost_b_out)
                    )
                    relation_repair_candidates = []
                await _raise_if_cancelled(cancel_check, doc_id=doc_id)
                ws.warnings = _merge_warnings(ws.warnings, graph_warnings)
                await _persist_graph_extraction(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    ghost_b_out=ghost_b_out,
                    ghost_b_failures=ghost_b_failures,
                    ghost_b_metrics=ghost_b_metrics,
                    warnings=ws.warnings,
                )
                if ghost_b_out is None:
                    raise GhostBFailure(
                        "Ghost B graph enrichment returned no extraction output"
                    )
                await _raise_if_cancelled(cancel_check, doc_id=doc_id)

                repair_state = await _enqueue_relation_repairs_for_doc(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    candidates=relation_repair_candidates,
                    config=ingestion_config,
                )
                counts = repair_state.get("counts") or {}
                ws.repair_status = repair_state.get("status")
                ws.repair_total = int(counts.get("total") or 0)
                ws.repair_pending = int(counts.get("pending") or 0)
                ws.repair_succeeded = int(counts.get("succeeded") or 0)
                ws.repair_discarded = int(counts.get("discarded") or 0)
                ws.repair_failed = int(counts.get("terminal_failed") or 0)
                if ghost_b_metrics is None:
                    ghost_b_metrics = {}
                ghost_b_metrics = dict(ghost_b_metrics)
                ghost_b_metrics["relation_repair_pending_count"] = ws.repair_pending
                ghost_b_metrics["relation_repair_total_count"] = ws.repair_total

                await _write_graph_vectors_for_doc(
                    qdrant_client=qdrant_client,
                    corpus_id=corpus_id,
                    ghost_b_out=ghost_b_out,
                    config=ingestion_config,
                )
                try:
                    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
                except IngestCancelled:
                    await qdrant_writer.delete_points_by_doc(
                        qdrant_client, corpus_id, doc_id
                    )
                    raise
                try:
                    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
                except IngestCancelled:
                    await qdrant_writer.delete_points_by_doc(
                        qdrant_client, corpus_id, doc_id
                    )
                    raise
                await _write_neo4j_for_doc(
                    neo4j_driver=neo4j_driver,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    user_id=user_id,
                    file_id=file_id,
                    children=children,
                    ghost_b_out=ghost_b_out,
                    summaries=summaries,
                )
                try:
                    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
                except IngestCancelled:
                    async with neo4j_driver.session() as session:
                        await session.run(
                            "MATCH (n {doc_id: $doc_id, corpus_id: $corpus_id}) "
                            "DETACH DELETE n",
                            doc_id=doc_id,
                            corpus_id=corpus_id,
                        )
                    raise
                try:
                    from services.graph.entity_quality import mark_graph_metrics_stale

                    await mark_graph_metrics_stale(
                        db,
                        corpus_id,
                        reason="graph_enrichment_write",
                    )
                except Exception as exc:
                    logger.warning(
                        "phase=graph_cache_stale doc=%s corpus=%s failed: %s",
                        doc_id[:12],
                        cid8,
                        exc,
                    )
                graph_status = _graph_status_after_extraction(
                    ghost_b_out=ghost_b_out,
                    ghost_b_failures=ghost_b_failures,
                    ghost_b_metrics=ghost_b_metrics,
                )
                await mongo_writer.update_write_state(
                    db, doc_id, corpus_id=corpus_id, neo4j_written=True
                )
                ws.neo4j_written = True
                await _update_graph_write_state(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    ws=ws,
                    status=graph_status,
                    ghost_b_out=ghost_b_out,
                    ghost_b_failures=ghost_b_failures,
                    ghost_b_metrics=ghost_b_metrics,
                    finished_at=datetime.utcnow(),
                )
                logger.info(
                    "phase=graph_enrichment duration=%.2fs doc=%s corpus=%s status=%s extractions=%d failures=%d",
                    time.monotonic() - t0,
                    doc_id[:12],
                    cid8,
                    graph_status,
                    len(ghost_b_out),
                    len(ghost_b_failures),
                )
            except IngestCancelled:
                raise
            except Exception as exc:
                ws.warnings = _merge_warnings(
                    ws.warnings,
                    [
                        f"Ghost B graph enrichment failed after vector_ready: {str(exc)[:500]}"
                    ],
                )
                await _persist_graph_extraction(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    ghost_b_out=ghost_b_out,
                    ghost_b_failures=ghost_b_failures,
                    ghost_b_metrics=ghost_b_metrics,
                    warnings=ws.warnings,
                )
                await _update_graph_write_state(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    ws=ws,
                    status=GRAPH_NEEDS_BACKFILL,
                    ghost_b_out=ghost_b_out,
                    ghost_b_failures=ghost_b_failures,
                    ghost_b_metrics=ghost_b_metrics,
                    finished_at=datetime.utcnow(),
                )
                logger.exception(
                    "phase=graph_enrichment_failed doc=%s corpus=%s: %s",
                    doc_id[:12],
                    cid8,
                    exc,
                )

    await _raise_if_cancelled(cancel_check, doc_id=doc_id)
    if (
        _graph_enabled(ingestion_config)
        and ws.mongo_written
        and ws.qdrant_written
        and ws.neo4j_written
    ):
        ws = await _auto_backfill_graph_failures_once(
            db=db,
            qdrant_client=qdrant_client,
            neo4j_driver=neo4j_driver,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            ws=ws,
        )

    # Corpus counters — only increment on a genuinely fresh ingest.
    if ws.mongo_written and not existing_doc:
        await db["corpora"].update_one(
            {"corpus_id": corpus_id},
            {
                "$inc": {"doc_count": 1, "chunk_count": len(children)},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )

    # ── Phase 8: Post-write verification ────────────────────────────────
    # Cross-store consistency check. Non-fatal — failures are recorded on
    # write_state so the UI can surface a ⚠ badge without breaking the
    # ingest flow.
    try:
        from services.ingestion.verify import verify_ingest

        ok, verify_errors = await verify_ingest(
            db=db,
            qdrant=qdrant_client,
            neo4j_driver=neo4j_driver,
            doc_id=doc_id,
            corpus_id=corpus_id,
            target_qdrant_collections=ingestion_config.target_qdrant_collections,
            use_neo4j=bool(_graph_enabled(ingestion_config) and ws.neo4j_written),
        )
        await mongo_writer.update_write_state(
            db,
            doc_id,
            corpus_id=corpus_id,
            verified=ok,
            verify_errors=verify_errors,
        )
        ws.verified = ok
        ws.verify_errors = verify_errors
    except Exception as exc:
        logger.warning(
            "phase=verify doc=%s corpus=%s crashed: %s",
            doc_id[:12],
            corpus_id[:8],
            exc,
        )

    if ws.qdrant_written and ws.neo4j_written:
        try:
            from services.graph.orchestrator import schedule_graph_discovery_cache_warm

            schedule_graph_discovery_cache_warm(
                qdrant=qdrant_client,
                neo4j_driver=neo4j_driver,
                db=db,
                corpus_id=corpus_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning(
                "phase=graph_cache_warm doc=%s corpus=%s schedule_failed: %s",
                doc_id[:12],
                corpus_id[:8],
                exc,
            )

    return IngestJobResponse(
        job_id=job_id,
        doc_id=doc_id,
        corpus_id=corpus_id,
        filename=filename,
        source_tier=source_tier.value,
        status="done",
        write_state=ws,
        chunk_count=len(children),
        parent_count=len(parents),
    )
