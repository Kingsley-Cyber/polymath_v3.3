"""
Reranker client — HTTP wrapper for the reranker sidecar.

Sidecar: llama.cpp reranking server at http://reranker:8080/rerank by default.
Falls back to score-sort if the service is unavailable.

Phase 4.5 — code-chunk bypass. Some prose-oriented cross-encoders demote
code-shaped chunks (Luau function bodies look like noise to the scoring head).
When `RERANKER_BYPASS_CODE` is on, the pool is partitioned: prose chunks go
through the cross-encoder, code chunks keep their pre-rerank scores
(vector + BM25 fused). Both halves are min-max normalized to [0,1]
independently before merge so the cross-encoder's wider score range does not
crowd code out.
"""
import logging
import os
import time
from typing import List

import httpx
from config import get_settings
from models.schemas import SourceChunk

logger = logging.getLogger(__name__)

# Per-document character cap for rerank requests. The reranker (llama.cpp
# Qwen3-Reranker) processes each (query, doc) pair against its context window
# and returns HTTP 500 — not a truncated score — when a pair overflows it.
# Large parent chunks / summaries in the candidate pool blow past it. The
# current llama.cpp Qwen3 sidecar hard-fails around a 512-token physical batch;
# a real 3.8k-char chunk still produced 655 tokens at the old 2k-char cap.
# A cross-encoder only needs the leading evidence window, so keep this tight.
_RERANK_MAX_DOC_CHARS = max(
    256,
    int(os.environ.get("RERANKER_MAX_DOC_CHARS", "1000") or 1000),
)
_RERANK_HTTP_BATCH_SIZE = max(
    1,
    int(os.environ.get("RERANKER_HTTP_BATCH_SIZE", "8") or 8),
)
_RERANK_PARTIAL_FAILURE_BUDGET = max(
    1,
    int(os.environ.get("RERANKER_PARTIAL_FAILURE_BUDGET", "6") or 6),
)


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


def _ranked_chunks_from_response(
    pool: List[SourceChunk],
    data: dict,
) -> List[SourceChunk]:
    """Convert supported reranker sidecar response shapes into ranked chunks.

    llama.cpp / Jina-style sidecar:
      {"results": [{"index": 0, "relevance_score": 7.1, ...}]}

    Legacy Docker sentence-transformers sidecar:
      {"results": [{"index": 0, "score": 7.1, "text": "..."}]}

    Apple MLX scaffold:
      {"scores": [0.91, 0.12, ...]}  # aligned to input documents
    """
    def _item_index(item: dict) -> int:
        if "index" in item:
            return int(item["index"])
        if "document_index" in item:
            return int(item["document_index"])
        raise KeyError("index")

    def _item_score(item: dict) -> float:
        if "score" in item:
            return float(item["score"])
        if "relevance_score" in item:
            return float(item["relevance_score"])
        raise KeyError("score")

    ranked_items = data.get("results")
    if not isinstance(ranked_items, list):
        ranked_items = data.get("data")

    if isinstance(ranked_items, list):
        reranked: list[SourceChunk] = []
        for item in ranked_items:
            if not isinstance(item, dict):
                continue
            chunk = pool[_item_index(item)].model_copy()
            chunk.score = _item_score(item)
            reranked.append(chunk)
        return reranked

    if isinstance(data.get("scores"), list):
        scored: list[SourceChunk] = []
        for index, score in enumerate(data["scores"][: len(pool)]):
            chunk = pool[index].model_copy()
            chunk.score = float(score)
            scored.append(chunk)
        return sorted(scored, key=lambda c: c.score, reverse=True)

    raise ValueError("Unsupported reranker response shape")


class RerankerService:
    """HTTP client for the configured reranker sidecar."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._disabled_until = 0.0
        self._last_failure = ""

    async def rerank(self, query: str, candidate_pool: List[SourceChunk]) -> List[SourceChunk]:
        """
        Rerank candidates against the query using the configured sidecar.
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
        """Send a single pool through the reranker sidecar. Falls
        back to vector-score sort on HTTP failure."""
        if not pool:
            return []

        now = time.monotonic()
        if now < self._disabled_until:
            remaining = self._disabled_until - now
            logger.info(
                "Reranker circuit open for %.1fs after %s — falling back to score sort",
                remaining,
                self._last_failure or "previous failure",
            )
            return sorted(pool, key=lambda x: x.score, reverse=True)

        url = f"{self._settings.RERANKER_URL}/rerank"
        timeout = float(getattr(self._settings, "RERANKER_TIMEOUT_SECONDS", 4.0) or 4.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                ranked, successes, failures = await self._rerank_batch_or_split(
                    client=client,
                    url=url,
                    query=query,
                    pool=pool,
                )

            if successes <= 0:
                raise RuntimeError(
                    f"all reranker batches failed (failures={failures})"
                )
            self._last_failure = ""
            self._disabled_until = 0.0
            if failures:
                logger.warning(
                    "Reranker recovered from %d failing sub-batch(es); "
                    "kept original scores for those candidates",
                    failures,
                )
                if failures >= _RERANK_PARTIAL_FAILURE_BUDGET:
                    breaker_seconds = float(
                        getattr(
                            self._settings,
                            "RERANKER_CIRCUIT_BREAKER_SECONDS",
                            120.0,
                        )
                        or 0.0
                    )
                    self._last_failure = (
                        f"partial reranker failure budget exceeded "
                        f"({failures} >= {_RERANK_PARTIAL_FAILURE_BUDGET})"
                    )
                    if breaker_seconds > 0:
                        self._disabled_until = time.monotonic() + breaker_seconds
                    logger.warning(
                        "Reranker partial-failure budget exceeded "
                        "(%d >= %d) — opening circuit for %.0fs",
                        failures,
                        _RERANK_PARTIAL_FAILURE_BUDGET,
                        breaker_seconds,
                    )
            return sorted(ranked, key=lambda c: c.score, reverse=True)

        except Exception as exc:
            breaker_seconds = float(
                getattr(self._settings, "RERANKER_CIRCUIT_BREAKER_SECONDS", 120.0)
                or 0.0
            )
            self._last_failure = str(exc)
            if breaker_seconds > 0:
                self._disabled_until = time.monotonic() + breaker_seconds
            logger.warning(
                "Reranker HTTP call failed after %.1fs (%s) — falling back to score sort%s",
                timeout,
                exc,
                f" and opening circuit for {breaker_seconds:.0f}s"
                if breaker_seconds > 0
                else "",
            )
            return sorted(pool, key=lambda x: x.score, reverse=True)

    async def _rerank_batch_or_split(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        query: str,
        pool: List[SourceChunk],
    ) -> tuple[List[SourceChunk], int, int]:
        """Rerank in bounded HTTP batches and isolate bad candidates.

        The llama.cpp reranker can return HTTP 500 for realistic 20-40 item
        pools even when `/health` and tiny requests succeed. Treat that as a
        batch-shape failure, not proof the service is down: split the batch and
        preserve reranker signal for every sub-batch that still scores.
        """

        if not pool:
            return [], 0, 0
        if len(pool) > _RERANK_HTTP_BATCH_SIZE:
            ranked: List[SourceChunk] = []
            successes = 0
            failures = 0
            for start in range(0, len(pool), _RERANK_HTTP_BATCH_SIZE):
                part, ok, bad = await self._rerank_batch_or_split(
                    client=client,
                    url=url,
                    query=query,
                    pool=pool[start : start + _RERANK_HTTP_BATCH_SIZE],
                )
                ranked.extend(part)
                successes += ok
                failures += bad
            return ranked, successes, failures

        try:
            return await self._post_rerank_batch(
                client=client,
                url=url,
                query=query,
                pool=pool,
            ), 1, 0
        except Exception as exc:
            if len(pool) <= 1:
                logger.warning(
                    "Reranker single-candidate failure; preserving original "
                    "score for chunk_id=%s: %s",
                    getattr(pool[0], "chunk_id", ""),
                    exc,
                )
                return sorted(pool, key=lambda c: c.score, reverse=True), 0, 1

            mid = len(pool) // 2
            logger.info(
                "Reranker batch of %d failed; splitting into %d + %d: %s",
                len(pool),
                mid,
                len(pool) - mid,
                exc,
            )
            left, left_ok, left_bad = await self._rerank_batch_or_split(
                client=client,
                url=url,
                query=query,
                pool=pool[:mid],
            )
            right, right_ok, right_bad = await self._rerank_batch_or_split(
                client=client,
                url=url,
                query=query,
                pool=pool[mid:],
            )
            return left + right, left_ok + right_ok, left_bad + right_bad

    async def _post_rerank_batch(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        query: str,
        pool: List[SourceChunk],
    ) -> List[SourceChunk]:
        # Cap each doc so no single (query, doc) pair overflows the reranker's
        # context and 500s the whole batch (the cross-encoder truncates anyway).
        documents = [(c.text or "")[:_RERANK_MAX_DOC_CHARS] for c in pool]
        resp = await client.post(
            url,
            json={"query": query, "documents": documents},
        )
        resp.raise_for_status()
        data = resp.json()
        return _ranked_chunks_from_response(pool, data)


reranker_service = RerankerService()
