"""Document-title anchored recall for hydrated retrieval tiers.

Vector/BM25 retrieval can miss a query that names source documents and then asks
for an abstract comparison inside those documents. The book title usually lives
in Mongo document metadata, not in every child chunk payload, so this lane first
matches document labels against the query, then runs bounded in-document lexical
recall to produce normal SourceChunk candidates.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from models.schemas import SourceChunk
from pymongo.errors import OperationFailure
from services.cache_util import TTLCache
from services.conversation import conversation_service
from services.facets import metadata_with_facets
from services.retriever.lexical import _regex_score, _terms

logger = logging.getLogger(__name__)

_DOC_ANCHOR_MAX_DOCS = 4
_DOC_ANCHOR_THRESHOLD = 0.72
# Doc-label table cache (speed campaign 2026-07-02). Keyed by corpus set;
# entries are (labels, slim_doc) tuples. See _doc_label_table for why.
# TTL raised 120s -> 900s after live evidence: real-world queries arrive
# minutes apart, so a 2-minute TTL made nearly EVERY turn pay the cold
# 486-doc fetch (observed anchor:9.64s cold vs 0.53s warm). Doc labels only
# change at ingest; a new book being invisible to title-anchor recall for
# up to 15 minutes is an acceptable trade until ingest-time invalidation
# is wired.
_DOC_LABEL_CACHE = TTLCache(maxsize=32, ttl_seconds=900.0)
_CHUNK_QUERY_NOISE = frozenset(
    {
        "according",
        "based",
        "compare",
        "defensible",
        "direct",
        "distinguish",
        "excerpt",
        "excerpts",
        "identify",
        "inferred",
        "retrieved",
        "recommendation",
        "recommendations",
        "support",
        "textual",
    }
)
_CONCEPT_RECALL_HINTS = frozenset(
    {
        "abstraction",
        "abstractions",
        "architecture",
        "architectural",
        "cognitive",
        "density",
        "depth",
        "domain",
        "enterprise",
        "flexibility",
        "flexible",
        "gateway",
        "gateways",
        "information",
        "interface",
        "interfaces",
        "layer",
        "layering",
        "layers",
        "logic",
        "mapper",
        "mappers",
        "mapping",
        "model",
        "models",
        "navigation",
        "personality",
        "preference",
        "preferences",
        "structure",
        "structures",
        "theory",
        "type",
        "types",
        "ui",
        "user",
        "users",
        "ux",
        "workflow",
    }
)


def _norm(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\.[a-z0-9]{1,8}$", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: Any) -> set[str]:
    return set(_terms(_norm(value)))


def _metadata_value(blob: Any, *keys: str) -> str:
    if not isinstance(blob, dict):
        return ""
    for key in keys:
        value = blob.get(key)
        if isinstance(value, list):
            value = " ".join(str(v) for v in value if v)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _metadata_values(blob: Any, *keys: str) -> list[str]:
    if not isinstance(blob, dict):
        return []
    values: list[str] = []
    for key in keys:
        value = blob.get(key)
        raw_values = value if isinstance(value, list) else [value]
        for raw in raw_values:
            text = str(raw or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def _label_variants(value: Any) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    base = re.sub(r"\.[a-zA-Z0-9]{1,8}$", "", raw).strip()
    variants = [raw, base]
    cleaned = re.sub(r"[_;]+", " ", base)
    variants.append(cleaned)
    for sep in (" -- ", " — ", " – ", " - ", ": "):
        if sep in base:
            variants.append(base.split(sep, 1)[0].strip())
        if sep in cleaned:
            variants.append(cleaned.split(sep, 1)[0].strip())
    if "_" in base:
        variants.append(base.split("_", 1)[0].strip())

    out: list[str] = []
    for item in variants:
        text = re.sub(r"\s+", " ", str(item or "")).strip(" -_")
        if text and text not in out:
            out.append(text)
    return out


def _doc_labels(doc: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    facet_profile = (
        doc.get("facet_profile") if isinstance(doc.get("facet_profile"), dict) else {}
    )
    title_sources = [
        doc.get("title"),
        doc.get("filename"),
        _metadata_value(doc.get("metadata"), "title", "book_title", "name"),
        _metadata_value(doc.get("document_metadata"), "title", "book_title", "name"),
        _metadata_value(doc.get("source_metadata"), "title", "book_title", "name"),
    ]
    authors: list[str] = []
    for blob in (
        doc.get("metadata"),
        doc.get("document_metadata"),
        doc.get("source_metadata"),
    ):
        authors.extend(
            _metadata_values(blob, "author", "authors", "creator", "creators")
        )

    titles: list[str] = []
    for source in title_sources:
        for variant in _label_variants(source):
            if variant not in titles:
                titles.append(variant)

    for value in titles:
        text = str(value or "").strip()
        if text and text not in labels:
            labels.append(text)
            for author in authors[:3]:
                combo = f"{author} {text}"
                if combo not in labels:
                    labels.append(combo)
    for facet in facet_profile.get("doc_facets") or []:
        if not isinstance(facet, dict):
            continue
        values = [
            facet.get("display_name"),
            str(facet.get("facet_id") or "").replace("_", " "),
            *(facet.get("aliases") or [])[:4],
            *(facet.get("search_terms") or [])[:4],
        ]
        for value in values:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if text and text not in labels:
                labels.append(text)
    return labels


def _score_doc_match(
    query: str,
    label: str,
    *,
    query_norm: str | None = None,
    q_tokens: set[str] | None = None,
    label_norm: str | None = None,
    label_tokens: set[str] | None = None,
) -> float:
    # H6 fast path: callers scoring ~4k labels per retrieve pass precomputed
    # norms/tokens (query once, labels from the cached table). Defaults keep
    # the one-off signature identical for tests/back-compat.
    query_norm = _norm(query) if query_norm is None else query_norm
    label_norm = _norm(label) if label_norm is None else label_norm
    if not query_norm or not label_norm:
        return 0.0
    if query_norm == label_norm:
        return 1.0
    q_tokens = _tokens(query_norm) if q_tokens is None else q_tokens
    label_tokens = _tokens(label_norm) if label_tokens is None else label_tokens
    if not q_tokens or not label_tokens:
        return 0.0
    if len(label_tokens) >= 2 and label_norm in query_norm:
        return 0.98
    overlap = q_tokens & label_tokens
    if len(overlap) < 2:
        return 0.0
    coverage = len(overlap) / max(1, len(label_tokens))
    query_coverage = len(overlap) / max(1, len(q_tokens))
    if len(label_tokens) <= 3:
        if coverage < 1.0:
            return 0.0
    elif len(label_tokens) <= 5:
        if coverage < 0.75:
            return 0.0
    elif coverage < 0.75 and len(overlap) < 4:
        return 0.0
    return min(0.94, 0.56 + 0.30 * coverage + 0.08 * query_coverage)


def _chunk_search_terms(query: str, anchor_terms: set[str]) -> list[str]:
    # Curated query concepts (_CONCEPT_RECALL_HINTS) are the recall payload, so
    # they are kept in full and a long filler word can never push them out of
    # the bounded term list. Non-concept terms compete for the remaining budget
    # by length/compound score. A concept-heavy query (>14 hints) keeps them all
    # rather than silently truncating the tail (e.g. dropping navigation/workflow
    # from a UI/UX question) — that was a real anchor-recall gap.
    raw_terms = _terms(query)
    # Some curated concepts (workflow, process, application, steps, task…) double
    # as operator/intent markers and are stripped by the lexical stop-word filter
    # even when used as CONTENT here. Recover any concept hint that literally
    # appears in the query — anchor chunk-search wants the content noun back.
    present = set(re.findall(r"[a-z0-9]+", query.lower()))
    seen = set(raw_terms)
    for hint in _CONCEPT_RECALL_HINTS:
        if hint in present and hint not in seen:
            raw_terms.append(hint)
            seen.add(hint)
    concept_terms: list[str] = []
    other_scored: list[tuple[float, int, str]] = []
    for index, term in enumerate(raw_terms):
        if term in anchor_terms or term in _CHUNK_QUERY_NOISE:
            continue
        if term in _CONCEPT_RECALL_HINTS:
            concept_terms.append(term)
            continue
        score = 0.0
        if len(term) >= 8:
            score += 0.25
        if "_" in term or "-" in term:
            score += 0.1
        other_scored.append((score, index, term))
    other_scored.sort(key=lambda item: (-item[0], item[1]))
    others = [term for _score, _index, term in other_scored]
    budget = max(14, len(concept_terms))
    return (concept_terms + others)[:budget] or raw_terms[:14]


def _candidate_text(row: dict[str, Any], document_label: str) -> str:
    body = str(row.get("text") or "")
    heading = " / ".join(str(part) for part in (row.get("heading_path") or []))
    prefix = f"Document: {document_label}"
    if heading:
        prefix += f"\nHeading: {heading}"
    return f"{prefix}\n{body}".strip()


class DocumentAnchorRetriever:
    """Mongo-backed source-title recall for Hybrid Search and Graph Augmentation tiers."""

    async def search(
        self,
        query: str,
        corpus_ids: list[str] | None,
        *,
        top_k: int = 6,
        per_doc: int = 3,
    ) -> list[SourceChunk]:
        if not query or not corpus_ids or top_k <= 0:
            return []
        db = conversation_service._db
        if db is None:
            return []

        docs = await self._matching_docs(db, query, corpus_ids)
        if not docs:
            return []

        out: list[SourceChunk] = []
        seen: set[str] = set()
        all_anchor_terms: set[str] = set()
        for _doc_score, _label, anchor_terms, _doc in docs[:_DOC_ANCHOR_MAX_DOCS]:
            all_anchor_terms.update(anchor_terms)

        # H6 — the ≤4 per-doc chunk lookups are independent ($text per doc,
        # regex fallback per doc); run them CONCURRENTLY and assemble in doc
        # order below, so wall = slowest doc instead of the sum. Selection is
        # identical: per-doc rows never depended on earlier docs.
        import asyncio as _asyncio

        matched = docs[:_DOC_ANCHOR_MAX_DOCS]
        rows_per_doc = await _asyncio.gather(*[
            self._chunks_for_doc(
                db,
                doc,
                terms=_chunk_search_terms(query, anchor_terms | all_anchor_terms),
                per_doc=max(1, per_doc),
            )
            for _doc_score, _label, anchor_terms, doc in matched
        ])
        for (doc_score, label, anchor_terms, doc), rows in zip(matched, rows_per_doc):
            for row, chunk_score in rows:
                chunk_id = str(row.get("chunk_id") or "")
                if not chunk_id or chunk_id in seen:
                    continue
                seen.add(chunk_id)
                score = min(0.99, 0.66 + 0.18 * doc_score + 0.16 * chunk_score)
                out.append(
                    SourceChunk(
                        chunk_id=chunk_id,
                        parent_id=str(row.get("parent_id") or ""),
                        doc_id=str(row.get("doc_id") or doc.get("doc_id") or ""),
                        corpus_id=str(row.get("corpus_id") or doc.get("corpus_id") or ""),
                        text=_candidate_text(row, label),
                        summary=None,
                        score=round(score, 4),
                        source_tier="document_anchor+lexical",
                        chunk_kind=str(row.get("chunk_kind") or "body"),
                        heading_path=row.get("heading_path") or None,
                        language=row.get("language"),
                        metadata=metadata_with_facets(row.get("metadata"), row),
                        provenance=[
                            {
                                "retriever": "document_anchor",
                                "document_label": label,
                                "document_score": round(doc_score, 3),
                            }
                        ],
                    )
                )
                if len(out) >= top_k:
                    logger.info(
                        "Document anchor recall returned %d candidates from %d docs",
                        len(out),
                        len(docs),
                    )
                    return out
        if out:
            logger.info(
                "Document anchor recall returned %d candidates from %d docs",
                len(out),
                len(docs),
            )
        return out

    _anchor_index_state: bool | None = None

    async def _ensure_anchor_index(self, db) -> bool:
        """H6 — one compound TEXT index over every label source _doc_labels
        reads. Idempotent create on first use; any failure (permissions, a
        conflicting text index, fake test db) downgrades this process to the
        legacy cached-table path permanently. Index name is stable so
        re-creates are no-ops."""
        if self._anchor_index_state is not None:
            return self._anchor_index_state
        fields = [("title", "text"), ("filename", "text")]
        for blob in ("metadata", "document_metadata", "source_metadata"):
            for key_ in ("title", "book_title", "name", "author", "authors",
                         "creator", "creators"):
                fields.append((f"{blob}.{key_}", "text"))
        fields += [
            ("facet_profile.doc_facets.display_name", "text"),
            ("facet_profile.doc_facets.aliases", "text"),
            ("facet_profile.doc_facets.search_terms", "text"),
        ]
        try:
            await db["documents"].create_index(
                fields, name="documents_anchor_text", background=True
            )
            self._anchor_index_state = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "documents anchor text index unavailable (%s) — using the "
                "cached label-table path", exc,
            )
            self._anchor_index_state = False
        return self._anchor_index_state

    async def _matching_docs_indexed(
        self,
        db,
        query: str,
        corpus_ids: list[str],
    ) -> list[tuple[float, str, set[str], dict[str, Any]]] | None:
        """H6 — indexed candidate lookup: ONE $text query over the documents
        anchor index returns <=24 candidate docs; scoring (same fn, same
        threshold) runs on those alone. Cold cost = one indexed query instead
        of fetching + decoding every document record (486 docs ≈ seconds).
        Side-win: no label-table TTL — new books anchor instantly.

        Returns None when the indexed path is unavailable (caller falls back
        to the cached-table scan). Known edge: a label made ENTIRELY of text-
        index stopwords can be missed here; DOCUMENT_ANCHOR_INDEXED=false
        restores the exhaustive scan.
        """
        from pymongo.errors import OperationFailure as _OpFail

        if not await self._ensure_anchor_index(db):
            return None
        projection: dict[str, int] = {
            "_id": 0, "doc_id": 1, "corpus_id": 1, "filename": 1, "title": 1,
            "facet_profile.doc_facets.display_name": 1,
            "facet_profile.doc_facets.facet_id": 1,
            "facet_profile.doc_facets.aliases": 1,
            "facet_profile.doc_facets.search_terms": 1,
        }
        for blob in ("metadata", "document_metadata", "source_metadata"):
            for subkey in ("title", "book_title", "name", "author", "authors",
                           "creator", "creators"):
                projection[f"{blob}.{subkey}"] = 1
        try:
            candidates = await db["documents"].find(
                {"corpus_id": {"$in": corpus_ids}, "$text": {"$search": query}},
                projection,
            ).limit(24).to_list(length=24)
        except _OpFail:
            self._anchor_index_state = False
            return None
        query_norm = _norm(query)
        q_tokens = _tokens(query_norm)
        scored: list[tuple[float, str, set[str], dict[str, Any]]] = []
        for doc in candidates:
            best_score, best_label, best_tokens = 0.0, "", set()
            for label in _doc_labels(doc):
                label_norm = _norm(label)
                label_tokens = _tokens(label_norm)
                score = _score_doc_match(
                    query, label,
                    query_norm=query_norm, q_tokens=q_tokens,
                    label_norm=label_norm, label_tokens=label_tokens,
                )
                if score > best_score:
                    best_score, best_label, best_tokens = score, label, label_tokens
            if best_score >= _DOC_ANCHOR_THRESHOLD:
                scored.append((
                    best_score, best_label, set(best_tokens),
                    {"doc_id": doc.get("doc_id"), "corpus_id": doc.get("corpus_id")},
                ))
        scored.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        return scored[:_DOC_ANCHOR_MAX_DOCS]

    async def _doc_label_table(
        self,
        db,
        corpus_ids: list[str],
    ) -> list[tuple[list[str], dict[str, Any]]]:
        """Cached (labels, slim_doc) table per corpus set.

        Speed campaign (2026-07-02, funnel_detail evidence): _matching_docs
        used to fetch EVERY document record — metadata blobs included — from
        Mongo and recompute the label variants on EVERY retrieval, main and
        each support pass alike. Under 3-4 concurrent retrievals the repeated
        fetch + BSON decode + label derivation stalled the event loop
        uniformly (all four funnels inflated together to ~3s, anchor the
        slowest at 4.4s). Doc titles/authors change only at ingest; 120s of
        staleness for brand-new docs is acceptable for anchor recall.
        """
        key = "|".join(sorted(corpus_ids))
        cached = _DOC_LABEL_CACHE.get(key)
        if cached is not None:
            return cached
        # H6 — project ONLY the subfields _doc_labels reads. The old
        # whole-blob projection decoded every doc's full metadata/docling
        # payload on a cold fetch (486 docs ≈ seconds of BSON decode).
        _META_SUBKEYS = (
            "title", "book_title", "name",
            "author", "authors", "creator", "creators",
        )
        projection: dict[str, int] = {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "filename": 1,
            "title": 1,
            "facet_profile.doc_facets.display_name": 1,
            "facet_profile.doc_facets.facet_id": 1,
            "facet_profile.doc_facets.aliases": 1,
            "facet_profile.doc_facets.search_terms": 1,
        }
        for blob in ("metadata", "document_metadata", "source_metadata"):
            for subkey in _META_SUBKEYS:
                projection[f"{blob}.{subkey}"] = 1
        cursor = db["documents"].find(
            {"corpus_id": {"$in": corpus_ids}}, projection
        )
        docs = await cursor.to_list(length=None)
        table: list[tuple[list[tuple[str, str, set[str]]], dict[str, Any]]] = []
        for doc in docs:
            labels = _doc_labels(doc)
            if not labels:
                continue
            # Precompute (label, norm, tokens) once at table build — scoring
            # runs per retrieve over every label; tokenizing ~4k labels per
            # call was pure waste (labels change only at ingest).
            label_structs = [
                (label, _norm(label), _tokens(label)) for label in labels
            ]
            # Only identifiers survive into the cache — downstream
            # (_chunks_for_doc) reads doc_id/corpus_id; the metadata blobs
            # stay out of memory.
            slim = {
                "doc_id": doc.get("doc_id"),
                "corpus_id": doc.get("corpus_id"),
            }
            table.append((label_structs, slim))
        _DOC_LABEL_CACHE.set(key, table)
        return table

    async def _matching_docs(
        self,
        db,
        query: str,
        corpus_ids: list[str],
    ) -> list[tuple[float, str, set[str], dict[str, Any]]]:
        from config import get_settings as _gs

        if bool(getattr(_gs(), "DOCUMENT_ANCHOR_INDEXED", True)):
            indexed = await self._matching_docs_indexed(db, query, corpus_ids)
            if indexed is not None:
                return indexed
        table = await self._doc_label_table(db, corpus_ids)
        query_norm = _norm(query)
        q_tokens = _tokens(query_norm)
        scored: list[tuple[float, str, set[str], dict[str, Any]]] = []
        for label_structs, doc in table:
            best_score = 0.0
            best_label = ""
            best_tokens: set[str] = set()
            for label, label_norm, label_tokens in label_structs:
                score = _score_doc_match(
                    query, label,
                    query_norm=query_norm, q_tokens=q_tokens,
                    label_norm=label_norm, label_tokens=label_tokens,
                )
                if score > best_score:
                    best_score = score
                    best_label = label
                    best_tokens = label_tokens
            if best_score >= _DOC_ANCHOR_THRESHOLD:
                scored.append((best_score, best_label, set(best_tokens), doc))
        scored.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        return scored[:_DOC_ANCHOR_MAX_DOCS]

    async def _chunks_for_doc(
        self,
        db,
        doc: dict[str, Any],
        *,
        terms: list[str],
        per_doc: int,
    ) -> list[tuple[dict[str, Any], float]]:
        from services.ingestion.section_classifier import NOISY_KINDS

        doc_id = str(doc.get("doc_id") or "")
        corpus_id = str(doc.get("corpus_id") or "")
        if not doc_id or not corpus_id:
            return []
        projection = {
            "_id": 0,
            "chunk_id": 1,
            "parent_id": 1,
            "doc_id": 1,
            "corpus_id": 1,
            "text": 1,
            "heading_path": 1,
            "source_tier": 1,
            "chunk_kind": 1,
            "language": 1,
            "metadata": 1,
            "facet_ids": 1,
            "facet_text": 1,
            "content_facet_ids": 1,
            "content_facet_text": 1,
            "content_facet_source": 1,
            "content_facet_confidence": 1,
            "score": {"$meta": "textScore"},
        }
        search_text = " ".join(terms[:12])
        rows: list[dict[str, Any]] = []
        if search_text:
            try:
                cursor = (
                    db["chunks"]
                    .find(
                        {
                            "corpus_id": corpus_id,
                            "doc_id": doc_id,
                            "$text": {"$search": search_text},
                            "chunk_kind": {"$nin": list(NOISY_KINDS)},
                        },
                        projection,
                    )
                    .sort([("score", {"$meta": "textScore"})])
                    .limit(per_doc)
                )
                rows = await cursor.to_list(length=per_doc)
            except OperationFailure:
                rows = []

        if rows:
            max_score = max(float(row.get("score") or 0.0) for row in rows) or 1.0
            return [
                (row, min(1.0, float(row.get("score") or 0.0) / max_score))
                for row in rows
            ]

        conditions = []
        for term in terms[:8]:
            conditions.append({"text": {"$regex": re.escape(term), "$options": "i"}})
            conditions.append({"heading_path": {"$regex": re.escape(term), "$options": "i"}})
            conditions.append({"facet_text": {"$regex": re.escape(term), "$options": "i"}})
            conditions.append({"facet_ids": {"$regex": re.escape(term), "$options": "i"}})
            conditions.append({"content_facet_text": {"$regex": re.escape(term), "$options": "i"}})
            conditions.append({"content_facet_ids": {"$regex": re.escape(term), "$options": "i"}})
        if conditions:
            cursor = (
                db["chunks"]
                .find(
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "$or": conditions,
                        "chunk_kind": {"$nin": list(NOISY_KINDS)},
                    },
                    {key: value for key, value in projection.items() if key != "score"},
                )
                .limit(max(per_doc * 8, 24))
            )
            rows = await cursor.to_list(length=max(per_doc * 8, 24))
            scored = [
                (row, _regex_score(" ".join(terms), terms, row))
                for row in rows
            ]
            scored = [(row, score) for row, score in scored if score > 0.0]
            scored.sort(key=lambda item: item[1], reverse=True)
            if scored:
                return scored[:per_doc]

        # Last-resort support for explicit source-constrained queries: return a
        # small body sample from the named document rather than pretending the
        # title was not found.
        cursor = (
            db["chunks"]
            .find(
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "chunk_kind": {"$nin": list(NOISY_KINDS)},
                },
                {key: value for key, value in projection.items() if key != "score"},
            )
            .limit(per_doc)
        )
        rows = await cursor.to_list(length=per_doc)
        return [(row, 0.25) for row in rows]


document_anchor_retriever = DocumentAnchorRetriever()
