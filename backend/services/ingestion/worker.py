"""
Ingestion pipeline worker — locked pipeline order:

  1. Parse     → docling_adapter.parse_document
  2. Chunk     → tier_chunker.chunk (parents + children)
  3. Ghosts ∥  → asyncio.gather(ghost_a, ghost_b)
                 Ghost A runs iff chunk_summarization=True.
                 Ghost B runs iff use_neo4j=True.
                 Either branch is a no-op (returns None) when its flag is off.
  4. Mongo     → ONE write pass: documents (summaries INLINE on parent_chunks)
                 + chunks. Flip mongo_written.
  5. Embed     → one embed_batch call over children+summary texts.
                 mode / dim / model-id come from ingestion_config.
  6. Qdrant    → children → naive / hrag (tier-filtered) / graph,
                 summaries → naive + hrag only. Flip qdrant_written.
  7. Neo4j     → write_document_graph. Flip neo4j_written.
                 Skipped entirely when use_neo4j=False.

Ghost A failure is a hard abort because parent summaries feed retrieval.
Ghost B partial extraction is a soft warning: Mongo/Qdrant still commit, Neo4j
keeps full chunk coverage, and only entity/relation extraction is partial.
Resume logic (Decision D) reuses existing Mongo summaries and probes Neo4j for
MENTIONS so we never pay the LLM twice for work already persisted.
"""

import asyncio
import hashlib
import logging
import mimetypes
import re
import time
import uuid
from datetime import datetime
from typing import Callable

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
    RelationItem,
    SchemaContext,
    extract_entities,
)
from services.ingestion import docling_adapter, tier_chunker
from services.ingestion.schema_lens import get_or_create_schema_lens
from services.ingestion.section_classifier import ChunkKind, should_skip_ghost_b
from services.secrets import decrypt as _decrypt_api_key
from services.storage import mongo_reader, mongo_writer, qdrant_writer
from services.storage.qdrant_writer import retrieve_schema_for_chunk

logger = logging.getLogger(__name__)
settings = get_settings()


class GhostAFailure(RuntimeError):
    """Ghost A produced fewer results than tasks — abort the document."""


class GhostBFailure(RuntimeError):
    """Ghost B failed catastrophically before returning usable extraction."""


@dataclass
class GhostRunResult:
    """Result envelope for the parallel Ghost A/Ghost B phase.

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
_DUPLICATE_DOC_THRESHOLD = 0.90
_DUPLICATE_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_'-]{2,}")
_DUPLICATE_STOP_WORDS = {
    "and", "are", "but", "for", "from", "have", "into", "not", "the", "that",
    "this", "with", "you", "your", "their", "there", "then", "than", "was",
    "were", "will", "would", "could", "should", "about", "which",
}


def _merge_warnings(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Append warnings once while preserving their original order."""
    merged: list[str] = []
    for warning in [*(existing or []), *(new or [])]:
        text = str(warning or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


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
        "schema_lens_ids": lens_ids,
        "error_counts": {},
    }


def _build_ghost_pool(refs) -> list[dict]:
    """
    Turn a list[ModelProfileRef] (Pydantic) or list[dict] into the plain-dict
    pool that ghost_a / ghost_b accept. Decrypts each entry's api_key exactly
    once here so the ghost layers stay ignorant of the secret format.
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
        out.append(
            {
                "model": data.get("model"),
                "base_url": data.get("base_url") or None,
                "api_key": data.get("api_key") or None,
                "max_concurrent": int(data.get("max_concurrent") or 1) or 1,
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
                entities=[EntityItem(**e) for e in r.get("entities", [])],
                relations=[RelationItem(**x) for x in r.get("relations", [])],
                entity_remap_count=r.get("entity_remap_count", 0),
                entity_drop_count=r.get("entity_drop_count", 0),
                relation_remap_count=r.get("relation_remap_count", 0),
                relation_drop_count=r.get("relation_drop_count", 0),
                domain_range_remap_count=r.get("domain_range_remap_count", 0),
                domain_range_warn_count=r.get("domain_range_warn_count", 0),
                endpoint_completion_count=r.get("endpoint_completion_count", 0),
                evidence_cue_repair_count=r.get("evidence_cue_repair_count", 0),
                schema_lens_id=r.get("schema_lens_id"),
            )
        )
    return out


def _reconstruct_summaries_from_mongo(
    parents, existing_parent_chunks: list[dict]
) -> list[SummaryResult]:
    """Rebuild SummaryResult list from Mongo-stored parent_chunks[].summary.

    Only called on the D.2 resume path when every existing parent has a
    non-empty summary. The parent_id set is stable across runs (deterministic
    from content-hashed doc_id), so we zip by parent_id map.
    """
    by_id = {ep["parent_id"]: ep for ep in existing_parent_chunks}
    out: list[SummaryResult] = []
    for p in parents:
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
) -> GhostRunResult:
    """Fan out GHOST A + GHOST B in parallel. Either branch may be disabled
    by config OR skipped via resume gates (Decision D).

    Hard-abort semantics: Ghost A still raises on partial summaries. Ghost B
    partials return usable extraction results plus warnings so the document can
    commit to Mongo/Qdrant and surface graph coverage honestly in the UI.
    """
    warnings: list[str] = []
    ghost_b_failures: list[ExtractionFailureItem] = []
    ghost_b_metrics: dict | None = None
    # ── GHOST A path decisions ────────────────────────────────────────────
    existing_parent_chunks: list[dict] = (
        (existing_doc or {}).get("parent_chunks") or []
    )
    summaries_from_mongo: list[SummaryResult] | None = None
    need_ghost_a = config.chunk_summarization

    if need_ghost_a and ws.qdrant_written:
        # Summaries already embedded into Qdrant on a prior run; nothing to do.
        need_ghost_a = False
    elif need_ghost_a and ws.mongo_written and existing_parent_chunks:
        all_filled = all(
            (p.get("summary") or "").strip() for p in existing_parent_chunks
        )
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
        config.use_neo4j and settings.NEO4J_ENABLED and not ws.neo4j_written
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
        if len(results) < len(tasks):
            raise GhostAFailure(
                f"Ghost A partial: {len(results)}/{len(tasks)} parents summarized"
            )
        return results

    async def _b_branch() -> list[ExtractionResult] | None:
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
        tasks = [
            ExtractionTask(
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                corpus_id=c.corpus_id,
                text=c.text,
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
        reason = "fresh_ingest" if not ws.mongo_written else "staging_missing_legacy_doc"
        logger.info(
            "phase=ghost_b_run reason=%s doc=%s corpus=%s children=%d pool=%d strict=%s",
            reason,
            doc_id[:12],
            corpus_id[:8],
            len(tasks),
            len(pool) or 1,
            schema_ctx.strict,
        )
        report = await extract_entities(
            tasks,
            schema=schema_ctx,
            schema_lens=schema_lens,
            chunk_vectors=None,
            schema_resolver=_schema_resolver,
            pool=pool,
            model=model,
            return_report=True,
        )
        if not isinstance(report, ExtractionBatchReport):
            results = report
            failures: list[ExtractionFailureItem] = []
            metrics = _ghost_b_metrics_for_skipped(results)
        else:
            results = report.results
            failures = report.failures
            metrics = report.metrics
        metrics = dict(metrics or {})
        metrics["schema_lens"] = schema_lens.to_dict()
        ghost_b_failures.extend(failures)
        nonlocal ghost_b_metrics
        ghost_b_metrics = metrics
        if len(results) < len(tasks):
            missing_ids = sorted({t.chunk_id for t in tasks} - {r.chunk_id for r in results})
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

    summaries, ghost_b_out = await asyncio.gather(_a_branch(), _b_branch())
    if ghost_b_metrics is None:
        ghost_b_metrics = _ghost_b_metrics_for_skipped(ghost_b_out)
    return GhostRunResult(
        summaries=summaries,
        ghost_b_out=ghost_b_out,
        warnings=warnings,
        ghost_b_failures=ghost_b_failures,
        ghost_b_metrics=ghost_b_metrics,
    )


def _build_parent_dicts(parents, summaries: list[SummaryResult] | None) -> list[dict]:
    """Assemble the parent_chunks[] array for the Mongo document record,
    populating `summary` inline from Ghost A output when available.
    """
    summary_by_parent = {s.parent_id: s.summary for s in (summaries or [])}
    return [
        {
            "parent_id": p.parent_id,
            "doc_id": p.doc_id,
            "corpus_id": p.corpus_id,
            "text": p.text,
            "heading_path": p.heading_path,
            "source_tier": p.source_tier,
            "page_start": getattr(p, "page_start", None),
            "page_end": getattr(p, "page_end", None),
            "summary": summary_by_parent.get(p.parent_id),
            "child_ids": [c.chunk_id for c in p.children],
        }
        for p in parents
    ]


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
    ws: WriteState,
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
    ghost_b_staging = (
        [asdict(r) for r in ghost_b_out] if ghost_b_out else None
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
        "ingestion_config": freeze_snapshot(ingestion_config),
        "chunking_config": chunking_config,
        "write_state": ws.model_dump(),
        "parent_chunks": parent_dicts,
        "ghost_b_staging": ghost_b_staging,
        "ghost_b_failures": ghost_b_failure_rows,
        "ghost_b_metrics": ghost_b_metrics or {},
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
    await mongo_writer.upsert_document(db, doc_record)
    await mongo_writer.upsert_chunks(db, child_dicts)


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
    child_texts = [c.text for c in children]
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
    )
    split = len(child_texts)
    child_vecs = all_vectors[:split]
    summary_vecs = all_vectors[split:]
    vec_map = {c.chunk_id: v for c, v in zip(children, child_vecs)}
    summary_vec_map = {s.parent_id: v for s, v in zip(summary_list, summary_vecs)}
    return vec_map, summary_vec_map


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
) -> None:
    """Write children + summaries to per-corpus Qdrant collections.

    Children: naive (always) / hrag (tier-filtered) / graph (all) — gated
    against the corpus's `target_qdrant_collections` list to preserve
    existing semantics for corpora that opt out of any kind.
    Summaries: naive + hrag only (qdrant_writer.upsert_summaries also
    enforces this defensively).
    """
    target_cols = config.target_qdrant_collections

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
        dicts = [_as_payload(c) for c in children]
        vecs = [vec_map[c.chunk_id] for c in children]
        await qdrant_writer.upsert_children(
            qdrant_client, corpus_id, dicts, vecs, ["naive"]
        )

    hrag_eligible = [c for c in children if c.source_tier in _HRAG_TIERS]
    if "hrag" in target_cols and hrag_eligible:
        dicts = [_as_payload(c) for c in hrag_eligible]
        vecs = [vec_map[c.chunk_id] for c in hrag_eligible]
        await qdrant_writer.upsert_children(
            qdrant_client, corpus_id, dicts, vecs, ["hrag"]
        )

    if "graph" in target_cols:
        dicts = [_as_payload(c) for c in children]
        vecs = [vec_map[c.chunk_id] for c in children]
        await qdrant_writer.upsert_children(
            qdrant_client, corpus_id, dicts, vecs, ["graph"]
        )

    if summaries:
        hp_map = {p.parent_id: p.heading_path for p in parents}
        kind_map = {p.parent_id: getattr(p, "chunk_kind", ChunkKind.BODY) for p in parents}
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
        summary_kinds = [k for k in target_cols if k in ("naive", "hrag")]
        await qdrant_writer.upsert_summaries(
            qdrant_client,
            corpus_id,
            summary_payloads,
            summary_vecs,
            summary_kinds,
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
    t0 = time.monotonic()
    mime_hint, _ = mimetypes.guess_type(filename)
    parse_result = await docling_adapter.parse_document(
        data,
        filename=filename,
        mime=mime_hint or "application/octet-stream",
        do_ocr=getattr(ingestion_config, "docling_ocr_enabled", True),
    )
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

    # Phase K — signal the HTTP endpoint that the doc_id is resolved. The
    # endpoint awaits this to return to the client while the rest of the
    # pipeline continues in the background.
    if on_doc_id is not None:
        try:
            on_doc_id(doc_id)
        except Exception as _exc:
            logger.debug("on_doc_id callback raised: %s", _exc)

    # ── Phase 2: Chunk ───────────────────────────────────────────────────
    t0 = time.monotonic()
    parents, children, injected_headers = tier_chunker.chunk(
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
    file_id = (
        existing_doc.get("file_id", str(uuid.uuid4()))
        if existing_doc
        else str(uuid.uuid4())
    )

    # ── Phase 3: Ghosts in parallel ──────────────────────────────────────
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
        ghost_b_metrics = ghost_tuple[4] if len(ghost_tuple) > 4 else _ghost_b_metrics_for_skipped(ghost_b_out)
    ws.warnings = _merge_warnings(ws.warnings, ingest_warnings)
    logger.info(
        "phase=ghosts duration=%.2fs doc=%s corpus=%s ghost_a=%s ghost_b=%s warnings=%d failed_chunks=%d",
        time.monotonic() - t0,
        doc_id[:12],
        cid8,
        "ok" if summaries is not None else "skipped",
        "partial" if ingest_warnings and ghost_b_out is not None else ("ok" if ghost_b_out is not None else "skipped"),
        len(ingest_warnings),
        len(ghost_b_failures),
    )

    # ── Phase 4: Mongo (ONE write pass, inline summaries) ────────────────
    if not ws.mongo_written:
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
    if not ws.qdrant_written:
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
        )
        await mongo_writer.update_write_state(
            db, doc_id, corpus_id=corpus_id, qdrant_written=True
        )
        ws.qdrant_written = True
        logger.info(
            "phase=qdrant duration=%.2fs doc=%s corpus=%s targets=%s",
            time.monotonic() - t0,
            doc_id[:12],
            cid8,
            ",".join(ingestion_config.target_qdrant_collections),
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
            await _write_neo4j_for_doc(
                neo4j_driver=neo4j_driver,
                doc_id=doc_id,
                corpus_id=corpus_id,
                user_id=user_id,
                file_id=file_id,
                children=children,
                ghost_b_out=ghost_b_out,
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
