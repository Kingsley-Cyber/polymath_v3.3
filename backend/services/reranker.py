"""
Reranker client — HTTP wrapper for the reranker sidecar.

Sidecar: cross-encoder/ms-marco-MiniLM-L6-v2 at http://reranker:8080/rerank.
Falls back to score-sort if the service is unavailable.

Phase 4.5 — code-chunk bypass. The MS MARCO cross-encoder is trained on
prose Q&A and systematically demotes code-shaped chunks (Luau function
bodies look like noise to its scoring head). When `RERANKER_BYPASS_CODE`
is on, the pool is partitioned: prose chunks go through the cross-encoder
as before, code chunks keep their pre-rerank scores (vector + BM25 fused).
Both halves are min-max normalized to [0,1] independently before merge
so the cross-encoder's wider score range doesn't crowd code out.
"""
import logging
from typing import List

import httpx
from config import get_settings
from models.schemas import SourceChunk

logger = logging.getLogger(__name__)
_TIMEOUT = 30.0


def _is_code_chunk(s: SourceChunk) -> bool:
    """Code chunks are those with a `language` field set. Mirrors the gate
    used by code_lane_skills.is_code_source so the formatter and reranker
    agree on what counts as code."""
    return bool(getattr(s, "language", None))


def _minmax_inplace(pool: List[SourceChunk]) -> None:
    """Normalize pool scores to [0, 1] in-place. Single-element or
    all-equal pools collapse to 1.0 (highest meaningful rank)."""
    if not pool:
        return
    scores = [c.score for c in pool]
    lo, hi = min(scores), max(scores)
    if hi <= lo:
        for c in pool:
            c.score = 1.0
        return
    span = hi - lo
    for c in pool:
        c.score = (c.score - lo) / span


class RerankerService:
    """HTTP client for the ms-marco cross-encoder reranker sidecar."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def rerank(self, query: str, candidate_pool: List[SourceChunk]) -> List[SourceChunk]:
        """
        Rerank candidates against the query using the cross-encoder sidecar.
        Falls back to vector-score sort if the sidecar is unreachable or returns an error.

        Phase 4.5: when `RERANKER_BYPASS_CODE` is enabled (default True),
        code chunks bypass the cross-encoder and keep their pre-rerank
        score. The pools are normalized and merged so neither side crowds
        the other out of the top ranks.
        """
        if not candidate_pool:
            return []

        bypass_code = bool(getattr(self._settings, "RERANKER_BYPASS_CODE", True))
        if not bypass_code:
            return await self._rerank_pool(query, candidate_pool)

        code_pool = [c for c in candidate_pool if _is_code_chunk(c)]
        prose_pool = [c for c in candidate_pool if not _is_code_chunk(c)]

        if not code_pool:
            return await self._rerank_pool(query, prose_pool)
        if not prose_pool:
            # All-code pool — cross-encoder would systematically misorder.
            # Keep the pre-rerank order (vector + BM25 fusion already gave
            # us the best signal available for these chunks).
            logger.info(
                "Reranker bypass: all %d candidates are code — skipping cross-encoder",
                len(code_pool),
            )
            return sorted(code_pool, key=lambda c: c.score, reverse=True)

        reranked_prose = await self._rerank_pool(query, prose_pool)

        # Tiny-pool short-circuit. Min-max normalization collapses single-
        # element pools to score=1.0 (no spread to normalize against),
        # which would force both a lone prose chunk and a lone code chunk
        # to tie at the top regardless of their actual relevance signal.
        # When either subpool has ≤1 element, skip normalization and trust
        # the raw scores. The cross-encoder / cosine scale mismatch is
        # less harmful than the all-1.0 collapse on 1-vs-N pools because
        # raw scores at least preserve within-pool ordering.
        if len(reranked_prose) <= 1 or len(code_pool) <= 1:
            merged = sorted(
                reranked_prose + code_pool,
                key=lambda c: c.score,
                reverse=True,
            )
        else:
            # Normalize both halves to [0,1] so a cross-encoder logit of +9
            # doesn't auto-beat a cosine-fused score of +0.8.
            _minmax_inplace(reranked_prose)
            _minmax_inplace(code_pool)
            merged = sorted(
                reranked_prose + code_pool,
                key=lambda c: c.score,
                reverse=True,
            )
        logger.info(
            "Reranker bypass: cross-encoded %d prose, kept original scores on %d code (merged %d)",
            len(reranked_prose), len(code_pool), len(merged),
        )
        return merged

    async def _rerank_pool(self, query: str, pool: List[SourceChunk]) -> List[SourceChunk]:
        """Send a single pool through the cross-encoder sidecar. Falls
        back to vector-score sort on HTTP failure."""
        if not pool:
            return []

        documents = [c.text for c in pool]
        url = f"{self._settings.RERANKER_URL}/rerank"

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={"query": query, "documents": documents},
                )
                resp.raise_for_status()
                data = resp.json()

            reranked: list[SourceChunk] = []
            for item in data["results"]:
                chunk = pool[item["index"]].model_copy()
                chunk.score = float(item["score"])
                reranked.append(chunk)
            return reranked

        except Exception as exc:
            logger.warning(
                "Reranker HTTP call failed (%s) — falling back to score sort", exc
            )
            return sorted(pool, key=lambda x: x.score, reverse=True)


reranker_service = RerankerService()
