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
import re
from pathlib import Path
from typing import List, Optional

from config import get_settings
from models.schemas import SourceChunk
from services.conversation import conversation_service
from services.facets import metadata_with_facets
from services.retriever.query_semantics import lexical_terms
from services.storage.record_status import with_active_records

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _bearing_count(text: str, terms: tuple[str, ...]) -> int:
    """How many distinct query content terms appear in a passage (word-boundary,
    punctuation-tolerant). Shared answer-bearingness signal for B2 excerpting."""
    if not text or not terms:
        return 0
    hay = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "
    return sum(1 for t in terms if (" " + t + " ") in hay)


def _query_guided_excerpt(
    parent_text: str,
    *,
    child_text: str,
    query: str,
    max_chars: int,
) -> str:
    """B2 — return a query-centered window of the parent body.

    Always includes the matched child passage and its immediate neighbours,
    then greedily adds the highest answer-bearing paragraphs (by query-term
    coverage) in original order until the char budget is reached. Returns the
    full parent unchanged when it already fits, or when inputs are too thin to
    excerpt safely. Elisions between kept blocks are marked with '[…]'.
    """
    parent_text = (parent_text or "").strip()
    if len(parent_text) <= max_chars:
        return parent_text
    # Paragraph units preserve structure better than raw sentences.
    paras = [p.strip() for p in re.split(r"\n\s*\n", parent_text) if p.strip()]
    if len(paras) <= 1:
        paras = [s.strip() for s in _SENT_SPLIT.split(parent_text) if s.strip()]
    if len(paras) <= 1:
        return parent_text[:max_chars].rstrip()

    terms = tuple(lexical_terms(query)) if query else ()
    child_norm = " ".join((child_text or "").lower().split())

    # Anchor = paragraph that contains the matched child passage.
    anchor = 0
    probe = child_norm[:60]
    if probe:
        for i, p in enumerate(paras):
            if probe in " ".join(p.lower().split()):
                anchor = i
                break

    selected: set[int] = set()
    budget = 0
    # 1) anchor + immediate neighbours (always kept; the child must survive).
    for i in (anchor, anchor - 1, anchor + 1):
        if 0 <= i < len(paras) and i not in selected:
            if not selected or budget + len(paras[i]) <= max_chars:
                selected.add(i)
                budget += len(paras[i])
    # 2) greedily add the most answer-bearing remaining paragraphs.
    ranked = sorted(
        (i for i in range(len(paras)) if i not in selected),
        key=lambda i: (-_bearing_count(paras[i], terms), i),
    )
    for i in ranked:
        if _bearing_count(paras[i], terms) <= 0:
            break
        if budget + len(paras[i]) > max_chars:
            continue
        selected.add(i)
        budget += len(paras[i])
    if not selected:
        return parent_text[:max_chars].rstrip()

    out_parts: list[str] = []
    prev: int | None = None
    for i in sorted(selected):
        if prev is not None and i != prev + 1:
            out_parts.append("[…]")
        out_parts.append(paras[i])
        prev = i
    return "\n\n".join(out_parts)


def _assemble_hydrated_text(
    mode: str,
    *,
    child_text: str,
    parent_text: str,
    summary: str,
    query: str | None = None,
    excerpt_enabled: bool = False,
    excerpt_max_chars: int = 1600,
) -> str:
    """Decide what text the LLM sees for a matched child chunk.

    'parent' (default): the parent body (small-to-big retrieval). When B2 is on
    (excerpt_enabled) and the parent exceeds the budget, return a query-guided
    excerpt centred on the matched child instead of the whole block.
    'child_summary': the precise child passage plus the section summary as
    context — ~4x denser, keeps every token relevant (NotebookLM-style). Falls
    back to the parent body when there is no usable child text.
    """

    child_text = (child_text or "").strip()
    parent_text = (parent_text or "").strip()
    summary = (summary or "").strip()
    if str(mode or "").lower() == "child_summary" and child_text:
        if summary:
            return f"{child_text}\n\n[Section context: {summary}]"
        return child_text
    body = parent_text or child_text
    if excerpt_enabled and query and body and len(body) > excerpt_max_chars:
        body = _query_guided_excerpt(
            body,
            child_text=child_text,
            query=query,
            max_chars=excerpt_max_chars,
        )
    return body

logger = logging.getLogger(__name__)


def _basename(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return Path(text.replace("\\", "/")).name.strip()


def _document_display_name(doc: dict) -> str:
    """Human label for source attribution.

    Uploads persist ``filename`` but usually do not have ``source_path``.
    Prefer the portable upload label, then optional titles, then a path basename.
    """
    for key in ("filename", "title", "doc_title"):
        value = str(doc.get(key) or "").strip()
        if value:
            return value
    return _basename(doc.get("source_path")) or str(doc.get("doc_id") or "").strip()


async def hydrate_chunks(
    chunks: List[SourceChunk],
    corpus_ids: Optional[List[str]] = None,
    *,
    query: Optional[str] = None,
) -> List[SourceChunk]:
    """
    Hydrate candidates with parent text + corpus/doc metadata from MongoDB.

    Pass 0 (Mode A fix): For any chunk that has chunk_id but no parent_id,
    resolve parent_id from the MongoDB chunks collection before hydration.

    Pass 1: Replace chunk.text with full parent body from parent_chunks
    collection, with legacy documents.parent_chunks[] fallback.

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
            orphan_query: dict = {"chunk_id": {"$in": orphan_ids}}
            if corpus_ids:
                orphan_query["corpus_id"] = {"$in": corpus_ids}
            chunk_records = await db["chunks"].find(
                with_active_records(orphan_query)
            ).to_list(length=None)
            pid_map = {r["chunk_id"]: r.get("parent_id", "") for r in chunk_records}
            did_map = {r["chunk_id"]: r.get("doc_id", "") for r in chunk_records}
            kind_map = {r["chunk_id"]: r.get("chunk_kind", "") for r in chunk_records}
            meta_map = {r["chunk_id"]: r for r in chunk_records}
            for chunk in orphans:
                chunk.parent_id = pid_map.get(chunk.chunk_id, "") or ""
                if not chunk.doc_id:
                    chunk.doc_id = did_map.get(chunk.chunk_id, "") or ""
                if not getattr(chunk, "chunk_kind", "") or chunk.chunk_kind == "body":
                    chunk.chunk_kind = kind_map.get(chunk.chunk_id, "") or chunk.chunk_kind
                chunk.metadata = metadata_with_facets(
                    chunk.metadata,
                    meta_map.get(chunk.chunk_id),
                )
            logger.debug(
                "Pass 0: resolved parent_id for %d orphan chunks", len(orphans)
            )
        except Exception as exc:
            logger.warning("Pass 0 parent_id lookup failed: %s", exc)

    # ── Pass 1: fetch parent text from split parent collection ───────────────
    doc_ids = {c.doc_id for c in chunks if c.parent_id and c.doc_id}
    parent_ids = {c.parent_id for c in chunks if c.parent_id}

    mongo_query: dict = {"doc_id": {"$in": list(doc_ids)}}
    if corpus_ids:
        mongo_query["corpus_id"] = {"$in": corpus_ids}

    try:
        docs = await db["documents"].find(with_active_records(mongo_query)).to_list(length=None)
    except Exception as exc:
        logger.error("Hydration documents query failed: %s", exc)
        return chunks

    # (doc_id, parent_id) → parent_chunk dict
    parent_lookup: dict[tuple[str, str], dict] = {}
    doc_meta: dict[str, dict] = {}
    if doc_ids and parent_ids:
        parent_query: dict = {
            "doc_id": {"$in": list(doc_ids)},
            "parent_id": {"$in": list(parent_ids)},
        }
        if corpus_ids:
            parent_query["corpus_id"] = {"$in": corpus_ids}
        try:
            parent_rows = await db["parent_chunks"].find(
                with_active_records(parent_query),
                {"_id": 0},
            ).to_list(length=None)
            for pc in parent_rows:
                did = pc.get("doc_id", "")
                pid = pc.get("parent_id", "")
                if did and pid:
                    parent_lookup[(did, pid)] = pc
        except Exception as exc:
            logger.debug("Split parent hydration lookup skipped: %s", exc)

    for doc in docs:
        did = doc.get("doc_id", "")
        doc_meta[did] = {
            "doc_name": _document_display_name(doc),
            "source_path": doc.get("source_path", ""),
            "corpus_id": doc.get("corpus_id", ""),
            "doc_artifact": (doc.get("doc_profile") or {}).get("doc_artifact") or None,
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
    _settings = get_settings()
    hydration_mode = str(getattr(_settings, "HYDRATION_MODE", "parent") or "parent")
    excerpt_enabled = bool(getattr(_settings, "PARENT_EXCERPT_ENABLED", False))
    excerpt_max_chars = int(getattr(_settings, "PARENT_EXCERPT_MAX_CHARS", 1600))
    hydrated: List[SourceChunk] = []
    for chunk in chunks:
        if chunk.parent_id and chunk.doc_id:
            pc = parent_lookup.get((chunk.doc_id, chunk.parent_id))
            if pc:
                # Capture the precise child passage BEFORE it is replaced — under
                # 'child_summary' mode the LLM sees child+summary instead of the
                # full parent body.
                child_text = chunk.text
                chunk.text = _assemble_hydrated_text(
                    hydration_mode,
                    child_text=child_text,
                    parent_text=pc.get("text") or "",
                    summary=pc.get("summary") or "",
                    query=query,
                    excerpt_enabled=excerpt_enabled,
                    excerpt_max_chars=excerpt_max_chars,
                )
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
                if pc.get("domain") and not getattr(chunk, "domain", None):
                    chunk.domain = pc["domain"]
                chunk.metadata = metadata_with_facets(
                    chunk.metadata or pc.get("metadata") or {},
                    pc,
                )

            meta = doc_meta.get(chunk.doc_id, {})
            chunk.doc_name = meta.get("doc_name") or chunk.doc_id
            doc_artifact = meta.get("doc_artifact")
            if doc_artifact:
                metadata = dict(chunk.metadata or {})
                metadata["doc_artifact"] = doc_artifact
                chunk.metadata = metadata

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
            with_active_records(query),
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
                "facet_ids": 1,
                "facet_text": 1,
                "content_facet_ids": 1,
                "content_facet_text": 1,
                "content_facet_source": 1,
                "content_facet_confidence": 1,
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
            copied.metadata = metadata_with_facets(
                copied.metadata or record.get("metadata") or {},
                record,
            )
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
    parent_ids: set[str] = set()
    for chunk in chunks:
        chunk_id = chunk.chunk_id or ""
        parent_id = chunk.parent_id or (
            chunk_id.removesuffix("_summary") if chunk_id.endswith("_summary") else ""
        )
        if not parent_id:
            continue
        summary_refs.append((chunk.doc_id or "", parent_id))
        parent_ids.add(parent_id)
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
            with_active_records(query),
            {
                "_id": 0,
                "doc_id": 1,
                "corpus_id": 1,
                "filename": 1,
                "title": 1,
                "doc_title": 1,
                "source_path": 1,
                "parent_chunks.parent_id": 1,
                "parent_chunks.summary": 1,
                "parent_chunks.domain": 1,
                "parent_chunks.heading_path": 1,
                "parent_chunks.chunk_kind": 1,
                "parent_chunks.metadata": 1,
                "parent_chunks.facet_ids": 1,
                "parent_chunks.facet_text": 1,
                "parent_chunks.content_facet_ids": 1,
                "parent_chunks.content_facet_text": 1,
                "parent_chunks.content_facet_source": 1,
                "parent_chunks.content_facet_confidence": 1,
            },
        ).to_list(length=None)
    except Exception as exc:
        logger.warning("Summary reranker text hydration failed: %s", exc)
        return chunks

    by_doc_parent: dict[tuple[str, str], dict] = {}
    by_parent: dict[str, dict] = {}
    doc_names: dict[str, str] = {}
    if parent_ids:
        parent_query: dict = {"parent_id": {"$in": list(parent_ids)}}
        if doc_ids:
            parent_query["doc_id"] = {"$in": list(doc_ids)}
        if corpus_ids:
            parent_query["corpus_id"] = {"$in": corpus_ids}
        try:
            parent_rows = await db["parent_chunks"].find(
                with_active_records(parent_query),
                {"_id": 0},
            ).to_list(length=None)
            for parent in parent_rows:
                parent_id = str(parent.get("parent_id") or "")
                summary = str(parent.get("summary") or "")
                doc_id = str(parent.get("doc_id") or "")
                if not parent_id or not summary:
                    continue
                by_doc_parent[(doc_id, parent_id)] = parent
                by_parent[parent_id] = parent
        except Exception as exc:
            logger.debug("Split summary hydration lookup skipped: %s", exc)

    for doc in docs:
        doc_id = str(doc.get("doc_id") or "")
        doc_names[doc_id] = _document_display_name(doc)
        for parent in doc.get("parent_chunks", []) or []:
            parent_id = str(parent.get("parent_id") or "")
            summary = str(parent.get("summary") or "")
            if not parent_id or not summary:
                continue
            by_doc_parent[(doc_id, parent_id)] = parent
            by_parent[parent_id] = parent

    if not by_doc_parent and not by_parent:
        if not doc_names:
            return chunks
        hydrated_names: List[SourceChunk] = []
        for chunk in chunks:
            copied = chunk.model_copy()
            if not copied.doc_name:
                copied.doc_name = doc_names.get(copied.doc_id or "") or copied.doc_name
            hydrated_names.append(copied)
        return hydrated_names

    hydrated: List[SourceChunk] = []
    replaced = 0
    for chunk in chunks:
        copied = chunk.model_copy()
        if not copied.doc_name:
            copied.doc_name = doc_names.get(copied.doc_id or "") or copied.doc_name
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
            # Global/summary-mode hydration must carry domain too, otherwise the
            # per-domain coverage cap (gated on search_mode=="global") sees
            # domain=None and is a no-op in the only mode it fires in.
            if record.get("domain") and not getattr(copied, "domain", None):
                copied.domain = record["domain"]
            copied.metadata = metadata_with_facets(
                copied.metadata or record.get("metadata") or {},
                record,
            )
        hydrated.append(copied)

    if replaced:
        logger.info(
            "Summary reranker text hydration replaced %d/%d candidate snippets",
            replaced,
            len(chunks),
        )
    return hydrated
