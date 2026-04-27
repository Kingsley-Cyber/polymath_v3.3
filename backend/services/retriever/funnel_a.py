import asyncio
import logging
from typing import List, Optional

from config import get_settings
from models.schemas import SourceChunk
from qdrant_client import AsyncQdrantClient, models

logger = logging.getLogger(__name__)


class FunnelA:
    """
    FUNNEL A - Summary Breadth
    Retrieves broad summary chunks from Qdrant across selected collections.
    """

    def __init__(self):
        settings = get_settings()
        # Initialize the async Qdrant client using settings URL
        self.client = AsyncQdrantClient(url=settings.QDRANT_URL)

    async def search(
        self,
        query_vector: list[float],
        corpus_ids: Optional[List[str]] = None,
        collections: Optional[List[str]] = None,
        top_k: int = 20,
        fair_mode: bool = True,
    ) -> List[SourceChunk]:
        """
        Execute breadth search for summaries across target collections in parallel.

        Args:
            query_vector: The embedded query vector.
            corpus_ids: List of allowed corpus IDs to scope the search.
            collections: Qdrant collections to search in parallel.
            top_k: Max results to return.
            fair_mode: If True, omits summary searches for multi-corpus queries
                       so summary-heavy corpora don't dominate the results.
        """
        if not collections:
            logger.warning("No collections specified for Funnel A search.")
            return []

        # Fair mode: Omit summary search if searching across multiple corpora
        if fair_mode and corpus_ids and len(corpus_ids) > 1:
            logger.info(
                "Fair mode active for cross-corpus: skipping Funnel A (summaries)."
            )
            return []

        # Build Qdrant filter
        must_conditions = [
            models.FieldCondition(
                key="chunk_type",
                match=models.MatchValue(value="summary"),
            )
        ]

        # Scope strictly to allowed corpus IDs to prevent data leakage
        if corpus_ids:
            must_conditions.append(
                models.FieldCondition(
                    key="corpus_id",
                    match=models.MatchAny(any=corpus_ids),
                )
            )

        # Mirror funnel_b: drop summary points that summarize TOC / biblio /
        # index / appendix / front_matter / back_matter parents. Legacy
        # summaries (no `chunk_kind` payload) pass through as before.
        from services.ingestion.section_classifier import NOISY_KINDS
        must_not_conditions = [
            models.FieldCondition(
                key="chunk_kind",
                match=models.MatchAny(any=list(NOISY_KINDS)),
            )
        ]

        query_filter = models.Filter(must=must_conditions, must_not=must_not_conditions)

        # Launch searches in parallel
        tasks = [
            self._search_collection(collection_name, query_vector, query_filter, top_k)
            for collection_name in collections
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge, dedupe gracefully, and flatten
        merged_chunks = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Funnel A search task failed: {result}")
            else:
                merged_chunks.extend(result)

        # Global sort across all collection results by vector similarity
        merged_chunks.sort(key=lambda x: x.score, reverse=True)
        return merged_chunks[:top_k]

    async def _search_collection(
        self,
        collection_name: str,
        query_vector: list[float],
        query_filter: models.Filter,
        limit: int,
    ) -> List[SourceChunk]:
        """
        Execute search on a specific Qdrant collection.
        """
        try:
            # qdrant-client ≥1.10 renamed `search()` → `query_points()`; the
            # old name was removed in 1.13+. `query_points` returns a wrapper
            # with .points containing the scored hits.
            resp = await self.client.query_points(
                collection_name=collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            hits = resp.points

            chunks = []
            for hit in hits:
                payload = hit.payload or {}

                # For summary chunks, the text stored is usually the summary itself
                chunk_text = payload.get("chunk_text", payload.get("text", ""))

                chunks.append(
                    SourceChunk(
                        chunk_id=payload.get("chunk_id", str(hit.id)),
                        parent_id=payload.get("parent_id", ""),
                        doc_id=payload.get("doc_id", ""),
                        corpus_id=payload.get("corpus_id", ""),
                        text=chunk_text,
                        summary=chunk_text,
                        score=hit.score,
                        source_tier=payload.get("source_tier", "summary"),
                        heading_path=payload.get("heading_path") or None,
                    )
                )
            return chunks
        except Exception as e:
            logger.error(
                f"Failed to search collection {collection_name} in Funnel A: {e}"
            )
            return []


# Singleton service instance
funnel_a = FunnelA()
