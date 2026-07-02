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
from services.facets import metadata_with_facets
from services.retriever.query_grounding import (
    chunk_concept_hits,
    concept_groups,
    group_matches_text,
)
from services.retriever.query_semantics import lexical_terms

logger = logging.getLogger(__name__)
_settings = get_settings()


def _terms(query: str) -> list[str]:
    """Extract lexical terms worth matching in Mongo text/regex search."""
    return lexical_terms(query)


def _regex_score(query: str, terms: list[str], row: dict[str, Any]) -> float:
    """Small fallback scorer used when Mongo text index is unavailable."""
    text = str(row.get("text") or "")
    heading = " ".join(str(h) for h in (row.get("heading_path") or []))
    facet_text = " ".join(
        [
            str(row.get("facet_text") or ""),
            " ".join(str(v) for v in (row.get("facet_ids") or [])),
            str(row.get("content_facet_text") or ""),
            " ".join(str(v) for v in (row.get("content_facet_ids") or [])),
        ]
    )
    haystack = f"{heading}\n{facet_text}\n{text}".lower()
    if not haystack:
        return 0.0

    # Word-boundary matching, NOT substring (live regression 2026-07-02: a
    # seduction question's lexical terms scored a Flutter chapter 0.92
    # because 'life' substring-matched "LIFEcycle", 'mid' matched "MIDdle" —
    # cross-domain polysemy amplified by partial-word hits).
    def _whole_word(term: str, hay: str) -> bool:
        return bool(
            re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", hay)
        )

    hits = sum(1 for term in terms if _whole_word(term, haystack))
    if hits <= 0:
        return 0.0
    coverage = hits / max(len(terms), 1)
    phrase = 0.18 if query.strip().lower() in haystack else 0.0
    heading_lower = heading.lower()
    heading_boost = (
        0.12 if any(_whole_word(term, heading_lower) for term in terms) else 0.0
    )
    return round(min(0.98, 0.45 + coverage * 0.35 + phrase + heading_boost), 4)


def _normalize_scores_to_unit(chunks: list[SourceChunk]) -> None:
    """Scale chunk scores into [0,1] in place by dividing by the pool max.

    Parity with the Mongo $text path (_text_search divides by its max). Raw
    Qdrant sparse BM25 scores run ~100-140; left unscaled, the lexical lane
    dominates merge_pools' max()+sort and the pre-rerank cut, starving dense +
    graph candidates before the cross-encoder. Pure, dependency-free, and safe
    on empty/None/zero pools — unit-tested without any live service.
    """
    if not chunks:
        return
    max_score = max(float(c.score or 0.0) for c in chunks) or 1.0
    for c in chunks:
        c.score = round(float(c.score or 0.0) / max_score, 4)


class LexicalRetriever:
    """BM25 lexical search. Routes to Qdrant sparse for new corpora,
    Mongo $text for legacy corpora — based on per-collection layout."""

    def __init__(self) -> None:
        # Lazily initialized. Importing AsyncQdrantClient at module load
        # works because qdrant_client is already a runtime dep.
        self._qdrant: AsyncQdrantClient | None = None

    def _client(self) -> AsyncQdrantClient:
        if self._qdrant is None:
            self._qdrant = AsyncQdrantClient(
                url=_settings.QDRANT_URL,
                timeout=_settings.QDRANT_TIMEOUT_SECONDS,
                prefer_grpc=_settings.QDRANT_PREFER_GRPC,
                grpc_port=_settings.QDRANT_GRPC_PORT,
            )
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
        db = conversation_service._db

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

        coverage_results = await self._concept_coverage_recall(
            query,
            sparse_corpora=sparse_corpora,
            legacy_corpora=legacy_corpora,
            existing=results,
            db=db,
            per_concept_k=2,
        )
        results.extend(coverage_results)

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

    async def _concept_coverage_recall(
        self,
        query: str,
        *,
        sparse_corpora: list[str],
        legacy_corpora: list[str],
        existing: list[SourceChunk],
        db,
        per_concept_k: int = 2,
    ) -> list[SourceChunk]:
        """Add tiny, per-concept lexical recall for missing query concepts.

        This prevents a multi-concept query from being dominated by one common
        term. Example: "NLP and Python" should seed at least some NLP-bearing
        evidence instead of returning only Python snippets.
        """
        groups = concept_groups(query, max_groups=6)
        if len(groups) <= 1:
            return []

        supplemental: list[SourceChunk] = []
        bridge_query = " ".join(group.key for group in groups[:4])
        if bridge_query:
            if sparse_corpora:
                supplemental.extend(
                    await self._qdrant_sparse_search(
                        bridge_query,
                        sparse_corpora,
                        top_k=per_concept_k,
                    )
                )
            if legacy_corpora and db is not None:
                try:
                    supplemental.extend(
                        await self._text_search(
                            db,
                            bridge_query,
                            legacy_corpora,
                            top_k=per_concept_k,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Lexical concept bridge search failed for %s: %s",
                        bridge_query,
                        exc,
                    )

        missing = [
            group
            for group in groups
            if not any(chunk_concept_hits(chunk, [group])[0] for chunk in existing)
        ]
        if not missing and not supplemental:
            return []

        for group in missing[:4]:
            variants = list(dict.fromkeys((group.key, *group.aliases)))[:2]
            for variant in variants:
                if sparse_corpora:
                    supplemental.extend(
                        await self._qdrant_sparse_search(
                            variant,
                            sparse_corpora,
                            top_k=per_concept_k,
                        )
                    )
                if legacy_corpora and db is not None:
                    try:
                        supplemental.extend(
                            await self._text_search(
                                db,
                                variant,
                                legacy_corpora,
                                top_k=per_concept_k,
                            )
                        )
                    except OperationFailure as exc:
                        logger.warning(
                            "Mongo text coverage search unavailable (%s); "
                            "falling back to regex for concept=%s",
                            exc,
                            group.key,
                        )
                        supplemental.extend(
                            await self._regex_search(
                                db,
                                variant,
                                legacy_corpora,
                                top_k=per_concept_k,
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "Lexical concept coverage search failed for %s: %s",
                            group.key,
                            exc,
                        )

        filtered: list[SourceChunk] = []
        for chunk in supplemental:
            hits, _ = chunk_concept_hits(chunk, groups)
            search_groups = missing or groups
            matched_group = next(
                (
                    group
                    for group in search_groups
                    if group_matches_text(
                        group,
                        " ".join([chunk.text, chunk.doc_name or ""]),
                    )
                ),
                None,
            )
            if matched_group is None:
                continue
            copied = chunk.model_copy()
            copied.source_tier = f"{copied.source_tier}+coverage"
            copied.provenance = list(copied.provenance or [])
            copied.provenance.append(
                {
                    "retriever": "lexical_coverage",
                    "concept": matched_group.key,
                    "concept_hits": hits,
                }
            )
            filtered.append(copied)

        if filtered:
            logger.info(
                "Lexical concept coverage added %d candidate(s) for missing concepts=%s",
                len(filtered),
                [group.key for group in missing],
            )
        return filtered

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
                        chunk_kind=payload.get("chunk_kind", "body"),
                        doc_name=payload.get("doc_name") or payload.get("filename"),
                        heading_path=payload.get("heading_path") or None,
                        language=payload.get("language"),
                        metadata=metadata_with_facets(payload.get("metadata"), payload),
                        provenance=[{"retriever": "qdrant_sparse"}],
                    )
                )
        # Normalize raw BM25/IDF scores to [0,1] for parity with the Mongo
        # $text path, so the lexical lane is commensurable with dense cosine +
        # graph scores at merge_pools (the cross-encoder remains final arbiter).
        _normalize_scores_to_unit(all_hits)
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
        conditions.extend(
            {"facet_text": {"$regex": re.escape(term), "$options": "i"}}
            for term in terms[:6]
        )
        conditions.extend(
            {"facet_ids": {"$regex": re.escape(term), "$options": "i"}}
            for term in terms[:6]
        )
        conditions.extend(
            {"content_facet_text": {"$regex": re.escape(term), "$options": "i"}}
            for term in terms[:6]
        )
        conditions.extend(
            {"content_facet_ids": {"$regex": re.escape(term), "$options": "i"}}
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
                    "chunk_kind": 1,
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
            chunk_kind=str(row.get("chunk_kind") or "body"),
            heading_path=row.get("heading_path") or None,
            language=row.get("language"),
            metadata=metadata_with_facets(row.get("metadata"), row),
            provenance=[{"retriever": "lexical"}],
        )


lexical_retriever = LexicalRetriever()
