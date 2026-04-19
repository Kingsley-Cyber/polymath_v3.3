"""
Retriever orchestrator — Phase 5 & 6 query pipeline (spec-locked).

Flow (spec §RETRIEVAL RECIPE):
  [0] Strategy intersection — downgrade tier if any corpus lacks the capability
  [1] embed query
  [2] FUNNEL A (summaries, polymath_hrag) [fair-mode skips for multi-corpus]
    + FUNNEL B (children, polymath_naive)  [per-corpus round-robin: 20 each]
  [3] merge & dedupe by parent_id
  [4] Mode A graph expansion (qdrant_mongo_graph tier + NEO4J_ENABLED only)
  [5] rerank ONCE on full pool (ms-marco sidecar, fallback: score sort)
  [6] trim to DEFAULT_RETRIEVAL_K
  [7] hydrate from MongoDB — parent text + corpus_name + doc_name
      (hydrate resolves parent_id for Mode A chunks; drops empty-text results)

Cross-corpus constraints (spec §CROSS-CORPUS QUERY CONSTRAINTS):
  - Hard cap: 3 corpora (validated in ChatRequest.corpus_ids)
  - Round-robin: limit=20 per corpus → 60 max → rerank → top-10
  - Fair mode: FUNNEL A (summaries) skipped for multi-corpus
  - Strategy intersection: graph requires use_neo4j=True on ALL selected corpora
"""
import asyncio
import logging

from config import get_settings
from models.schemas import RetrievalResult, RetrievalTier, SourceChunk
from services.embedder import embed_query
from services.reranker import reranker_service
from services.retriever.funnel_a import funnel_a
from services.retriever.funnel_b import funnel_b
from services.retriever.hydrate import hydrate_chunks
from services.retriever.merge import merge_pools
from services.retriever.mode_a import mode_a_expansion

logger = logging.getLogger(__name__)
settings = get_settings()

_PER_CORPUS_LIMIT = 20   # spec: 20 per corpus for round-robin
_SINGLE_CORPUS_LIMIT = 40  # spec §5.9a: retrieve 40 pre-rerank for single-corpus


class RetrieverOrchestrator:
    """Orchestrates the full spec-locked retrieval pipeline."""

    # ── helpers ────────────────────────────────────────────────────────────────

    def _resolve_collections(
        self,
        tier: RetrievalTier,
        corpus_ids: list[str] | None,
        collections: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        """
        Returns (funnel_a_cols, funnel_b_cols) — expanded per-corpus.

        Phase 7.5: every corpus owns its own collections. We resolve to
        `corpus_{cid8}_hrag/naive/graph` per corpus and let the funnels
        iterate over the list (they already loop `for collection_name in
        collections`). Per-corpus payload filter on corpus_id is still
        applied inside the funnels as defense-in-depth.

        If a caller passes `collections` explicitly (escape hatch — e.g.
        debug tools), we honor those names verbatim. Otherwise we expand
        from `corpus_ids`.
        """
        from services.storage.qdrant_writer import _col_for_corpus

        if collections:
            # Explicit override — caller knows what they're doing.
            a = [c for c in collections if "hrag" in c]
            b = [c for c in collections if "naive" in c or "graph" in c]
            return a, b

        if not corpus_ids:
            return [], []

        a_cols = [_col_for_corpus(cid, "hrag") for cid in corpus_ids]
        b_cols = [_col_for_corpus(cid, "naive") for cid in corpus_ids]
        if tier == RetrievalTier.qdrant_mongo_graph:
            b_cols.extend(_col_for_corpus(cid, "graph") for cid in corpus_ids)
        return a_cols, b_cols

    async def _enforce_strategy_intersection(
        self,
        requested_tier: RetrievalTier,
        corpus_ids: list[str] | None,
    ) -> tuple[RetrievalTier, str | None]:
        """
        Spec §CROSS-CORPUS: "strategy intersection: available strategies = what
        ALL selected corpora support."

        Graph tier requires use_neo4j=True on ALL selected corpora.
        Any mismatch → downgrade to qdrant_mongo (never fail the request).

        Returns:
            (effective_tier, downgrade_reason) — reason is None when no downgrade.
        """
        if not corpus_ids:
            return requested_tier, None
        if requested_tier != RetrievalTier.qdrant_mongo_graph:
            return requested_tier, None  # qdrant_only / qdrant_mongo always available

        try:
            from services.conversation import conversation_service

            db = conversation_service._db
            if db is None:
                return requested_tier, None

            corpus_docs = await db["corpora"].find(
                {"corpus_id": {"$in": corpus_ids}}
            ).to_list(length=None)

            if len(corpus_docs) < len(corpus_ids):
                reason = (
                    f"Strategy intersection: {len(corpus_docs)}/{len(corpus_ids)} "
                    "corpora found — graph tier downgraded to qdrant_mongo."
                )
                logger.warning(reason)
                return RetrievalTier.qdrant_mongo, reason

            configs = [c.get("default_ingestion_config", {}) for c in corpus_docs]

            if not all(c.get("use_neo4j", False) for c in configs):
                missing = [
                    c.get("name") or c.get("corpus_id", "?")
                    for c in corpus_docs
                    if not c.get("default_ingestion_config", {}).get("use_neo4j", False)
                ]
                reason = (
                    "Strategy intersection: graph tier requires use_neo4j=True on "
                    "all selected corpora. Missing: "
                    + ", ".join(missing)
                    + ". Downgraded to qdrant_mongo."
                )
                logger.warning(reason)
                return RetrievalTier.qdrant_mongo, reason

        except Exception as exc:
            logger.warning(
                "Strategy intersection check failed (%s) — keeping requested tier", exc
            )

        return requested_tier, None

    # ── main entry ─────────────────────────────────────────────────────────────

    async def retrieve(
        self,
        query: str,
        corpus_ids: list[str] | None,
        retrieval_tier: RetrievalTier,
        collections: list[str] | None,
        # Phase 18 — optional per-request overrides resolved by chat_orchestrator.
        # When absent, retriever uses server defaults (existing behavior).
        retrieval_k: int | None = None,
        rerank_enabled: bool = True,
    ) -> RetrievalResult:
        """
        Execute the full retrieval pipeline.

        Returns a RetrievalResult with chunks + requested/effective tier +
        downgrade reason (if any). Empty chunks list if the embedder is
        unavailable — chat still works but without RAG context (graceful
        degradation).

        Phase 18 — `retrieval_k` overrides `_SINGLE_CORPUS_LIMIT` for the
        pre-rerank pool (single-corpus path); `rerank_enabled=False` skips the
        cross-encoder call entirely (fixes the previously-dead UI toggle).
        """
        single_limit = (
            retrieval_k
            if retrieval_k is not None
            else _SINGLE_CORPUS_LIMIT
        )
        logger.info(
            "Retrieval start: requested_tier=%s corpus_count=%d k=%d rerank=%s",
            retrieval_tier,
            len(corpus_ids) if corpus_ids else 0,
            single_limit,
            rerank_enabled,
        )

        # [0] Strategy intersection — downgrade tier if a corpus can't support it
        effective_tier, downgrade_reason = await self._enforce_strategy_intersection(
            retrieval_tier, corpus_ids
        )
        if effective_tier != retrieval_tier:
            logger.info(
                "Retrieval tier downgraded: %s → %s", retrieval_tier, effective_tier
            )

        # [1] Embed query — bail gracefully if embedder is down
        try:
            query_vector = await embed_query(query)
        except Exception as exc:
            logger.warning("Embedder unreachable, skipping retrieval: %s", exc)
            return RetrievalResult(
                chunks=[],
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
            )

        # [2] Determine Qdrant collections for each funnel
        a_cols, b_cols = self._resolve_collections(effective_tier, corpus_ids, collections)

        # [3] Parallel Funnel A + B — per-corpus round-robin for multi-corpus
        multi = corpus_ids is not None and len(corpus_ids) > 1

        if multi:
            # Phase 7.5 — scope each per-corpus funnel call to its OWN
            # collection family. Prior behavior passed all b_cols to every
            # call which made sense when collections were shared globals;
            # now it would fan out N×N Qdrant requests with the cross-corpus
            # results filtered out anyway.
            from services.storage.qdrant_writer import _col_for_corpus

            def _b_cols_for(cid: str) -> list[str]:
                cols = [_col_for_corpus(cid, "naive")]
                if effective_tier == RetrievalTier.qdrant_mongo_graph:
                    cols.append(_col_for_corpus(cid, "graph"))
                return cols

            b_tasks = [
                funnel_b.search(query_vector, [cid], _b_cols_for(cid), top_k=_PER_CORPUS_LIMIT)
                for cid in corpus_ids  # type: ignore[union-attr]
            ]
            # Fair mode in funnel_a auto-skips summaries for multi-corpus (see funnel_a.py)
            a_task = funnel_a.search(query_vector, corpus_ids, a_cols)
            a_results, *per_corpus_b = await asyncio.gather(a_task, *b_tasks)
            b_results: list[SourceChunk] = [c for pool in per_corpus_b for c in pool]
        else:
            a_results, b_results = await asyncio.gather(
                funnel_a.search(query_vector, corpus_ids, a_cols),
                funnel_b.search(query_vector, corpus_ids, b_cols, top_k=single_limit),
            )

        def _result(chunks: list[SourceChunk]) -> RetrievalResult:
            return RetrievalResult(
                chunks=chunks,
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
            )

        # [4] Merge + dedupe by parent_id
        merged = merge_pools(a_results, b_results)
        if not merged:
            return _result([])

        # [5] Mode A graph expansion (graph tier + Neo4j live only)
        if effective_tier == RetrievalTier.qdrant_mongo_graph and settings.NEO4J_ENABLED:
            try:
                expanded = await mode_a_expansion.expand(merged, corpus_ids)
                if expanded:
                    merged = merge_pools(merged, expanded)
            except Exception as exc:
                logger.warning("Mode A expansion failed, continuing: %s", exc)

        # [6] Rerank ONCE on full pool (Phase 18 — skippable per-request)
        if not rerank_enabled:
            logger.info("Reranker skipped by override — score-sorting directly")
            ranked = sorted(merged, key=lambda x: x.score, reverse=True)
        else:
            try:
                ranked = await reranker_service.rerank(query, merged)
            except Exception as exc:
                logger.warning("Reranker failed, score-sorting: %s", exc)
                ranked = sorted(merged, key=lambda x: x.score, reverse=True)

        candidates = ranked[: settings.DEFAULT_RETRIEVAL_K]

        # [7] Hydrate from MongoDB (parent text + corpus_name + doc_name)
        # hydrate_chunks also: resolves parent_id for Mode A chunks (Pass 0)
        #                      drops empty-text chunks that couldn't be resolved (Pass 3)
        if effective_tier in (RetrievalTier.qdrant_mongo, RetrievalTier.qdrant_mongo_graph):
            try:
                hydrated = await hydrate_chunks(candidates, corpus_ids)
                return _result(hydrated)
            except Exception as exc:
                logger.warning("Hydration failed, returning unhydrated: %s", exc)

        return _result(candidates)


retriever_orchestrator = RetrieverOrchestrator()
