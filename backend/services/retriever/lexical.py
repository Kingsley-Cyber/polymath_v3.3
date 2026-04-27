"""
Lexical retriever sidecar for true hybrid search.

Vector retrieval is excellent for semantic recall, but it can miss exact
anchors such as filenames, headings, function names, product names, and quoted
phrases. This module adds a bounded MongoDB text-search candidate pool that the
main retriever can merge with Qdrant results before graph expansion/reranking.
It is intentionally additive: qdrant_only stays pure vector, while hybrid tiers
can opt into this exact-match recall path.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from models.schemas import SourceChunk
from pymongo.errors import OperationFailure
from services.conversation import conversation_service

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
        "from", "has", "have", "in", "into", "is", "it", "its", "of",
        "on", "or", "that", "the", "this", "to", "was", "were", "what",
        "when", "where", "which", "who", "why", "will", "with", "how",
        "do", "does", "did", "about", "between", "vs", "versus",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]{1,}")


def _terms(query: str) -> list[str]:
    """Extract lexical terms worth matching in Mongo text/regex search."""
    seen: set[str] = set()
    out: list[str] = []
    for term in _TOKEN_RE.findall(query or ""):
        low = term.lower().strip("-_'")
        if len(low) < 2 or low in _STOP_WORDS or low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def _regex_score(query: str, terms: list[str], row: dict[str, Any]) -> float:
    """Small fallback scorer used when Mongo text index is unavailable."""
    text = str(row.get("text") or "")
    heading = " ".join(str(h) for h in (row.get("heading_path") or []))
    haystack = f"{heading}\n{text}".lower()
    if not haystack:
        return 0.0

    hits = sum(1 for term in terms if term in haystack)
    if hits <= 0:
        return 0.0
    coverage = hits / max(len(terms), 1)
    phrase = 0.18 if query.strip().lower() in haystack else 0.0
    heading_boost = 0.12 if any(term in heading.lower() for term in terms) else 0.0
    return round(min(0.98, 0.45 + coverage * 0.35 + phrase + heading_boost), 4)


class LexicalRetriever:
    """Bounded Mongo text search over child chunks."""

    async def search(
        self,
        query: str,
        corpus_ids: list[str] | None,
        *,
        top_k: int = 10,
    ) -> list[SourceChunk]:
        """Return lexical child-chunk candidates scoped to selected corpora."""
        if top_k <= 0 or not query.strip() or not corpus_ids:
            return []

        db = conversation_service._db
        if db is None:
            logger.warning("Lexical search skipped: MongoDB is not connected")
            return []

        try:
            return await self._text_search(db, query, corpus_ids, top_k=top_k)
        except OperationFailure as exc:
            logger.warning(
                "Mongo text search unavailable (%s); falling back to bounded regex",
                exc,
            )
            return await self._regex_search(db, query, corpus_ids, top_k=top_k)
        except Exception as exc:
            logger.warning("Lexical search failed (%s)", exc)
            return []

    async def _text_search(
        self,
        db,
        query: str,
        corpus_ids: list[str],
        *,
        top_k: int,
    ) -> list[SourceChunk]:
        projection = {
            "_id": 0,
            "chunk_id": 1,
            "parent_id": 1,
            "doc_id": 1,
            "corpus_id": 1,
            "text": 1,
            "heading_path": 1,
            "source_tier": 1,
            "score": {"$meta": "textScore"},
        }
        # Same default-noise filter as the Qdrant funnels (funnel_a / funnel_b):
        # exclude TOC / bibliography / index / appendix / front_matter /
        # back_matter chunks. `$nin` on a field that doesn't exist returns
        # True, so legacy chunks without `chunk_kind` pass through unchanged
        # — same backwards-compat behavior as the vector path.
        from services.ingestion.section_classifier import NOISY_KINDS
        cursor = (
            db["chunks"]
            .find(
                {
                    "corpus_id": {"$in": corpus_ids},
                    "$text": {"$search": query},
                    "chunk_kind": {"$nin": list(NOISY_KINDS)},
                },
                projection,
            )
            .sort([("score", {"$meta": "textScore"})])
            .limit(top_k)
        )
        rows = await cursor.to_list(length=top_k)
        if not rows:
            return []

        max_score = max(float(r.get("score") or 0.0) for r in rows) or 1.0
        chunks = [
            self._row_to_chunk(
                row,
                score=round(float(row.get("score") or 0.0) / max_score, 4),
            )
            for row in rows
        ]
        logger.info("Lexical text search returned %d candidates", len(chunks))
        return chunks

    async def _regex_search(
        self,
        db,
        query: str,
        corpus_ids: list[str],
        *,
        top_k: int,
    ) -> list[SourceChunk]:
        terms = _terms(query)
        if not terms:
            return []
        # Bounded fallback for dev/old DBs before the text index exists.
        conditions = [
            {"text": {"$regex": re.escape(term), "$options": "i"}}
            for term in terms[:6]
        ]
        conditions.extend(
            {"heading_path": {"$regex": re.escape(term), "$options": "i"}}
            for term in terms[:6]
        )
        # Mirror the text-search default-noise filter on the regex fallback.
        from services.ingestion.section_classifier import NOISY_KINDS
        cursor = (
            db["chunks"]
            .find(
                {
                    "corpus_id": {"$in": corpus_ids},
                    "$or": conditions,
                    "chunk_kind": {"$nin": list(NOISY_KINDS)},
                },
                {
                    "_id": 0,
                    "chunk_id": 1,
                    "parent_id": 1,
                    "doc_id": 1,
                    "corpus_id": 1,
                    "text": 1,
                    "heading_path": 1,
                    "source_tier": 1,
                },
            )
            .limit(max(top_k * 4, 20))
        )
        rows = await cursor.to_list(length=max(top_k * 4, 20))
        scored = [
            (row, _regex_score(query, terms, row))
            for row in rows
        ]
        scored = [(row, score) for row, score in scored if score > 0.0]
        scored.sort(key=lambda item: item[1], reverse=True)
        chunks = [
            self._row_to_chunk(row, score=score)
            for row, score in scored[:top_k]
        ]
        logger.info("Lexical regex fallback returned %d candidates", len(chunks))
        return chunks

    @staticmethod
    def _row_to_chunk(row: dict[str, Any], *, score: float) -> SourceChunk:
        return SourceChunk(
            chunk_id=str(row.get("chunk_id") or ""),
            parent_id=str(row.get("parent_id") or ""),
            doc_id=str(row.get("doc_id") or ""),
            corpus_id=str(row.get("corpus_id") or ""),
            text=str(row.get("text") or ""),
            summary=None,
            score=float(score),
            source_tier=f"{row.get('source_tier') or 'chunk'}+lexical",
            heading_path=row.get("heading_path") or None,
            provenance=[{"retriever": "lexical"}],
        )


lexical_retriever = LexicalRetriever()
