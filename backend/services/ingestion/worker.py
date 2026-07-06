"""
Ingestion pipeline worker — locked pipeline order:

  1. Parse     → docling_adapter.parse_document
  2. Chunk     → tier_chunker.chunk (parents + children)
  3. Mongo     → compact progress doc + parent/chunk checkpoints
  4. Ghosts    → summary then extraction under the model-phase semaphore
                 Ghost A runs iff chunk_summarization=True.
                 Ghost B runs iff use_neo4j=True.
                 Either branch is a no-op (returns None) when its flag is off.
  5. Mongo     → compact document metadata + parent summaries +
                 Ghost B extraction rows. Flip mongo_written.
  6. Embed     → one embed_batch call over children+summary texts.
                 mode / dim / model-id come from ingestion_config.
  7. Qdrant    → children → naive / hrag (tier-filtered) / graph,
                 summaries → naive + hrag only. Flip qdrant_written.
  8. Neo4j     → write_document_graph. Flip neo4j_written.
                 Skipped entirely when use_neo4j=False.

Ghost A total failure is a hard abort because parent summaries feed retrieval;
partial summary coverage continues as a warning so later storage/graph phases
still commit.
Ghost B partial extraction is a soft warning: Mongo/Qdrant still commit, Neo4j
keeps full chunk coverage, and only entity/relation extraction is partial.
Resume logic reuses split Mongo checkpoints for parent summaries and Ghost B
extractions so large books never need one giant document write.
"""

import asyncio
import functools
from collections import Counter
import hashlib
import inspect
import logging
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable


# Pt 10e — filename sanitizer.
# Anna's Archive / libgen / z-library export filenames carry aggregator
# markers that pollute (a) the chat citation display, (b) the Ghost B
# extraction context (filename is passed alongside chunk text to the LLM),
# and (c) downstream entity extraction when the markers leak into the
# parsed markdown title. This sanitizer strips ONLY unambiguous aggregator
# suffixes — leaves author, year, publisher in the title since those may
# be useful metadata. Conservative by design — never strips content that
# could be a real part of the title.
_FILENAME_AGGREGATOR_PATTERNS = [
    re.compile(r"\s*[-–]+\s*libgen\.\w+\b.*$", re.IGNORECASE),
    re.compile(r"\s*[-–]+\s*z[\s-]?library\b.*$", re.IGNORECASE),
    re.compile(r"\s*[-–]+\s*anna_?s?\s+archive\b.*$", re.IGNORECASE),
    re.compile(r"\s*[-–]+\s*[0-9a-f]{32,}\b", re.IGNORECASE),
]


def _sanitize_filename(name: str) -> str:
    """Strip aggregator markers (libgen.li, Anna's Archive, file-hash suffixes)
    from a filename so they don't leak into citations or the Ghost B prompt.

    Preserves the file extension and any author/year/publisher tokens since
    those are often part of the legitimate title (e.g. "Cialdini - Pre-suasion
    _2016_ Random House.md" should keep "Cialdini" and "2016").

    Returns the original name on any error.
    """
    if not name or not isinstance(name, str):
        return name
    try:
        stem, ext = os.path.splitext(name)
        cleaned = stem
        for pattern in _FILENAME_AGGREGATOR_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-–")
        if not cleaned:
            return name
        return f"{cleaned}{ext}" if ext else cleaned
    except Exception:
        return name

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse, SourceTier, WriteState
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient
from services.embedder import embed_batch
from dataclasses import asdict, dataclass, field

from services.ghost_a import SummaryResult, SummaryTask, summarize_parents
from services.ghost_b import (
    EntityItem,
    ExtractionBatchReport,
    ExtractionFailureItem,
    ExtractionResult,
    ExtractionTask,
    FactItem,
    RelationItem,
    SchemaContext,
)

# Phase A — fully-local, deterministic Ghost B. The data-layer dataclasses
# (above) stay sourced from services.ghost_b; only the extractor is rerouted to
# the local GLiNER×2 + GLiREL + Python-rules implementation. Same signature,
# same ExtractionResult shape, so this branch and everything downstream are
# unchanged. Ghost A (summaries) remains the cloud path.
from services.ghost_b_local import extract_entities
from services.facets import build_ingest_facet_profile
from services.ingestion import dedup, docling_adapter, tier_chunker
from services.ingestion.summary_semantics import topic_key_for as _topic_key_for
from services.ingestion.schema_lens import get_or_create_schema_lens
from services.ingestion.section_classifier import (
    ChunkKind,
    is_noisy,
    should_skip_ghost_b,
)
from services.ingestion.resource_planner import (
    ResourceProfile,
    log_resource_profile,
    plan_ingestion_resources,
)
from services.ingestion.source_identity import source_identity_doc_fields
from services.secrets import decrypt as _decrypt_api_key
from services.storage import mongo_reader, mongo_writer, qdrant_writer
from services.storage.qdrant_writer import retrieve_schema_for_chunk

logger = logging.getLogger(__name__)
settings = get_settings()
_PARSE_SEMAPHORE = asyncio.Semaphore(max(1, settings.INGEST_MAX_PARSE_JOBS))

# ── Chunk-stage process pool (GIL escape) ───────────────────────────────────
# asyncio.to_thread serialized all concurrent docs' chunking onto ONE core
# (cpu=101% with 10 slots, 125s/doc, 2026-07-06). Spawn context on purpose:
# forking a threaded asyncio process inherits held locks. Workers lazily
# import the chunker on first use; startup cost amortizes over the pool life.
_CHUNK_POOL = None


def _chunk_process_pool():
    global _CHUNK_POOL
    if _CHUNK_POOL is None:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor

        workers = max(1, int(getattr(get_settings(), "INGEST_CHUNK_PROCESSES", 6)))
        _CHUNK_POOL = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
        )
        logger.info("chunk process pool started: workers=%d (spawn)", workers)
    return _CHUNK_POOL


def _chunk_in_subprocess(parse_result, doc_id, corpus_id, config):
    """Top-level (picklable) chunk entrypoint for the process pool."""
    from services.ingestion import tier_chunker as _tc

    return _tc.chunk(
        parse_result=parse_result,
        doc_id=doc_id,
        corpus_id=corpus_id,
        config=config,
    )

_MODEL_PHASE_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_MODEL_PHASE_SEMAPHORE_STATE: dict[str, tuple[int, asyncio.AbstractEventLoop]] = {}
_GHOST_B_FILE_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_GHOST_B_FILE_SEMAPHORE_STATE: dict[str, tuple[int, asyncio.AbstractEventLoop]] = {}
_QDRANT_WRITE_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_QDRANT_WRITE_SEMAPHORE_STATE: dict[str, tuple[int, asyncio.AbstractEventLoop]] = {}
_NEO4J_WRITE_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_NEO4J_WRITE_SEMAPHORE_STATE: dict[str, tuple[int, asyncio.AbstractEventLoop]] = {}


def _plain_model_ref(ref: Any) -> dict[str, Any]:
    return ref.model_dump() if hasattr(ref, "model_dump") else dict(ref or {})


def _pool_entry_uses_managed_vllm(entry: dict[str, Any] | Any) -> bool:
    data = _plain_model_ref(entry)
    provider = str(data.get("provider_preset") or "").strip().lower()
    model = str(data.get("model") or "").strip().lower()
    base_url = str(data.get("base_url") or "").strip().lower()
    lifecycle_base = str(data.get("lifecycle_base_url") or "").strip().lower()
    extra = data.get("extra_params") or {}
    if not isinstance(extra, dict):
        extra = {}
    if provider in {"vllm", "vllm-rtx"}:
        return True
    if bool(extra.get("managed_vllm")) or str(extra.get("resource_class") or "").lower() == "rtx":
        return True
    return "vllm" in model or "vllm" in base_url or "vllm" in lifecycle_base


def _cloud_pool_refs_for_config(config: IngestionConfig) -> list[Any]:
    if getattr(config, "models_linked", True):
        return list(getattr(config, "summary_models", []) or [])
    return list(getattr(config, "extraction_models", []) or [])


def _resource_profile_for_config(
    config: IngestionConfig,
    *,
    extraction_engine: str | None = None,
    pool: list[Any] | None = None,
    source_location: str | None = None,
) -> ResourceProfile:
    return plan_ingestion_resources(
        config=config,
        extraction_engine=extraction_engine
        if extraction_engine is not None
        else getattr(config, "extraction_engine", None),
        extraction_pool=pool if pool is not None else _cloud_pool_refs_for_config(config),
        source_location=source_location,
        settings=get_settings(),
    )


def _model_phase_doc_limit(config: IngestionConfig) -> int:
    profile = _resource_profile_for_config(config)
    return max(1, int(profile.model_phase_docs))


def _model_phase_gate_key(config: IngestionConfig) -> str:
    profile = _resource_profile_for_config(config)
    if "remote_vllm" in profile.extraction_lanes:
        return "remote_vllm"
    return "default"


def _shared_semaphore(
    semaphores: dict[str, asyncio.Semaphore],
    state: dict[str, tuple[int, asyncio.AbstractEventLoop]],
    *,
    key: str,
    limit: int,
) -> asyncio.Semaphore:
    normalized = max(1, int(limit or 1))
    loop = asyncio.get_running_loop()
    previous = state.get(key)
    if (
        key not in semaphores
        or previous is None
        or previous[0] != normalized
        or previous[1] is not loop
    ):
        semaphores[key] = asyncio.Semaphore(normalized)
        state[key] = (normalized, loop)
    return semaphores[key]


def _model_phase_semaphore(config: IngestionConfig) -> asyncio.Semaphore:
    return _shared_semaphore(
        _MODEL_PHASE_SEMAPHORES,
        _MODEL_PHASE_SEMAPHORE_STATE,
        key=_model_phase_gate_key(config),
        limit=_model_phase_doc_limit(config),
    )


def _ghost_b_active_doc_limit(
    *,
    pool: list[dict[str, Any]],
    extraction_engine: str,
    config: IngestionConfig | None = None,
) -> int:
    if config is not None:
        profile = _resource_profile_for_config(
            config,
            extraction_engine=extraction_engine,
            pool=pool,
        )
        return max(1, int(profile.extraction_active_docs))
    current = get_settings()
    cloud_lane_active = extraction_engine in {
        "cloud",
        "dual",
        "local_then_cloud",
        "local_then_enrich",
    }
    if cloud_lane_active and any(_pool_entry_uses_managed_vllm(entry) for entry in pool):
        return max(1, int(getattr(current, "EXTRACTION_MANAGED_VLLM_MAX_ACTIVE_DOCS", 2)))
    return max(1, int(getattr(current, "EXTRACTION_MAX_ACTIVE_DOCS", 1)))


def _ghost_b_file_gate_key(
    *,
    pool: list[dict[str, Any]],
    extraction_engine: str,
    config: IngestionConfig | None = None,
) -> str:
    if config is not None:
        profile = _resource_profile_for_config(
            config,
            extraction_engine=extraction_engine,
            pool=pool,
        )
        if "remote_vllm" in profile.extraction_lanes:
            return "remote_vllm"
    cloud_lane_active = extraction_engine in {
        "cloud",
        "dual",
        "local_then_cloud",
        "local_then_enrich",
    }
    if cloud_lane_active and any(_pool_entry_uses_managed_vllm(entry) for entry in pool):
        return "remote_vllm"
    return "default"


def _ghost_b_file_semaphore(
    *,
    pool: list[dict[str, Any]],
    extraction_engine: str,
    config: IngestionConfig | None = None,
) -> asyncio.Semaphore:
    return _shared_semaphore(
        _GHOST_B_FILE_SEMAPHORES,
        _GHOST_B_FILE_SEMAPHORE_STATE,
        key=_ghost_b_file_gate_key(
            pool=pool,
            extraction_engine=extraction_engine,
            config=config,
        ),
        limit=_ghost_b_active_doc_limit(
            pool=pool,
            extraction_engine=extraction_engine,
            config=config,
        ),
    )


def _qdrant_write_semaphore() -> asyncio.Semaphore:
    return _shared_semaphore(
        _QDRANT_WRITE_SEMAPHORES,
        _QDRANT_WRITE_SEMAPHORE_STATE,
        key="qdrant",
        limit=max(1, int(getattr(get_settings(), "QDRANT_INGEST_WRITE_CONCURRENCY", 2))),
    )


def _neo4j_write_semaphore() -> asyncio.Semaphore:
    return _shared_semaphore(
        _NEO4J_WRITE_SEMAPHORES,
        _NEO4J_WRITE_SEMAPHORE_STATE,
        key="neo4j",
        limit=max(1, int(getattr(get_settings(), "NEO4J_INGEST_WRITE_CONCURRENCY", 1))),
    )


class GhostAFailure(RuntimeError):
    """Ghost A produced no usable summaries, so the document must abort."""


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

    def __iter__(self):
        yield self.summaries
        yield self.ghost_b_out


_HRAG_TIERS = (
    SourceTier.tier_a.value,
    SourceTier.tier_b.value,
    SourceTier.tier_b_plus.value,
)
_SUMMARY_QDRANT_KINDS = ("naive", "hrag")


def _summary_target_kinds(config: IngestionConfig) -> list[str]:
    """Return Qdrant collections that carry parent-summary points."""
    targets = list(getattr(config, "target_qdrant_collections", None) or [])
    return [kind for kind in targets if kind in _SUMMARY_QDRANT_KINDS]


def _summarizable_parents(parents) -> list:
    """Parents that Ghost A is expected to summarize."""
    return [
        p
        for p in parents
        if not should_skip_ghost_b(
            getattr(p, "chunk_kind", None) or ChunkKind.BODY
        )
    ]


async def _qdrant_summary_counts(
    qdrant_client: AsyncQdrantClient,
    *,
    corpus_id: str,
    doc_id: str,
    target_kinds: list[str],
) -> dict[str, int]:
    """Count summary points for a document in each summary-bearing collection."""
    from qdrant_client import models as qmodels

    counts: dict[str, int] = {}
    for kind in target_kinds:
        res = await qdrant_client.count(
            collection_name=qdrant_writer._col_for_corpus(corpus_id, kind),
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="doc_id", match=qmodels.MatchValue(value=doc_id)
                    ),
                    qmodels.FieldCondition(
                        key="chunk_type",
                        match=qmodels.MatchValue(value="summary"),
                    ),
                ]
            ),
            exact=True,
        )
        counts[kind] = int(res.count)
    return counts


async def _qdrant_has_summary_points(
    qdrant_client: AsyncQdrantClient,
    *,
    corpus_id: str,
    doc_id: str,
    expected_count: int,
    target_kinds: list[str],
) -> bool:
    """Return True when every summary target has the expected summary count."""
    if not target_kinds:
        return True
    counts = await _qdrant_summary_counts(
        qdrant_client,
        corpus_id=corpus_id,
        doc_id=doc_id,
        target_kinds=target_kinds,
    )
    return all(count == expected_count for count in counts.values())


# Near-duplicate document detection shares ONE deterministic core with the
# corpus-wide DETECT/CORRECT pipeline (services/ingestion/dedup.py). The
# ingest-time PREVENT check below and the corpus scan call the same shingle_set
# / jaccard / threshold, so they can never drift. Shingle (5-gram) Jaccard, not
# token-set: a same-book PDF-vs-MD pair scores ~0.32 while different books score
# ~0.003, so 0.10 separates them with a wide margin. Overridable via
# settings.INGEST_NEAR_DUPLICATE_THRESHOLD.
_DUPLICATE_DOC_THRESHOLD = dedup.DEFAULT_DUPLICATE_THRESHOLD


def _is_vectorized_child(chunk) -> bool:
    """True if this child chunk is part of the retrieval surface.

    Uses `is_noisy()` (NOISY_KINDS = ALL_KINDS - _RETRIEVABLE), NOT
    `should_skip_ghost_b()` (GHOST_B_SKIP_KINDS = NOISY_KINDS ∪ {CODE}).
    The two sets diverge on CODE: Ghost B SKIPS code chunks (don't send raw
    code to the LLM extractor — Phase 4/5 deterministic synthesis handles
    them), but code chunks ARE retrievable and MUST be embedded so vector
    + BM25 search can find them. Using should_skip_ghost_b here was a
    Pt 11 regression that silently zeroed out code-chunk embeddings.
    """
    kind = getattr(chunk, "chunk_kind", None) or ChunkKind.BODY
    return not is_noisy(kind)


def _merge_warnings(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Append warnings once while preserving their original order."""
    merged: list[str] = []
    for warning in [*(existing or []), *(new or [])]:
        text = str(warning or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _build_ghost_b_error_event_sink(
    db: AsyncIOMotorDatabase,
    *,
    run_id: str,
) -> Callable[[dict[str, Any]], Any] | None:
    if not getattr(settings, "EXTRACTION_ERROR_AUDIT_ENABLED", True):
        return None

    max_failed = max(
        0,
        int(getattr(settings, "EXTRACTION_ERROR_AUDIT_MAX_FAILED_ATTEMPTS_PER_DOC", 25) or 0),
    )
    max_success = max(
        0,
        int(getattr(settings, "EXTRACTION_ERROR_AUDIT_MAX_SUCCESS_ATTEMPTS_PER_DOC", 2) or 0),
    )
    counts = {
        "ghost_b_attempt_failed": 0,
        "ghost_b_attempt_succeeded": 0,
        "ghost_b_failure_budget_tripped": 0,
    }
    lock = asyncio.Lock()

    async def _sink(event: dict[str, Any]) -> None:
        name = str(event.get("event") or "")
        async with lock:
            if name == "ghost_b_attempt_failed":
                if counts[name] >= max_failed:
                    return
                counts[name] += 1
                sample_index = counts[name]
            elif name == "ghost_b_attempt_succeeded":
                if counts[name] >= max_success:
                    return
                counts[name] += 1
                sample_index = counts[name]
            elif name == "ghost_b_failure_budget_tripped":
                counts[name] += 1
                sample_index = counts[name]
            else:
                return
        doc = dict(event)
        doc["run_id"] = doc.get("run_id") or run_id
        doc["sample_index"] = sample_index
        doc["created_at"] = datetime.utcnow()
        try:
            await db["ghost_b_error_events"].insert_one(doc)
        except Exception as exc:
            logger.warning("phase=ghost_b_error_audit_write_failed error=%s", exc)

    return _sink


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


def _ghost_b_total_failure_warning(*, total: int) -> str:
    return (
        f"Ghost B graph extraction produced 0/{total} chunk results; treating "
        "as an extraction provider outage. Mongo/Qdrant can continue, but Neo4j "
        "will remain pending for retry/backfill."
    )


def _ghost_a_partial_warning(
    *,
    summarized: int,
    total: int,
) -> str:
    skipped = max(total - summarized, 0)
    return (
        f"Ghost A parent summarization partial: {summarized}/{total} parents "
        f"summarized; {skipped} parent summaries are missing, but child chunks "
        "remain available for vector RAG and graph extraction."
    )


def _ghost_b_metrics_for_skipped(results: list[ExtractionResult] | None) -> dict | None:
    if results is None:
        return None
    relation_count = sum(len(r.relations) for r in results)
    related_to_count = sum(
        1
        for r in results
        for rel in r.relations
        if rel.predicate == "related_to"
    )
    lens_ids = sorted({r.schema_lens_id for r in results if r.schema_lens_id})
    return {
        "requested_chunks": len(results),
        "extracted_chunks": len(results),
        "failed_chunks": 0,
        "success_rate": 1.0,
        "attempt_count": 0,
        "models": [],
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_duration_seconds": 0.0,
        "entity_count": sum(len(r.entities) for r in results),
        "relation_count": relation_count,
        "fact_count": sum(len(getattr(r, "facts", []) or []) for r in results),
        "related_to_count": related_to_count,
        "related_to_ratio": round(related_to_count / relation_count, 4) if relation_count else 0.0,
        "entity_remap_count": sum(r.entity_remap_count for r in results),
        "relation_remap_count": sum(r.relation_remap_count for r in results),
        "domain_range_remap_count": sum(r.domain_range_remap_count for r in results),
        "domain_range_warn_count": sum(r.domain_range_warn_count for r in results),
        "endpoint_completion_count": sum(r.endpoint_completion_count for r in results),
        "evidence_cue_repair_count": sum(r.evidence_cue_repair_count for r in results),
        "entity_drop_count": sum(r.entity_drop_count for r in results),
        "relation_drop_count": sum(r.relation_drop_count for r in results),
        "fact_drop_count": sum(getattr(r, "fact_drop_count", 0) for r in results),
        "schema_lens_ids": lens_ids,
        "error_counts": {},
    }


def _ghost_b_metrics_with_failures(
    results: list[ExtractionResult] | None,
    failures: list[ExtractionFailureItem] | None,
    base_metrics: dict | None = None,
) -> dict | None:
    """Preserve Ghost B partial coverage when a retry reuses staged output."""
    if results is None:
        return dict(base_metrics) if isinstance(base_metrics, dict) else None

    metrics = (
        dict(base_metrics)
        if isinstance(base_metrics, dict)
        else (_ghost_b_metrics_for_skipped(results) or {})
    )
    extracted = len(results)
    failed_from_rows = len(failures or [])

    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    failed = max(_as_int(metrics.get("failed_chunks")), failed_from_rows)
    requested = max(_as_int(metrics.get("requested_chunks")), extracted + failed)

    metrics["requested_chunks"] = requested
    metrics["extracted_chunks"] = extracted
    metrics["failed_chunks"] = failed
    metrics["success_rate"] = round(extracted / requested, 4) if requested else 1.0

    if failed_from_rows:
        error_counts = metrics.get("error_counts")
        counted = Counter(f.error_type or "unknown" for f in failures or [])
        if not isinstance(error_counts, dict) or sum(
            _as_int(v) for v in error_counts.values()
        ) < failed_from_rows:
            metrics["error_counts"] = dict(counted)

    return metrics


def _build_ghost_pool(refs) -> list[dict]:
    """
    Turn a list[ModelProfileRef] (Pydantic) or list[dict] into the plain-dict
    pool that ghost_a / ghost_b accept. Decrypts each entry's secrets exactly
    once here so the ghost layers stay ignorant of the storage format.
    """
    if not refs:
        return []
    out: list[dict] = []
    for ref in refs:
        data = ref.model_dump() if hasattr(ref, "model_dump") else dict(ref)
        for secret_field in ("api_key", "lifecycle_api_key"):
            ct = data.get(secret_field)
            if ct:
                pt = _decrypt_api_key(ct)
                data[secret_field] = pt if pt is not None else ct
        out.append(
            {
                "provider_preset": data.get("provider_preset") or "",
                "model": data.get("model"),
                "base_url": data.get("base_url") or None,
                "api_key": data.get("api_key") or None,
                "max_concurrent": int(data.get("max_concurrent") or 1) or 1,
                "lifecycle_base_url": data.get("lifecycle_base_url") or None,
                "lifecycle_api_key": data.get("lifecycle_api_key") or None,
                "lifecycle_auto_start": bool(data.get("lifecycle_auto_start")),
                "lifecycle_auto_stop": bool(data.get("lifecycle_auto_stop")),
                "lifecycle_up_path": data.get("lifecycle_up_path") or "/up",
                "lifecycle_status_path": data.get("lifecycle_status_path") or "/status",
                "lifecycle_down_path": data.get("lifecycle_down_path") or "/down",
                "lifecycle_ready_timeout_seconds": int(
                    data.get("lifecycle_ready_timeout_seconds") or 360
                ),
                "extra_params": data.get("extra_params") or {},
            }
        )
    return out


def _rehydrate_ghost_b_staging(staged: list[dict]) -> list[ExtractionResult]:
    """Reconstruct ExtractionResult dataclasses from a Mongo-stored staging list.

    Dataclasses aren't Pydantic, so `**r` unpack won't work directly — the
    nested `entities` / `relations` arrays need their own EntityItem /
    RelationItem construction.
    """
    out: list[ExtractionResult] = []
    for r in staged:
        out.append(
            ExtractionResult(
                schema_version=r.get("schema_version", "polymath.extract.v1"),
                chunk_id=r["chunk_id"],
                doc_id=r["doc_id"],
                corpus_id=r["corpus_id"],
                # Pt 10b — chunk text was added to ExtractionResult to feed
                # taxonomy synonym matching. Stale staging written before
                # the fix won't have this field; default to "" so backfill
                # still runs (ontology stays empty for those — re-ingest if
                # you want the new behavior).
                text=r.get("text", ""),
                entities=[EntityItem(**e) for e in r.get("entities", [])],
                relations=[RelationItem(**x) for x in r.get("relations", [])],
                facts=[FactItem(**f) for f in r.get("facts", [])],
                entity_remap_count=r.get("entity_remap_count", 0),
                entity_drop_count=r.get("entity_drop_count", 0),
                relation_remap_count=r.get("relation_remap_count", 0),
                relation_drop_count=r.get("relation_drop_count", 0),
                domain_range_remap_count=r.get("domain_range_remap_count", 0),
                domain_range_warn_count=r.get("domain_range_warn_count", 0),
                endpoint_completion_count=r.get("endpoint_completion_count", 0),
                evidence_cue_repair_count=r.get("evidence_cue_repair_count", 0),
                evidence_drop_count=r.get("evidence_drop_count", 0),
                fact_drop_count=r.get("fact_drop_count", 0),
                schema_lens_id=r.get("schema_lens_id"),
            )
        )
    return out


def _reconstruct_summaries_from_mongo(
    parents, existing_parent_chunks: list[dict]
) -> list[SummaryResult]:
    """Rebuild SummaryResult list from Mongo-stored parent_chunks[].summary.

    Only called on the D.2 resume path when every summarizable parent has a
    non-empty summary. Non-body parents are intentionally skipped by Ghost A,
    so they are not required for resume. The parent_id set is stable across
    runs (deterministic from content-hashed doc_id), so we zip by parent_id map.
    """
    by_id = {ep["parent_id"]: ep for ep in existing_parent_chunks}
    out: list[SummaryResult] = []
    for p in parents:
        kind = getattr(p, "chunk_kind", None) or ChunkKind.BODY
        if should_skip_ghost_b(kind):
            continue
        ep = by_id.get(p.parent_id)
        if not ep:
            continue
        summary = (ep.get("summary") or "").strip()
        if not summary:
            continue
        out.append(
            SummaryResult(
                parent_id=p.parent_id,
                doc_id=p.doc_id,
                corpus_id=p.corpus_id,
                source_tier=p.source_tier,
                summary=summary,
                domain=ep.get("domain"),
                topics=ep.get("topics"),
                semantic_chunk_type=ep.get("semantic_chunk_type"),
                key_terms=ep.get("key_terms"),
                mechanisms=ep.get("mechanisms"),
                schema_version=ep.get("schema_version"),
                summary_type=ep.get("summary_type"),
                central_claim=ep.get("central_claim"),
                key_points=ep.get("key_points"),
                main_mechanism=ep.get("main_mechanism"),
                concept_tags=ep.get("concept_tags"),
                entity_hints=ep.get("entity_hints"),
                retrieval_uses=ep.get("retrieval_uses"),
                abstraction_level=ep.get("abstraction_level"),
                source_child_ids=ep.get("source_child_ids") or ep.get("child_ids"),
            )
        )
    return out


def _rehydrate_ghost_b_failures(rows: list[dict]) -> list[ExtractionFailureItem]:
    out: list[ExtractionFailureItem] = []
    for row in rows or []:
        try:
            out.append(
                ExtractionFailureItem(
                    chunk_id=str(row.get("chunk_id") or ""),
                    doc_id=str(row.get("doc_id") or ""),
                    corpus_id=str(row.get("corpus_id") or ""),
                    model=str(row.get("model") or ""),
                    lane=int(row.get("lane") or 0),
                    attempts=int(row.get("attempts") or 0),
                    error_type=str(row.get("error_type") or "unknown"),
                    error_message=str(row.get("error_message") or ""),
                )
            )
        except Exception:
            continue
    return [failure for failure in out if failure.chunk_id]


def _doc_shingle_set(texts: list[str], k: int = dedup.DEFAULT_SHINGLE_K) -> set[str]:
    """Near-duplicate fingerprint — delegates to the shared dedup core so the
    ingest-time PREVENT check and the corpus-wide DETECT scan use the identical
    deterministic algorithm."""
    return dedup.shingle_set(texts, k=k)


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
    incoming = _doc_shingle_set(parent_texts)
    if len(incoming) < dedup.MIN_SHINGLES:
        return []

    candidates: list[dict] = []
    cursor = db["documents"].find(
        {"corpus_id": corpus_id, "doc_id": {"$ne": doc_id}},
        {"doc_id": 1, "corpus_id": 1, "filename": 1},
    )
    async for doc in cursor:
        parent_rows = await mongo_reader.get_parent_chunks(
            db,
            str(doc.get("doc_id") or ""),
            str(doc.get("corpus_id") or corpus_id),
        )
        existing_texts = [
            str(p.get("text") or "")
            for p in parent_rows
            if isinstance(p, dict)
        ]
        existing = _doc_shingle_set(existing_texts)
        if not existing:
            continue
        similarity = dedup.jaccard(incoming, existing)
        if similarity >= threshold:
            # Containment of the INCOMING doc inside the existing one — how much
            # of what we're about to ingest is ALREADY present. This (not the
            # symmetric Jaccard) is what decides block-vs-flag: a distinct work
            # that merely shares prose is not fully contained, so it is flagged
            # and ingested, never silently skipped.
            inter = len(incoming & existing)
            cont = inter / len(incoming) if incoming else 0.0
            candidates.append(
                {
                    "doc_id": doc.get("doc_id"),
                    "filename": doc.get("filename") or "",
                    "similarity": round(float(similarity), 3),
                    "containment": round(float(cont), 3),
                }
            )

    # Sort by containment first (the decision signal), then Jaccard.
    candidates.sort(
        key=lambda c: (
            float(c.get("containment") or 0.0),
            float(c.get("similarity") or 0.0),
        ),
        reverse=True,
    )
    return candidates[:limit]


async def _run_ghosts_parallel(
    *,
    config: IngestionConfig,
    parents: list,
    children: list,
    doc_id: str,
    corpus_id: str,
    user_id: str | None = None,
    model: str,
    filename: str | None = None,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver,
    existing_doc: dict | None,
    ws: WriteState,
) -> GhostRunResult:
    """Fan out GHOST A + GHOST B in parallel. Either branch may be disabled
    by config OR skipped via resume gates (Decision D).

    Hard-abort semantics: Ghost A raises only when it produces zero summaries
    for requested work. Partial Ghost A and Ghost B results return usable output
    plus warnings so the document can commit to Mongo/Qdrant/Neo4j and surface
    coverage honestly in the UI.
    """
    warnings: list[str] = []
    ghost_b_failures: list[ExtractionFailureItem] = []
    ghost_b_metrics: dict | None = None
    # ── GHOST A path decisions ────────────────────────────────────────────
    existing_parent_chunks: list[dict] = await mongo_reader.get_parent_chunks(
        db,
        doc_id,
        corpus_id,
    )
    if not existing_parent_chunks and existing_doc:
        existing_parent_chunks = (existing_doc or {}).get("parent_chunks") or []
    summaries_from_mongo: list[SummaryResult] | None = None
    need_ghost_a = config.chunk_summarization
    summary_targets = _summary_target_kinds(config)
    summarizable_parents = _summarizable_parents(parents)
    expected_summary_count = len(summarizable_parents)

    if need_ghost_a and ws.summaries_indexed:
        # This is the only safe fast-skip: children may already be in Qdrant
        # while summaries are absent, so qdrant_written is not enough.
        try:
            if await _qdrant_has_summary_points(
                qdrant_client,
                corpus_id=corpus_id,
                doc_id=doc_id,
                expected_count=expected_summary_count,
                target_kinds=summary_targets,
            ):
                need_ghost_a = False
                logger.info(
                    "Ghost A skipped (summaries indexed) doc=%s corpus=%s parents=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    expected_summary_count,
                )
            else:
                ws.summaries_indexed = False
                logger.warning(
                    "phase=ghost_a_resume reason=summary_points_missing doc=%s corpus=%s expected=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    expected_summary_count,
                )
        except Exception as exc:  # noqa: BLE001 - resume probe is best-effort
            ws.summaries_indexed = False
            logger.warning(
                "phase=ghost_a_summary_check_failed doc=%s corpus=%s: %s",
                doc_id[:12],
                corpus_id[:8],
                exc,
            )

    if need_ghost_a and existing_parent_chunks:
        existing_by_parent_id = {p.get("parent_id"): p for p in existing_parent_chunks}
        all_filled = all(
            (
                existing_by_parent_id.get(p.parent_id, {}).get("summary")
                or ""
            ).strip()
            for p in summarizable_parents
        )
        if all_filled:
            summaries_from_mongo = _reconstruct_summaries_from_mongo(
                parents, existing_parent_chunks
            )
            if len(summaries_from_mongo) == len(summarizable_parents):
                need_ghost_a = False
                try:
                    ws.summaries_indexed = await _qdrant_has_summary_points(
                        qdrant_client,
                        corpus_id=corpus_id,
                        doc_id=doc_id,
                        expected_count=len(summaries_from_mongo),
                        target_kinds=summary_targets,
                    )
                except Exception as exc:  # noqa: BLE001 - reindex path is safe
                    ws.summaries_indexed = False
                    logger.warning(
                        "phase=ghost_a_reconstruct_summary_check_failed doc=%s corpus=%s: %s",
                        doc_id[:12],
                        corpus_id[:8],
                        exc,
                    )
                logger.info(
                    "Ghost A skipped (resume) doc=%s corpus=%s parents=%d summaries_indexed=%s",
                    doc_id[:12],
                    corpus_id[:8],
                    len(summaries_from_mongo),
                    ws.summaries_indexed,
                )
            else:
                summaries_from_mongo = None  # partial reconstruct → rerun

    # ── GHOST B path decisions ────────────────────────────────────────────
    need_ghost_b = (
        config.use_neo4j and settings.NEO4J_ENABLED and not ws.neo4j_written
    )
    ghost_b_from_staging: list[ExtractionResult] | None = None
    ghost_b_missing_ids: set[str] | None = None
    if need_ghost_b and neo4j_driver is None:
        need_ghost_b = False
    elif need_ghost_b:
        staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
        if staged:
            ghost_b_from_staging = _rehydrate_ghost_b_staging(staged)
            expected_ids = {
                c.chunk_id
                for c in children
                if getattr(c, "chunk_id", None)
                and not should_skip_ghost_b(
                    getattr(c, "chunk_kind", None) or ChunkKind.BODY
                )
            }
            staged_ids = {r.chunk_id for r in ghost_b_from_staging}
            missing_ids = expected_ids - staged_ids
            if not missing_ids:
                ghost_b_failures.extend(
                    _rehydrate_ghost_b_failures(
                        await mongo_reader.read_ghost_b_failures(
                            db,
                            doc_id,
                            corpus_id,
                        )
                    )
                )
                ghost_b_metrics = _ghost_b_metrics_with_failures(
                    ghost_b_from_staging,
                    ghost_b_failures,
                    (existing_doc or {}).get("ghost_b_metrics"),
                )
                need_ghost_b = False
                logger.info(
                    "phase=ghost_b_skip reason=staging_complete doc=%s corpus=%s entries=%d failures=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    len(ghost_b_from_staging),
                    len(ghost_b_failures),
                )
            else:
                ghost_b_missing_ids = missing_ids
                logger.info(
                    "phase=ghost_b_resume reason=staging_partial doc=%s corpus=%s staged=%d missing=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    len(ghost_b_from_staging),
                    len(missing_ids),
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
                source_child_ids=[c.chunk_id for c in getattr(p, "children", [])],
                child_boundaries="\n\n".join(
                    f"[{c.chunk_id}]\n{c.text}" for c in getattr(p, "children", [])
                ),
            )
            for p in body_parents
        ]
        pool = _build_ghost_pool(config.summary_models)
        summary_max_concurrent: int | None = None
        max_summary_tokens = config.max_summary_tokens
        if not pool or user_id:
            try:
                from services.settings import settings_service

                runtime_ingestion = await settings_service.get_runtime_ingestion_settings(user_id)
                runtime_summary = runtime_ingestion.summary
                summary_max_concurrent = runtime_summary.max_concurrent
                default_tokens = IngestionConfig.model_fields["max_summary_tokens"].default
                if (
                    max_summary_tokens == default_tokens
                    and runtime_summary.max_summary_tokens != default_tokens
                ):
                    max_summary_tokens = runtime_summary.max_summary_tokens
                if not pool and runtime_summary.summary_models:
                    pool = _build_ghost_pool(runtime_summary.summary_models)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Ghost A global summary settings unavailable doc=%s corpus=%s: %s",
                    doc_id[:12],
                    corpus_id[:8],
                    exc,
                )
        logger.info(
            "Ghost A start doc=%s corpus=%s parents=%d pool=%d global_cap=%s",
            doc_id[:12],
            corpus_id[:8],
            len(tasks),
            len(pool) or 1,
            summary_max_concurrent or "-",
        )
        results = await summarize_parents(
            tasks,
            max_summary_tokens=max_summary_tokens,
            pool=pool,
            model=model,
            global_max_concurrent=summary_max_concurrent,
        )
        if len(results) < len(tasks):
            if not results and tasks:
                raise GhostAFailure(
                    f"Ghost A produced 0/{len(tasks)} summaries; treating as provider outage"
                )
            missing_ids = sorted(
                {t.parent_id for t in tasks} - {r.parent_id for r in results}
            )
            warning = _ghost_a_partial_warning(
                summarized=len(results),
                total=len(tasks),
            )
            warnings.append(warning)
            logger.warning(
                "phase=ghost_a_partial doc=%s corpus=%s summarized=%d total=%d missing_sample=%s",
                doc_id[:12],
                corpus_id[:8],
                len(results),
                len(tasks),
                missing_ids[:5],
            )
        return results

    async def _b_branch() -> list[ExtractionResult] | None:
        # TWO-PHASE INGEST (§12.6-aligned, gated OFF): defer extraction so
        # the doc is QUERYABLE right after embed; the post-qdrant hook fires
        # the existing graph-backfill machinery as background enrichment.
        from config import get_settings as _gs_tp

        if bool(getattr(_gs_tp(), "TWO_PHASE_INGEST", False)):
            logger.info(
                "phase=ghost_b DEFERRED (two-phase) doc=%s — enrichment "
                "runs post-qdrant in background", doc_id[:12],
            )
            return ghost_b_from_staging
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
            elif ghost_b_missing_ids is not None and c.chunk_id not in ghost_b_missing_ids:
                continue
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
        tasks = [
            ExtractionTask(
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                corpus_id=c.corpus_id,
                text=c.text,
                chunk_kind=getattr(c, "chunk_kind", None) or ChunkKind.BODY,
                metadata=getattr(c, "metadata", None) or {},
            )
            for c in body_children
        ]
        schema_ctx = SchemaContext(
            entity_schema=config.entity_schema,
            relation_schema=config.relation_schema,
            strict=config.schema_strict,
        )
        # Deterministic per-corpus extraction contract (§13 ground-truth
        # correction): the corpus engine wins, 'inherit' falls back to the
        # global Settings engine, and contract violations fail the doc with one
        # clear error instead of thousands of silent chunk failures. Resolve it
        # before pool/schema-lens setup so every downstream decision shares one
        # source of truth.
        from services.ingestion.extraction_contract import (
            resolve_extraction_contract,
        )

        _global_engine = "local"
        _endpoint_urls: list[str] = []
        try:
            from services import ghost_b_local as _gbl
            from services.settings import settings_service as _ss

            ext = await _ss.get_system_extraction()
            _global_engine = str(getattr(ext, "engine", "local") or "local")
            _endpoint_urls = [
                e.url.strip().rstrip("/")
                for e in (ext.endpoints or [])
                if e.enabled and e.url and e.url.strip()
            ]
            _gbl.RUNTIME_ENDPOINT_URLS = _endpoint_urls or None
        except Exception as exc:  # noqa: BLE001 — env fallback is fine
            logger.warning("extraction endpoint settings unavailable: %s", exc)

        contract = resolve_extraction_contract(
            corpus_engine=getattr(config, "extraction_engine", None),
            global_engine=_global_engine,
            models_linked=getattr(config, "models_linked", True),
            summary_model_count=len(config.summary_models or []),
            extraction_model_count=len(config.extraction_models or []),
            enabled_endpoint_urls=_endpoint_urls,
        )
        extraction_engine = contract.engine
        cloud_pool_refs = (
            config.summary_models
            if getattr(config, "models_linked", True)
            else config.extraction_models
        )
        pool = _build_ghost_pool(cloud_pool_refs)
        resource_profile = _resource_profile_for_config(
            config,
            extraction_engine=extraction_engine,
            pool=pool,
        )
        cloud_primary = contract.engine in {"cloud", "dual"}
        if cloud_primary and pool:
            from services.ingestion.model_lifecycle import ensure_model_lifecycle_ready

            await ensure_model_lifecycle_ready(pool, purpose="schema_lens")
        # Exclude noisy parents/children from the schema lens — letting
        # bibliography page entries (publishers, ISBNs, citation bric-a-brac)
        # influence which schema terms get retrieved would erode entity
        # extraction quality on body content.
        body_parents_for_lens = [
            p for p in parents
            if not should_skip_ghost_b(getattr(p, "chunk_kind", None) or ChunkKind.BODY)
        ]
        body_children_for_lens = [
            c for c in children
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
            pool=pool if cloud_primary else [],
            model=model,
            allow_llm=cloud_primary,
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
        reason = (
            "staging_partial_resume"
            if ghost_b_missing_ids is not None
            else ("fresh_ingest" if not ws.mongo_written else "staging_missing_legacy_doc")
        )
        logger.info(
            "phase=ghost_b_run reason=%s doc=%s corpus=%s children=%d pool=%d strict=%s",
            reason,
            doc_id[:12],
            corpus_id[:8],
            len(tasks),
            len(pool) or 1,
            schema_ctx.strict,
        )
        active_doc_limit = _ghost_b_active_doc_limit(
            pool=pool,
            extraction_engine=extraction_engine,
            config=config,
        )
        async with _ghost_b_file_semaphore(
            pool=pool,
            extraction_engine=extraction_engine,
            config=config,
        ):
            logger.info(
                "phase=ghost_b_file_gate doc=%s corpus=%s active_doc_limit=%d children=%d",
                doc_id[:12],
                corpus_id[:8],
                active_doc_limit,
                len(tasks),
            )
            log_resource_profile(
                resource_profile,
                extra={
                    "doc": doc_id[:12],
                    "corpus": corpus_id[:8],
                    "phase": "ghost_b",
                    "contract_engine": extraction_engine,
                    "pool_size": len(pool),
                },
            )
            logger.info(
                "phase=ghost_b_contract doc=%s corpus=%s engine=%s source=%s "
                "pool=%s/%d endpoints=%d errors=%d warnings=%d",
                doc_id[:12],
                corpus_id[:8],
                contract.engine,
                contract.source,
                contract.pool_source,
                contract.pool_size,
                len(contract.endpoint_urls),
                len(contract.errors),
                len(contract.warnings),
            )
            for _w in contract.warnings:
                logger.warning("ghost_b_contract doc=%s: %s", doc_id[:12], _w)
            if contract.errors:
                raise RuntimeError(
                    "extraction contract violation — " + "; ".join(contract.errors)
                )
            ghost_b_run_id = str(uuid.uuid4())
            _extract_kwargs = dict(
                schema=schema_ctx,
                schema_lens=schema_lens,
                chunk_vectors=None,
                schema_resolver=_schema_resolver,
                pool=pool,
                model=model,
                return_report=True,
                enable_facts=settings.EXTRACTION_ENABLE_FACTS,
                audit_event_sink=_build_ghost_b_error_event_sink(
                    db,
                    run_id=ghost_b_run_id,
                ),
                audit_run_id=ghost_b_run_id,
            )
            # Owner-selectable engine (per-corpus contract; two-toggle model):
            # off = vectors-only (explicit), local = GLiNER/GLiREL sidecars,
            # cloud = Ghost B LLM pool, dual = both (even/odd chunk split),
            # local_then_cloud = local primary with cloud rescue.
            if extraction_engine == "off":
                report = ExtractionBatchReport(
                    results=[],
                    failures=[],
                    metrics={
                        "engine": "off",
                        "requested_chunks": len(tasks),
                        "extracted_chunks": 0,
                        "failed_chunks": 0,
                        "skipped": True,
                    },
                )
            elif extraction_engine == "cloud":
                from services.ghost_b import extract_entities as _cloud_extract

                report = await _cloud_extract(tasks, **_extract_kwargs)
            elif extraction_engine == "dual":
                # DUAL (owner: throughput) — split the doc across BOTH engines
                # concurrently: even-index chunks → local GLiNER/GLiREL, odd →
                # cloud pool. Deterministic split; downstream maps results by
                # chunk_id so interleaved order is irrelevant. Dual is a SPEED
                # mode, not a fallback: either engine failing fails the doc
                # loudly (use local_then_cloud for resilience instead).
                from services.ghost_b import extract_entities as _cloud_extract

                _local_part = tasks[0::2]
                _cloud_part = tasks[1::2]
                _rep_local, _rep_cloud = await asyncio.gather(
                    extract_entities(_local_part, **_extract_kwargs),
                    _cloud_extract(_cloud_part, **_extract_kwargs),
                )
                if isinstance(_rep_local, ExtractionBatchReport) and isinstance(
                    _rep_cloud, ExtractionBatchReport
                ):
                    report = ExtractionBatchReport(
                        results=list(_rep_local.results) + list(_rep_cloud.results),
                        failures=list(_rep_local.failures) + list(_rep_cloud.failures),
                        metrics={
                            "engine": "dual",
                            "local": _rep_local.metrics,
                            "cloud": _rep_cloud.metrics,
                        },
                    )
                else:  # return_report=False shape (raw result lists)
                    report = list(_rep_local) + list(_rep_cloud)
            elif extraction_engine == "local_then_cloud":
                try:
                    report = await extract_entities(tasks, **_extract_kwargs)
                except Exception as _local_exc:  # noqa: BLE001
                    if contract.pool_size == 0:
                        raise
                    logger.warning(
                        "phase=ghost_b local engine failed (%s) — cloud fallback",
                        _local_exc,
                    )
                    from services.ghost_b import extract_entities as _cloud_extract

                    report = await _cloud_extract(tasks, **_extract_kwargs)
            elif extraction_engine == "local_then_enrich":
                # §13-H E1 — Fast Local Graph + RTX Enrichment. Local
                # GLiNER/GLiREL always builds the skeleton; the enrichment
                # gate scores the pass and the cloud/RTX lane re-extracts
                # ONLY the selected gap chunks (bounded by
                # EXTRACTION_ENRICH_MAX_CHUNK_RATIO). Cloud results REPLACE
                # local results for enriched chunk_ids — no double writes.
                # Enrichment rate is surfaced in metrics per the
                # silent-fallback accounting law.
                from services.ingestion.enrichment_gate import (
                    enrichment_verdict,
                    select_enrichment_tasks,
                )

                report = await extract_entities(tasks, **_extract_kwargs)
                if isinstance(report, ExtractionBatchReport):
                    _verdict = enrichment_verdict(
                        report.metrics,
                        min_coverage=settings.EXTRACTION_ENRICH_MIN_COVERAGE,
                        min_facts_per_chunk=settings.EXTRACTION_ENRICH_MIN_FACTS_PER_CHUNK,
                        max_related_to_ratio=settings.EXTRACTION_ENRICH_MAX_RELATED_TO_RATIO,
                    )
                    _base_metrics = {
                        **dict(report.metrics or {}),
                        "engine": "local_then_enrich",
                        "enrich_reasons": list(_verdict.reasons),
                        "enriched_chunks": 0,
                    }
                    if _verdict.enrich and contract.pool_size == 0:
                        _base_metrics["enrich_skipped"] = "no_cloud_pool"
                        report = ExtractionBatchReport(
                            results=report.results,
                            failures=report.failures,
                            metrics=_base_metrics,
                        )
                    elif _verdict.enrich:
                        _picks = select_enrichment_tasks(
                            tasks,
                            report.results,
                            report.failures,
                            _verdict,
                            max_chunk_ratio=settings.EXTRACTION_ENRICH_MAX_CHUNK_RATIO,
                        )
                        if _picks:
                            logger.info(
                                "phase=ghost_b_enrich doc=%s corpus=%s chunks=%d/%d "
                                "reasons=%s",
                                doc_id[:12],
                                corpus_id[:8],
                                len(_picks),
                                len(tasks),
                                "; ".join(_verdict.reasons),
                            )
                            from services.ghost_b import (
                                extract_entities as _cloud_extract,
                            )

                            _rep_cloud = await _cloud_extract(
                                _picks, **_extract_kwargs
                            )
                            _enriched_ids = {
                                r.chunk_id for r in _rep_cloud.results
                            }
                            _kept = [
                                r
                                for r in report.results
                                if r.chunk_id not in _enriched_ids
                            ]
                            _kept_failures = [
                                f
                                for f in report.failures
                                if str(getattr(f, "chunk_id", ""))
                                not in _enriched_ids
                            ]
                            _base_metrics["enriched_chunks"] = len(_picks)
                            _base_metrics["enrich_succeeded"] = len(
                                _rep_cloud.results
                            )
                            _base_metrics["enrich_cloud"] = {
                                k: v
                                for k, v in dict(_rep_cloud.metrics or {}).items()
                                if not isinstance(v, (list, dict))
                            }
                            report = ExtractionBatchReport(
                                results=_kept + list(_rep_cloud.results),
                                failures=_kept_failures
                                + list(_rep_cloud.failures),
                                metrics=_base_metrics,
                            )
                        else:
                            report = ExtractionBatchReport(
                                results=report.results,
                                failures=report.failures,
                                metrics=_base_metrics,
                            )
                    else:
                        report = ExtractionBatchReport(
                            results=report.results,
                            failures=report.failures,
                            metrics=_base_metrics,
                        )
            else:
                report = await extract_entities(tasks, **_extract_kwargs)
        if not isinstance(report, ExtractionBatchReport):
            fresh_results = report
            failures: list[ExtractionFailureItem] = []
            metrics = _ghost_b_metrics_for_skipped(fresh_results)
        else:
            fresh_results = report.results
            failures = report.failures
            metrics = report.metrics
        metrics = dict(metrics or {})
        metrics["schema_lens"] = schema_lens.to_dict()
        ghost_b_failures.extend(failures)
        nonlocal ghost_b_metrics
        ghost_b_metrics = metrics
        if len(fresh_results) < len(tasks):
            if not fresh_results and tasks:
                warning = _ghost_b_total_failure_warning(total=len(tasks))
                warnings.append(warning)
                logger.error(
                    "phase=ghost_b_total_failure doc=%s corpus=%s total=%d failures=%d error_counts=%s",
                    doc_id[:12],
                    corpus_id[:8],
                    len(tasks),
                    len(failures),
                    metrics.get("error_counts") if isinstance(metrics, dict) else None,
                )
                if ghost_b_from_staging:
                    logger.warning(
                        "phase=ghost_b_resume_using_staging_after_missing_retry_failure "
                        "doc=%s corpus=%s staged=%d failed_missing=%d",
                        doc_id[:12],
                        corpus_id[:8],
                        len(ghost_b_from_staging),
                        len(tasks),
                    )
                    return ghost_b_from_staging
                return None
            missing_ids = sorted(
                {t.chunk_id for t in tasks} - {r.chunk_id for r in fresh_results}
            )
            warning = _ghost_b_partial_warning(
                extracted=len(fresh_results),
                total=len(tasks),
            )
            warnings.append(warning)
            logger.warning(
                "phase=ghost_b_partial doc=%s corpus=%s extracted=%d total=%d missing_sample=%s",
                doc_id[:12],
                corpus_id[:8],
                len(fresh_results),
                len(tasks),
                missing_ids[:5],
            )
        results = list(fresh_results)
        if ghost_b_from_staging:
            merged_by_chunk = {result.chunk_id: result for result in ghost_b_from_staging}
            for result in fresh_results:
                merged_by_chunk[result.chunk_id] = result
            results = list(merged_by_chunk.values())
        return results

    # Keep these branches sequential inside a document. User-configured
    # summary/extraction pool concurrency already fans out within each branch;
    # running both branches at once doubles provider pressure and makes
    # high-throughput API settings unsafe during batch ingest.
    summaries: list[SummaryResult] | None = None
    try:
        summaries = await _a_branch()
    except GhostAFailure as exc:
        if not bool(getattr(settings, "INGEST_SAFE_SUMMARY_FAILURES", True)):
            raise
        warning = (
            "Ghost A parent summarization deferred: "
            f"{exc}. Continuing ingest so chunks/extractions can be staged; "
            "rerun this document later to fill summaries."
        )
        warnings.append(warning)
        ws.summaries_indexed = False
        logger.warning(
            "phase=ghost_a_deferred doc=%s corpus=%s reason=%s",
            doc_id[:12],
            corpus_id[:8],
            exc,
        )
    ghost_b_out = await _b_branch()
    # Phase A: deterministic enrichment (numeric + qualitative facts, in-text
    # aliases) now runs INSIDE services.ghost_b_local per chunk, so the former
    # external Pass-1/Pass-2 (services.ingestion.slm_enrich) call is removed —
    # keeping it would double the deterministic facts and re-introduce the
    # retired SLM sidecar. The slm_enrich module stays in the tree for
    # reference but is no longer wired into ingestion.
    if ghost_b_metrics is None:
        ghost_b_metrics = _ghost_b_metrics_for_skipped(ghost_b_out)
    ghost_b_metrics = _ghost_b_metrics_with_failures(
        ghost_b_out,
        ghost_b_failures,
        ghost_b_metrics,
    )
    return GhostRunResult(
        summaries=summaries,
        ghost_b_out=ghost_b_out,
        warnings=warnings,
        ghost_b_failures=ghost_b_failures,
        ghost_b_metrics=ghost_b_metrics,
    )


def _document_facet_profile(facet_profile: dict | None) -> dict | None:
    """Return the compact document-level facet profile.

    Child/parent facet maps are stored on the child/parent records themselves,
    not duplicated inside documents.facet_profile.
    """

    if not isinstance(facet_profile, dict):
        return None
    return {
        "schema_version": facet_profile.get("schema_version"),
        "doc_facets": facet_profile.get("doc_facets") or [],
        "facet_ids": facet_profile.get("facet_ids") or [],
        "facet_text": facet_profile.get("facet_text") or "",
        "primary_facet_id": facet_profile.get("primary_facet_id"),
        "source": facet_profile.get("source") or "ingestion",
    }


def _metadata_with_facets(metadata: dict | None, facet_meta: dict | None) -> dict:
    base = dict(metadata or {})
    semantic = (facet_meta or {}).get("semantic_facets")
    if semantic:
        base["semantic_facets"] = semantic
    return base


def _build_parent_dicts(
    parents,
    summaries: list[SummaryResult] | None,
    parent_facets: dict[str, dict] | None = None,
) -> list[dict]:
    """Assemble parent chunk rows, populating ``summary`` from Ghost A output.

    Code lane (Phase 1) — emits chunk_kind, language, and metadata on every
    parent. `chunk_kind` was a pre-existing gap here (children had it, parents
    didn't); fixing alongside the code-lane additions so retrieval / decoration
    code reading parent records gets a consistent shape.
    """
    summary_by_parent = {s.parent_id: s for s in (summaries or [])}
    rows: list[dict] = []
    parent_facets = parent_facets or {}
    for p in parents:
        facet_meta = parent_facets.get(p.parent_id, {})
        sr = summary_by_parent.get(p.parent_id)
        rows.append(
            {
                "parent_id": p.parent_id,
                "doc_id": p.doc_id,
                "corpus_id": p.corpus_id,
                "text": p.text,
                "heading_path": p.heading_path,
                "source_tier": p.source_tier,
                "page_start": getattr(p, "page_start", None),
                "page_end": getattr(p, "page_end", None),
                "summary": getattr(sr, "summary", None) if sr else None,
                "schema_version": getattr(sr, "schema_version", None) if sr else None,
                "summary_type": getattr(sr, "summary_type", None) if sr else None,
                "central_claim": getattr(sr, "central_claim", None) if sr else None,
                "key_points": getattr(sr, "key_points", None) if sr else None,
                "main_mechanism": getattr(sr, "main_mechanism", None) if sr else None,
                "concept_tags": getattr(sr, "concept_tags", None) if sr else None,
                "entity_hints": getattr(sr, "entity_hints", None) if sr else None,
                "retrieval_uses": getattr(sr, "retrieval_uses", None) if sr else None,
                "abstraction_level": getattr(sr, "abstraction_level", None) if sr else None,
                "source_child_ids": (
                    getattr(sr, "source_child_ids", None)
                    if sr and getattr(sr, "source_child_ids", None)
                    else [c.chunk_id for c in p.children]
                ),
                "domain": getattr(sr, "domain", None) if sr else None,
                "semantic_chunk_type": getattr(sr, "semantic_chunk_type", None) if sr else None,
                "key_terms": getattr(sr, "key_terms", None) if sr else None,
                "mechanisms": getattr(sr, "mechanisms", None) if sr else None,
                "topic_key": _topic_key_for(
                    getattr(sr, "domain", None) if sr else None, p.heading_path
                ),
                "child_ids": [c.chunk_id for c in p.children],
                "chunk_kind": getattr(p, "chunk_kind", ChunkKind.BODY),
                "language": getattr(p, "language", None),
                "metadata": _metadata_with_facets(
                    getattr(p, "metadata", {}) or {},
                    facet_meta,
                ),
                "facet_ids": facet_meta.get("facet_ids") or [],
                "facet_text": facet_meta.get("facet_text") or "",
                "content_facet_ids": facet_meta.get("content_facet_ids") or [],
                "content_facet_text": facet_meta.get("content_facet_text") or "",
                "content_facet_source": facet_meta.get("content_facet_source") or "",
                "content_facet_confidence": facet_meta.get("content_facet_confidence"),
            }
        )
    return rows


def _build_child_dicts(
    children,
    user_id: str,
    child_facets: dict[str, dict] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    child_facets = child_facets or {}
    for c in children:
        facet_meta = child_facets.get(c.chunk_id, {})
        rows.append(
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
                "language": getattr(c, "language", None),
                # M1: domain is a parent-level Ghost-A signal; on the child it
                # is populated by scripts/backfill_child_domain.py (Mongo +
                # Qdrant) after Ghost A. Present here as None so the schema is
                # consistent and a later set_payload can update it in place.
                "domain": getattr(c, "domain", None),
                "metadata": _metadata_with_facets(
                    getattr(c, "metadata", {}) or {},
                    facet_meta,
                ),
                "facet_ids": facet_meta.get("facet_ids") or [],
                "facet_text": facet_meta.get("facet_text") or "",
                "content_facet_ids": facet_meta.get("content_facet_ids") or [],
                "content_facet_text": facet_meta.get("content_facet_text") or "",
                "content_facet_source": facet_meta.get("content_facet_source") or "",
                "content_facet_confidence": facet_meta.get("content_facet_confidence"),
            }
        )
    return rows


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
    facet_profile: dict | None,
    ws: WriteState,
    replace_existing_artifacts: bool = False,
    source_url: str | None = None,
    source_identity: dict | None = None,
    source_meta: dict | None = None,
) -> None:
    """Persist compact document metadata and split durable ingest artifacts.

    Parent summaries are stored in ``parent_chunks`` and Ghost B outputs are
    stored in ``ghost_b_extractions``. The ``documents`` row keeps only counts,
    flags, metrics, and human-facing metadata.
    """
    parent_dicts = _build_parent_dicts(
        parents,
        summaries,
        (facet_profile or {}).get("parent_facets"),
    )
    # When blocking is on, near-duplicates are skipped up-front (before chunking)
    # so anything reaching here is already non-duplicate — skip the redundant
    # full-corpus scan. The advisory scan only runs when blocking is disabled.
    duplicate_candidates = (
        []
        if settings.INGEST_BLOCK_NEAR_DUPLICATES
        else await _find_near_duplicate_documents(
            db=db,
            corpus_id=corpus_id,
            doc_id=doc_id,
            parent_texts=[p.get("text") or "" for p in parent_dicts],
        )
    )
    child_dicts = _build_child_dicts(
        children,
        user_id,
        (facet_profile or {}).get("child_facets"),
    )
    ghost_b_staging = (
        [asdict(r) for r in ghost_b_out] if ghost_b_out is not None else None
    )
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
        "source_mime": source_mime,
        "source_tier": source_tier.value,
        # M2 parse-time metadata + per-document routing report
        **{k: v for k, v in (source_meta or {}).items()
           if k in ("title", "author", "document_date", "source_type", "routing_trace")},
        "ingestion_config": freeze_snapshot(ingestion_config),
        "chunking_config": chunking_config,
        "write_state": ws.model_dump(),
        "ghost_b_failures": ghost_b_failure_rows[:20],
        "parent_count": len(parent_dicts),
        "child_count": len(child_dicts),
        "summary_count": len(summaries or []),
        "ghost_b_staging_count": len(ghost_b_staging or []),
        "ghost_b_failure_count": len(ghost_b_failure_rows),
        "ghost_b_metrics": ghost_b_metrics or {},
        "schema_lens": (ghost_b_metrics or {}).get("schema_lens"),
        "facet_profile": _document_facet_profile(facet_profile),
        "is_near_duplicate": bool(duplicate_candidates),
        "near_duplicate_candidates": duplicate_candidates,
        "created_at": now,
        "updated_at": now,
    }
    doc_record.update(
        source_identity_doc_fields(
            source_url=source_url,
            source_identity=source_identity,
        )
    )
    if duplicate_candidates:
        logger.warning(
            "phase=duplicate_check doc=%s corpus=%s filename=%s candidates=%s",
            doc_id[:12],
            corpus_id[:8],
            filename,
            duplicate_candidates,
        )
    if replace_existing_artifacts:
        await db["parent_chunks"].delete_many({"doc_id": doc_id, "corpus_id": corpus_id})
        await db["chunks"].delete_many({"doc_id": doc_id, "corpus_id": corpus_id})
        await db["ghost_b_extractions"].delete_many(
            {"doc_id": doc_id, "corpus_id": corpus_id}
        )
    await mongo_writer.upsert_document(db, doc_record)
    await mongo_writer.upsert_parent_chunks(db, parent_dicts)
    await mongo_writer.upsert_chunks(db, child_dicts)
    if ghost_b_staging is not None:
        await mongo_writer.stash_ghost_b(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            results=ghost_b_staging,
        )
    await mongo_writer.stash_ghost_b_failures(
        db,
        doc_id=doc_id,
        corpus_id=corpus_id,
        failures=ghost_b_failure_rows,
    )


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
    facet_profile: dict | None,
    ws: WriteState,
    source_url: str | None = None,
    source_identity: dict | None = None,
) -> None:
    """Create a compact progress document and checkpoint parent rows early."""
    from services.ingestion_service import freeze_snapshot

    now = datetime.utcnow()
    parent_dicts = _build_parent_dicts(
        parents,
        None,
        (facet_profile or {}).get("parent_facets"),
    )
    doc_record = {
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "file_id": file_id,
        "filename": filename,
        "source_mime": source_mime,
        "source_tier": source_tier.value,
        "ingestion_config": freeze_snapshot(ingestion_config),
        "chunking_config": chunking_config,
        "write_state": ws.model_dump(),
        "parent_count": len(parent_dicts),
        "child_count": 0,
        "summary_count": 0,
        "ghost_b_staging_count": 0,
        "ghost_b_failure_count": 0,
        "facet_profile": _document_facet_profile(facet_profile),
        "ghost_b_failures": [],
        "ghost_b_metrics": {},
        "ingest_stage": "chunked",
        "created_at": now,
        "updated_at": now,
    }
    doc_record.update(
        source_identity_doc_fields(
            source_url=source_url,
            source_identity=source_identity,
        )
    )
    await mongo_writer.upsert_document(db, doc_record)
    await mongo_writer.upsert_parent_chunks(db, parent_dicts)


async def _ensure_parse_progress_document(
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
    ws: WriteState,
    source_url: str | None = None,
    source_identity: dict | None = None,
) -> None:
    """Create the first compact row as soon as parse resolves doc_id."""
    from services.ingestion_service import freeze_snapshot

    now = datetime.utcnow()
    doc_record = {
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "user_id": user_id,
        "file_id": file_id,
        "filename": filename,
        "source_mime": source_mime,
        "source_tier": source_tier.value,
        "ingestion_config": freeze_snapshot(ingestion_config),
        "chunking_config": {},
        "write_state": ws.model_dump(),
        "parent_count": 0,
        "child_count": 0,
        "summary_count": 0,
        "ghost_b_staging_count": 0,
        "ghost_b_failure_count": 0,
        "facet_profile": {},
        "ghost_b_failures": [],
        "ghost_b_metrics": {},
        "ingest_stage": "chunking",
        "created_at": now,
        "updated_at": now,
    }
    doc_record.update(
        source_identity_doc_fields(
            source_url=source_url,
            source_identity=source_identity,
        )
    )
    await mongo_writer.upsert_document(db, doc_record)


async def _checkpoint_child_chunks(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    children,
    facet_profile: dict | None,
) -> None:
    """Persist child chunks before Ghost B so graph failures cannot strand them."""

    child_dicts = _build_child_dicts(
        children,
        user_id,
        (facet_profile or {}).get("child_facets"),
    )
    await mongo_writer.upsert_chunks(db, child_dicts)
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "child_count": len(child_dicts),
                "ingest_stage": "chunks_saved",
                "updated_at": datetime.utcnow(),
            }
        },
    )
    logger.info(
        "phase=chunk_checkpoint doc=%s corpus=%s children=%d",
        doc_id[:12],
        corpus_id[:8],
        len(child_dicts),
    )


async def _mark_ingest_failed(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    message: str,
    stage: str,
) -> None:
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "error": message[:1000],
                "ingest_stage": stage,
                "updated_at": datetime.utcnow(),
            },
            "$addToSet": {
                "write_state.warnings": f"Ingest failed: {message[:1000]}",
            },
        },
    )


async def _mark_ingest_skipped_duplicate(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    candidates: list[dict],
) -> str:
    """Mark a document skipped because it near-duplicates an existing one. No
    chunks/vectors/graph are written. Returns a human-readable reason string."""
    top = candidates[0] if candidates else {}
    reason = (
        f"Near-duplicate of '{top.get('filename') or top.get('doc_id') or '?'}' "
        f"(lexical overlap {top.get('similarity')}). Skipped to avoid doubling "
        "corpus weight; set INGEST_BLOCK_NEAR_DUPLICATES=false to force ingest."
    )
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "ingest_stage": "skipped_duplicate",
                "skipped_reason": reason,
                "near_duplicate_of": candidates,
                "is_near_duplicate": True,
                "updated_at": datetime.utcnow(),
            },
            "$addToSet": {"write_state.warnings": reason},
        },
    )
    return reason


async def _flag_document_near_duplicate(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    candidates: list[dict],
) -> str:
    """Flag a document as a near-duplicate WITHOUT skipping ingestion. Used when
    overlap is high but the doc is NOT near-identical to an existing one — it may
    be a distinct work that merely shares prose (e.g. a different-language
    edition of a textbook). The doc ingests normally and is marked for review so
    the corpus dedup tool can surface it; it is never silently dropped."""
    top = candidates[0] if candidates else {}
    reason = (
        f"Near-duplicate of '{top.get('filename') or top.get('doc_id') or '?'}' "
        f"(overlap {top.get('similarity')}, containment {top.get('containment')}). "
        "Ingested and FLAGGED for review — not auto-skipped because it may be a "
        "distinct work; resolve via the corpus duplicate tool."
    )
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "$set": {
                "is_near_duplicate": True,
                "near_duplicate_flagged": True,
                "near_duplicate_of": candidates,
                "updated_at": datetime.utcnow(),
            },
            "$addToSet": {"write_state.warnings": reason},
        },
    )
    return reason


async def _call_optional_callback(callback, *args) -> None:
    if callback is None:
        return
    try:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logger.debug("ingest callback raised: %s", exc)


async def _emit_ingest_phase(
    callback,
    phase: str,
    *,
    doc_id: str,
    corpus_id: str,
    **details,
) -> None:
    payload = {"doc_id": doc_id, "corpus_id": corpus_id, **details}
    await _call_optional_callback(callback, phase, payload)


async def _set_ingest_stage(
    *,
    db: AsyncIOMotorDatabase,
    doc_id: str,
    corpus_id: str,
    stage: str,
    on_phase=None,
    **details,
) -> None:
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"$set": {"ingest_stage": stage, "updated_at": datetime.utcnow()}},
    )
    await _emit_ingest_phase(
        on_phase,
        stage,
        doc_id=doc_id,
        corpus_id=corpus_id,
        **details,
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
    summary_texts = [s.summary for s in summary_list]
    all_texts = [*child_texts, *summary_texts]
    if not all_texts:
        return {}, {}

    # Decrypt embed_api_key once per ingest; embed_batch sees plaintext only.
    raw_key = getattr(config, "embed_api_key", None)
    plaintext_key = _decrypt_api_key(raw_key) if raw_key else None
    if raw_key and plaintext_key is None:
        # Value stored but couldn't decrypt — most likely plaintext passed
        # through a migration shim. Pass as-is rather than failing the embed.
        plaintext_key = raw_key

    all_vectors = await embed_batch(
        all_texts,
        mode=getattr(config, "embed_mode", "local"),
        expected_dim=getattr(config, "embedding_dimension", 1024),
        expected_model_id=getattr(config, "embedding_model_id", None),
        base_url=getattr(config, "embed_base_url", None),
        api_key=plaintext_key,
        max_concurrent=getattr(config, "embed_max_concurrent", None),
        modal_containers=getattr(config, "modal_containers", None),
        api_pool=_build_ghost_pool(getattr(config, "embedding_models", None)),
    )
    # ALIGNMENT GUARD — a short vector list would silently drop the tail
    # child below (zip) or, with summaries present, hand summary vectors to
    # children. Fail the doc loudly instead; Phase 5+6 is best-effort, so
    # the item lands failed-with-reason and re-embeds on resume.
    if len(all_vectors) != len(all_texts):
        raise RuntimeError(
            f"embed_batch returned {len(all_vectors)} vectors for "
            f"{len(all_texts)} texts — refusing to slice misaligned"
        )
    split = len(child_texts)
    child_vecs = all_vectors[:split]
    summary_vecs = all_vectors[split:]
    vec_map = {c.chunk_id: v for c, v in zip(vector_children, child_vecs)}
    summary_vec_map = {s.parent_id: v for s, v in zip(summary_list, summary_vecs)}
    return vec_map, summary_vec_map


async def _write_qdrant_for_doc(
    *,
    qdrant_client: AsyncQdrantClient,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    filename: str,
    parents,
    children,
    vec_map: dict[str, list[float]],
    summaries: list[SummaryResult] | None,
    summary_vec_map: dict[str, list[float]],
    config: IngestionConfig,
    child_sparse_map: dict[str, Any] | None = None,
    summary_sparse_map: dict[str, Any] | None = None,
    facet_profile: dict | None = None,
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
    schema_version = (facet_profile or {}).get("schema_version")
    doc_facet_ids = (facet_profile or {}).get("facet_ids") or []
    child_facet_map = (facet_profile or {}).get("child_facets") or {}
    parent_facet_map = (facet_profile or {}).get("parent_facets") or {}

    def _as_payload(c) -> dict:
        facet_meta = child_facet_map.get(c.chunk_id, {})
        metadata = _metadata_with_facets(getattr(c, "metadata", {}) or {}, facet_meta)
        # CRITICAL: include language + metadata so the Qdrant writer can
        # stamp them onto the point payload (qdrant_writer.upsert_children
        # at line 548-549 reads these fields). Without this, code-lane
        # chunks landed in Qdrant with `language=None` and `metadata={}`
        # — defeating BM25 lexical augmentation against symbols_called /
        # roblox_apis / symbols_defined at retrieval time (Pt 11.1).
        return {
            "chunk_id": c.chunk_id,
            "parent_id": c.parent_id,
            "doc_id": c.doc_id,
            "corpus_id": c.corpus_id,
            "user_id": user_id,
            "filename": filename,
            "doc_name": filename,
            "text": c.text,
            "source_tier": c.source_tier,
            "heading_path": c.heading_path,
            "page_start": getattr(c, "page_start", None),
            "page_end": getattr(c, "page_end", None),
            "chunk_kind": getattr(c, "chunk_kind", ChunkKind.BODY),
            "language": getattr(c, "language", None),
            "metadata": metadata,
            "facet_ids": facet_meta.get("facet_ids") or doc_facet_ids[:6],
            "facet_text": facet_meta.get("facet_text") or "",
            "content_facet_ids": facet_meta.get("content_facet_ids") or [],
            "content_facet_text": facet_meta.get("content_facet_text") or "",
            "content_facet_source": facet_meta.get("content_facet_source") or "",
            "content_facet_confidence": facet_meta.get("content_facet_confidence"),
            "doc_facet_ids": doc_facet_ids,
            "facet_schema_version": schema_version,
        }

    # Replace the document's vector surface, don't only upsert. Retries and
    # chunker/schema changes can remove chunk ids; stale points break verify.
    await qdrant_writer.delete_points_by_doc(qdrant_client, corpus_id, doc_id)

    if "naive" in target_cols:
        dicts = [_as_payload(c) for c in vector_children]
        vecs = [vec_map[c.chunk_id] for c in vector_children]
        sparse = [child_sparse_map.get(c.chunk_id) for c in vector_children]
        await qdrant_writer.upsert_children(
            qdrant_client, corpus_id, dicts, vecs, ["naive"],
            sparse_vectors=sparse,
        )

    hrag_eligible = [c for c in vector_children if c.source_tier in _HRAG_TIERS]
    if "hrag" in target_cols and hrag_eligible:
        dicts = [_as_payload(c) for c in hrag_eligible]
        vecs = [vec_map[c.chunk_id] for c in hrag_eligible]
        sparse = [child_sparse_map.get(c.chunk_id) for c in hrag_eligible]
        await qdrant_writer.upsert_children(
            qdrant_client, corpus_id, dicts, vecs, ["hrag"],
            sparse_vectors=sparse,
        )

    if "graph" in target_cols:
        dicts = [_as_payload(c) for c in vector_children]
        vecs = [vec_map[c.chunk_id] for c in vector_children]
        sparse = [child_sparse_map.get(c.chunk_id) for c in vector_children]
        await qdrant_writer.upsert_children(
            qdrant_client, corpus_id, dicts, vecs, ["graph"],
            sparse_vectors=sparse,
        )

    if summaries:
        hp_map = {p.parent_id: p.heading_path for p in parents}
        kind_map = {p.parent_id: getattr(p, "chunk_kind", ChunkKind.BODY) for p in parents}
        summary_payloads = []
        for s in summaries:
            facet_meta = parent_facet_map.get(s.parent_id, {})
            summary_payloads.append(
                {
                    "parent_id": s.parent_id,
                    "doc_id": s.doc_id,
                    "corpus_id": s.corpus_id,
                    "source_tier": s.source_tier,
                    "filename": filename,
                    "doc_name": filename,
                    "summary": s.summary,
                    "schema_version": getattr(s, "schema_version", None),
                    "summary_type": getattr(s, "summary_type", None),
                    "central_claim": getattr(s, "central_claim", None),
                    "key_points": getattr(s, "key_points", None),
                    "main_mechanism": getattr(s, "main_mechanism", None),
                    "concept_tags": getattr(s, "concept_tags", None),
                    "entity_hints": getattr(s, "entity_hints", None),
                    "retrieval_uses": getattr(s, "retrieval_uses", None),
                    "abstraction_level": getattr(s, "abstraction_level", None),
                    "source_child_ids": getattr(s, "source_child_ids", None),
                    "heading_path": hp_map.get(s.parent_id),
                    "user_id": user_id,
                    "chunk_kind": kind_map.get(s.parent_id, ChunkKind.BODY),
                    "metadata": _metadata_with_facets({}, facet_meta),
                    "facet_ids": facet_meta.get("facet_ids") or doc_facet_ids[:6],
                    "facet_text": facet_meta.get("facet_text") or "",
                    "content_facet_ids": facet_meta.get("content_facet_ids") or [],
                    "content_facet_text": facet_meta.get("content_facet_text") or "",
                    "content_facet_source": facet_meta.get("content_facet_source") or "",
                    "content_facet_confidence": facet_meta.get("content_facet_confidence"),
                    "doc_facet_ids": doc_facet_ids,
                    "facet_schema_version": schema_version,
                }
            )
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


def _searchable_text(chunk) -> str:
    """Phase 4.5 — augment a chunk's text with metadata tokens for BM25
    indexing. Code chunks get symbols_defined / imports / file_path
    appended so lexical search hits structured-API names even when the
    chunk body uses local aliases. Prose chunks pass through unchanged.

    The augmented text is ONLY fed to the sparse encoder. The Qdrant
    payload carries the unaugmented chunk text, and hydrate.py can still
    replace chunk.text with the full Mongo parent body at retrieval.
    """
    base = getattr(chunk, "text", "") or ""
    meta = getattr(chunk, "metadata", None) or {}
    if not meta:
        return base
    tokens: list[str] = []
    for sym in (meta.get("symbols_defined") or [])[:30]:
        s = str(sym).strip()
        if s:
            tokens.append(s)
    for imp in (meta.get("imports") or [])[:15]:
        s = str(imp).strip()
        if s:
            tokens.append(s)
    for call in (meta.get("symbols_called") or [])[:30]:
        s = str(call).strip()
        if s:
            tokens.append(s)
    # Phase 5 — explicit roblox_apis indexing. In production these mostly
    # overlap with symbols_called (the regex extractor unions them into
    # symbols_called at _extract_metadata_for_chunk time), but other paths
    # (graphify backfill, manual writes) can populate roblox_apis without
    # touching symbols_called. Index defensively so a Roblox-flavored
    # corpus's BM25 surface always hits engine terms.
    seen_tokens = {t for t in tokens}
    for api in (meta.get("roblox_apis") or [])[:30]:
        s = str(api).strip()
        if s and s not in seen_tokens:
            tokens.append(s)
            seen_tokens.add(s)
    file_path = str(meta.get("file_path") or "").strip()
    if file_path:
        tokens.append(file_path)
        # Tokenize the path so each segment becomes a BM25 term:
        # "ReplicatedStorage/Combat/CombatModule.luau"
        # → ReplicatedStorage Combat CombatModule luau
        for part in file_path.replace("/", " ").replace("\\", " ").replace(".", " ").split():
            if part and part not in tokens:
                tokens.append(part)
    if not tokens:
        return base
    return base + "\n\n" + " ".join(tokens)


def _synthesize_code_extraction_results(graph_children) -> list:
    """Phase 4 code graph — turn each CODE-kind chunk's AST metadata into a
    synthetic ExtractionResult so it flows through write_document_graph
    alongside Ghost B's prose extractions.

    Inputs are tier_chunker ChildChunk objects (post-filter against
    NOISY_KINDS). For every chunk with `chunk_kind == ChunkKind.CODE`:
      - `metadata.symbols_defined` → :Entity(Method) with MENTIONS
      - `metadata.imports`         → :Entity(Artifact) with MENTIONS
    Confidence is fixed at 1.0 because the symbols came from the
    tree-sitter parse, not from an LLM guess. Within a single chunk the
    same symbol is deduped (case-insensitive) so a function and its
    fully-qualified form ("foo" vs "Module.foo") don't double-count.

    Returns a list of ExtractionResult — empty when no CODE chunks were
    passed in or none carried symbols/imports.
    """
    from services.ghost_b import EntityItem, ExtractionResult
    from services.graph.roblox_ontology import resolve_code_entity_type
    from services.ingestion.section_classifier import ChunkKind

    out: list[ExtractionResult] = []
    for c in graph_children:
        if getattr(c, "chunk_kind", None) != ChunkKind.CODE:
            continue
        meta = getattr(c, "metadata", None) or {}
        entities: list[EntityItem] = []
        seen: set[str] = set()
        # Phase 5 Gate 1 — `resolve_code_entity_type(name, c)` returns a
        # scoped Roblox type (RobloxService / RobloxNetworkPrimitive /
        # RobloxClass / LuauDataType) ONLY when the chunk is Luau/Lua
        # or its metadata already carries `roblox_apis`. Returns None for
        # everything else and we fall through to the default Method type.
        # This avoids polluting non-Roblox corpora with Roblox-specific
        # entity tags (a Python `Spring` variable stays Method, a Luau
        # `Spring` stays Method too — `Spring` is intentionally NOT in
        # _ROBLOX_ENTITY_TYPES because it collides with Fusion's Spring
        # physics module name).
        for name in meta.get("symbols_defined", []) or []:
            sym = str(name).strip()
            if not sym:
                continue
            key = sym.lower()
            if key in seen:
                continue
            seen.add(key)
            entity_type = resolve_code_entity_type(sym, c) or "Method"
            entities.append(EntityItem(
                canonical_name=sym, surface_form=sym,
                entity_type=entity_type, confidence=1.0,
            ))
        # symbols_called now includes Roblox APIs from Step 1's regex
        # extractor + graphify backfill (Pt 11.1). The resolver turns
        # `TweenService` into `RobloxService`, etc.; unknown names default
        # to Method so call-site BM25 indexing still works.
        for sym in meta.get("symbols_called", []) or []:
            sym = str(sym).strip()
            if not sym:
                continue
            key = sym.lower()
            if key in seen:
                continue
            seen.add(key)
            entity_type = resolve_code_entity_type(sym, c) or "Method"
            entities.append(EntityItem(
                canonical_name=sym, surface_form=sym,
                entity_type=entity_type, confidence=1.0,
            ))
        for imp in meta.get("imports", []) or []:
            src = str(imp).strip()
            if not src:
                continue
            key = src.lower()
            if key in seen:
                continue
            seen.add(key)
            entity_type = resolve_code_entity_type(src, c) or "Artifact"
            entities.append(EntityItem(
                canonical_name=src, surface_form=src,
                entity_type=entity_type, confidence=1.0,
            ))
        if not entities:
            continue
        out.append(ExtractionResult(
            schema_version="polymath.code.v1",
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            corpus_id=c.corpus_id,
            text=getattr(c, "text", "") or "",
            entities=entities,
            relations=[],
            facts=[],
        ))
    return out


async def _write_neo4j_for_doc(
    *,
    neo4j_driver,
    doc_id: str,
    corpus_id: str,
    user_id: str,
    file_id: str,
    children,
    ghost_b_out: list[ExtractionResult] | None,
    filename: str | None = None,
    parents: list | None = None,
    ghost_b_metrics: dict | None = None,
    schema_lens_id: str | None = None,
    source_tier: str | None = None,
    graphify_enrichment=None,
) -> None:
    """Delegate to neo4j_writer.write_document_graph with rich anchor metadata.

    `filename` and `parents` are forwarded so the :Document node serves as
    a Brain-View cluster anchor without a follow-up MongoDB round-trip. The
    three ghost_b_* metrics are flattened out of the nested metrics dict for
    Neo4j (which doesn't accept dict-valued node properties).
    """
    from services.graph.neo4j_writer import delete_document_graph, write_document_graph
    from services.ingestion.section_classifier import NOISY_KINDS

    metrics = ghost_b_metrics or {}
    success_rate = metrics.get("success_rate")
    extracted = metrics.get("extracted_chunks")
    total = metrics.get("requested_chunks")

    # Pt 8c — exclude only NOISY_KINDS from the graph (toc / index /
    # bibliography / front_matter / back_matter / appendix). Body AND code
    # chunks pass through. Mongo `chunks` keeps the noisy ones so opt-in
    # citation queries still work; the GRAPH stays clean.
    #
    # Phase 4 (code lane): code chunks aren't extracted by Ghost B (skipped
    # to avoid hallucinated Method/Artifact entities), but their AST
    # metadata.symbols_defined / metadata.imports are already deterministic
    # ground truth. Synthesize ExtractionResult objects from that metadata
    # so they flow through the standard write_document_graph entity +
    # MENTIONS pipeline. Confidence is fixed at 1.0 because the symbols
    # came from the tree-sitter parse, not from an LLM guess.
    graph_children = [c for c in children if c.chunk_kind not in NOISY_KINDS]
    skipped_count = len(children) - len(graph_children)
    if skipped_count:
        logger.info(
            "Pt8c neo4j writer skipping %d noisy chunks (toc/index/biblio/...) "
            "of %d total for doc_id=%s",
            skipped_count, len(children), doc_id,
        )

    code_extraction_results = _synthesize_code_extraction_results(graph_children)

    if code_extraction_results:
        logger.info(
            "Phase 4 code graph: synthesized %d ExtractionResult objects "
            "(deterministic, from metadata.symbols_defined / imports) "
            "for doc_id=%s", len(code_extraction_results), doc_id[:12],
        )

    all_extraction_results = list(ghost_b_out or []) + code_extraction_results

    # Neo4j MERGE is idempotent for existing chunk ids, but it cannot remove
    # chunk ids that disappeared on retry. Replace this doc before rewriting.
    await delete_document_graph(neo4j_driver, corpus_id=corpus_id, doc_id=doc_id)

    await write_document_graph(
        driver=neo4j_driver,
        doc_id=doc_id,
        corpus_id=corpus_id,
        extraction_results=all_extraction_results,
        user_id=user_id,
        file_id=file_id,
        all_chunk_ids=[c.chunk_id for c in graph_children],
        filename=filename,
        parent_count=len(parents) if parents is not None else 0,
        schema_lens_id=schema_lens_id,
        source_tier=source_tier,
        ghost_b_success_rate=float(success_rate) if success_rate is not None else None,
        ghost_b_extracted=int(extracted) if extracted is not None else None,
        ghost_b_total=int(total) if total is not None else None,
    )

    # Phase 4.5 — opt-in graphify augmentation. Runs only when the setting
    # is on; safe-by-default off so private/emotional corpora are never
    # touched. Code chunks get cross-symbol :CALLS edges and Leiden
    # community labels on top of Phase 4's deterministic entity write.
    # The augmenter never raises — failures degrade silently to no-op.
    #
    # Pt 11.1 — `graphify_enrichment` is now passed in by run_ingest_job
    # (computed earlier so it could backfill metadata.symbols_called for
    # BM25 indexing). When provided, reuse it for the Neo4j writes. When
    # None (e.g. called directly outside run_ingest_job, like
    # backfill_document_graph), compute it locally as before for back-compat.
    try:
        from config import get_settings
        if get_settings().GRAPHIFY_AUGMENT_CODE_LANE:
            from services import code_graph_augmenter
            from services.graph.neo4j_writer import write_graphify_enrichment
            from services.ingestion.section_classifier import ChunkKind

            enrichment = graphify_enrichment
            if enrichment is None:
                code_only = [c for c in graph_children if c.chunk_kind == ChunkKind.CODE]
                if code_only:
                    enrichment = code_graph_augmenter.augment_code_chunks(code_only)
            if enrichment is not None:
                await write_graphify_enrichment(
                    driver=neo4j_driver,
                    corpus_id=corpus_id,
                    enrichment=enrichment,
                )
    except Exception as exc:
        # Pure augmentation — never block the ingest if it fails.
        logger.warning(
            "Phase 4.5 graphify augmentation skipped for doc=%s: %s",
            doc_id[:12] if doc_id else "?", exc,
        )


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
    source_url: str | None = None,
    source_identity: dict | None = None,
    # Phase K — called with the resolved doc_id as soon as docling parse
    # completes, BEFORE the expensive ghost + embed + write phases run.
    # The HTTP endpoint uses this to return {doc_id, status: "queued"} in
    # under ~2s even when the full pipeline will run for 30+ minutes.
    on_doc_id: "Callable[[str], None] | None" = None,
    on_phase: "Callable[[str, dict], None] | None" = None,
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

    # Pt 10e — sanitize the filename ONCE at the top of the pipeline so every
    # downstream consumer (Mongo document.filename, Ghost B prompt context,
    # chat citation display) sees the cleaned name. The aggregator markers
    # (libgen.li, Anna's Archive, file-hash suffixes) used to leak into the
    # extraction context and pollute entity names. Sanitizer is conservative
    # and idempotent — clean filenames pass through unchanged.
    filename = _sanitize_filename(filename)

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
    startup_profile = _resource_profile_for_config(
        ingestion_config,
        source_location=source_url,
    )
    log_resource_profile(
        startup_profile,
        extra={
            "doc": "preparse",
            "corpus": cid8,
            "filename": filename,
            "phase": "startup",
        },
    )

    # ── Phase 1: Parse ───────────────────────────────────────────────────
    async with _PARSE_SEMAPHORE:
        t0 = time.monotonic()
        mime_hint, _ = mimetypes.guess_type(filename)
        parse_result = await docling_adapter.parse_document(
            data,
            filename=filename,
            mime=mime_hint or "application/octet-stream",
            do_ocr=False,
        )
    docling_adapter.finalize_source_meta(parse_result, filename)  # M2 + routing_trace
    _norm = re.sub(
        r"\s+", " ", (parse_result.markdown or parse_result.text or "").strip()
    )
    doc_id = hashlib.sha256(_norm.encode("utf-8")).hexdigest()
    source_tier = parse_result.source_tier
    source_mime = mime_hint or "application/octet-stream"
    logger.info(
        "phase=parse duration=%.2fs doc=%s corpus=%s tier=%s",
        time.monotonic() - t0,
        doc_id[:12],
        cid8,
        source_tier.value,
    )

    # Create the durable doc anchor before chunking. Pathological Markdown can
    # spend minutes in the chunker or fail there; the browser route, local
    # batch status, and resume logic still need a compact row to track it.
    existing_doc = await mongo_reader.get_document(db, doc_id, corpus_id=corpus_id)
    if existing_doc and existing_doc.get("write_state"):
        ws = WriteState(**existing_doc["write_state"])
    else:
        ws = WriteState()
    repairing_verify_failure = bool(
        existing_doc
        and ws.verified is False
        and (ws.verify_errors or existing_doc.get("ingest_stage") == "failed")
    )
    if repairing_verify_failure:
        logger.warning(
            "phase=verify_repair_reset doc=%s corpus=%s errors=%s",
            doc_id[:12],
            cid8,
            ws.verify_errors[:3],
        )
        ws.mongo_written = False
        ws.qdrant_written = False
        ws.summaries_indexed = False
        ws.summary_points = None
        ws.neo4j_written = False
        ws.verified = None
        ws.verify_errors = []
    file_id = (
        existing_doc.get("file_id", str(uuid.uuid4()))
        if existing_doc
        else str(uuid.uuid4())
    )
    if existing_doc is None:
        await _ensure_parse_progress_document(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            file_id=file_id,
            filename=filename,
            source_tier=source_tier,
            source_mime=source_mime,
            ingestion_config=ingestion_config,
            ws=ws,
            source_url=source_url,
            source_identity=source_identity,
        )

    # Preventive retrieval setup gate. Startup tries to repair all corpora, but
    # an ingest can start immediately after corpus creation or after a partial
    # restore on a new machine. Enforce the same Qdrant/Neo4j readiness contract
    # here while we have a durable doc row to mark if setup is broken.
    try:
        from services.retrieval_readiness import ensure_corpus_retrieval_ready

        readiness = await ensure_corpus_retrieval_ready(
            db=db,
            qdrant_client=qdrant_client,
            neo4j_driver=neo4j_driver,
            corpus_id=corpus_id,
            corpus_doc=corpus_doc,
            corpus_name=corpus_doc.get("name"),
            ingestion_config=ingestion_config,
            neo4j_enabled=settings.NEO4J_ENABLED,
            default_dim=settings.EMBEDDING_DIMENSION,
        )
        if not readiness.ok:
            raise RuntimeError("; ".join(readiness.errors))
        logger.info(
            "phase=retrieval_setup ok=true doc=%s corpus=%s qdrant=%s neo4j=%s dim=%s",
            doc_id[:12],
            cid8,
            readiness.qdrant_ready,
            readiness.neo4j_ready,
            readiness.embedding_dimension,
        )
    except Exception as exc:
        message = f"retrieval setup failed: {exc}"
        await _mark_ingest_failed(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            message=message,
            stage="setup_failed",
        )
        await _emit_ingest_phase(
            on_phase,
            "setup_failed",
            doc_id=doc_id,
            corpus_id=corpus_id,
            error=message,
        )
        raise RuntimeError(message) from exc

    # ── Near-duplicate block ─────────────────────────────────────────────
    # Before any chunk/embed/extract work, skip a document that near-duplicates
    # one already in this corpus (e.g. the same book ingested as PDF *and* MD —
    # different exact hash, near-identical content). Runs once per fresh ingest
    # against the parsed markdown token-set vs existing docs' parent chunks. A
    # resume of a previously-skipped doc re-checks, so removing the original
    # lets the duplicate back in.
    _prior_stage = (existing_doc or {}).get("ingest_stage")
    if settings.INGEST_BLOCK_NEAR_DUPLICATES and (
        existing_doc is None or _prior_stage == "skipped_duplicate"
    ):
        dup_candidates = await _find_near_duplicate_documents(
            db=db,
            corpus_id=corpus_id,
            doc_id=doc_id,
            parent_texts=[parse_result.markdown or parse_result.text or ""],
            threshold=float(
                getattr(
                    settings,
                    "INGEST_NEAR_DUPLICATE_THRESHOLD",
                    _DUPLICATE_DOC_THRESHOLD,
                )
            ),
        )
        if dup_candidates:
            top = dup_candidates[0]
            top_containment = float(top.get("containment") or 0.0)
            block_containment = float(
                getattr(settings, "INGEST_NEAR_DUPLICATE_BLOCK_CONTAINMENT", 0.95)
            )
            # SKIP only when the incoming doc is ~fully contained in an existing
            # one (near-identical reformat) — losing it costs nothing. A merely
            # near-duplicate doc may be a DISTINCT work that shares prose (e.g. a
            # different-language edition of a textbook); ingest + flag it instead
            # so it is never silently destroyed.
            if top_containment >= block_containment:
                reason = await _mark_ingest_skipped_duplicate(
                    db=db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    candidates=dup_candidates,
                )
                ws.warnings.append(reason)
                logger.info(
                    "phase=skipped_duplicate doc=%s corpus=%s dup_of=%s sim=%s cont=%s",
                    doc_id[:12],
                    cid8,
                    str(top.get("doc_id"))[:12],
                    top.get("similarity"),
                    top.get("containment"),
                )
                await _call_optional_callback(on_doc_id, doc_id)
                await _emit_ingest_phase(
                    on_phase,
                    "skipped_duplicate",
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    near_duplicate_of=dup_candidates,
                )
                return IngestJobResponse(
                    job_id=job_id,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    filename=filename,
                    source_tier=source_tier.value,
                    status="skipped_duplicate",
                    write_state=ws,
                    chunk_count=0,
                    parent_count=0,
                    error=reason,
                )
            # Near-duplicate but not near-identical → INGEST and FLAG for review.
            reason = await _flag_document_near_duplicate(
                db=db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                candidates=dup_candidates,
            )
            ws.warnings.append(reason)
            logger.info(
                "phase=near_duplicate_flagged doc=%s corpus=%s dup_of=%s sim=%s cont=%s",
                doc_id[:12],
                cid8,
                str(top.get("doc_id"))[:12],
                top.get("similarity"),
                top.get("containment"),
            )

    # Phase K — signal the HTTP endpoint after the parse progress row exists,
    # so the frontend/SSE never observes a real running job as "not found".
    await _call_optional_callback(on_doc_id, doc_id)
    await _emit_ingest_phase(
        on_phase,
        "chunking",
        doc_id=doc_id,
        corpus_id=corpus_id,
        filename=filename,
    )

    # ── Phase 2: Chunk ───────────────────────────────────────────────────
    # Run the sync chunker in a PROCESS pool with a wall-clock cap.
    # asyncio.to_thread serialized every concurrent doc's chunking onto one
    # core via the GIL (observed: cpu=101% with 10 slots, 125s/doc,
    # 2026-07-06) — separate interpreters make chunking scale with cores.
    # Timeout note: cancelling the future does NOT kill a busy subprocess;
    # the worker finishes its doc and returns to the pool — bounded waste,
    # and pathological docs are skip-listed anyway.
    t0 = time.monotonic()
    _chunk_timeout = max(
        60, int(getattr(settings, "TIER_CHUNKER_DOC_TIMEOUT_SECONDS", 600))
    )
    try:
        parents, children, injected_headers = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _chunk_process_pool(),
                functools.partial(
                    _chunk_in_subprocess,
                    parse_result,
                    doc_id,
                    corpus_id,
                    ingestion_config,
                ),
            ),
            timeout=_chunk_timeout,
        )
    except asyncio.TimeoutError as exc:
        logger.error(
            "phase=tier_chunker_timeout doc=%s corpus=%s timeout=%ds — "
            "doc has pathological content (long unbroken token sequences). "
            "Pre-process with Pandoc / strip code-math blocks and retry, "
            "or raise TIER_CHUNKER_DOC_TIMEOUT_SECONDS.",
            doc_id[:12], corpus_id[:8], _chunk_timeout,
        )
        message = (
            f"tier_chunker exceeded {_chunk_timeout}s wall-clock for this "
            "document — likely pathological content (long code/math/table "
            "blocks with no sentence boundaries). Pre-process and retry."
        )
        await _mark_ingest_failed(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            message=message,
            stage="chunk_failed",
        )
        await _emit_ingest_phase(
            on_phase,
            "chunk_failed",
            doc_id=doc_id,
            corpus_id=corpus_id,
            error=message,
        )
        raise RuntimeError(message) from exc
    except Exception as exc:
        message = f"tier_chunker failed: {exc}"
        await _mark_ingest_failed(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            message=message,
            stage="chunk_failed",
        )
        await _emit_ingest_phase(
            on_phase,
            "chunk_failed",
            doc_id=doc_id,
            corpus_id=corpus_id,
            error=message,
        )
        raise RuntimeError(message) from exc
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
    base_facet_profile = build_ingest_facet_profile(
        filename=filename,
        doc_id=doc_id,
        corpus_id=corpus_id,
        parents=parents,
        children=children,
    )

    # ── Resume: checkpoint chunk artifacts before model work ─────────────
    if not ws.mongo_written:
        await _ensure_progress_document(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            file_id=file_id,
            filename=filename,
            source_tier=source_tier,
            source_mime=source_mime,
            ingestion_config=ingestion_config,
            chunking_config=chunking_config,
            parents=parents,
            facet_profile=base_facet_profile,
            ws=ws,
            source_url=source_url,
            source_identity=source_identity,
        )

    if not ws.mongo_written:
        await _checkpoint_child_chunks(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            children=children,
            facet_profile=base_facet_profile,
        )

    # ── Phase 3: Ghost model phases ──────────────────────────────────────
    await _set_ingest_stage(
        db=db,
        doc_id=doc_id,
        corpus_id=corpus_id,
        stage="ghosts",
        on_phase=on_phase,
    )
    async with _model_phase_semaphore(ingestion_config):
        t0 = time.monotonic()
        ghost_result = await _run_ghosts_parallel(
            config=ingestion_config,
            parents=parents,
            children=children,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            filename=filename,
            model=model,
            db=db,
            qdrant_client=qdrant_client,
            neo4j_driver=neo4j_driver,
            existing_doc=existing_doc,
            ws=ws,
        )
    if isinstance(ghost_result, GhostRunResult):
        summaries = ghost_result.summaries
        ghost_b_out = ghost_result.ghost_b_out
        ingest_warnings = ghost_result.warnings
        ghost_b_failures = ghost_result.ghost_b_failures
        ghost_b_metrics = ghost_result.ghost_b_metrics
    else:
        # Backward-compatible path for older tests/mocks that still return the
        # pre-metrics two-tuple.
        ghost_tuple = tuple(ghost_result)
        summaries = ghost_tuple[0] if len(ghost_tuple) > 0 else None
        ghost_b_out = ghost_tuple[1] if len(ghost_tuple) > 1 else None
        ingest_warnings = ghost_tuple[2] if len(ghost_tuple) > 2 else []
        ghost_b_failures = ghost_tuple[3] if len(ghost_tuple) > 3 else []
        ghost_b_metrics = (
            ghost_tuple[4]
            if len(ghost_tuple) > 4
            else _ghost_b_metrics_for_skipped(ghost_b_out)
        )
    ghost_b_metrics = _ghost_b_metrics_with_failures(
        ghost_b_out,
        ghost_b_failures,
        ghost_b_metrics,
    )
    facet_profile = build_ingest_facet_profile(
        filename=filename,
        doc_id=doc_id,
        corpus_id=corpus_id,
        schema_lens=(ghost_b_metrics or {}).get("schema_lens"),
        parents=parents,
        children=children,
        summaries=summaries,
    )
    ws.warnings = _merge_warnings(ws.warnings, ingest_warnings)
    ghost_a_partial = any(w.startswith("Ghost A ") for w in ingest_warnings)
    ghost_b_partial = any(w.startswith("Ghost B ") for w in ingest_warnings)
    ghost_a_status = "partial" if ghost_a_partial else ("ok" if summaries is not None else "skipped")
    ghost_b_status = "partial" if ghost_b_partial else ("ok" if ghost_b_out is not None else "skipped")
    logger.info(
        "phase=ghosts duration=%.2fs doc=%s corpus=%s ghost_a=%s ghost_b=%s warnings=%d failed_chunks=%d",
        time.monotonic() - t0,
        doc_id[:12],
        cid8,
        ghost_a_status,
        ghost_b_status,
        len(ingest_warnings),
        len(ghost_b_failures),
    )

    # ── Phase 4: Mongo durable checkpoints ──────────────────────────────
    if not ws.mongo_written:
        await _set_ingest_stage(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            stage="mongo",
            on_phase=on_phase,
        )
        t0 = time.monotonic()
        await _write_mongo_all(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            file_id=file_id,
            filename=filename,
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
            facet_profile=facet_profile,
            ws=ws,
            replace_existing_artifacts=repairing_verify_failure,
            source_url=source_url,
            source_identity=source_identity,
            source_meta={
                k: getattr(parse_result, k, None)
                for k in ("title", "author", "document_date", "source_type", "routing_trace")
            },
        )
        await mongo_writer.update_write_state(
            db,
            doc_id,
            corpus_id=corpus_id,
            mongo_written=True,
            warnings=ws.warnings,
        )
        ws.mongo_written = True
        # B3 — owner summary tree (parent summaries → rollups → sections →
        # document profile). Best-effort: never fails the ingest.
        try:
            from config import get_settings as _gs
            if bool(getattr(_gs(), "SUMMARY_TREE_ENABLED", True)):
                from services.ingestion.summary_tree import build_and_store_tree
                _tree_counts = await build_and_store_tree(
                    db=db, doc_id=doc_id, corpus_id=corpus_id
                )
                logger.info(
                    "phase=summary_tree doc=%s corpus=%s %s",
                    doc_id[:12], corpus_id[:8], _tree_counts,
                )
        except Exception as _tree_exc:  # noqa: BLE001
            logger.warning(
                "phase=summary_tree doc=%s FAILED (non-fatal): %s",
                doc_id[:12], _tree_exc,
            )
        logger.info(
            "phase=mongo duration=%.2fs doc=%s corpus=%s parents=%d children=%d summaries=%d",
            time.monotonic() - t0,
            doc_id[:12],
            cid8,
            len(parents),
            len(children),
            len(summaries or []),
        )
    elif (
        ingest_warnings
        or ghost_b_failures
        or ghost_b_metrics
        or summaries is not None
        or ghost_b_out is not None
    ):
        parent_dicts = _build_parent_dicts(
            parents,
            summaries,
            (facet_profile or {}).get("parent_facets"),
        )
        await mongo_writer.upsert_parent_chunks(db, parent_dicts)
        if ghost_b_out is not None:
            await mongo_writer.stash_ghost_b(
                db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                results=[asdict(r) for r in ghost_b_out],
            )
        await mongo_writer.stash_ghost_b_failures(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            failures=[asdict(f) for f in ghost_b_failures],
        )
        await db["documents"].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "$set": {
                    "write_state.warnings": ws.warnings,
                    "ghost_b_failures": [asdict(f) for f in ghost_b_failures][:20],
                    "parent_count": len(parent_dicts),
                    "child_count": len(children),
                    "summary_count": len(summaries or []),
                    "ghost_b_staging_count": len(ghost_b_out or []),
                    "ghost_b_failure_count": len(ghost_b_failures),
                    "ghost_b_metrics": ghost_b_metrics or {},
                    "schema_lens": (ghost_b_metrics or {}).get("schema_lens"),
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    # Phase 4.5 graphify augmentation — opt-in per Settings. Runs HERE,
    # before sparse encoding, so the augmenter's `chunk_calls` mapping
    # can backfill `metadata.symbols_called` BEFORE BM25 indexes the
    # chunks. The same enrichment is threaded forward to Phase 7 (Neo4j)
    # so write_graphify_enrichment uses it without re-running graphify.
    graphify_enrichment = None
    if settings.GRAPHIFY_AUGMENT_CODE_LANE:
        try:
            from services import code_graph_augmenter
            code_only = [
                c for c in children
                if getattr(c, "chunk_kind", None) == ChunkKind.CODE
            ]
            if code_only:
                t0 = time.monotonic()
                graphify_enrichment = code_graph_augmenter.augment_code_chunks(code_only)
                # Backfill: write graphify-derived call sites into each
                # chunk's metadata.symbols_called so _searchable_text
                # appends them to the BM25 input. Dedupe case-insensitively;
                # cap at 60 (matches code_splitter._extract_metadata cap).
                backfilled = 0
                for c in code_only:
                    called = graphify_enrichment.chunk_calls.get(c.chunk_id, [])
                    if not called:
                        continue
                    existing = list(c.metadata.get("symbols_called", []) or [])
                    seen = {x.lower() for x in existing}
                    for sym in called:
                        if sym.lower() in seen or len(existing) >= 60:
                            continue
                        existing.append(sym)
                        seen.add(sym.lower())
                    c.metadata["symbols_called"] = existing
                    backfilled += 1
                logger.info(
                    "phase=graphify_backfill duration=%.2fs doc=%s corpus=%s "
                    "code_chunks=%d chunks_backfilled=%d call_edges=%d",
                    time.monotonic() - t0, doc_id[:12], cid8,
                    len(code_only), backfilled,
                    len(graphify_enrichment.call_edges),
                )
        except Exception as exc:
            # Pure augmentation — never block the ingest if it fails.
            logger.warning(
                "phase=graphify_backfill doc=%s status=failed_continue err=%s",
                doc_id[:12], exc,
            )

    # ── Phase 5: Embed + Phase 6: Qdrant ─────────────────────────────────
    #
    # Pt 9 — Phase 5+6 is now best-effort and DOES NOT abort the function
    # on failure. Pre-Pt-9, an embedder 500 or Qdrant outage raised through
    # this block, exited run_ingest_job, and left Neo4j permanently
    # unwritten even though ghost_b_out was fully populated and Mongo had
    # everything staged. Fix: catch the exception, log + warn, and fall
    # through to Phase 7 so Neo4j can still get the document.
    #
    # Reconcile-on-resume: qdrant_written=True only proves the upsert call
    # completed once — a partial embed response or interrupted upsert can
    # leave the flag true WITH HOLES (pilot: 1725 of 1726 vectors, and the
    # resume path trusted the flag forever). Cheap exact count vs the
    # vector-eligible children; any mismatch reruns this phase for the doc
    # (upserts are idempotent — point ids are md5(chunk_id)).
    summary_targets = _summary_target_kinds(ingestion_config)
    summary_gate_required = bool(
        ingestion_config.chunk_summarization
        and summary_targets
        and _summarizable_parents(parents)
    )
    summary_write_required = bool(summary_targets and summaries)
    expected_summary_points = len(summaries or [])
    if ws.qdrant_written:
        try:
            from qdrant_client import models as qmodels

            from services.storage.qdrant_writer import _col_for_corpus

            targets = list(
                getattr(ingestion_config, "target_qdrant_collections", None) or ["naive"]
            )
            # naive's membership == all vector-eligible children; hrag is
            # tier-filtered, so only naive gives an exact expected count.
            primary = "naive" if "naive" in targets else targets[0]
            expected_n = sum(1 for c in children if _is_vectorized_child(c))
            res = await qdrant_client.count(
                collection_name=_col_for_corpus(corpus_id, primary),
                count_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="doc_id", match=qmodels.MatchValue(value=doc_id)
                        ),
                        qmodels.FieldCondition(
                            key="chunk_type", match=qmodels.MatchValue(value="child")
                        ),
                    ]
                ),
                exact=True,
            )
            if primary == "naive" and int(res.count) != expected_n:
                logger.warning(
                    "phase=embed_reconcile doc=%s corpus=%s qdrant=%d expected=%d "
                    "— re-running embed+qdrant for this doc",
                    doc_id[:12],
                    cid8,
                    int(res.count),
                    expected_n,
                )
                ws.qdrant_written = False
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
            logger.warning(
                "phase=embed_reconcile_check_failed doc=%s corpus=%s: %s",
                doc_id[:12],
                cid8,
                exc,
            )

    if ws.qdrant_written and summary_write_required:
        try:
            counts = await _qdrant_summary_counts(
                qdrant_client,
                corpus_id=corpus_id,
                doc_id=doc_id,
                target_kinds=summary_targets,
            )
            if all(count == expected_summary_points for count in counts.values()):
                if not ws.summaries_indexed:
                    await mongo_writer.update_write_state(
                        db,
                        doc_id,
                        corpus_id=corpus_id,
                        summaries_indexed=True,
                    )
                ws.summaries_indexed = True
            else:
                ws.summaries_indexed = False
                logger.warning(
                    "phase=summary_reconcile doc=%s corpus=%s qdrant=%s expected=%d "
                    "— re-running embed+qdrant for this doc",
                    doc_id[:12],
                    cid8,
                    counts,
                    expected_summary_points,
                )
        except Exception as exc:  # noqa: BLE001 - summary reconcile is best-effort
            ws.summaries_indexed = False
            logger.warning(
                "phase=summary_reconcile_check_failed doc=%s corpus=%s: %s",
                doc_id[:12],
                cid8,
                exc,
            )

    qdrant_phase_needed = (
        not ws.qdrant_written
        or (summary_write_required and not ws.summaries_indexed)
    )
    if qdrant_phase_needed:
        try:
            await _set_ingest_stage(
                db=db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                stage="embedding",
                on_phase=on_phase,
            )
            async with _model_phase_semaphore(ingestion_config):
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
            #
            # Phase 4.5 metadata indexing — for CODE chunks we append the
            # AST-derived metadata (symbols_defined, imports, file_path) to
            # the BM25 input. This makes call sites and import targets
            # surface via lexical search even when the literal string
            # doesn't appear in the first ~500 chars of the chunk text
            # (e.g. a function that uses TweenService via a local alias).
            # Prose chunks pass through unchanged.
            from services.storage.sparse_encoder import encode_text as _bm25_encode
            t0 = time.monotonic()
            child_sparse_map = {
                c.chunk_id: _bm25_encode(_searchable_text(c))
                for c in children
                if c.chunk_id in vec_map
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
            await _set_ingest_stage(
                db=db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                stage="qdrant",
                on_phase=on_phase,
            )
            qdrant_sem = _qdrant_write_semaphore()
            qdrant_wait_started = time.monotonic()
            async with qdrant_sem:
                qdrant_wait = time.monotonic() - qdrant_wait_started
                logger.info(
                    "phase=qdrant_write_gate doc=%s corpus=%s wait=%.2fs limit=%d",
                    doc_id[:12],
                    cid8,
                    qdrant_wait,
                    max(1, int(getattr(settings, "QDRANT_INGEST_WRITE_CONCURRENCY", 2))),
                )
                await _write_qdrant_for_doc(
                    qdrant_client=qdrant_client,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    user_id=user_id,
                    filename=filename,
                    parents=parents,
                    children=children,
                    vec_map=vec_map,
                    summaries=summaries,
                    summary_vec_map=summary_vec_map,
                    config=ingestion_config,
                    child_sparse_map=child_sparse_map,
                    summary_sparse_map=summary_sparse_map,
                    facet_profile=facet_profile,
                )
            write_updates: dict[str, Any] = {"qdrant_written": True}
            if summary_write_required:
                write_updates["summaries_indexed"] = True
                write_updates["summary_points"] = expected_summary_points
            elif not summary_gate_required:
                write_updates["summaries_indexed"] = False
                # writer intent = zero summary vectors; the verifier must not
                # re-derive an expectation from heal-added Mongo summaries
                write_updates["summary_points"] = 0
            # gate-required-but-none-produced stays UNSTAMPED on purpose:
            # verifier falls back to the strict Mongo derivation and the
            # genuine summarization failure still fails loudly
            await mongo_writer.update_write_state(
                db, doc_id, corpus_id=corpus_id, **write_updates
            )
            ws.qdrant_written = True
            # B2 promote-at-ingest — MUST run after the Qdrant upserts (the
            # points now exist; at the Mongo barrier they don't yet). Best
            # effort: never fails the ingest.
            try:
                from config import get_settings as _gs2
                if bool(getattr(_gs2(), "SUMMARY_TREE_ENABLED", True)):
                    from services.ingestion.promote import promote_doc
                    _promo = await promote_doc(db, corpus_id=corpus_id, doc_id=doc_id)
                    logger.info(
                        "phase=promote doc=%s corpus=%s %s",
                        doc_id[:12], corpus_id[:8], _promo,
                    )
            except Exception as _promo_exc:  # noqa: BLE001
                logger.warning(
                    "phase=promote doc=%s FAILED (non-fatal): %s",
                    doc_id[:12], _promo_exc,
                )
            # W1 Tier-0 — embed the doc routing card into the universal
            # polymath_doc_summaries collection. Additive + best-effort;
            # consumption stays gated behind TIER0_ROUTING.
            try:
                from config import get_settings as _gs3
                if bool(getattr(_gs3(), "TIER0_AUTO_EMBED", True)):
                    from services.ingestion.tier0 import embed_doc_profile
                    _t0 = await embed_doc_profile(
                        db, qdrant_client,
                        corpus_id=corpus_id, doc_id=doc_id,
                        dim=int(getattr(ingestion_config, "embedding_dimension", 1024)),
                    )
                    logger.info(
                        "phase=tier0 doc=%s corpus=%s %s",
                        doc_id[:12], corpus_id[:8], _t0,
                    )
            except Exception as _t0_exc:  # noqa: BLE001
                logger.warning(
                    "phase=tier0 doc=%s FAILED (non-fatal): %s",
                    doc_id[:12], _t0_exc,
                )
            # TWO-PHASE: doc is queryable NOW — fire enrichment (extraction
            # retry via the receipted graph-backfill path, then promote) as a
            # detached task. Best-effort; failures land in ghost_b_failures
            # exactly like a normal failed lane and remain retryable.
            try:
                from config import get_settings as _gs_tp2
                if bool(getattr(_gs_tp2(), "TWO_PHASE_INGEST", False)):
                    async def _enrich(cid=corpus_id, did=doc_id):
                        try:
                            from services.ingestion_service import ingestion_service as _svc
                            await _svc.backfill_graph_failures(cid, did)
                            from services.ingestion.promote import promote_doc
                            await promote_doc(db, corpus_id=cid, doc_id=did)
                            logger.info("phase=enrich done doc=%s", did[:12])
                        except Exception as _en_exc:  # noqa: BLE001
                            logger.warning("phase=enrich FAILED doc=%s: %s",
                                           did[:12], _en_exc)
                    asyncio.create_task(_enrich())
                    logger.info("phase=enrich scheduled doc=%s", doc_id[:12])
            except Exception:  # noqa: BLE001
                pass
            if summary_write_required:
                ws.summaries_indexed = True
                ws.summary_points = expected_summary_points
            elif not summary_gate_required:
                ws.summary_points = 0
            logger.info(
                "phase=qdrant duration=%.2fs doc=%s corpus=%s targets=%s summaries_indexed=%s",
                time.monotonic() - t0,
                doc_id[:12],
                cid8,
                ",".join(ingestion_config.target_qdrant_collections),
                ws.summaries_indexed,
            )
        except Exception as embed_qdrant_exc:
            # Pt 9 — capture, warn, continue. Neo4j will still write below
            # if ghost_b_out is populated. The user can later retry Qdrant
            # via the backfill endpoint without re-doing extraction.
            warning = f"Embed/Qdrant failed: {embed_qdrant_exc}"
            ws.warnings = _merge_warnings(ws.warnings, [warning])
            await _emit_ingest_phase(
                on_phase,
                "qdrant_failed",
                doc_id=doc_id,
                corpus_id=corpus_id,
                error=warning,
            )
            logger.warning(
                "phase=embed_qdrant doc=%s corpus=%s status=failed_continue err=%s",
                doc_id[:12], cid8, embed_qdrant_exc,
            )
            try:
                await db["documents"].update_one(
                    {"doc_id": doc_id, "corpus_id": corpus_id},
                    {"$set": {
                        "write_state.warnings": ws.warnings,
                        "updated_at": datetime.utcnow(),
                    }},
                )
            except Exception as warn_persist_exc:
                logger.warning(
                    "phase=embed_qdrant warn-persist failed doc=%s err=%s",
                    doc_id[:12], warn_persist_exc,
                )

    # ── Phase 7: Neo4j (optional) ────────────────────────────────────────
    if (
        ingestion_config.use_neo4j
        and settings.NEO4J_ENABLED
        and not ws.neo4j_written
    ):
        if neo4j_driver is None:
            logger.warning(
                "Neo4j enabled in config but driver not initialized; skipping phase=neo4j doc=%s",
                doc_id[:12],
            )
        elif ghost_b_out is None:
            # Defensive: under the staging-backed flow, ghost_b_out is either
            # a fresh LLM run or a rehydrated staging list — never None at
            # this point when use_neo4j is True. Log and skip without
            # flipping the flag so the next retry still has a chance to fix.
            logger.warning(
                "phase=neo4j doc=%s corpus=%s status=ghost_b_out_missing — staging absent and ghost B did not run; skipping write",
                doc_id[:12],
                cid8,
            )
        else:
            t0 = time.monotonic()
            await _set_ingest_stage(
                db=db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                stage="neo4j",
                on_phase=on_phase,
            )
            neo4j_sem = _neo4j_write_semaphore()
            neo4j_wait_started = time.monotonic()
            async with neo4j_sem:
                neo4j_wait = time.monotonic() - neo4j_wait_started
                logger.info(
                    "phase=neo4j_write_gate doc=%s corpus=%s wait=%.2fs limit=%d",
                    doc_id[:12],
                    cid8,
                    neo4j_wait,
                    max(1, int(getattr(settings, "NEO4J_INGEST_WRITE_CONCURRENCY", 1))),
                )
                await _write_neo4j_for_doc(
                    neo4j_driver=neo4j_driver,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    user_id=user_id,
                    file_id=file_id,
                    children=children,
                    ghost_b_out=ghost_b_out,
                    filename=filename,
                    parents=parents,
                    ghost_b_metrics=ghost_b_metrics,
                    graphify_enrichment=graphify_enrichment,
                )
            await mongo_writer.update_write_state(
                db, doc_id, corpus_id=corpus_id, neo4j_written=True
            )
            ws.neo4j_written = True
            logger.info(
                "phase=neo4j duration=%.2fs doc=%s corpus=%s extractions=%d",
                time.monotonic() - t0,
                doc_id[:12],
                cid8,
                len(ghost_b_out),
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

        await _set_ingest_stage(
            db=db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            stage="verifying",
            on_phase=on_phase,
        )
        ok, verify_errors = await verify_ingest(
            db=db,
            qdrant=qdrant_client,
            neo4j_driver=neo4j_driver,
            doc_id=doc_id,
            corpus_id=corpus_id,
            target_qdrant_collections=ingestion_config.target_qdrant_collections,
            use_neo4j=bool(ingestion_config.use_neo4j and settings.NEO4J_ENABLED),
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

    summary_complete_for_cleanup = (not summary_gate_required) or ws.summaries_indexed
    if ws.qdrant_written and ws.neo4j_written and summary_complete_for_cleanup:
        # Schedule post-ingest graph cache warmup. The orchestrator wrapper
        # fans out to the tracked analytics warmup worker, and also calls the
        # legacy warm function if that artifact is restored on a deployment.
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
                "phase=graph_cache_warm doc=%s corpus=%s legacy_schedule_failed: %s",
                doc_id[:12],
                corpus_id[:8],
                exc,
            )

        # GOTCHAS #70 — drop ghost_b_staging after the graph has landed
        # in Neo4j. Staging was the intermediate buffer used to get
        # from Ghost B output to Neo4j writes; once neo4j_written is
        # True the entities + relations live in the graph and the
        # staged dicts are dead weight on the Mongo `documents`
        # collection. At 500 files × ~50KB staging each = 25 MB of
        # bloat per batch that never goes away without this step.
        # An operator who wants to re-extract can rerun Ghost B from
        # the raw `chunks` collection; staging was only needed for
        # the resume-after-Neo4j-crash path which is no longer
        # relevant once Neo4j writes confirmed.
        try:
            res = await db["documents"].update_one(
                {"doc_id": doc_id, "corpus_id": corpus_id},
                {
                    "$unset": {"ghost_b_staging": ""},
                    "$set": {"updated_at": datetime.utcnow()},
                },
            )
            if res.modified_count:
                logger.debug(
                    "phase=staging_cleanup doc=%s corpus=%s "
                    "ghost_b_staging field dropped (neo4j_written=True)",
                    doc_id[:12],
                    corpus_id[:8],
                )
        except Exception as exc:
            # Non-fatal — staging cleanup is a storage optimization,
            # not a correctness step. Log and continue so the rest of
            # the post-write block (final status update) still runs.
            logger.warning(
                "phase=staging_cleanup doc=%s corpus=%s failed: %s",
                doc_id[:12],
                corpus_id[:8],
                exc,
            )

    neo4j_required = bool(ingestion_config.use_neo4j and settings.NEO4J_ENABLED)
    summary_complete = (not summary_gate_required) or ws.summaries_indexed
    awaiting_summary = bool(
        summary_gate_required
        and not summary_complete
        and ws.mongo_written
        and ws.qdrant_written
        and ((not neo4j_required) or ws.neo4j_written)
        and bool(getattr(settings, "INGEST_SAFE_SUMMARY_FAILURES", True))
    )
    storage_complete = (
        ws.mongo_written
        and ws.qdrant_written
        and summary_complete
        and ((not neo4j_required) or ws.neo4j_written)
    )
    verified_complete = storage_complete and ws.verified is True
    final_status = (
        "done"
        if verified_complete
        else ("awaiting_summary" if awaiting_summary else "failed")
    )
    final_error = None
    if awaiting_summary:
        final_error = (
            "Summary pending: chunks, vectors, and available graph extractions "
            "were persisted; rerun this document when the summary lane is healthy."
        )
    elif ws.verified is False and ws.verify_errors:
        final_error = "; ".join(ws.verify_errors)
    elif not verified_complete:
        missing = []
        if not ws.mongo_written:
            missing.append("mongo")
        if not ws.qdrant_written:
            missing.append("qdrant")
        if not summary_complete:
            missing.append("summaries")
        if neo4j_required and not ws.neo4j_written:
            missing.append("neo4j")
        if ws.verified is not True:
            missing.append("verification")
        final_error = "Ingest incomplete: " + ", ".join(missing)
    final_stage = (
        "complete"
        if verified_complete
        else ("awaiting_summary" if awaiting_summary else "failed")
    )
    final_update: dict[str, Any] = {
        "ingest_stage": final_stage,
        "updated_at": datetime.utcnow(),
    }
    final_unset: dict[str, str] = {}
    if awaiting_summary:
        final_update["summary_pending_reason"] = final_error
        final_update["write_state.warnings"] = ws.warnings
        final_unset["error"] = ""
    elif verified_complete:
        # A resumed ingest can repair an earlier phase failure. Clear the
        # stale top-level error and remove only the synthetic failure warning;
        # genuine coverage warnings such as Ghost B partial extraction remain.
        repaired_warnings = [
            w for w in (ws.warnings or []) if not str(w).startswith("Ingest failed:")
        ]
        if repaired_warnings != ws.warnings:
            ws.warnings = repaired_warnings
        final_update["write_state.warnings"] = ws.warnings
        final_unset["summary_pending_reason"] = ""
        final_unset["error"] = ""
    else:
        final_update["error"] = final_error
    update_doc: dict[str, Any] = {"$set": final_update}
    if final_unset:
        update_doc["$unset"] = final_unset
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        update_doc,
    )
    await _emit_ingest_phase(
        on_phase,
        final_stage,
        doc_id=doc_id,
        corpus_id=corpus_id,
        error=final_error,
    )

    return IngestJobResponse(
        job_id=job_id,
        doc_id=doc_id,
        corpus_id=corpus_id,
        filename=filename,
        source_tier=source_tier.value,
        status=final_status,
        write_state=ws,
        chunk_count=len(children),
        parent_count=len(parents),
        error=final_error,
    )
