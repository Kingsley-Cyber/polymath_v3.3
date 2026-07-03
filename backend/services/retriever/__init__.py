"""
Retriever orchestrator — Phase 5 & 6 query pipeline (spec-locked).

Flow (spec §RETRIEVAL RECIPE):
  [0] Strategy intersection — downgrade tier if any corpus lacks the capability
  [1] Graph Augmentation only: lightweight entity detection -> Neo4j facts
      -> supporting chunk seeds
  [2] embed query
  [3] FUNNEL A (summaries, polymath_hrag) [fair-mode skips for multi-corpus]
    + FUNNEL B (children, polymath_naive)  [per-corpus round-robin: 20 each]
    + lexical MongoDB recall for hydrated tiers (bounded by speed profile)
    + document-title anchor recall for hydrated tiers
  [4] merge fact seeds + vector + lexical + document anchors & dedupe by parent_id
  [5] Mode A graph expansion (qdrant_mongo_graph tier + NEO4J_ENABLED only)
  [6] rerank ONCE on full pool (Qwen3 reranker sidecar, fallback: score sort)
  [7] trim to DEFAULT_RETRIEVAL_K
  [8] hydrate from MongoDB — parent text + corpus_name + doc_name
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
from models.schemas import RetrievalResult, RetrievalTier, SourceChunk, SourceFact
from services.cache_util import TTLCache, hash_key
from services.embedder import embed_query
from services.reranker import reranker_service
from services.retriever.document_anchor import document_anchor_retriever
from services.retriever.funnel_a import funnel_a
from services.retriever.funnel_b import funnel_b
from services.retriever.graph_rerank import (
    apply_graph_degree_boost,
    apply_graph_degree_boost_metrics_aware,
)
from services.retriever.hydrate import (
    hydrate_chunks,
    hydrate_rerank_texts,
    hydrate_summary_rerank_texts,
)
from services.retriever.intent_policy import (
    FunnelLimits,
    QueryNeed,
    adaptive_funnel_limits,
    infer_retrieval_intent,
)
from services.retriever.lexical import _terms, lexical_retriever
from services.retriever.merge import merge_pools
from services.retriever.mode_a import mode_a_expansion
from services.graph.cache_warmup import ensure_graph_metrics_fresh
from services.retriever.ranking_policy import (
    apply_candidate_weights,
    apply_query_grounding,
    select_with_diversity,
)

logger = logging.getLogger(__name__)
settings = get_settings()

_PER_CORPUS_LIMIT = 20   # spec: 20 per corpus for round-robin
_SINGLE_CORPUS_LIMIT = 40  # spec §5.9a: retrieve 40 pre-rerank for single-corpus
_DEFAULT_SUMMARY_LIMIT = 20
# Single-corpus embedding config is frozen after ingest, but
# _embedding_config_for_query did an uncached Mongo find_one on EVERY retrieve()
# (so every facet/lane support retrieval re-fetched it). Cache by corpus_id with
# a short TTL — bounds staleness if a corpus is ever re-ingested with new config.
_EMBED_CONFIG_CACHE: dict[str, tuple[float, "dict[str, Any] | None"]] = {}
_EMBED_CONFIG_TTL_SECONDS = 300.0
# Retrieval-result cache: deterministic facet/lane support queries recur within
# a turn and across turns, and identical questions repeat. Caching the assembled
# RetrievalResult by (query, corpus, tier, mode, knobs) skips the whole
# funnel+hydrate+rerank on a hit. Short TTL bounds re-ingest staleness; results
# are deep-copied on store/return so downstream mutation can't poison the cache.
_RETRIEVAL_CACHE = TTLCache(maxsize=512, ttl_seconds=120.0)

# Document-anchor wall budget (speed campaign 2026-07-02). The anchor lane is
# an optional recall boost — its per-doc chunk recall ($text plus a bounded
# regex fallback per anchored doc) measured 11.8s on a multi-doc anchor at
# the graph tier. Recall help is never worth stalling the funnels gather;
# past the budget the lane degrades to no candidates.
_DOC_ANCHOR_BUDGET_SECONDS = 2.5


def _unwrap_funnel_result(
    raw: list[SourceChunk] | BaseException,
    label: str,
) -> list[SourceChunk]:
    """Partial-failure handler for the funnel-level asyncio.gather calls.

    When `return_exceptions=True`, gather returns the raised exception as
    a value in place of the task's result. Pre-fix the orchestrator
    treated those exceptions as real result lists and crashed downstream
    (merge_pools would call list ops on an Exception). This helper
    converts any exception into an empty list with a single WARNING log
    so the OTHER funnels' results still flow through.

    Each of the three funnel implementations (funnel_a, funnel_b,
    lexical) ALREADY uses return_exceptions=True internally for its
    per-collection sub-searches. This wrapper extends that safety
    contract to the top-level fan-out: one funnel failing should
    degrade quality, not 500 the whole turn.
    """
    if isinstance(raw, BaseException):
        logger.warning(
            "retriever: %s funnel raised — degrading to empty pool (%s: %s)",
            label,
            type(raw).__name__,
            raw,
        )
        return []
    if raw is None:
        return []
    return list(raw)


def _lexical_limit_for(
    effective_tier: RetrievalTier,
    *,
    retrieval_k: int,
    rerank_enabled: bool,
) -> int:
    """Map the speed/thoroughness selector to a lexical recall budget.

    Fast Search stays vector-only. Hybrid Search and Graph Augmentation always keep a
    small lexical lane, then scale it up as the retrieval pool gets wider.
    """
    if effective_tier == RetrievalTier.qdrant_only:
        return 0
    if retrieval_k >= 60:
        return 18
    if retrieval_k >= 40:
        return 12
    return 6


def _document_anchor_limit_for(effective_tier: RetrievalTier, *, retrieval_k: int) -> int:
    """Small source-title recall budget for hydrated tiers.

    This is metadata/Mongo-backed recall, so Fast Search must not use it.
    """
    if effective_tier == RetrievalTier.qdrant_only:
        return 0
    return 8 if retrieval_k >= 40 else 4


def _retrieval_store_contract(tier: RetrievalTier) -> dict[str, Any]:
    """Human-readable store contract for UI diagnostics."""

    if tier == RetrievalTier.qdrant_only:
        return {
            "label": "Fast Search",
            "qdrant_vectors": True,
            "qdrant_sparse": True,
            "qdrant_rrf": True,
            "qdrant_summaries": True,
            "mongo_lexical": False,
            "mongo_hydration": False,
            "neo4j_facts": False,
            "neo4j_expansion": False,
            "description": (
                "Qdrant dense vector search plus in-collection sparse BM25/RRF "
                "when available, over child chunks and parent summaries. Mongo "
                "lexical search and Neo4j graph expansion are disabled."
            ),
        }
    if tier == RetrievalTier.qdrant_mongo:
        return {
            "label": "Hybrid Search",
            "qdrant_vectors": True,
            "qdrant_sparse": True,
            "qdrant_rrf": True,
            "qdrant_summaries": True,
            "mongo_lexical": True,
            "mongo_hydration": True,
            "neo4j_facts": False,
            "neo4j_expansion": False,
            "description": (
                "Qdrant vector recall plus Mongo lexical/document-anchor recall "
                "and parent hydration. Neo4j is disabled."
            ),
        }
    return {
        "label": "Graph Augmentation",
        "qdrant_vectors": True,
        "qdrant_sparse": True,
        "qdrant_rrf": True,
        "qdrant_summaries": True,
        "mongo_lexical": True,
        "mongo_hydration": True,
        "neo4j_facts": True,
        "neo4j_expansion": True,
        "description": (
            "Highest quality: Hybrid Search plus Neo4j fact seeds, mention/call "
            "walks, bridge expansion, and graph-aware ranking signals."
        ),
    }


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
    score_scale: str | None = None,
    low_confidence_threshold: float | None = None,
) -> bool:
    """Drop reranked results when the whole pool looks unrelated.

    Raw-logit cross-encoders can use strongly negative top scores as a useful
    "probably irrelevant" signal. Bounded score scales such as cosine or
    probability cannot use the same threshold, so this guard is disabled for
    those providers and term overlap + ordinary ranking handles selection.
    """
    if not rerank_enabled or not ranked:
        return False
    scale = (score_scale or settings.RERANKER_SCORE_SCALE or "logit").lower()
    if scale != "logit":
        return False
    threshold = (
        low_confidence_threshold
        if low_confidence_threshold is not None
        else settings.RERANKER_LOW_CONFIDENCE_THRESHOLD
    )
    top_score = ranked[0].score
    if top_score > threshold:
        return False
    return not _has_query_term_overlap(ranked[:10], ranking_query)


def _trim_bounded_rerank_tail(
    ranked: list[SourceChunk],
    *,
    rerank_enabled: bool,
    score_scale: str | None = None,
    tier: RetrievalTier | None = None,
) -> list[SourceChunk]:
    """Drop near-zero bounded rerank tails for simple vector retrieval only.

    With probability/cosine-style rerankers, top hits can be very strong while
    unrelated candidates still occupy the remaining final_top_k slots with
    scores near zero. final_top_k is a cap, not a requirement to feed junk to
    the LLM.

    Hydrated tiers are different: Hybrid Search and Graph Augmentation need the post-
    rerank pool for Mongo/Neo4j evidence coverage. Trimming before grounding can
    erase semantically useful chunks and make the richer tiers narrower than
    Fast Search, so those tiers keep the full pool and let final selection cap
    the prompt.
    """
    if not rerank_enabled or len(ranked) <= 1:
        return ranked
    if tier in (RetrievalTier.qdrant_mongo, RetrievalTier.qdrant_mongo_graph):
        return ranked
    scale = (score_scale or settings.RERANKER_SCORE_SCALE or "logit").lower()
    if scale not in {"probability", "cosine"}:
        return ranked

    # Safety valve for mixed-scale pools. Bypassed code/lexical candidates can
    # preserve pre-rerank scores while prose candidates are bounded 0..1. In
    # that state, using the raw top score to compute a tail floor can delete
    # every useful prose result and make Hybrid Search/Graph Augmentation worse than Fast Search.
    if any(float(chunk.score or 0.0) > 1.0001 for chunk in ranked):
        return ranked

    top_score = float(ranked[0].score or 0.0)
    if top_score < 0.50:
        return ranked

    floor = max(0.05, top_score * 0.20)
    trimmed = [chunk for chunk in ranked if float(chunk.score or 0.0) >= floor]
    return trimmed or ranked[:1]


def _fact_context_text(fact: SourceFact) -> str:
    """Compact fact text used as reranker-visible evidence for fact seeds."""
    subject = (fact.subject or "").strip()
    fact_type = (fact.fact_type or "").strip()
    prop = (fact.property_name or "").strip()
    value = (fact.value or "").strip()
    unit = (fact.unit or "").strip()
    condition = (fact.condition or "").strip()
    evidence = (fact.evidence_phrase or "").strip()

    parts: list[str] = []
    if subject:
        parts.append(subject)
    if prop and value:
        rendered_value = f"{prop} = {value}{(' ' + unit) if unit else ''}"
        parts.append(rendered_value)
    elif value:
        parts.append(f"{fact_type}: {value}{(' ' + unit) if unit else ''}")
    elif fact_type:
        parts.append(fact_type)
    if condition:
        parts.append(f"when {condition}")
    if evidence:
        parts.append(f"Evidence: {evidence}")
    return ". ".join(p for p in parts if p) or subject or evidence


def _fact_seed_chunks(facts: list[SourceFact]) -> list[SourceChunk]:
    """Convert Neo4j facts into chunk candidates for merge/rerank.

    Facts stay available as structured prompt context, but their supporting
    chunk_ids also seed the normal evidence path. Hydration later replaces the
    compact evidence text with the full parent chunk.
    """
    chunks: list[SourceChunk] = []
    seen_chunk_ids: set[str] = set()

    for fact in facts:
        chunk_id = (fact.chunk_id or "").strip()
        if not chunk_id or chunk_id in seen_chunk_ids:
            continue
        seen_chunk_ids.add(chunk_id)
        confidence = max(0.0, min(1.0, float(fact.confidence or 0.0)))
        fact_text = _fact_context_text(fact)
        chunks.append(
            SourceChunk(
                chunk_id=chunk_id,
                parent_id="",
                doc_id=fact.doc_id or "",
                corpus_id=fact.corpus_id or "",
                text=fact_text,
                summary=fact_text,
                score=0.82 + (confidence * 0.18),
                source_tier="graph_fact_seed",
                provenance=[
                    {
                        "retriever": "neo4j_fact",
                        "fact_id": fact.fact_id,
                        "entity": fact.subject,
                        "confidence": confidence,
                        "predicate": fact.property_name or fact.fact_type,
                        "evidence_phrase": fact.evidence_phrase or "",
                    }
                ],
            )
        )

    return chunks


async def _drop_noisy_fact_seed_chunks(chunks: list[SourceChunk]) -> list[SourceChunk]:
    """Drop fact-seed chunks whose Mongo chunk_kind is NOISY_KINDS.

    The graph fact-seed lane (like mode_a) bypasses the funnels' Qdrant-payload
    NOISY_KINDS filter, so an on-topic fact whose EVIDENCE chunk is a
    bibliography/reference block would still inject citation noise into context.
    The facts themselves (query-relevant) stay in <key_facts>; only their noisy
    evidence CHUNKS are removed. Best-effort: any failure returns chunks as-is.
    """
    if not chunks:
        return chunks
    try:
        from services.ingestion_service import ingestion_service
        from services.ingestion.section_classifier import NOISY_KINDS

        db = getattr(ingestion_service, "db", None)
        if db is None:
            return chunks
        ids = [c.chunk_id for c in chunks if c.chunk_id]
        noisy = {
            doc["chunk_id"]
            async for doc in db["chunks"].find(
                {"chunk_id": {"$in": ids}, "chunk_kind": {"$in": list(NOISY_KINDS)}},
                {"_id": 0, "chunk_id": 1},
            )
        }
        if not noisy:
            return chunks
        logger.info(
            "fact-seed NOISY_KINDS filter: dropped %d citation evidence chunk(s)",
            len(noisy),
        )
        return [c for c in chunks if c.chunk_id not in noisy]
    except Exception as exc:  # noqa: BLE001
        logger.warning("fact-seed NOISY_KINDS filter skipped (%s)", exc)
        return chunks


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

    async def _retrieve_graph_seed_facts(
        self,
        query: str,
        corpus_ids: list[str] | None,
        fact_seed_limit: int | None = None,
    ) -> list[SourceFact]:
        """Graph-tier fact lane: query entities -> Neo4j facts.

        This is called only after strategy intersection confirms the effective
        tier is qdrant_mongo_graph. Fast Search and Hybrid Search never enter this
        lane.
        """
        if not settings.NEO4J_ENABLED or not corpus_ids:
            return []

        try:
            from services.graph.graph_query import extract_query_entities
            from services.ingestion_service import ingestion_service
            from services.retriever.fact_retrieval import fact_retrieval

            driver = (
                getattr(ingestion_service, "neo4j_driver", None)
                or getattr(fact_retrieval, "_driver", None)
            )
            if driver is None:
                return []
            qdrant = getattr(ingestion_service, "qdrant_client", None)
            if qdrant is None:
                logger.info(
                    "Graph fact seeding skipped: Qdrant client unavailable; "
                    "avoiding slow literal Neo4j scan"
                )
                return []

            entity_names: list[str] = []
            entity_ids: list[str] = []
            seen_entities: set[str] = set()
            seen_ids: set[str] = set()

            # Query-relevance + junk gates for seed entities. Without these the
            # most-MENTIONED entity wins regardless of the query — on a citation-
            # heavy corpus that is a conference entity (e.g. "association for
            # computational linguistics"), flooding <key_facts> with useless
            # conference-year facts. Reuse the GERG generic-token exclusion so a
            # seed must hit a real query concept (not bare 'model'/'data'/...).
            from services.retriever.query_grounding import (
                concept_groups,
                group_matches_text,
            )
            from services.retriever.graph_decoration import _GENERIC_QUERY_CONCEPTS
            from services.graph.entity_cleaning import is_junk_entity_name

            _groups = concept_groups(query or "")
            _non_generic = [
                g for g in _groups
                if getattr(g, "key", "") not in _GENERIC_QUERY_CONCEPTS
            ]

            for cid in corpus_ids[:3]:
                try:
                    rows = await extract_query_entities(
                        query,
                        cid,
                        driver,
                        limit_per_token=3,
                        qdrant=qdrant,
                        allow_literal_fallback=False,
                    )
                except Exception as exc:
                    logger.warning(
                        "Graph fact seeding: entity detection failed for corpus=%s: %s",
                        cid,
                        exc,
                    )
                    continue
                for row in rows:
                    name = str(row.get("display_name") or "").strip()
                    eid = str(row.get("entity_id") or "").strip()
                    if not name:
                        continue
                    # Junk-name gate (catches "[12]", "page 3", single letters).
                    if is_junk_entity_name(name):
                        continue
                    # Query-relevance gate — drops the off-topic high-mention
                    # entity (the ACL conference). Over-prune guard: a purely
                    # generic query (no anchor concept) keeps frequency order.
                    if _non_generic and not any(
                        group_matches_text(g, name) for g in _non_generic
                    ):
                        continue
                    key = name.lower()
                    if key not in seen_entities:
                        entity_names.append(name)
                        seen_entities.add(key)
                    if eid and eid not in seen_ids:
                        entity_ids.append(eid)
                        seen_ids.add(eid)

            if not entity_names and not entity_ids:
                return []

            graph_fact_limit = max(0, min(int(settings.GRAPH_FACT_SEED_LIMIT), 50))
            limit = max(0, min(int(fact_seed_limit or graph_fact_limit), graph_fact_limit))
            if limit <= 0:
                return []
            entity_limit = max(1, min(int(getattr(settings, "GRAPH_ENTITY_LIMIT", 8)), 50))

            facts = await fact_retrieval.retrieve_facts_for_entities(
                entity_names=entity_names[:entity_limit],
                entity_ids=entity_ids[:entity_limit],
                corpus_ids=corpus_ids,
                fact_types=None,
                limit=limit,
            )
            logger.info(
                "Graph fact seeding: entities=%d ids=%d facts=%d entity_limit=%d fact_limit=%d",
                len(entity_names),
                len(entity_ids),
                len(facts),
                entity_limit,
                limit,
            )
            return facts
        except Exception as exc:
            logger.warning("Graph fact seeding skipped: %s", exc)
            return []

    async def _embedding_config_for_query(
        self, corpus_ids: list[str] | None
    ) -> dict[str, Any] | None:
        """Use the selected corpus's embedding provider for single-corpus search."""
        if not corpus_ids or len(corpus_ids) != 1:
            return None
        cid = corpus_ids[0]
        cached = _EMBED_CONFIG_CACHE.get(cid)
        if cached is not None and (perf_counter() - cached[0]) < _EMBED_CONFIG_TTL_SECONDS:
            return cached[1]
        try:
            from services.conversation import conversation_service

            db = conversation_service._db
            if db is None:
                return None
            doc = await db["corpora"].find_one(
                {"corpus_id": cid},
                {"default_ingestion_config": 1, "_id": 0},
            )
            cfg = (doc or {}).get("default_ingestion_config")
            _EMBED_CONFIG_CACHE[cid] = (perf_counter(), cfg)
            return cfg
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

    async def retrieve(self, *args, **kwargs) -> RetrievalResult:
        """Cache wrapper around the retrieval pipeline.

        Deterministic facet/lane support-query retrievals recur within a turn and
        across turns, and identical questions repeat — so caching the assembled
        result by (query, corpus, tier, mode, knobs) skips the whole
        funnel + hydrate + rerank on a hit. HyDE-expanded main queries vary per
        turn (cache miss, no harm). Results are deep-copied on store and return so
        downstream mutation (e.g. hydration) can't poison the cache. Positional
        calls bypass the cache, since the key is built from the kwargs.
        """

        key = None
        if not args:
            try:
                corpus_ids = kwargs.get("corpus_ids") or []
                key = hash_key(
                    "retrieval_v1",
                    kwargs.get("query"),
                    tuple(sorted(str(c) for c in corpus_ids)),
                    str(kwargs.get("retrieval_tier")),
                    tuple(str(c) for c in (kwargs.get("collections") or ())),
                    kwargs.get("retrieval_k"),
                    bool(kwargs.get("rerank_enabled", True)),
                    kwargs.get("ranking_query"),
                    kwargs.get("top_k_summary"),
                    kwargs.get("rerank_top_n"),
                    kwargs.get("similarity_threshold"),
                    kwargs.get("neo4j_expansion_cap"),
                    kwargs.get("max_corpora_per_query"),
                    kwargs.get("final_top_k"),
                    kwargs.get("fact_seed_limit"),
                    kwargs.get("search_mode"),
                    bool(kwargs.get("support_profile", False)),
                )
            except Exception:
                key = None
        if key is not None:
            hit = _RETRIEVAL_CACHE.get(key)
            if hit is not None:
                try:
                    return hit.model_copy(deep=True)
                except Exception:
                    return hit
        result = await self._retrieve_uncached(*args, **kwargs)
        if key is not None and getattr(result, "chunks", None):
            try:
                _RETRIEVAL_CACHE.set(key, result.model_copy(deep=True))
            except Exception:
                pass
        return result

    async def _retrieve_uncached(
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
        fact_seed_limit: int | None = None,
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
        # Speed campaign (2026-07-02) — lightweight profile for gap-fill
        # SUPPORT retrievals (coverage facets / evidence-plan lanes). They
        # select by facet-fit / lane-match scoring over chunk TEXT, so the
        # summary funnel (A) and the document-title anchor lane add wall
        # time (anchor measured 1.7-4.2s per support) without changing the
        # pick. Children (Funnel B) + lexical only.
        support_profile: bool = False,
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
        retrieval_intent = infer_retrieval_intent(rank_query)
        summary_base = (
            top_k_summary
            if top_k_summary is not None
            else _DEFAULT_SUMMARY_LIMIT
        )
        funnel_limits = adaptive_funnel_limits(
            retrieval_intent,
            child_base=single_limit,
            summary_base=summary_base,
        )
        logger.info(
            "Retrieval start: requested_tier=%s corpus_count=%d k=%d "
            "summary_k=%d intent=%s ratios=%.2f/%.2f rerank=%s thresh=%s",
            retrieval_tier,
            len(corpus_ids) if corpus_ids else 0,
            funnel_limits.child_top_k,
            funnel_limits.summary_top_k,
            retrieval_intent.need.value,
            retrieval_intent.child_ratio,
            retrieval_intent.summary_ratio,
            rerank_enabled,
            similarity_threshold,
        )
        retrieval_started = perf_counter()
        timings: dict[str, float] = {
            "setup": 0.0,
            "fact_seed": 0.0,
            "embed": 0.0,
            "funnels": 0.0,
            "merge": 0.0,
            "graph": 0.0,
            "rerank": 0.0,
            "hydrate": 0.0,
        }
        counts: dict[str, int] = {}
        funnel_durations: dict[str, float] = {}

        def _add_timing(label: str, started: float) -> None:
            timings[label] = timings.get(label, 0.0) + (perf_counter() - started)

        async def _timed_funnel(name: str, awaitable):
            """Record per-funnel wall time inside the funnels gather."""
            _started = perf_counter()
            try:
                return await awaitable
            finally:
                funnel_durations[name] = perf_counter() - _started

        def _log_timings(status: str, final_count: int) -> None:
            logger.info(
                "Retrieval timings status=%s total=%.2fs setup=%.2fs fact_seed=%.2fs embed=%.2fs "
                "funnels=%.2fs funnel_detail=%s merge=%.2fs graph=%.2fs rerank=%.2fs hydrate=%.2fs "
                "counts=%s final=%d",
                status,
                perf_counter() - retrieval_started,
                timings.get("setup", 0.0),
                timings.get("fact_seed", 0.0),
                timings.get("embed", 0.0),
                timings.get("funnels", 0.0),
                # Per-funnel wall inside the gather — the aggregate stage time
                # hides WHICH store is slow under concurrency (speed campaign
                # 2026-07-02: support funnels 5-7s vs 2-3.5s solo, cause unknown
                # until this breakdown existed).
                ",".join(
                    f"{name}:{dur:.2f}s" for name, dur in sorted(funnel_durations.items())
                ) or "none",
                timings.get("merge", 0.0),
                timings.get("graph", 0.0),
                timings.get("rerank", 0.0),
                timings.get("hydrate", 0.0),
                counts,
                final_count,
            )

        def _source_tier_counts(chunks: list[SourceChunk] | None) -> dict[str, int]:
            tier_counts: dict[str, int] = {}
            for chunk in chunks or []:
                tier = str(getattr(chunk, "source_tier", None) or "unknown")
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
            return tier_counts

        def _doc_concentration(
            chunks: list[SourceChunk] | None,
        ) -> tuple[int, float]:
            """Final-set document spread: (distinct docs, max single-doc share).

            ``max_doc_share_final`` is the fraction of the final context drawn
            from the single most-represented document — the candidate-collapse
            signal (e.g. 6 of 9 chunks from one book -> 0.6667). Pure read over
            the already-selected final chunks; adds no behavior, only telemetry.
            """
            doc_counts: dict[str, int] = {}
            for chunk in chunks or []:
                doc_id = getattr(chunk, "doc_id", None)
                if not doc_id:
                    continue
                key = str(doc_id)
                doc_counts[key] = doc_counts.get(key, 0) + 1
            total = sum(doc_counts.values())
            if total <= 0:
                return 0, 0.0
            return len(doc_counts), round(max(doc_counts.values()) / total, 4)

        selection_diagnostics: dict[str, Any] = {}
        reranker_diagnostics: dict[str, Any] = {}

        def _diagnostics(
            status: str,
            final_count: int,
            final_chunks: list[SourceChunk] | None = None,
        ) -> dict[str, Any]:
            effective = effective_tier
            unique_docs_final, max_doc_share_final = _doc_concentration(final_chunks)
            return {
                "status": status,
                "requested_tier": getattr(retrieval_tier, "value", retrieval_tier),
                "effective_tier": getattr(effective, "value", effective),
                "store_contract": _retrieval_store_contract(effective),
                "search_mode": search_mode,
                "intent": {
                    "need": retrieval_intent.need.value,
                    "broad_score": retrieval_intent.broad_score,
                    "specific_score": retrieval_intent.specific_score,
                    "child_ratio": retrieval_intent.child_ratio,
                    "summary_ratio": retrieval_intent.summary_ratio,
                },
                "limits": {
                    "child_top_k": funnel_limits.child_top_k,
                    "summary_top_k": funnel_limits.summary_top_k,
                    "requested_retrieval_k": single_limit,
                    "final_top_k": final_top_k
                    if final_top_k is not None
                    else settings.DEFAULT_RETRIEVAL_K,
                    "rerank_enabled": rerank_enabled,
                },
                "counts": {key: int(value) for key, value in counts.items()},
                "timings_s": {
                    key: round(float(value), 3) for key, value in timings.items()
                },
                "total_s": round(float(perf_counter() - retrieval_started), 3),
                "final_count": int(final_count),
                "final_source_tiers": _source_tier_counts(final_chunks),
                "unique_docs_final": unique_docs_final,
                "max_doc_share_final": max_doc_share_final,
                "selection": selection_diagnostics,
                "reranker": reranker_diagnostics,
            }

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
        if effective_tier == RetrievalTier.qdrant_mongo_graph:
            child_cap = int(getattr(settings, "GRAPH_CHILD_TOP_K", 40))
            summary_cap = int(getattr(settings, "GRAPH_SUMMARY_TOP_K", 20))
            capped_child_base = min(
                int(single_limit),
                child_cap,
            )
            capped_summary_base = min(
                int(summary_base),
                summary_cap,
            )
            next_funnel_limits = adaptive_funnel_limits(
                retrieval_intent,
                child_base=capped_child_base,
                summary_base=capped_summary_base,
            )
            next_funnel_limits = FunnelLimits(
                child_top_k=min(next_funnel_limits.child_top_k, child_cap),
                summary_top_k=min(next_funnel_limits.summary_top_k, summary_cap),
            )
            if (
                capped_child_base != single_limit
                or capped_summary_base != summary_base
                or next_funnel_limits != funnel_limits
            ):
                single_limit = capped_child_base
                summary_base = capped_summary_base
                funnel_limits = next_funnel_limits
                counts["graph_child_top_k_cap"] = funnel_limits.child_top_k
                counts["graph_summary_top_k_cap"] = funnel_limits.summary_top_k
                logger.info(
                    "Graph budget caps applied: child_top_k=%d summary_top_k=%d",
                    funnel_limits.child_top_k,
                    funnel_limits.summary_top_k,
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

        seed_facts: list[SourceFact] = []
        fact_seed_chunks: list[SourceChunk] = []
        # Kick off Graph fact seeding CONCURRENTLY (it only needs the query +
        # corpus_ids, not the embedding) so its ~0.5s Neo4j round-trip overlaps
        # the embed + funnel work instead of running before it. The result is
        # awaited lazily via _resolve_fact_seed() right before its first use on
        # each return path. _add_timing records only the blocking await time, so
        # the timings reflect the overlap.
        _fact_seed_resolved = False
        _fact_seed_task: asyncio.Future | None = None
        if effective_tier == RetrievalTier.qdrant_mongo_graph and settings.NEO4J_ENABLED:
            # Self-heal the graph-analytics cache (bridges / analogies / transfer
            # candidates). Non-blocking and signature-driven: any staleness — from
            # ingest, delete, backfill, dedup, or a lost post-ingest warm — repairs
            # itself for the next graph query. The query never waits on it.
            for _cid in (corpus_ids or []):
                asyncio.ensure_future(ensure_graph_metrics_fresh(_cid))
            _fact_seed_task = asyncio.ensure_future(
                asyncio.wait_for(
                    self._retrieve_graph_seed_facts(
                        rank_query,
                        corpus_ids,
                        fact_seed_limit=fact_seed_limit,
                    ),
                    timeout=float(settings.GRAPH_FACT_SEED_TIMEOUT_SECONDS),
                )
            )

        async def _resolve_fact_seed() -> None:
            """Await the concurrent fact-seed task once and populate state.

            Idempotent: safe to call on every return path; only the first call
            does work. A timeout/failure degrades to no facts, matching the
            previous behavior.
            """
            nonlocal seed_facts, fact_seed_chunks, _fact_seed_resolved
            if _fact_seed_resolved or _fact_seed_task is None:
                return
            _fact_seed_resolved = True
            await_started = perf_counter()
            try:
                seed_facts = await _fact_seed_task
            except asyncio.TimeoutError:
                logger.warning(
                    "Graph fact seeding timed out after %.1fs; continuing without facts",
                    float(settings.GRAPH_FACT_SEED_TIMEOUT_SECONDS),
                )
                seed_facts = []
            except Exception as exc:  # never let seeding break retrieval
                logger.warning("Graph fact seeding failed: %s", exc)
                seed_facts = []
            fact_seed_chunks = await _drop_noisy_fact_seed_chunks(
                _fact_seed_chunks(seed_facts)
            )
            counts["facts"] = len(seed_facts)
            counts["fact_seed_chunks"] = len(fact_seed_chunks)
            _add_timing("fact_seed", await_started)

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
            # Fallback path consumes fact_seed_chunks/seed_facts below.
            await _resolve_fact_seed()
            if effective_tier != RetrievalTier.qdrant_only:
                lexical_limit = _lexical_limit_for(
                    effective_tier,
                    retrieval_k=single_limit,
                    rerank_enabled=rerank_enabled,
                )
                document_anchor_limit = _document_anchor_limit_for(
                    effective_tier,
                    retrieval_k=single_limit,
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
                document_anchors = (
                    await document_anchor_retriever.search(
                        rank_query,
                        corpus_ids,
                        top_k=document_anchor_limit,
                    )
                    if document_anchor_limit > 0
                    else []
                )
                fallback_fact_seeds = apply_candidate_weights(
                    fact_seed_chunks,
                    intent=retrieval_intent,
                    tier=effective_tier,
                )
                fallback_lexical = apply_candidate_weights(
                    lexical,
                    intent=retrieval_intent,
                    tier=effective_tier,
                )
                fallback_doc_anchors = apply_candidate_weights(
                    document_anchors,
                    intent=retrieval_intent,
                    tier=effective_tier,
                )
                fallback_pool = merge_pools(
                    fallback_fact_seeds,
                    fallback_lexical,
                    fallback_doc_anchors,
                )
                if fallback_pool:
                    if rerank_enabled:
                        try:
                            ranked = await reranker_service.rerank(
                                rank_query, fallback_pool
                            )
                        except Exception as rerank_exc:
                            logger.warning(
                                "Fallback reranker failed, score-sorting: %s",
                                rerank_exc,
                            )
                            ranked = sorted(
                                fallback_pool, key=lambda x: x.score, reverse=True
                            )
                    else:
                        ranked = sorted(fallback_pool, key=lambda x: x.score, reverse=True)
                    effective_final_k = (
                        final_top_k
                        if final_top_k is not None
                        else settings.DEFAULT_RETRIEVAL_K
                    )
                    ranked = apply_query_grounding(
                        ranked,
                        query=rank_query,
                        tier=effective_tier,
                        score_scale=settings.RERANKER_SCORE_SCALE,
                    )
                    counts["ranked"] = len(ranked)
                    counts["ranked_query_grounded"] = len(ranked)
                    counts["candidates"] = min(len(ranked), effective_final_k)
                    hydrated = await hydrate_chunks(
                        ranked[:effective_final_k], corpus_ids, query=rank_query
                    )
                    _log_timings("embed_failed_fallback", len(hydrated))
                    return RetrievalResult(
                        chunks=hydrated,
                        facts=seed_facts,
                        requested_tier=retrieval_tier,
                        effective_tier=effective_tier,
                        downgrade_reason=downgrade_reason,
                        diagnostics=_diagnostics(
                            "embed_failed_fallback",
                            len(hydrated),
                            hydrated,
                        ),
                    )
            _log_timings("embed_failed_empty", 0)
            return RetrievalResult(
                chunks=[],
                facts=seed_facts,
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
                diagnostics=_diagnostics("embed_failed_empty", 0),
            )

        # [2] Determine Qdrant collections for each funnel
        a_cols, b_cols = self._resolve_collections(effective_tier, corpus_ids, collections)

        # ─── Phase 27 — Global mode short-circuit ─────────────────────────
        # Funnel A only. Summary chunks are hydrated to canonical Mongo
        # summaries, then returned without merging children, graph expansion,
        # or hydration to full parent text. Parent hydration would defeat the
        # token-density advantage that makes "50 summaries instead of 5
        # chunks" work.
        if search_mode == "global":
            global_top_k = top_k_summary if top_k_summary is not None else 50
            # Global return carries facts=seed_facts; resolve before returning.
            await _resolve_fact_seed()
            phase_started = perf_counter()
            try:
                a_results_global = await funnel_a.search(
                    query_vector,
                    corpus_ids,
                    a_cols,
                    top_k=global_top_k,
                    query_text=rank_query,
                )
            except Exception as exc:
                logger.warning("global-mode Funnel A failed: %s", exc)
                a_results_global = []
            _add_timing("funnels", phase_started)
            counts["global_summaries"] = len(a_results_global)
            # Optional rerank — summaries are usually well-ordered by
            # vector similarity, but the cross-encoder catches mismatches
            # between query intent and summary phrasing. Honored when the
            # caller explicitly passes rerank_enabled=True.
            phase_started = perf_counter()
            a_results_global = await hydrate_summary_rerank_texts(
                a_results_global,
                corpus_ids,
            )
            _add_timing("rerank_text_hydrate", phase_started)
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
                trimmed_global = _trim_bounded_rerank_tail(
                    a_results_global,
                    rerank_enabled=True,
                    score_scale=settings.RERANKER_SCORE_SCALE,
                    tier=effective_tier,
                )
                if len(trimmed_global) != len(a_results_global):
                    logger.info(
                        "Global bounded rerank tail trim: %d → %d candidates "
                        "(top_score=%.3f scale=%s)",
                        len(a_results_global),
                        len(trimmed_global),
                        a_results_global[0].score if a_results_global else 0.0,
                        settings.RERANKER_SCORE_SCALE,
                    )
                    counts["global_rerank_tail_trimmed"] = (
                        len(a_results_global) - len(trimmed_global)
                    )
                    a_results_global = trimmed_global
            effective_global_k = (
                final_top_k
                if final_top_k is not None
                else max(20, settings.DEFAULT_RETRIEVAL_K)
            )
            a_results_global = apply_query_grounding(
                a_results_global,
                query=rank_query,
                tier=effective_tier,
                score_scale=settings.RERANKER_SCORE_SCALE,
            )
            top = a_results_global[:effective_global_k]
            counts["ranked_query_grounded"] = len(a_results_global)
            counts["candidates"] = len(top)
            _log_timings("global_done", len(top))
            return RetrievalResult(
                chunks=top,
                facts=seed_facts,
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
                diagnostics=_diagnostics("global_done", len(top), top),
            )

        lexical_limit = _lexical_limit_for(
            effective_tier,
            retrieval_k=single_limit,
            rerank_enabled=rerank_enabled,
        )
        document_anchor_limit = _document_anchor_limit_for(
            effective_tier,
            retrieval_k=single_limit,
        )
        if support_profile:
            # Gap-fill supports: children + lexical only (see param doc).
            document_anchor_limit = 0
            funnel_limits = FunnelLimits(
                child_top_k=funnel_limits.child_top_k,
                summary_top_k=0,
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
                funnel_b.search(
                    query_vector,
                    [cid],
                    _b_cols_for(cid),
                    top_k=_PER_CORPUS_LIMIT,
                    query_text=rank_query,
                )
                for cid in corpus_ids  # type: ignore[union-attr]
            ]
            # Fair mode normally skips summaries for multi-corpus. Broad
            # synthesis queries intentionally keep summaries alive, capped by
            # the adaptive budget, so overview evidence can compete.
            a_kwargs = {
                "top_k": funnel_limits.summary_top_k,
                "fair_mode": retrieval_intent.need != QueryNeed.BROAD,
                "query_text": rank_query,
            }
            a_task = (
                funnel_a.search(query_vector, corpus_ids, a_cols, **a_kwargs)
                if funnel_limits.summary_top_k > 0
                else asyncio.sleep(0, result=[])
            )
            lexical_task = (
                lexical_retriever.search(
                    rank_query,
                    corpus_ids,
                    top_k=lexical_limit,
                )
                if lexical_limit > 0
                else asyncio.sleep(0, result=[])
            )
            document_anchor_task = (
                asyncio.wait_for(
                    document_anchor_retriever.search(
                        rank_query,
                        corpus_ids,
                        top_k=document_anchor_limit,
                    ),
                    timeout=_DOC_ANCHOR_BUDGET_SECONDS,
                )
                if document_anchor_limit > 0
                else asyncio.sleep(0, result=[])
            )
            # Partial-failure safety: if any single funnel raises (Qdrant
            # timeout, Mongo blip, network drop), we want the OTHER funnels'
            # results to still flow through. `return_exceptions=True`
            # surfaces the exception as a value in the gather result, which
            # `_unwrap` then converts to an empty list with a warning log.
            # Pre-fix: any funnel raise = entire chat/graph query 500.
            raw_a, raw_lex, raw_doc_anchor, *raw_b = await asyncio.gather(
                _timed_funnel("a", a_task),
                _timed_funnel("lex", lexical_task),
                _timed_funnel("anchor", document_anchor_task),
                *[_timed_funnel(f"b{i}", t) for i, t in enumerate(b_tasks)],
                return_exceptions=True,
            )
            a_results = _unwrap_funnel_result(raw_a, "funnel_a")
            lexical_results = _unwrap_funnel_result(raw_lex, "lexical")
            document_anchor_results = _unwrap_funnel_result(
                raw_doc_anchor,
                "document_anchor",
            )
            per_corpus_b = [
                _unwrap_funnel_result(r, f"funnel_b[{i}]")
                for i, r in enumerate(raw_b)
            ]
            b_results: list[SourceChunk] = [c for pool in per_corpus_b for c in pool]
        else:
            a_kwargs = {
                "top_k": funnel_limits.summary_top_k,
                "query_text": rank_query,
            }
            # Same partial-failure safety as the multi-corpus branch above.
            raw_a, raw_b, raw_lex, raw_doc_anchor = await asyncio.gather(
                _timed_funnel(
                    "a",
                    (
                        funnel_a.search(query_vector, corpus_ids, a_cols, **a_kwargs)
                        if funnel_limits.summary_top_k > 0
                        else asyncio.sleep(0, result=[])
                    ),
                ),
                _timed_funnel(
                    "b",
                    funnel_b.search(
                        query_vector,
                        corpus_ids,
                        b_cols,
                        top_k=funnel_limits.child_top_k,
                        query_text=rank_query,
                    ),
                ),
                _timed_funnel(
                    "lex",
                    (
                        lexical_retriever.search(
                            rank_query,
                            corpus_ids,
                            top_k=lexical_limit,
                        )
                        if lexical_limit > 0
                        else asyncio.sleep(0, result=[])
                    ),
                ),
                _timed_funnel(
                    "anchor",
                    (
                        asyncio.wait_for(
                            document_anchor_retriever.search(
                                rank_query,
                                corpus_ids,
                                top_k=document_anchor_limit,
                            ),
                            timeout=_DOC_ANCHOR_BUDGET_SECONDS,
                        )
                        if document_anchor_limit > 0
                        else asyncio.sleep(0, result=[])
                    ),
                ),
                return_exceptions=True,
            )
            a_results = _unwrap_funnel_result(raw_a, "funnel_a")
            b_results = _unwrap_funnel_result(raw_b, "funnel_b")
            lexical_results = _unwrap_funnel_result(raw_lex, "lexical")
            document_anchor_results = _unwrap_funnel_result(
                raw_doc_anchor,
                "document_anchor",
            )
        _add_timing("funnels", phase_started)
        # Normal path: fact seed overlapped embed + funnels; collect it now.
        await _resolve_fact_seed()
        counts["funnel_a"] = len(a_results)
        counts["funnel_b"] = len(b_results)
        counts["lexical"] = len(lexical_results)
        counts["document_anchor"] = len(document_anchor_results)
        if fact_seed_chunks:
            fact_seed_chunks = apply_candidate_weights(
                fact_seed_chunks,
                intent=retrieval_intent,
                tier=effective_tier,
            )
        a_results = apply_candidate_weights(
            a_results,
            intent=retrieval_intent,
            tier=effective_tier,
        )
        b_results = apply_candidate_weights(
            b_results,
            intent=retrieval_intent,
            tier=effective_tier,
        )
        lexical_results = apply_candidate_weights(
            lexical_results,
            intent=retrieval_intent,
            tier=effective_tier,
        )
        document_anchor_results = apply_candidate_weights(
            document_anchor_results,
            intent=retrieval_intent,
            tier=effective_tier,
        )

        def _result(chunks: list[SourceChunk], *, status: str = "result") -> RetrievalResult:
            return RetrievalResult(
                chunks=chunks,
                facts=seed_facts,
                requested_tier=retrieval_tier,
                effective_tier=effective_tier,
                downgrade_reason=downgrade_reason,
                diagnostics=_diagnostics(status, len(chunks), chunks),
            )

        # Retrieval Layer v4 Phase 1 (scoring wall): record each lane's OWN
        # rank order before merging. Pool selection (which candidates the
        # cross-encoder sees) is rank-fused across lanes — raw lane scores are
        # NOT comparable (dense cosine vs sparse BM25 vs anchor heuristics)
        # and score-sorting the merged pool let scale artifacts crowd out
        # genuine evidence (task #12).
        _LANE_RRF_WEIGHTS = {
            "b": 1.0,       # dense/hybrid children — the semantic core
            "anchor": 0.9,  # title-anchored recall
            "lex": 0.8,     # sparse/lexical recall
            "graph": 0.9,   # Mode A expansion (added later)
            "a": 0.7,       # summaries
            "fact": 0.9,    # fact-seed evidence
        }
        _lane_ranks: dict[str, dict[str, int]] = {}

        def _record_lane_ranks(lane: str, chunks_in_lane: list[SourceChunk]) -> None:
            table = _lane_ranks.setdefault(lane, {})
            for _rank, _c in enumerate(chunks_in_lane):
                _key = str(_c.chunk_id or _c.parent_id or "")
                if _key and _key not in table:
                    table[_key] = _rank

        def _rrf_fused(chunk: SourceChunk) -> float:
            _key = str(chunk.chunk_id or chunk.parent_id or "")
            _pkey = str(chunk.parent_id or "")
            fused = 0.0
            for _lane, _table in _lane_ranks.items():
                _rank = _table.get(_key)
                if _rank is None and _pkey:
                    _rank = _table.get(_pkey)
                if _rank is not None:
                    fused += _LANE_RRF_WEIGHTS.get(_lane, 0.8) / (60.0 + _rank)
            return fused

        def _rank_fused_order(chunks_to_sort: list[SourceChunk]) -> list[SourceChunk]:
            """Deterministic rank-only ordering: RRF over lane ranks, with the
            global total-order tie-break. Never reads chunk.score."""
            return sorted(
                chunks_to_sort,
                key=lambda c: (
                    -_rrf_fused(c),
                    c.doc_id or "",
                    c.chunk_id or "",
                ),
            )

        _record_lane_ranks("fact", fact_seed_chunks)
        _record_lane_ranks("a", a_results)
        _record_lane_ranks("b", b_results)
        _record_lane_ranks("lex", lexical_results)
        _record_lane_ranks("anchor", document_anchor_results)

        # [4] Merge + dedupe by parent_id. Lexical and fact-seeded candidates
        # are deliberately merged before graph expansion, so exact
        # filename/heading hits and query-entity facts can seed Neo4j context
        # when Graph Augmentation is active.
        phase_started = perf_counter()
        merged = merge_pools(
            fact_seed_chunks,
            a_results,
            b_results,
            lexical_results,
            document_anchor_results,
            dedupe_by_parent=effective_tier != RetrievalTier.qdrant_only,
        )
        counts["merged_initial"] = len(merged)
        counts["distinct_docs_merged"] = len(
            {c.doc_id for c in merged if getattr(c, "doc_id", None)}
        )
        _add_timing("merge", phase_started)
        if not merged:
            _log_timings("empty_after_merge", 0)
            return _result([], status="empty_after_merge")

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
                return _result([], status="empty_after_threshold")

        # [5] Mode A graph expansion (graph tier + Neo4j live only)
        if effective_tier == RetrievalTier.qdrant_mongo_graph and settings.NEO4J_ENABLED:
            phase_started = perf_counter()
            try:
                # Phase 23 — Custom profile `neo4j_expansion_cap`
                graph_expansion_limit = max(
                    0,
                    min(int(getattr(settings, "GRAPH_EXPANSION_LIMIT", 8)), 100),
                )
                requested_expansion = (
                    int(neo4j_expansion_cap)
                    if neo4j_expansion_cap is not None
                    else graph_expansion_limit
                )
                effective_expansion_cap = min(requested_expansion, graph_expansion_limit)
                expand_kwargs = (
                    {
                        "limit": effective_expansion_cap,
                        "seed_limit": getattr(settings, "GRAPH_SEED_CHUNKS", 8),
                        # P2 A1 — the RAW user query drives entity linking
                        # (ranking_query/HyDE prose would slug to junk grams)
                        "query": query,
                    }
                    if effective_expansion_cap > 0
                    else {"limit": 0, "seed_limit": getattr(settings, "GRAPH_SEED_CHUNKS", 8)}
                )
                # Phase 5b — pass db through so Mode A can run its
                # cache-driven bridge bonus expansion when the flag is
                # on. db=None falls back to mention + calls passes only.
                db_for_mode_a = getattr(
                    __import__(
                        "services.ingestion_service",
                        fromlist=["ingestion_service"],
                    ).ingestion_service,
                    "db",
                    None,
                )
                graph_expansion_timeout = float(
                    getattr(settings, "GRAPH_EXPANSION_TIMEOUT_SECONDS", 4.0)
                    or 4.0
                )
                counts["graph_expansion_timeout_seconds"] = round(
                    graph_expansion_timeout, 2
                )
                expanded = await asyncio.wait_for(
                    mode_a_expansion.expand(
                        merged, corpus_ids, db=db_for_mode_a, **expand_kwargs
                    ),
                    timeout=graph_expansion_timeout,
                )
                counts["graph_seed_chunks"] = min(
                    len(merged),
                    int(getattr(settings, "GRAPH_SEED_CHUNKS", 8)),
                )
                counts["graph_expansion_cap"] = effective_expansion_cap
                counts["graph_expanded"] = len(expanded)
                if expanded:
                    _record_lane_ranks("graph", expanded)
                    merged = merge_pools(merged, expanded)
                    counts["merged_after_graph"] = len(merged)
            except asyncio.TimeoutError:
                counts["graph_expansion_timed_out"] = 1
                logger.warning(
                    "Mode A expansion timed out after %.2fs, continuing with hybrid seeds",
                    float(
                        getattr(settings, "GRAPH_EXPANSION_TIMEOUT_SECONDS", 4.0)
                        or 4.0
                    ),
                )
            except Exception as exc:
                counts["graph_expansion_failed"] = 1
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
                ingestion_svc = __import__(
                    "services.ingestion_service",
                    fromlist=["ingestion_service"],
                ).ingestion_service
                neo4j_driver = getattr(ingestion_svc, "neo4j_driver", None)
                # Phase 5a — flag-gated metrics-aware variant. Default
                # OFF; both code paths sit inside the SAME tier gate so
                # non-graph queries are equally unaffected by either.
                # Cold-cache fallback is built into the metrics-aware
                # function itself; when the flag is on but cache is
                # empty, degree-only behavior is the natural result.
                if getattr(settings, "RETRIEVAL_CACHE_GRAPH_METRICS", False):
                    db_handle = getattr(ingestion_svc, "db", None)
                    merged = await apply_graph_degree_boost_metrics_aware(
                        merged, corpus_ids, neo4j_driver, db_handle
                    )
                else:
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

        # [5a] Phase 23 — Custom profile `rerank_top_n` pool cap before reranker.
        # Graph Augmentation expands the Hybrid Search pool with Neo4j neighbors. A narrow
        # pre-rerank cap can let graph expansion crowd out the semantic core
        # before the cross-encoder sees hydrated text, making the "full RAG"
        # tier worse than Hybrid/Vector. Keep a wider graph floor, then let
        # final_top_k cap what reaches the model.
        #
        # The floor is moderate (48 for the default final_top_k=8) rather than
        # the old 64: still ~2x Hybrid's pre-rerank pool so the semantic core is
        # preserved, but ~25% fewer cross-encoder forward passes (~0.8s on the
        # linear Metal reranker). Graph-neighbor survival no longer depends on a
        # huge window — select_with_diversity now uses graph-aware MMR and a
        # small graph-provenance reservation, so narrowing here is safe.
        if effective_tier == RetrievalTier.qdrant_mongo_graph and merged:
            graph_prefilter_pool = max(
                1,
                min(int(getattr(settings, "GRAPH_PREFILTER_POOL", 64)), 300),
            )
            if len(merged) > graph_prefilter_pool:
                merged = _rank_fused_order(merged)[:graph_prefilter_pool]
                counts["graph_prefilter_pool"] = len(merged)

        effective_rerank_top_n = rerank_top_n
        if (
            effective_tier == RetrievalTier.qdrant_mongo_graph
            and merged
        ):
            graph_mlx_pool = max(
                1,
                min(int(getattr(settings, "GRAPH_MLX_RERANK_POOL", 28)), 200),
            )
            effective_rerank_top_n = min(
                len(merged),
                int(rerank_top_n) if rerank_top_n is not None else graph_mlx_pool,
                graph_mlx_pool,
            )
            if effective_rerank_top_n != rerank_top_n:
                counts["rerank_top_n_graph_cap"] = effective_rerank_top_n
        if effective_rerank_top_n is not None and len(merged) > effective_rerank_top_n:
            # v4 P1: pool selection is RANK-FUSED (RRF over per-lane ranks),
            # never score-sorted — lane scores live on incomparable scales.
            pre_sorted = _rank_fused_order(merged)
            merged = pre_sorted[:effective_rerank_top_n]
            counts["merged_after_rerank_cap"] = len(merged)
            counts["pool_rank_fused"] = 1
            logger.info(
                "rerank_top_n=%d cap applied via rank fusion (dropped %d candidates)",
                effective_rerank_top_n,
                len(pre_sorted) - effective_rerank_top_n,
            )

        if effective_tier in (RetrievalTier.qdrant_mongo, RetrievalTier.qdrant_mongo_graph):
            phase_started = perf_counter()
            merged = await hydrate_rerank_texts(merged, corpus_ids)
            _add_timing("rerank_text_hydrate", phase_started)

        # [6] Rerank ONCE on full pool (Phase 18 — skippable per-request)
        if not rerank_enabled:
            # v4 P1: the no-rerank path orders by rank fusion — scale-free and
            # deterministic — never by mixed-scale raw scores.
            logger.info("Reranker skipped by override — rank-fusion ordering")
            phase_started = perf_counter()
            ranked = _rank_fused_order(merged)
            reranker_diagnostics = {
                "status": "skipped_by_request",
                "fallback": True,
                "ordering": "rrf_rank_fusion",
                "candidate_count": len(merged),
                "score_scale": settings.RERANKER_SCORE_SCALE,
            }
            _add_timing("rerank", phase_started)
        else:
            phase_started = perf_counter()
            try:
                ranked = await reranker_service.rerank(rank_query, merged)
                reranker_diagnostics = reranker_service.diagnostics()
            except Exception as exc:
                logger.warning(
                    "Reranker failed — DEGRADED rank-fusion ordering: %s", exc
                )
                ranked = _rank_fused_order(merged)
                reranker_diagnostics = {
                    "status": "exception_rank_fusion",
                    "fallback": True,
                    "degraded": True,
                    "ordering": "rrf_rank_fusion",
                    "candidate_count": len(merged),
                    "score_scale": settings.RERANKER_SCORE_SCALE,
                    "error": str(exc),
                }
            _add_timing("rerank", phase_started)
        counts["ranked"] = len(ranked)

        if _should_drop_low_confidence_rerank(
            ranked,
            rank_query,
            rerank_enabled=rerank_enabled,
            score_scale=settings.RERANKER_SCORE_SCALE,
            low_confidence_threshold=settings.RERANKER_LOW_CONFIDENCE_THRESHOLD,
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
            return _result([], status="empty_low_confidence_rerank")

        trimmed_ranked = _trim_bounded_rerank_tail(
            ranked,
            rerank_enabled=rerank_enabled,
            score_scale=settings.RERANKER_SCORE_SCALE,
            tier=effective_tier,
        )
        if len(trimmed_ranked) != len(ranked):
            logger.info(
                "Bounded rerank tail trim: %d → %d candidates "
                "(top_score=%.3f scale=%s)",
                len(ranked),
                len(trimmed_ranked),
                ranked[0].score if ranked else 0.0,
                settings.RERANKER_SCORE_SCALE,
            )
            counts["rerank_tail_trimmed"] = len(ranked) - len(trimmed_ranked)
            ranked = trimmed_ranked
            counts["ranked"] = len(ranked)

        grounded_ranked = apply_query_grounding(
            ranked,
            query=rank_query,
            tier=effective_tier,
            score_scale=settings.RERANKER_SCORE_SCALE,
        )
        if grounded_ranked is not ranked:
            ranked = grounded_ranked
        counts["ranked_query_grounded"] = len(ranked)

        # Phase 24 — final_top_k (Custom profile slider) overrides the
        # legacy DEFAULT_RETRIEVAL_K env cap. Never silently swap models or
        # tiers; just let the user crank the chunks-to-LLM count.
        effective_final_k = (
            final_top_k if final_top_k is not None else settings.DEFAULT_RETRIEVAL_K
        )
        _pool_doc_ids = {c.doc_id for c in ranked if getattr(c, "doc_id", None)}
        counts["distinct_docs_in_pool"] = len(_pool_doc_ids)
        diversity = select_with_diversity(
            ranked,
            final_top_k=effective_final_k,
            intent=retrieval_intent,
            tier=effective_tier,
            multi_corpus=multi,
            query=rank_query,
        )
        selection_diagnostics = dict(diversity.diagnostics or {})
        candidates = diversity.candidates
        counts["candidates"] = len(candidates)
        counts["diversity_added"] = diversity.added
        if selection_diagnostics:
            counts["sufficiency_repair_rounds"] = int(
                selection_diagnostics.get("repair_rounds") or 0
            )
            counts["selected_outside_raw_top_k"] = int(
                selection_diagnostics.get("selected_outside_raw_top_k") or 0
            )
            counts["near_duplicate_pairs"] = int(
                selection_diagnostics.get("near_duplicate_pairs") or 0
            )
        logger.info(
            "retrieval_pool_breadth: tier=%s distinct_docs_postmerge=%d distinct_docs_postrerank=%d ranked=%d final_top_k=%d diversity_added=%d",
            getattr(effective_tier, "value", effective_tier),
            counts.get("distinct_docs_merged", -1),
            len(_pool_doc_ids),
            len(ranked),
            effective_final_k,
            diversity.added,
        )
        logger.info(
            "final_top_k=%d diversity_added=%d (post-rerank cut, %d candidates available)",
            effective_final_k,
            diversity.added,
            len(ranked),
        )

        # [7] Hydrate from MongoDB (parent text + corpus_name + doc_name)
        # hydrate_chunks also: resolves parent_id for Mode A chunks (Pass 0)
        #                      drops empty-text chunks that couldn't be resolved (Pass 3)
        if effective_tier in (RetrievalTier.qdrant_mongo, RetrievalTier.qdrant_mongo_graph):
            phase_started = perf_counter()
            try:
                hydrated = await hydrate_chunks(candidates, corpus_ids, query=rank_query)
                _add_timing("hydrate", phase_started)
                result = _result(hydrated, status="ok_hydrated")
                # W2 §10.3 — waterfall packet rides ALONGSIDE the legacy
                # chunks (renderer picks it downstream); OFF -> not even built.
                if bool(getattr(settings, "WATERFALL_ASSEMBLY", False)):
                    phase_started = perf_counter()
                    from services.retriever.assembly import (
                        build_waterfall_packet,
                        packet_to_dict,
                    )

                    packet = await build_waterfall_packet(
                        hydrated, corpus_ids, query=query, settings=settings
                    )
                    _add_timing("waterfall_assembly", phase_started)
                    if packet is not None:
                        result.packet = packet_to_dict(packet)
                        result.diagnostics["packet_hash"] = packet.packet_hash
                        result.diagnostics["packet_items"] = len(packet.items)
                        result.diagnostics["packet_used_tokens"] = packet.used_tokens
                        logger.info(
                            "Waterfall packet: %d items, %d/%d tokens, hash=%s",
                            len(packet.items), packet.used_tokens,
                            packet.budget_tokens, packet.packet_hash[:12],
                        )
                _log_timings("ok_hydrated", len(hydrated))
                return result
            except Exception as exc:
                _add_timing("hydrate", phase_started)
                logger.warning("Hydration failed, returning unhydrated: %s", exc)

        _log_timings("ok_unhydrated", len(candidates))
        return _result(candidates, status="ok_unhydrated")


retriever_orchestrator = RetrieverOrchestrator()
