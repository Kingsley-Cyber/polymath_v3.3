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
from services.conversation import conversation_service
from services.facets import metadata_with_facets
from services.retriever.lexical import _regex_score, _terms

logger = logging.getLogger(__name__)

_DOC_ANCHOR_MAX_DOCS = 4
_DOC_ANCHOR_THRESHOLD = 0.72
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


def _score_doc_match(query: str, label: str) -> float:
    query_norm = _norm(query)
    label_norm = _norm(label)
    if not query_norm or not label_norm:
        return 0.0
    if query_norm == label_norm:
        return 1.0
    q_tokens = _tokens(query_norm)
    label_tokens = _tokens(label_norm)
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


def _score_doc(query: str, doc: dict[str, Any]) -> tuple[float, str, set[str]]:
    best_score = 0.0
    best_label = ""
    best_terms: set[str] = set()
    for label in _doc_labels(doc):
        score = _score_doc_match(query, label)
        if score > best_score:
            best_score = score
            best_label = label
            best_terms = _tokens(label)
    return best_score, best_label, best_terms


def _chunk_search_terms(query: str, anchor_terms: set[str]) -> list[str]:
    scored: list[tuple[float, int, str]] = []
    raw_terms = _terms(query)
    for index, term in enumerate(raw_terms):
        if term in anchor_terms or term in _CHUNK_QUERY_NOISE:
            continue
        score = 0.0
        if term in _CONCEPT_RECALL_HINTS:
            score += 1.0
        if len(term) >= 8:
            score += 0.25
        if "_" in term or "-" in term:
            score += 0.1
        scored.append((score, index, term))
    scored.sort(key=lambda item: (-item[0], item[1]))
    terms = [term for _score, _index, term in scored]
    return terms[:14] or raw_terms[:14]


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

        for doc_score, label, anchor_terms, doc in docs[:_DOC_ANCHOR_MAX_DOCS]:
            terms = _chunk_search_terms(query, anchor_terms | all_anchor_terms)
            rows = await self._chunks_for_doc(
                db,
                doc,
                terms=terms,
                per_doc=max(1, per_doc),
            )
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

    async def _matching_docs(
        self,
        db,
        query: str,
        corpus_ids: list[str],
    ) -> list[tuple[float, str, set[str], dict[str, Any]]]:
        cursor = db["documents"].find(
            {"corpus_id": {"$in": corpus_ids}},
            {
                "_id": 0,
                "doc_id": 1,
                "corpus_id": 1,
                "filename": 1,
                "title": 1,
                "metadata": 1,
                "document_metadata": 1,
                "source_metadata": 1,
                "facet_profile": 1,
            },
        )
        docs = await cursor.to_list(length=None)
        scored: list[tuple[float, str, set[str], dict[str, Any]]] = []
        for doc in docs:
            score, label, label_terms = _score_doc(query, doc)
            if score >= _DOC_ANCHOR_THRESHOLD:
                scored.append((score, label, label_terms, doc))
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
