import asyncio
import logging
from typing import List, Optional

from config import get_settings
from models.schemas import SourceChunk
from services.facets import metadata_with_facets
from qdrant_client import AsyncQdrantClient, models

logger = logging.getLogger(__name__)


class FunnelB:
    """
    FUNNEL B - Child Precision
    Retrieves precise child chunks from Qdrant across selected collections.
    """

    def __init__(self):
        settings = get_settings()
        # Initialize the async Qdrant client using settings URL
        self.client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
        )

    async def search(
        self,
        query_vector: list[float],
        corpus_ids: Optional[List[str]] = None,
        collections: Optional[List[str]] = None,
        top_k: int = 30,
        query_text: str | None = None,
    ) -> List[SourceChunk]:
        """
        Execute precision search across target collections in parallel.
        """
        if not collections:
            logger.warning("No collections specified for Funnel B search.")
            return []

        # Build Qdrant filter
        must_conditions = [
            models.FieldCondition(
                key="chunk_type",
                match=models.MatchValue(value="child"),
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

        # Default-exclude noisy chunk_kind values (toc, bibliography, index,
        # appendix, front_matter, back_matter). Legacy points written before
        # the field existed have no `chunk_kind` payload and so don't match
        # `MatchAny`, which keeps them included by default — this is the
        # intended backwards-compat path. To opt-in noisy kinds, callers
        # should override `query_filter` upstream.
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
            self._search_collection(
                collection_name,
                query_vector,
                query_filter,
                top_k,
                query_text=query_text,
            )
            for collection_name in collections
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge, dedupe gracefully, and flatten
        merged_chunks = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Funnel B search task failed: {result}")
            else:
                merged_chunks.extend(result)

        # Global sort across all collection results by retrieval score
        # (dense cosine for legacy collections, Qdrant RRF score for hybrid).
        merged_chunks.sort(key=lambda x: x.score, reverse=True)
        return merged_chunks[:top_k]

    async def _search_collection(
        self,
        collection_name: str,
        query_vector: list[float],
        query_filter: models.Filter,
        limit: int,
        *,
        query_text: str | None = None,
    ) -> List[SourceChunk]:
        """
        Execute search on a specific Qdrant collection.
        """
        try:
            # New corpora use named vectors {"dense", "sparse"}; legacy
            # corpora use unnamed dense. When both named vectors are present,
            # Fast Search runs native Qdrant hybrid retrieval:
            #
            #   dense prefetch + sparse BM25 prefetch -> FusionQuery(RRF)
            #
            # This keeps the Fast lane Qdrant-only while recovering exact
            # tokens such as identifiers, acronyms, section numbers, and error
            # codes that dense embeddings can smear away. Legacy collections
            # stay on the previous dense-only call shape.
            from services.storage.qdrant_writer import _collection_layout
            from services.storage.sparse_encoder import encode_query

            has_named, has_sparse = await _collection_layout(self.client, collection_name)
            sparse_query = encode_query(query_text)
            kwargs = {
                "collection_name": collection_name,
                "query": query_vector,
                "query_filter": query_filter,
                "limit": limit,
                "with_payload": True,
            }
            retriever_provenance = [{"retriever": "qdrant_dense"}]
            used_hybrid_rrf = False
            if has_named and has_sparse and getattr(sparse_query, "indices", None):
                kwargs = {
                    "collection_name": collection_name,
                    "query": models.FusionQuery(fusion=models.Fusion.RRF),
                    "prefetch": [
                        models.Prefetch(
                            query=query_vector,
                            using="dense",
                            filter=query_filter,
                            limit=limit,
                        ),
                        models.Prefetch(
                            query=sparse_query,
                            using="sparse",
                            filter=query_filter,
                            limit=limit,
                        ),
                    ],
                    "limit": limit,
                    "with_payload": True,
                }
                retriever_provenance = [
                    {"retriever": "qdrant_dense"},
                    {"retriever": "qdrant_sparse"},
                    {"retriever": "qdrant_rrf"},
                ]
                used_hybrid_rrf = True
            elif has_named:
                kwargs["using"] = "dense"

            try:
                resp = await self.client.query_points(**kwargs)
            except Exception as exc:
                if not used_hybrid_rrf:
                    raise
                logger.warning(
                    "Qdrant dense+sparse RRF failed for %s; falling back to dense-only: %s",
                    collection_name,
                    exc,
                )
                fallback_kwargs = {
                    "collection_name": collection_name,
                    "query": query_vector,
                    "query_filter": query_filter,
                    "limit": limit,
                    "with_payload": True,
                    "using": "dense",
                }
                retriever_provenance = [{"retriever": "qdrant_dense"}]
                resp = await self.client.query_points(**fallback_kwargs)
            hits = resp.points

            chunks = []
            for hit in hits:
                payload = hit.payload or {}
                chunks.append(
                    SourceChunk(
                        chunk_id=payload.get("chunk_id", str(hit.id)),
                        parent_id=payload.get("parent_id", ""),
                        doc_id=payload.get("doc_id", ""),
                        corpus_id=payload.get("corpus_id", ""),
                        text=payload.get("chunk_text", payload.get("text", "")),
                        summary=None,  # Not applicable for children by default
                        score=hit.score,
                        source_tier=payload.get("source_tier", "vector"),
                        chunk_kind=payload.get("chunk_kind", "body"),
                        doc_name=payload.get("doc_name") or payload.get("filename"),
                        heading_path=payload.get("heading_path") or None,
                        language=payload.get("language"),
                        metadata=metadata_with_facets(payload.get("metadata"), payload),
                        provenance=list(retriever_provenance),
                    )
                )
            return chunks
        except Exception as e:
            logger.error(
                f"Failed to search collection {collection_name} in Funnel B: {e}"
            )
            return []


# Singleton service instance
funnel_b = FunnelB()
