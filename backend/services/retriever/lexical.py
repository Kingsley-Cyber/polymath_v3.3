"""
Lexical retriever sidecar for true hybrid search.

Vector retrieval is excellent for semantic recall, but it can miss exact
anchors such as filenames, headings, function names, product names, and quoted
phrases. This module provides the lexical / BM25 half of hybrid retrieval.

Backend selection is per-corpus:
  • New corpora → Qdrant sparse vectors (BM25 with server-side IDF).
    Lives in the same engine as dense retrieval, same `chunk_kind` /
    `corpus_id` filters apply, no cross-engine merge cost.
  • Legacy corpora → MongoDB `$text` index + regex fallback (the original
    pre-Phase-22 path), kept intact so existing data keeps working until
    a backfill migration converts those collections.

The two backends return the same `SourceChunk` shape, so the merge.py /
rerank pipeline downstream is unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from config import get_settings
from models.schemas import SourceChunk
from pymongo.errors import OperationFailure
from qdrant_client import AsyncQdrantClient, models as qmodels
from services.conversation import conversation_service

logger = logging.getLogger(__name__)
_settings = get_settings()

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
    """BM25 lexical search. Routes to Qdrant sparse for new corpora,
    Mongo $text for legacy corpora — based on per-collection layout."""

    def __init__(self) -> None:
        # Lazily initialized. Importing AsyncQdrantClient at module load
        # works because qdrant_client is already a runtime dep.
        self._qdrant: AsyncQdrantClient | None = None

    def _client(self) -> AsyncQdrantClient:
        if self._qdrant is None:
            self._qdrant = AsyncQdrantClient(url=_settings.QDRANT_URL)
        return self._qdrant

    async def search(
        self,
        query: str,
        corpus_ids: list[str] | None,
        *,
        top_k: int = 10,
    ) -> list[SourceChunk]:
        """Return lexical child-chunk candidates scoped to selected corpora.

        Per-corpus routing: each corpus is checked once for whether its
        Qdrant collection carries a "sparse" named vector. New ingests
        get Qdrant sparse search; legacy collections fall back to the
        Mongo $text path.
        """
        if top_k <= 0 or not query.strip() or not corpus_ids:
            return []

        sparse_corpora, legacy_corpora = await self._split_by_layout(corpus_ids)
        results: list[SourceChunk] = []

        if sparse_corpora:
            try:
                results.extend(
                    await self._qdrant_sparse_search(
                        query, sparse_corpora, top_k=top_k
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Qdrant sparse lexical search failed (%s) — falling back to Mongo for %d corpora",
                    exc, len(sparse_corpora),
                )
                # On Qdrant failure, fall through to Mongo for these too.
                legacy_corpora = list(set(legacy_corpora) | set(sparse_corpora))

        if legacy_corpora:
            db = conversation_service._db
            if db is None:
                logger.warning(
                    "Lexical Mongo fallback skipped for %d corpora: MongoDB not connected",
                    len(legacy_corpora),
                )
            else:
                try:
                    results.extend(
                        await self._text_search(db, query, legacy_corpora, top_k=top_k)
                    )
                except OperationFailure as exc:
                    logger.warning(
                        "Mongo text search unavailable (%s); falling back to bounded regex",
                        exc,
                    )
                    results.extend(
                        await self._regex_search(db, query, legacy_corpora, top_k=top_k)
                    )
                except Exception as exc:
                    logger.warning("Mongo lexical search failed (%s)", exc)

        # Dedupe across the two backends by chunk_id and sort by score.
        seen: set[str] = set()
        deduped: list[SourceChunk] = []
        for chunk in sorted(results, key=lambda c: c.score, reverse=True):
            cid = chunk.chunk_id
            if cid and cid in seen:
                continue
            if cid:
                seen.add(cid)
            deduped.append(chunk)
        return deduped[:top_k]

    async def _split_by_layout(
        self, corpus_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """Group corpora by whether their `naive` collection has sparse
        vectors. Sparse corpora go through Qdrant; legacy go through Mongo.
        Errors / missing collections fall through as legacy (safest)."""
        from services.storage.qdrant_writer import _col_for_corpus, _collection_layout
        sparse_corpora: list[str] = []
        legacy_corpora: list[str] = []
        client = self._client()
        for cid in corpus_ids:
            name = _col_for_corpus(cid, "naive")
            try:
                _, has_sparse = await _collection_layout(client, name)
            except Exception:
                has_sparse = False
            if has_sparse:
                sparse_corpora.append(cid)
            else:
                legacy_corpora.append(cid)
        return sparse_corpora, legacy_corpora

    async def _qdrant_sparse_search(
        self,
        query: str,
        corpus_ids: list[str],
        *,
        top_k: int,
    ) -> list[SourceChunk]:
        """BM25 search inside Qdrant via the named "sparse" vector. One
        query per per-corpus collection; results merged by score."""
        from services.ingestion.section_classifier import NOISY_KINDS
        from services.storage.qdrant_writer import _col_for_corpus
        from services.storage.sparse_encoder import encode_query

        sparse_query = encode_query(query)
        if not sparse_query.indices:
            # Query had no usable tokens after stopword stripping — skip.
            return []

        # Same default-noise filter as funnel_a / funnel_b, so all three
        # halves of hybrid see the same candidate universe.
        query_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="chunk_type",
                    match=qmodels.MatchValue(value="child"),
                )
            ],
            must_not=[
                qmodels.FieldCondition(
                    key="chunk_kind",
                    match=qmodels.MatchAny(any=list(NOISY_KINDS)),
                )
            ],
        )

        client = self._client()
        all_hits: list[SourceChunk] = []
        for cid in corpus_ids:
            name = _col_for_corpus(cid, "naive")
            try:
                resp = await client.query_points(
                    collection_name=name,
                    query=sparse_query,
                    using="sparse",
                    query_filter=query_filter,
                    limit=top_k,
                    with_payload=True,
                )
            except Exception as exc:
                logger.warning(
                    "Qdrant sparse query failed for collection=%s: %s", name, exc,
                )
                continue
            for hit in resp.points or []:
                payload = hit.payload or {}
                all_hits.append(
                    SourceChunk(
                        chunk_id=str(payload.get("chunk_id") or hit.id),
                        parent_id=str(payload.get("parent_id") or ""),
                        doc_id=str(payload.get("doc_id") or ""),
                        corpus_id=str(payload.get("corpus_id") or ""),
                        text=str(payload.get("chunk_text") or payload.get("text") or ""),
                        summary=None,
                        score=float(hit.score or 0.0),
                        source_tier=f"{payload.get('source_tier') or 'chunk'}+lexical",
                        heading_path=payload.get("heading_path") or None,
                        provenance=[{"retriever": "qdrant_sparse"}],
                    )
                )
        return all_hits

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
