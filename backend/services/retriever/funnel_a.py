import asyncio
import logging
from typing import List, Optional

from config import get_settings
from models.schemas import SourceChunk
from services.facets import metadata_with_facets
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
        self.client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
            prefer_grpc=settings.QDRANT_PREFER_GRPC,
            grpc_port=settings.QDRANT_GRPC_PORT,
        )

    async def search(
        self,
        query_vector: list[float],
        corpus_ids: Optional[List[str]] = None,
        collections: Optional[List[str]] = None,
        top_k: int = 20,
        fair_mode: bool = True,
        query_text: str | None = None,
        doc_ids: Optional[List[str]] = None,
        parent_ids: Optional[List[str]] = None,
    ) -> List[SourceChunk]:
        """
        Execute breadth search for summaries across target collections in parallel.

        Args:
            query_vector: The embedded query vector.
            corpus_ids: List of allowed corpus IDs to scope the search.
            collections: Qdrant collections to search in parallel.
            top_k: Max results to return.
            fair_mode: If True, multi-corpus queries run one summary search PER
                       corpus with an equal share of ``top_k`` each, so a
                       summary-heavy corpus cannot dominate the pool. (This
                       used to skip summaries entirely for multi-corpus, which
                       threw away the broadest cross-corpus signal.)
        """
        if not collections:
            logger.warning("No collections specified for Funnel A search.")
            return []

        # Fair mode: balanced per-corpus summary search instead of a blackout.
        # Each corpus gets its own budget; the summary-heavy corpus is capped
        # at its share, but every corpus still contributes breadth.
        if fair_mode and corpus_ids and len(corpus_ids) > 1:
            per_corpus_k = max(2, top_k // len(corpus_ids))
            logger.info(
                "Fair mode cross-corpus: per-corpus Funnel A (%d corpora × k=%d).",
                len(corpus_ids),
                per_corpus_k,
            )
            per_corpus_pools = await asyncio.gather(
                *[
                    self._search_scoped(
                        query_vector,
                        corpus_scope=[corpus_id],
                        collections=collections,
                        top_k=per_corpus_k,
                        query_text=query_text,
                        doc_ids=doc_ids,
                        parent_ids=parent_ids,
                    )
                    for corpus_id in corpus_ids
                ]
            )
            merged: List[SourceChunk] = []
            for pool in per_corpus_pools:
                merged.extend(pool)
            merged.sort(key=lambda x: x.score, reverse=True)
            return merged[:top_k]

        return await self._search_scoped(
            query_vector,
            corpus_scope=corpus_ids,
            collections=collections,
            top_k=top_k,
            query_text=query_text,
            doc_ids=doc_ids,
            parent_ids=parent_ids,
        )

    async def _search_scoped(
        self,
        query_vector: list[float],
        *,
        corpus_scope: Optional[List[str]],
        collections: List[str],
        top_k: int,
        query_text: str | None = None,
        doc_ids: Optional[List[str]] = None,
        parent_ids: Optional[List[str]] = None,
    ) -> List[SourceChunk]:
        """One summary search over ``collections`` scoped to ``corpus_scope``."""

        # Build Qdrant filter
        must_conditions = [
            models.FieldCondition(
                key="chunk_type",
                match=models.MatchValue(value="summary"),
            )
        ]

        # Scope strictly to allowed corpus IDs to prevent data leakage
        if corpus_scope:
            must_conditions.append(
                models.FieldCondition(
                    key="corpus_id",
                    match=models.MatchAny(any=corpus_scope),
                )
            )

        if doc_ids:
            must_conditions.append(
                models.FieldCondition(
                    key="doc_id",
                    match=models.MatchAny(any=list(dict.fromkeys(doc_ids))),
                )
            )

        if parent_ids:
            must_conditions.append(
                models.FieldCondition(
                    key="parent_id",
                    match=models.MatchAny(any=list(dict.fromkeys(parent_ids))),
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
        *,
        query_text: str | None = None,
    ) -> List[SourceChunk]:
        """
        Execute search on a specific Qdrant collection.
        """
        try:
            # qdrant-client ≥1.10 renamed `search()` → `query_points()`; the
            # old name was removed in 1.13+. New corpora use named dense+sparse
            # vectors, so summary search can also run Qdrant-native RRF. Legacy
            # corpora keep the unnamed dense default.
            from services.storage.qdrant_writer import _collection_layout
            from services.storage.sparse_encoder import encode_query

            has_named, has_sparse = await _collection_layout(
                self.client, collection_name
            )
            sparse_query = encode_query(query_text)
            kwargs = {
                "collection_name": collection_name,
                "query": query_vector,
                "query_filter": query_filter,
                "limit": limit,
                "with_payload": True,
            }
            retriever_provenance = [{"retriever": "qdrant_dense_summary"}]
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
                    {"retriever": "qdrant_dense_summary"},
                    {"retriever": "qdrant_sparse_summary"},
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
                    "Qdrant summary dense+sparse RRF failed for %s; falling back to dense-only: %s",
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
                retriever_provenance = [{"retriever": "qdrant_dense_summary"}]
                resp = await self.client.query_points(**fallback_kwargs)
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
                f"Failed to search collection {collection_name} in Funnel A: {e}"
            )
            return []


# Singleton service instance
funnel_a = FunnelA()
