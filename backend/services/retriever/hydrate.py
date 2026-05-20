"""
Hydrate — replace child chunk text with full parent body from MongoDB.

Also populates corpus_name and doc_name on each SourceChunk so the
context_manager can build the spec-compliant [Source: ...] header.

Mode A expansion note:
  Neo4j Chunk nodes carry chunk_id + doc_id but NOT parent_id (parent_id lives
  only in MongoDB chunks collection). This module resolves parent_id from the
  chunks collection as a first pass before attempting parent text hydration, so
  Mode A results are hydrated correctly rather than passing through with empty text.
"""
import logging
from pathlib import Path
from typing import List, Optional

from models.schemas import SourceChunk
from services.conversation import conversation_service

logger = logging.getLogger(__name__)


async def hydrate_chunks(
    chunks: List[SourceChunk], corpus_ids: Optional[List[str]] = None
) -> List[SourceChunk]:
    """
    Hydrate candidates with parent text + corpus/doc metadata from MongoDB.

    Pass 0 (Mode A fix): For any chunk that has chunk_id but no parent_id,
    resolve parent_id from the MongoDB chunks collection before hydration.

    Pass 1: Replace chunk.text with full parent body from documents.parent_chunks[].

    Pass 2: Populate corpus_name and doc_name for prompt attribution.

    Pass 3: Drop any chunks whose text is still empty after all passes (safety net).
    """
    if not chunks:
        return []

    db = conversation_service._db
    if db is None:
        logger.error("DB not connected — cannot hydrate chunks.")
        return chunks

    # ── Pass 0: resolve parent_id for Mode A expansion results ──────────────
    # Neo4j Chunk nodes don't store parent_id; look it up from MongoDB chunks.
    orphans = [c for c in chunks if not c.parent_id and c.chunk_id]
    if orphans:
        orphan_ids = [c.chunk_id for c in orphans]
        try:
            chunk_records = await db["chunks"].find(
                {"chunk_id": {"$in": orphan_ids}}
            ).to_list(length=None)
            pid_map = {r["chunk_id"]: r.get("parent_id", "") for r in chunk_records}
            did_map = {r["chunk_id"]: r.get("doc_id", "") for r in chunk_records}
            kind_map = {r["chunk_id"]: r.get("chunk_kind", "") for r in chunk_records}
            for chunk in orphans:
                chunk.parent_id = pid_map.get(chunk.chunk_id, "") or ""
                if not chunk.doc_id:
                    chunk.doc_id = did_map.get(chunk.chunk_id, "") or ""
                if not getattr(chunk, "chunk_kind", "") or chunk.chunk_kind == "body":
                    chunk.chunk_kind = kind_map.get(chunk.chunk_id, "") or chunk.chunk_kind
            logger.debug(
                "Pass 0: resolved parent_id for %d orphan chunks", len(orphans)
            )
        except Exception as exc:
            logger.warning("Pass 0 parent_id lookup failed: %s", exc)

    # ── Pass 1: fetch parent text from documents collection ──────────────────
    doc_ids = {c.doc_id for c in chunks if c.parent_id and c.doc_id}

    mongo_query: dict = {"doc_id": {"$in": list(doc_ids)}}
    if corpus_ids:
        mongo_query["corpus_id"] = {"$in": corpus_ids}

    try:
        docs = await db["documents"].find(mongo_query).to_list(length=None)
    except Exception as exc:
        logger.error("Hydration documents query failed: %s", exc)
        return chunks

    # (doc_id, parent_id) → parent_chunk dict
    parent_lookup: dict[tuple[str, str], dict] = {}
    doc_meta: dict[str, dict] = {}
    for doc in docs:
        did = doc.get("doc_id", "")
        doc_meta[did] = {
            "source_path": doc.get("source_path", ""),
            "corpus_id": doc.get("corpus_id", ""),
        }
        for pc in doc.get("parent_chunks", []):
            pid = pc.get("parent_id", "")
            if did and pid:
                parent_lookup[(did, pid)] = pc

    # ── Pass 2: corpus name lookup ───────────────────────────────────────────
    corpus_id_set = {c.corpus_id for c in chunks if c.corpus_id}
    corpus_name_map: dict[str, str] = {}
    if corpus_id_set:
        try:
            corpus_docs = await db["corpora"].find(
                {"corpus_id": {"$in": list(corpus_id_set)}}
            ).to_list(length=None)
            corpus_name_map = {
                c["corpus_id"]: c.get("name", "") for c in corpus_docs
            }
        except Exception as exc:
            logger.warning("Corpus name lookup failed: %s", exc)

    # ── Hydrate each chunk ───────────────────────────────────────────────────
    hydrated: List[SourceChunk] = []
    for chunk in chunks:
        if chunk.parent_id and chunk.doc_id:
            pc = parent_lookup.get((chunk.doc_id, chunk.parent_id))
            if pc:
                chunk.text = pc.get("text", chunk.text)
                if pc.get("summary") and not chunk.summary:
                    chunk.summary = pc["summary"]
                # Backfill heading_path when Qdrant payload didn't carry it
                # (e.g. Mode A graph expansion produced empty-text chunks).
                if not chunk.heading_path and pc.get("heading_path"):
                    chunk.heading_path = pc["heading_path"]
                if (not getattr(chunk, "chunk_kind", "") or chunk.chunk_kind == "body") and pc.get("chunk_kind"):
                    chunk.chunk_kind = pc["chunk_kind"]
                # Code lane (Phase 1) — propagate language + AST metadata
                # from the parent Mongo record. Mode A / Mode B chunks
                # arrive empty-shaped (no Qdrant payload) so this is the
                # only spot they get language/metadata. Qdrant-sourced
                # chunks already have these from the payload; we only fill
                # when missing so payload values are preserved.
                if not chunk.language and pc.get("language"):
                    chunk.language = pc["language"]
                if not chunk.metadata and pc.get("metadata"):
                    chunk.metadata = pc["metadata"] or {}

            meta = doc_meta.get(chunk.doc_id, {})
            sp = meta.get("source_path", "")
            chunk.doc_name = Path(sp).name if sp else chunk.doc_id

        chunk.corpus_name = (
            corpus_name_map.get(chunk.corpus_id, "") or chunk.corpus_id
        )
        hydrated.append(chunk)

    # ── Pass 3: drop empty-text chunks (unresolvable Mode A results) ─────────
    before = len(hydrated)
    hydrated = [c for c in hydrated if c.text.strip()]
    dropped = before - len(hydrated)
    if dropped:
        logger.warning(
            "Dropped %d empty-text chunks after hydration (unresolvable Mode A results)",
            dropped,
        )

    logger.info(
        "Hydration complete: %d chunks returned (docs fetched: %d)",
        len(hydrated),
        len(doc_ids),
    )
    return hydrated


async def hydrate_rerank_texts(
    chunks: List[SourceChunk], corpus_ids: Optional[List[str]] = None
) -> List[SourceChunk]:
    """Replace Qdrant display snippets with full child text before reranking.

    Existing Qdrant payloads may carry compact ``chunk_text`` snippets for
    fast display. The reranker needs the full retrieval unit, especially for
    table chunks where the exact matching row can land after the display
    snippet. This pass reads from Mongo ``chunks`` only and does not hydrate to
    parent text, so reranker inputs remain child-sized.
    """
    if not chunks:
        return []

    db = conversation_service._db
    if db is None:
        logger.warning("DB not connected — cannot hydrate reranker texts.")
        return chunks

    chunk_ids = [
        c.chunk_id
        for c in chunks
        if c.chunk_id and not c.chunk_id.endswith("_summary")
    ]
    if not chunk_ids:
        return chunks

    query: dict = {"chunk_id": {"$in": chunk_ids}}
    if corpus_ids:
        query["corpus_id"] = {"$in": corpus_ids}

    try:
        records = await db["chunks"].find(
            query,
            {
                "_id": 0,
                "chunk_id": 1,
                "text": 1,
                "parent_id": 1,
                "doc_id": 1,
                "corpus_id": 1,
                "chunk_kind": 1,
                "heading_path": 1,
                "language": 1,
                "metadata": 1,
            },
        ).to_list(length=None)
    except Exception as exc:
        logger.warning("Reranker text hydration failed: %s", exc)
        return chunks

    by_id = {str(r.get("chunk_id")): r for r in records if r.get("chunk_id")}
    if not by_id:
        return chunks

    hydrated: List[SourceChunk] = []
    replaced = 0
    for chunk in chunks:
        copied = chunk.model_copy()
        record = by_id.get(copied.chunk_id)
        if record and record.get("text"):
            original_len = len(copied.text or "")
            copied.text = str(record["text"])
            if len(copied.text) > original_len:
                replaced += 1
            copied.parent_id = record.get("parent_id") or copied.parent_id
            copied.doc_id = record.get("doc_id") or copied.doc_id
            copied.corpus_id = record.get("corpus_id") or copied.corpus_id
            copied.chunk_kind = record.get("chunk_kind") or copied.chunk_kind
            if not copied.heading_path and record.get("heading_path"):
                copied.heading_path = record["heading_path"]
            if not copied.language and record.get("language"):
                copied.language = record["language"]
            if not copied.metadata and record.get("metadata"):
                copied.metadata = record["metadata"] or {}
        hydrated.append(copied)

    if replaced:
        logger.info(
            "Reranker text hydration replaced %d/%d candidate snippets",
            replaced,
            len(chunks),
        )
    return hydrated


async def hydrate_summary_rerank_texts(
    chunks: List[SourceChunk], corpus_ids: Optional[List[str]] = None
) -> List[SourceChunk]:
    """Replace Qdrant summary snippets with full Mongo summaries.

    Global search mode intentionally works over summaries rather than parent
    bodies. Existing Qdrant summary payloads may still be preview-sized, so
    reranking global candidates should read the canonical summary from Mongo
    before ranking.
    """
    if not chunks:
        return []

    db = conversation_service._db
    if db is None:
        logger.warning("DB not connected — cannot hydrate summary reranker texts.")
        return chunks

    summary_refs: list[tuple[str, str]] = []
    doc_ids: set[str] = set()
    for chunk in chunks:
        chunk_id = chunk.chunk_id or ""
        parent_id = chunk.parent_id or (
            chunk_id.removesuffix("_summary") if chunk_id.endswith("_summary") else ""
        )
        if not parent_id:
            continue
        summary_refs.append((chunk.doc_id or "", parent_id))
        if chunk.doc_id:
            doc_ids.add(chunk.doc_id)
    if not summary_refs:
        return chunks

    query: dict = {}
    if doc_ids:
        query["doc_id"] = {"$in": list(doc_ids)}
    if corpus_ids:
        query["corpus_id"] = {"$in": corpus_ids}
    if not query:
        return chunks

    try:
        docs = await db["documents"].find(
            query,
            {
                "_id": 0,
                "doc_id": 1,
                "corpus_id": 1,
                "parent_chunks.parent_id": 1,
                "parent_chunks.summary": 1,
                "parent_chunks.heading_path": 1,
                "parent_chunks.chunk_kind": 1,
                "parent_chunks.metadata": 1,
            },
        ).to_list(length=None)
    except Exception as exc:
        logger.warning("Summary reranker text hydration failed: %s", exc)
        return chunks

    by_doc_parent: dict[tuple[str, str], dict] = {}
    by_parent: dict[str, dict] = {}
    for doc in docs:
        doc_id = str(doc.get("doc_id") or "")
        for parent in doc.get("parent_chunks", []) or []:
            parent_id = str(parent.get("parent_id") or "")
            summary = str(parent.get("summary") or "")
            if not parent_id or not summary:
                continue
            by_doc_parent[(doc_id, parent_id)] = parent
            by_parent[parent_id] = parent

    if not by_doc_parent and not by_parent:
        return chunks

    hydrated: List[SourceChunk] = []
    replaced = 0
    for chunk in chunks:
        copied = chunk.model_copy()
        chunk_id = copied.chunk_id or ""
        parent_id = copied.parent_id or (
            chunk_id.removesuffix("_summary") if chunk_id.endswith("_summary") else ""
        )
        record = by_doc_parent.get((copied.doc_id or "", parent_id)) or by_parent.get(parent_id)
        if record and record.get("summary"):
            original_len = len(copied.text or "")
            summary = str(record["summary"])
            copied.text = summary
            copied.summary = summary
            if len(summary) > original_len:
                replaced += 1
            if not copied.parent_id:
                copied.parent_id = parent_id
            if not copied.heading_path and record.get("heading_path"):
                copied.heading_path = record["heading_path"]
            if (not copied.chunk_kind or copied.chunk_kind == "body") and record.get("chunk_kind"):
                copied.chunk_kind = record["chunk_kind"]
            if not copied.metadata and record.get("metadata"):
                copied.metadata = record["metadata"] or {}
        hydrated.append(copied)

    if replaced:
        logger.info(
            "Summary reranker text hydration replaced %d/%d candidate snippets",
            replaced,
            len(chunks),
        )
    return hydrated
