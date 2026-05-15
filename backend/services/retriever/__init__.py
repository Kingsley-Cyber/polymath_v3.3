"""
Retriever orchestrator — Phase 5 & 6 query pipeline (spec-locked).

Flow (spec §RETRIEVAL RECIPE):
  [0] Strategy intersection — downgrade tier if any corpus lacks the capability
  [1] embed query
  [2] FUNNEL A (summaries, polymath_hrag) [fair-mode skips for multi-corpus]
    + FUNNEL B (children, polymath_naive)  [per-corpus round-robin: 20 each]
    + lexical MongoDB recall for hydrated tiers (bounded by speed profile)
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
from time import perf_counter
from typing import Any

from config import get_settings
from models.schemas import RetrievalResult, RetrievalTier, SourceChunk
from services.embedder import embed_query
from services.reranker import reranker_service
from services.retriever.funnel_a import funnel_a
from services.retriever.funnel_b import funnel_b
from services.retriever.hydrate import hydrate_chunks
from services.retriever.lexical import _terms, lexical_retriever
from services.retriever.graph_rerank import apply_graph_degree_boost
from services.retriever.merge import merge_pools
from services.retriever.mode_a import mode_a_expansion

logger = logging.getLogger(__name__)
settings = get_settings()

_PER_CORPUS_LIMIT = 20   # spec: 20 per corpus for round-robin
_SINGLE_CORPUS_LIMIT = 40  # spec §5.9a: retrieve 40 pre-rerank for single-corpus
_LOW_CONFIDENCE_RERANK_SCORE = -2.5


def _lexical_limit_for(
    effective_tier: RetrievalTier,
    *,
    retrieval_k: int,
    rerank_enabled: bool,
) -> int:
    """Map the SPEED selector to a small lexical recall budget.

    Fast profile is intentionally vector-only. Balanced/Thorough get lexical
    candidates that merge into the same reranker pool as vector/graph results.
    """
    if effective_tier == RetrievalTier.qdrant_only:
        return 0
    if not rerank_enabled and retrieval_k <= 10:
        return 0
    if retrieval_k >= 60:
        return 18
    if retrieval_k >= 40:
        return 12
    return 6


def _has_query_term_overlap(chunks: list[SourceChunk], query: str) -> bool:
    """True when any meaningful original-query term appears in retrieved text.

    This is a conservative guard against nearest-neighbor junk. Vector search
    will always return something, but for short lookup queries a result set
    with no lexical overlap and very poor reranker scores is usually worse
    than returning no RAG context.
    """
    terms = _terms(query)
    if not terms:
        return True

    for chunk in chunks:
        heading = " ".join(chunk.heading_path or [])
        haystack = " ".join(
            [
                chunk.text or "",
                chunk.summary or "",
                heading,
                chunk.doc_name or "",
                chunk.doc_id or "",
            ]
        ).lower()
        if any(term in haystack for term in terms):
            return True
    return False


def _should_drop_low_confidence_rerank(
    ranked: list[SourceChunk],
    ranking_query: str,
    *,
    rerank_enabled: bool,
) -> bool:
    """Drop reranked results when the whole pool looks unrelated.

    The ms-marco cross-encoder returns raw logits. Strongly negative top scores
    are a useful "this is probably irrelevant" signal. We only act on that
    signal when none of the top candidates contains a meaningful term from the
    original user query, which preserves exact-match/file-heading retrieval.
    """
    if not rerank_enabled or not ranked:
        return False
    top_score = ranked[0].score
    if top_score > _LOW_CONFIDENCE_RERANK_SCORE:
        return False
    return not _has_query_term_overlap(ranked[:10], ranking_query)


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

    async def _filter_existing_corpora(
        self, corpus_ids: list[str] | None
    ) -> tuple[list[str] | None, list[str]]:
        """Drop corpus_ids not present in MongoDB. Stale IDs (deleted corpora
        still referenced by frontend settings) would otherwise 404 in Qdrant
        and silently force tier downgrades. Returns (filtered_ids, dropped_ids).
        """
        if not corpus_ids:
            return corpus_ids, []
        try:
            from services.conversation import conversation_service

            db = conversation_service._db
            if db is None:
                return corpus_ids, []
            docs = await db["corpora"].find(
                {"corpus_id": {"$in": corpus_ids}}, {"corpus_id": 1}
            ).to_list(length=None)
            existing = {d["corpus_id"] for d in docs}
            filtered = [c for c in corpus_ids if c in existing]
            dropped = [c for c in corpus_ids if c not in existing]
            if dropped:
                logger.warning(
                    "Dropping %d stale corpus_id(s) not in Mongo: %s",
                    len(dropped), dropped,
                )
            return filtered, dropped
        except Exception as exc:
            logger.warning("Corpus existence check failed (%s) — keeping all ids", exc)
            return corpus_ids, []

    async def _embedding_config_for_query(
        self, corpus_ids: list[str] | None
    ) -> dict[str, Any] | None:
        """Use the selected corpus's embedding provider for single-corpus search."""
        if not corpus_ids or len(corpus_ids) != 1:
            return None
        try:
            from services.conversation import conversation_service

            db = conversation_service._db
            if db is None:
                return None
            doc = await db["corpora"].find_one(
                {"corpus_id": corpus_ids[0]},
                {"default_ingestion_config": 1, "_id": 0},
            )
            return (doc or {}).get("default_ingestion_config")
        except Exception as exc:
            logger.warning("Corpus embedding config lookup failed: %s", exc)
            return None

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
        ranking_query: str | None = None,
        # Phase 23 — Custom profile knobs. None = use server defaults.
        top_k_summary: int | None = None,
        rerank_top_n: int | None = None,
        similarity_threshold: float | None = None,
        neo4j_expansion_cap: int | None = None,
        max_corpora_per_query: int | None = None,
        # Phase 24 — Final K (chunks fed to LLM after rerank). When None,
        # falls back to settings.DEFAULT_RETRIEVAL_K (the legacy hardcoded 5).
        final_top_k: int | None = None,
        # Phase 27 — Search-mode dispatch. "local" (default) runs the full
        # pipeline: Funnel A + B + lexical, merge, graph expand, rerank,
        # hydrate. "global" runs Funnel A only and returns SUMMARY chunks
        # directly to the LLM (no hydration to full text) — used for
        # thematic / corpus-wide queries where overview beats evidence.
        # The caller resolves "auto" to local|global before this point
        # (see services/retriever/search_mode.py:resolve_search_mode).
        search_mode: str = "local",
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
        rank_query = ranking_query or query
        logger.info(
            "Retrieval start: requested_tier=%s corpus_count=%d k=%d rerank=%s thresh=%s",
            retrieval_tier,
            len(corpus_ids) if corpus_ids else 0,
            single_limit,
            rerank_enabled,
            similarity_threshold,
        )
        retrieval_started = perf_counter()
        timings: dict[str, float] = {
            "setup": 0.0,
            "embed": 0.0,
            "funnels": 0.0,
            "merge": 0.0,
            "graph": 0.0,
            "rerank": 0.0,
            "hydrate": 0.0,
        }
        counts: dict[str, int] = {}

        def _add_timing(label: str, started: float) -> None:
            timings[label] = timings.get(label, 0.0) + (perf_counter() - started)

        def _log_timings(status: str, final_count: int) -> None:
            logger.info(
                "Retrieval timings status=%s total=%.2fs setup=%.2fs embed=%.2fs "
                "funnels=%.2fs merge=%.2fs graph=%.2fs rerank=%.2fs hydrate=%.2fs "
                "counts=%s final=%d",
                status,
                perf_counter() - retrieval_started,
                timings.get("setup", 0.0),
                timings.get("embed", 0.0),
                timings.get("funnels", 0.0),
                timings.get("merge", 0.0),
                timings.get("graph", 0.0),
                timings.get("rerank", 0.0),
                timings.get("hydrate", 0.0),
                counts,
                final_count,
            )

        # [0a] Filter stale corpus_ids (frontend may reference deleted corpora)
        phase_started = perf_counter()
        corpus_ids, dropped_ids = await self._filter_existing_corpora(corpus_ids)

        # [0c] Phase 23 — Custom profile `max_corpora_per_query` cap
        if (
            max_corpora_per_query is not None
            and corpus_ids
            and len(corpus_ids) > max_corpora_per_query
        ):
            logger.info(
                "Truncating corpus_ids %d → %d (max_corpora_per_query)",
                len(corpus_ids),
                max_corpora_per_query,
            )
            corpus_ids = corpus_ids[:max_corpora_per_query]

        # [0b] Strategy intersection — downgrade tier if a corpus can't support it
        effective_tier, downgrade_reason = await self._enforce_strategy_intersection(
            retrieval_tier, corpus_ids
        )
        if dropped_ids and not downgrade_reason:
            downgrade_reason = (
                f"Skipped {len(dropped_ids)} deleted corpus id(s): {dropped_ids}"
            )
        if effective_tier != retrieval_tier:
            logger.info(
                "Retrieval tier downgraded: %s → %s", retrieval_tier, effective_tier
            )
        _add_timing("setup", phase_started)

        # [1] Embed query. Hydrated tiers can still fall back to lexical
        # retrieval if the embedder is down; qdrant_only remains pure vector.
        phase_started = perf_counter()
        try:
            query_vector = await embed_query(
                query,
                await self._embedding_config_for_query(corpus_ids),
            )
            _add_timing("embed", phase_started)
        except Exception as exc:
            _add_timing("embed", phase_started)
            logger.warning("Embedder unreachable, skipping retrieval: %s", exc)
            if effective_tier != RetrievalTier.qdrant_only:
                lexical_limit = _lexical_limit_for(
                    effective_tier,
                    retrieval_k=single_limit,
                    rerank_enabled=rerank_enabled,
                )
                lexical = (
                    await lexical_retriever.search(
                        rank_query,
                        corpus_ids,
                        top_k=lexical_limit,
                    )
                    if lexical_limit > 0
                    else []
                )
                if lexical:
                    ranked = (
                        await reranker_service.rerank(rank_query, lexical)
                        if rerank_enabled
                        else sorted(lexical, key=lambda x: x.score, reverse=True)
                    )
                    effective_final_k = (
                        final_top_k
                        if final_top_k is not None
                        else settings.DEFAULT_RETRIEVAL_K
                    )
                    hydrated = await hydrate_chunks(ranked[:effective_final_k], corpus_ids)
                    _log_timings("embed_failed_lexical_fallback", len(hydrated))
                    return RetrievalResult(
                        chunks=hydrated,
                        requested_tier=retrieval_tier,
                        effective_tier=effective_tier,
                        downgrade_reason=downgrade_reason,
                    )
            _log_timings("embed_failed_empty", 0)
            return RetrievalResult(
                chunks=[],
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
            )

        # [2] Determine Qdrant collections for each funnel
        a_cols, b_cols = self._resolve_collections(effective_tier, corpus_ids, collections)

        # ─── Phase 27 — Global mode short-circuit ─────────────────────────
        # Funnel A only. Summary chunks are returned verbatim (no merge
        # with children, no graph expansion, no hydration to parent text)
        # because the WHOLE POINT of global mode is to feed summaries to
        # the LLM as the synthesis substrate. Hydrating would defeat the
        # token-density advantage that makes "50 summaries instead of 5
        # chunks" work.
        if search_mode == "global":
            global_top_k = top_k_summary if top_k_summary is not None else 50
            try:
                a_results_global = await funnel_a.search(
                    query_vector, corpus_ids, a_cols, top_k=global_top_k,
                )
            except Exception as exc:
                logger.warning("global-mode Funnel A failed: %s", exc)
                a_results_global = []
            _add_timing("global_funnel_a", phase_started)
            counts["global_summaries"] = len(a_results_global)
            # Optional rerank — summaries are usually well-ordered by
            # vector similarity, but the cross-encoder catches mismatches
            # between query intent and summary phrasing. Honored when the
            # caller explicitly passes rerank_enabled=True.
            if rerank_enabled and a_results_global:
                try:
                    a_results_global = await reranker_service.rerank(
                        rank_query, a_results_global,
                    )
                except Exception as exc:
                    logger.warning(
                        "global-mode rerank failed, score-sorting: %s", exc,
                    )
                    a_results_global = sorted(
                        a_results_global, key=lambda c: c.score, reverse=True,
                    )
            effective_global_k = (
                final_top_k
                if final_top_k is not None
                else max(20, settings.DEFAULT_RETRIEVAL_K)
            )
            top = a_results_global[:effective_global_k]
            _log_timings("global_done", len(top))
            return RetrievalResult(
                chunks=top,
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
            )

        lexical_limit = _lexical_limit_for(
            effective_tier,
            retrieval_k=single_limit,
            rerank_enabled=rerank_enabled,
        )

        # [3] Parallel Funnel A + B (+ lexical for hybrid/graph tiers)
        multi = corpus_ids is not None and len(corpus_ids) > 1

        phase_started = perf_counter()
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
            a_kwargs = {"top_k": top_k_summary} if top_k_summary is not None else {}
            a_task = funnel_a.search(query_vector, corpus_ids, a_cols, **a_kwargs)
            lexical_task = lexical_retriever.search(
                rank_query, corpus_ids, top_k=lexical_limit
            )
            a_results, lexical_results, *per_corpus_b = await asyncio.gather(
                a_task, lexical_task, *b_tasks
            )
            b_results: list[SourceChunk] = [c for pool in per_corpus_b for c in pool]
        else:
            a_kwargs = {"top_k": top_k_summary} if top_k_summary is not None else {}
            a_results, b_results, lexical_results = await asyncio.gather(
                funnel_a.search(query_vector, corpus_ids, a_cols, **a_kwargs),
                funnel_b.search(query_vector, corpus_ids, b_cols, top_k=single_limit),
                lexical_retriever.search(rank_query, corpus_ids, top_k=lexical_limit),
            )
        _add_timing("funnels", phase_started)
        counts["funnel_a"] = len(a_results)
        counts["funnel_b"] = len(b_results)
        counts["lexical"] = len(lexical_results)

        def _result(chunks: list[SourceChunk]) -> RetrievalResult:
            return RetrievalResult(
                chunks=chunks,
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
            )

        # [4] Merge + dedupe by parent_id. Lexical candidates are deliberately
        # merged before graph expansion, so exact filename/heading hits can seed
        # Neo4j context when Graph Augmented is active.
        phase_started = perf_counter()
        merged = merge_pools(a_results, b_results, lexical_results)
        counts["merged_initial"] = len(merged)
        _add_timing("merge", phase_started)
        if not merged:
            _log_timings("empty_after_merge", 0)
            return _result([])

        # [4a] Phase 23 — Custom profile `similarity_threshold` noise filter.
        # Drops anything below the cosine score floor. Applied before graph
        # expansion so weak seeds don't drag expansion into noise.
        if similarity_threshold is not None and similarity_threshold > 0.0:
            before = len(merged)
            merged = [c for c in merged if c.score >= similarity_threshold]
            logger.info(
                "similarity_threshold=%.2f filter: %d → %d chunks",
                similarity_threshold,
                before,
                len(merged),
            )
            counts["merged_after_threshold"] = len(merged)
            if not merged:
                _log_timings("empty_after_threshold", 0)
                return _result([])

        # [5] Mode A graph expansion (graph tier + Neo4j live only)
        if effective_tier == RetrievalTier.qdrant_mongo_graph and settings.NEO4J_ENABLED:
            phase_started = perf_counter()
            try:
                # Phase 23 — Custom profile `neo4j_expansion_cap`
                expand_kwargs = (
                    {"limit": neo4j_expansion_cap}
                    if neo4j_expansion_cap is not None
                    else {}
                )
                expanded = await mode_a_expansion.expand(
                    merged, corpus_ids, **expand_kwargs
                )
                counts["graph_expanded"] = len(expanded)
                if expanded:
                    merged = merge_pools(merged, expanded)
                    counts["merged_after_graph"] = len(merged)
            except Exception as exc:
                logger.warning("Mode A expansion failed, continuing: %s", exc)
            finally:
                _add_timing("graph", phase_started)

        # [5b] Sprint #1 — graph-degree boost (PageRank-shaped multiplier).
        # Applied BEFORE the rerank_top_n cap so chunks that mention hub
        # entities (Humanoid, TweenService, etc.) can promote into the
        # cap window. Gated by RETRIEVAL_GRAPH_RERANK_ENABLED.
        if (
            getattr(settings, "RETRIEVAL_GRAPH_RERANK_ENABLED", True)
            and effective_tier == RetrievalTier.qdrant_mongo_graph
            and settings.NEO4J_ENABLED
            and merged
        ):
            phase_started = perf_counter()
            try:
                neo4j_driver = getattr(
                    __import__(
                        "services.ingestion_service",
                        fromlist=["ingestion_service"],
                    ).ingestion_service,
                    "neo4j_driver",
                    None,
                )
                merged = await apply_graph_degree_boost(
                    merged, corpus_ids, neo4j_driver
                )
                # Re-sort after multiplier so the rerank_top_n cap reflects
                # the boosted ordering.
                merged.sort(key=lambda c: c.score, reverse=True)
                counts["merged_after_graph_boost"] = len(merged)
            except Exception as exc:
                logger.warning("graph_rerank boost failed, continuing: %s", exc)
            finally:
                _add_timing("graph_boost", phase_started)

        # [5a] Phase 23 — Custom profile `rerank_top_n` pool cap before reranker
        if rerank_top_n is not None and len(merged) > rerank_top_n:
            pre_sorted = sorted(merged, key=lambda x: x.score, reverse=True)
            merged = pre_sorted[:rerank_top_n]
            counts["merged_after_rerank_cap"] = len(merged)
            logger.info(
                "rerank_top_n=%d cap applied (dropped %d candidates)",
                rerank_top_n,
                len(pre_sorted) - rerank_top_n,
            )

        # [6] Rerank ONCE on full pool (Phase 18 — skippable per-request)
        if not rerank_enabled:
            logger.info("Reranker skipped by override — score-sorting directly")
            phase_started = perf_counter()
            ranked = sorted(merged, key=lambda x: x.score, reverse=True)
            _add_timing("rerank", phase_started)
        else:
            phase_started = perf_counter()
            try:
                ranked = await reranker_service.rerank(rank_query, merged)
            except Exception as exc:
                logger.warning("Reranker failed, score-sorting: %s", exc)
                ranked = sorted(merged, key=lambda x: x.score, reverse=True)
            _add_timing("rerank", phase_started)
        counts["ranked"] = len(ranked)

        if _should_drop_low_confidence_rerank(
            ranked,
            rank_query,
            rerank_enabled=rerank_enabled,
        ):
            counts["low_confidence_dropped"] = len(ranked)
            logger.info(
                "Low-confidence rerank guard dropped %d candidates "
                "(top_score=%.3f query='%s')",
                len(ranked),
                ranked[0].score if ranked else 0.0,
                rank_query[:80],
            )
            _log_timings("empty_low_confidence_rerank", 0)
            return _result([])

        # Phase 24 — final_top_k (Custom profile slider) overrides the
        # legacy DEFAULT_RETRIEVAL_K env cap. Never silently swap models or
        # tiers; just let the user crank the chunks-to-LLM count.
        effective_final_k = (
            final_top_k if final_top_k is not None else settings.DEFAULT_RETRIEVAL_K
        )
        candidates = ranked[:effective_final_k]
        counts["candidates"] = len(candidates)
        logger.info(
            "final_top_k=%d (post-rerank cut, %d candidates available)",
            effective_final_k,
            len(ranked),
        )

        # [7] Hydrate from MongoDB (parent text + corpus_name + doc_name)
        # hydrate_chunks also: resolves parent_id for Mode A chunks (Pass 0)
        #                      drops empty-text chunks that couldn't be resolved (Pass 3)
        if effective_tier in (RetrievalTier.qdrant_mongo, RetrievalTier.qdrant_mongo_graph):
            phase_started = perf_counter()
            try:
                hydrated = await hydrate_chunks(candidates, corpus_ids)
                _add_timing("hydrate", phase_started)
                _log_timings("ok_hydrated", len(hydrated))
                return _result(hydrated)
            except Exception as exc:
                _add_timing("hydrate", phase_started)
                logger.warning("Hydration failed, returning unhydrated: %s", exc)

        _log_timings("ok_unhydrated", len(candidates))
        return _result(candidates)


retriever_orchestrator = RetrieverOrchestrator()
