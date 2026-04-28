"""
Reranker client — HTTP wrapper for the reranker sidecar.

Sidecar: cross-encoder/ms-marco-MiniLM-L6-v2 at http://reranker:8080/rerank
Falls back to score-sort if the service is unavailable.
"""
import logging
from typing import List

import httpx
from config import get_settings
from models.schemas import SourceChunk

logger = logging.getLogger(__name__)
_TIMEOUT = 30.0


class RerankerService:
    """HTTP client for the ms-marco cross-encoder reranker sidecar."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def rerank(self, query: str, candidate_pool: List[SourceChunk]) -> List[SourceChunk]:
        """
        Rerank candidates against the query using the cross-encoder sidecar.
        Falls back to vector-score sort if the sidecar is unreachable or returns an error.
        """
        if not candidate_pool:
            return []

        documents = [c.text for c in candidate_pool]
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
                chunk = candidate_pool[item["index"]].model_copy()
                chunk.score = float(item["score"])
                reranked.append(chunk)
            return reranked

        except Exception as exc:
            logger.warning(
                "Reranker HTTP call failed (%s) — falling back to score sort", exc
            )
            return sorted(candidate_pool, key=lambda x: x.score, reverse=True)


reranker_service = RerankerService()
