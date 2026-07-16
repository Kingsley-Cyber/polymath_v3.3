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
import asyncio
import hashlib
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


def _document_source_hash(doc: dict) -> str:
    identity = doc.get("source_identity") or {}
    return str(
        doc.get("source_file_hash")
        or doc.get("content_sha256")
        or identity.get("content_sha256")
        or ""
    ).strip()


async def attach_document_identities(
    chunks: List[SourceChunk],
    corpus_ids: Optional[List[str]] = None,
) -> List[SourceChunk]:
    """Attach source hashes before cross-corpus fusion/reranking."""

    if not chunks:
        return []
    db = conversation_service._db
    if db is None:
        return chunks
    chunk_ids = sorted(
        {str(chunk.chunk_id) for chunk in chunks if chunk.chunk_id and not chunk.doc_id}
    )
    parent_ids = sorted(
        {
            str(chunk.parent_id)
            for chunk in chunks
            if chunk.parent_id and not chunk.doc_id
        }
    )
    doc_id_by_chunk: dict[tuple[str, str], str] = {}
    doc_id_by_parent: dict[tuple[str, str], str] = {}
    if chunk_ids or parent_ids:
        identity_terms: list[dict] = []
        if chunk_ids:
            identity_terms.append({"chunk_id": {"$in": chunk_ids}})
        if parent_ids:
            identity_terms.append({"parent_id": {"$in": parent_ids}})
        identity_query: dict = {"$or": identity_terms}
        if corpus_ids:
            identity_query["corpus_id"] = {"$in": corpus_ids}
        try:
            chunk_rows = (
                await db["chunks"]
                .find(
                    with_active_records(identity_query),
                    {
                        "_id": 0,
                        "corpus_id": 1,
                        "chunk_id": 1,
                        "parent_id": 1,
                        "doc_id": 1,
                    },
                )
                .to_list(length=None)
            )
            doc_id_by_chunk = {
                (str(row.get("corpus_id") or ""), str(row.get("chunk_id"))): str(
                    row.get("doc_id")
                )
                for row in chunk_rows
                if row.get("chunk_id") and row.get("doc_id")
            }
            doc_id_by_parent = {
                (str(row.get("corpus_id") or ""), str(row.get("parent_id"))): str(
                    row.get("doc_id")
                )
                for row in chunk_rows
                if row.get("parent_id") and row.get("doc_id")
            }
        except Exception as exc:
            logger.warning("Chunk document identity hydration failed: %s", exc)

    resolved_doc_ids = {
        str(chunk.doc_id or "")
        or doc_id_by_chunk.get((str(chunk.corpus_id or ""), str(chunk.chunk_id)), "")
        or doc_id_by_parent.get((str(chunk.corpus_id or ""), str(chunk.parent_id)), "")
        for chunk in chunks
    }
    doc_ids = sorted(doc_id for doc_id in resolved_doc_ids if doc_id)
    if not doc_ids:
        return chunks
    query: dict = {"doc_id": {"$in": doc_ids}}
    if corpus_ids:
        query["corpus_id"] = {"$in": corpus_ids}
    try:
        rows = (
            await db["documents"]
            .find(
                with_active_records(query),
                {
                    "_id": 0,
                    "doc_id": 1,
                    "corpus_id": 1,
                    "source_file_hash": 1,
                    "content_sha256": 1,
                    "source_identity": 1,
                },
            )
            .to_list(length=None)
        )
    except Exception as exc:
        logger.warning("Document identity hydration failed: %s", exc)
        return chunks

    identity_by_doc = {
        (str(row.get("corpus_id") or ""), str(row.get("doc_id"))): {
            "source_file_hash": _document_source_hash(row),
            "corpus_id": str(row.get("corpus_id") or ""),
        }
        for row in rows
        if row.get("doc_id")
    }
    output: List[SourceChunk] = []
    for chunk in chunks:
        copied = chunk.model_copy(deep=True)
        resolved_doc_id = (
            str(copied.doc_id or "")
            or doc_id_by_chunk.get(
                (str(copied.corpus_id or ""), str(copied.chunk_id)), ""
            )
            or doc_id_by_parent.get(
                (str(copied.corpus_id or ""), str(copied.parent_id)), ""
            )
        )
        if resolved_doc_id and not copied.doc_id:
            copied.doc_id = resolved_doc_id
        identity = (
            identity_by_doc.get((str(copied.corpus_id or ""), resolved_doc_id)) or {}
        )
        source_hash = str(identity.get("source_file_hash") or "")
        if source_hash:
            metadata = dict(copied.metadata or {})
            metadata["source_file_hash"] = source_hash
            membership = str(identity.get("corpus_id") or copied.corpus_id or "")
            metadata["corpus_memberships"] = [membership] if membership else []
            copied.metadata = metadata
        output.append(copied)
    return output


def dedupe_cross_corpus_evidence(
    chunks: List[SourceChunk],
) -> tuple[List[SourceChunk], int]:
    """Collapse identical passages from duplicate documents across corpora.

    The document hash alone is not enough because a useful answer may need
    several passages from one book. The key combines the document source hash
    with normalized evidence text; equivalent copies collapse while distinct
    passages remain. Every corpus membership is retained in metadata and
    provenance.
    """

    output: List[SourceChunk] = []
    index_by_key: dict[tuple[str, str], int] = {}
    dropped = 0
    for chunk in chunks:
        metadata = dict(chunk.metadata or {})
        source_hash = str(metadata.get("source_file_hash") or "").strip()
        normalized_text = " ".join(str(chunk.text or "").lower().split())
        if not source_hash or not normalized_text:
            output.append(chunk)
            continue
        text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        key = (source_hash, text_hash)
        existing_index = index_by_key.get(key)
        if existing_index is None:
            index_by_key[key] = len(output)
            output.append(chunk.model_copy(deep=True))
            continue

        dropped += 1
        existing = output[existing_index]
        existing_meta = dict(existing.metadata or {})
        memberships = {
            str(value)
            for value in existing_meta.get("corpus_memberships") or []
            if value
        }
        memberships.update(
            str(value) for value in metadata.get("corpus_memberships") or [] if value
        )
        if chunk.corpus_id:
            memberships.add(str(chunk.corpus_id))
        existing_meta["corpus_memberships"] = sorted(memberships)
        existing.metadata = existing_meta
        existing.score = max(float(existing.score or 0.0), float(chunk.score or 0.0))
        provenance = list(existing.provenance or [])
        provenance.extend(
            item for item in (chunk.provenance or []) if item not in provenance
        )
        provenance.append(
            {
                "retriever": "cross_corpus_hash_dedupe",
                "corpus_id": chunk.corpus_id,
                "doc_id": chunk.doc_id,
            }
        )
        existing.provenance = provenance
    return output, dropped


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
            chunk_records = (
                await db["chunks"]
                .find(with_active_records(orphan_query))
                .to_list(length=None)
            )
            pid_map = {
                (str(r.get("corpus_id") or ""), str(r["chunk_id"])): r.get(
                    "parent_id", ""
                )
                for r in chunk_records
            }
            did_map = {
                (str(r.get("corpus_id") or ""), str(r["chunk_id"])): r.get("doc_id", "")
                for r in chunk_records
            }
            kind_map = {
                (str(r.get("corpus_id") or ""), str(r["chunk_id"])): r.get(
                    "chunk_kind", ""
                )
                for r in chunk_records
            }
            meta_map = {
                (str(r.get("corpus_id") or ""), str(r["chunk_id"])): r
                for r in chunk_records
            }
            for chunk in orphans:
                key = (str(chunk.corpus_id or ""), str(chunk.chunk_id or ""))
                chunk.parent_id = pid_map.get(key, "") or ""
                if not chunk.doc_id:
                    chunk.doc_id = did_map.get(key, "") or ""
                if not getattr(chunk, "chunk_kind", "") or chunk.chunk_kind == "body":
                    chunk.chunk_kind = kind_map.get(key, "") or chunk.chunk_kind
                chunk.metadata = metadata_with_facets(
                    chunk.metadata,
                    meta_map.get(key),
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
        docs = (
            await db["documents"]
            .find(with_active_records(mongo_query))
            .to_list(length=None)
        )
    except Exception as exc:
        logger.error("Hydration documents query failed: %s", exc)
        return chunks

    # (corpus_id, doc_id, parent_id) → parent_chunk dict
    parent_lookup: dict[tuple[str, str, str], dict] = {}
    doc_meta: dict[tuple[str, str], dict] = {}
    if doc_ids and parent_ids:
        parent_query: dict = {
            "doc_id": {"$in": list(doc_ids)},
            "parent_id": {"$in": list(parent_ids)},
        }
        if corpus_ids:
            parent_query["corpus_id"] = {"$in": corpus_ids}
        try:
            parent_rows = (
                await db["parent_chunks"]
                .find(
                    with_active_records(parent_query),
                    {"_id": 0},
                )
                .to_list(length=None)
            )
            for pc in parent_rows:
                did = pc.get("doc_id", "")
                pid = pc.get("parent_id", "")
                if did and pid:
                    parent_lookup[(str(pc.get("corpus_id") or ""), did, pid)] = pc
        except Exception as exc:
            logger.debug("Split parent hydration lookup skipped: %s", exc)

    for doc in docs:
        did = doc.get("doc_id", "")
        doc_corpus_id = str(doc.get("corpus_id") or "")
        doc_meta[(doc_corpus_id, did)] = {
            "doc_name": _document_display_name(doc),
            "source_path": doc.get("source_path", ""),
            "corpus_id": doc.get("corpus_id", ""),
            "doc_artifact": (doc.get("doc_profile") or {}).get("doc_artifact") or None,
        }
        for pc in doc.get("parent_chunks", []):
            pid = pc.get("parent_id", "")
            if did and pid:
                parent_lookup[(doc_corpus_id, did, pid)] = pc

    # ── Pass 2: corpus name lookup ───────────────────────────────────────────
    corpus_id_set = {c.corpus_id for c in chunks if c.corpus_id}
    corpus_name_map: dict[str, str] = {}
    if corpus_id_set:
        try:
            corpus_docs = (
                await db["corpora"]
                .find({"corpus_id": {"$in": list(corpus_id_set)}})
                .to_list(length=None)
            )
            corpus_name_map = {c["corpus_id"]: c.get("name", "") for c in corpus_docs}
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
            pc = parent_lookup.get(
                (str(chunk.corpus_id or ""), chunk.doc_id, chunk.parent_id)
            )
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
                if (
                    not getattr(chunk, "chunk_kind", "") or chunk.chunk_kind == "body"
                ) and pc.get("chunk_kind"):
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

            meta = doc_meta.get((str(chunk.corpus_id or ""), chunk.doc_id), {})
            chunk.doc_name = meta.get("doc_name") or chunk.doc_id
            doc_artifact = meta.get("doc_artifact")
            if doc_artifact:
                metadata = dict(chunk.metadata or {})
                metadata["doc_artifact"] = doc_artifact
                chunk.metadata = metadata

        chunk.corpus_name = corpus_name_map.get(chunk.corpus_id, "") or chunk.corpus_id
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
        c.chunk_id for c in chunks if c.chunk_id and not c.chunk_id.endswith("_summary")
    ]
    if not chunk_ids:
        return chunks

    query: dict = {"chunk_id": {"$in": chunk_ids}}
    if corpus_ids:
        query["corpus_id"] = {"$in": corpus_ids}

    parent_ids = sorted(
        {str(c.parent_id) for c in chunks if str(c.parent_id or "").strip()}
    )

    async def _chunk_records():
        return (
            await db["chunks"]
            .find(
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
            )
            .to_list(length=None)
        )

    async def _parent_records():
        if not parent_ids:
            return []
        parent_query: dict = {"parent_id": {"$in": parent_ids}}
        if corpus_ids:
            parent_query["corpus_id"] = {"$in": corpus_ids}
        return (
            await db["parent_chunks"]
            .find(
                with_active_records(parent_query),
                {
                    "_id": 0,
                    "parent_id": 1,
                    "doc_id": 1,
                    "corpus_id": 1,
                    "summary": 1,
                    "retrieval_text": 1,
                },
            )
            .to_list(length=None)
        )

    try:
        records, parent_records = await asyncio.gather(
            _chunk_records(),
            _parent_records(),
        )
    except Exception as exc:
        logger.warning("Reranker text hydration failed: %s", exc)
        return chunks

    by_id = {
        (str(r.get("corpus_id") or ""), str(r.get("chunk_id"))): r
        for r in records
        if r.get("chunk_id")
    }
    parent_context_by_key: dict[tuple[str, str, str], str] = {}
    parent_context_by_id: dict[str, str] = {}
    for parent in parent_records:
        parent_id = str(parent.get("parent_id") or "")
        context = str(
            parent.get("retrieval_text") or parent.get("summary") or ""
        ).strip()
        if not parent_id or not context:
            continue
        parent_context_by_key[
            (
                str(parent.get("corpus_id") or ""),
                str(parent.get("doc_id") or ""),
                parent_id,
            )
        ] = context
        parent_context_by_id.setdefault(parent_id, context)
    if not by_id:
        return chunks

    hydrated: List[SourceChunk] = []
    replaced = 0
    for chunk in chunks:
        copied = chunk.model_copy()
        record = by_id.get((str(copied.corpus_id or ""), str(copied.chunk_id or "")))
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
        parent_context = parent_context_by_key.get(
            (
                str(copied.corpus_id or ""),
                str(copied.doc_id or ""),
                str(copied.parent_id or ""),
            )
        )
        if not parent_context and not copied.corpus_id:
            parent_context = parent_context_by_id.get(str(copied.parent_id or ""))
        if parent_context:
            metadata = dict(copied.metadata or {})
            metadata["reranker_parent_context"] = parent_context[:1200]
            copied.metadata = metadata
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
        docs = (
            await db["documents"]
            .find(
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
            )
            .to_list(length=None)
        )
    except Exception as exc:
        logger.warning("Summary reranker text hydration failed: %s", exc)
        return chunks

    by_doc_parent: dict[tuple[str, str, str], dict] = {}
    by_parent: dict[str, dict] = {}
    doc_names: dict[tuple[str, str], str] = {}
    if parent_ids:
        parent_query: dict = {"parent_id": {"$in": list(parent_ids)}}
        if doc_ids:
            parent_query["doc_id"] = {"$in": list(doc_ids)}
        if corpus_ids:
            parent_query["corpus_id"] = {"$in": corpus_ids}
        try:
            parent_rows = (
                await db["parent_chunks"]
                .find(
                    with_active_records(parent_query),
                    {"_id": 0},
                )
                .to_list(length=None)
            )
            for parent in parent_rows:
                parent_id = str(parent.get("parent_id") or "")
                summary = str(parent.get("summary") or "")
                doc_id = str(parent.get("doc_id") or "")
                corpus_id = str(parent.get("corpus_id") or "")
                if not parent_id or not summary:
                    continue
                by_doc_parent[(corpus_id, doc_id, parent_id)] = parent
                by_parent[parent_id] = parent
        except Exception as exc:
            logger.debug("Split summary hydration lookup skipped: %s", exc)

    for doc in docs:
        doc_id = str(doc.get("doc_id") or "")
        corpus_id = str(doc.get("corpus_id") or "")
        doc_names[(corpus_id, doc_id)] = _document_display_name(doc)
        for parent in doc.get("parent_chunks", []) or []:
            parent_id = str(parent.get("parent_id") or "")
            summary = str(parent.get("summary") or "")
            if not parent_id or not summary:
                continue
            by_doc_parent[(corpus_id, doc_id, parent_id)] = parent
            by_parent[parent_id] = parent

    if not by_doc_parent and not by_parent:
        if not doc_names:
            return chunks
        hydrated_names: List[SourceChunk] = []
        for chunk in chunks:
            copied = chunk.model_copy()
            if not copied.doc_name:
                copied.doc_name = (
                    doc_names.get(
                        (str(copied.corpus_id or ""), str(copied.doc_id or ""))
                    )
                    or copied.doc_name
                )
            hydrated_names.append(copied)
        return hydrated_names

    hydrated: List[SourceChunk] = []
    replaced = 0
    for chunk in chunks:
        copied = chunk.model_copy()
        if not copied.doc_name:
            copied.doc_name = (
                doc_names.get((str(copied.corpus_id or ""), str(copied.doc_id or "")))
                or copied.doc_name
            )
        chunk_id = copied.chunk_id or ""
        parent_id = copied.parent_id or (
            chunk_id.removesuffix("_summary") if chunk_id.endswith("_summary") else ""
        )
        record = by_doc_parent.get(
            (
                str(copied.corpus_id or ""),
                str(copied.doc_id or ""),
                parent_id,
            )
        )
        if record is None and not copied.corpus_id:
            record = by_parent.get(parent_id)
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
            if (not copied.chunk_kind or copied.chunk_kind == "body") and record.get(
                "chunk_kind"
            ):
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
