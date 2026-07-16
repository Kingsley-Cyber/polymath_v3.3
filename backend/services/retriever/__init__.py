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
  - Request cap: ChatRequest.corpus_ids enforces the configured max (currently 32)
  - Funnel round-robin: limit=20 per corpus → rerank → top-k
  - Graph fact seeding probes all selected corpora, then round-robin selects
    seed entities within GRAPH_ENTITY_LIMIT
  - Fair mode: FUNNEL A (summaries) skipped for multi-corpus
  - Strategy intersection: graph requires use_neo4j=True on ALL selected corpora
"""

import asyncio
import logging
import re
from time import perf_counter
from typing import Any

from config import get_settings
from models.schemas import RetrievalResult, RetrievalTier, SourceChunk, SourceFact
from services.cache_util import TTLCache, hash_key
from services.embedder import embed_queries, embed_query
from services.reranker import reranker_service
from services.text_quality import is_separator_only_text
from services.retriever.document_anchor import document_anchor_retriever
from services.retriever.funnel_a import funnel_a
from services.retriever.funnel_b import funnel_b
from services.retriever.graph_rerank import (
    apply_graph_degree_boost,
    apply_graph_degree_boost_metrics_aware,
)
from services.retriever.hydrate import (
    attach_document_identities,
    dedupe_cross_corpus_evidence,
    hydrate_chunks,
    hydrate_rerank_texts,
    hydrate_summary_rerank_texts,
)
from services.retriever.planned_fusion import (
    PlannedPool,
    annotate_planned_lane_grounding,
    dedupe_enumeration_finalists,
    dedupe_document_lane_finalists,
    dedupe_parent_finalists,
    filter_grounded_planned_candidates,
    fuse_planned_pools,
    grounded_planned_lane_ids,
    limit_candidates_per_document,
    order_enumeration_finalists,
    prioritize_enumeration_candidates,
    propagate_grounded_lane_aliases,
    reserve_planned_finalists,
)
from services.retriever.query_plan import (
    FALLBACK_PROBE_ID,
    QueryPlanV2,
    answer_object_lane_ids,
    answer_object_title_terms,
    query_plan_curation_query,
    query_plan_execution_batches,
    query_plan_execution_lanes,
    query_plan_vocabulary_lanes,
)
from services.retriever.tier0_router import (
    merge_grounded_document_route_hints,
    tier0_document_router,
)
from services.retriever.summary_tree_navigator import summary_tree_navigator
from services.retriever.vocabulary import (
    VOCABULARY_RESOLVER_VERSION,
    corpus_vocabulary_resolver,
    definition_reference_vocabulary_matches,
    grounded_document_route_hints,
    grounded_translation_lane_targets,
    grounded_vocabulary_lanes,
    hierarchy_bound_vocabulary_matches,
)
from services.retriever.grounded_planner import (
    filter_aligned_planner_lanes,
    grounded_planner_lanes,
    run_grounded_planner,
)
from services.retriever.intent_policy import (
    FunnelLimits,
    QueryNeed,
    adaptive_funnel_limits,
    infer_retrieval_intent,
    promote_compositional_intent,
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
from services.retriever.query_semantics import (
    concept_support_phrases,
    is_curated_concept,
    requires_explicit_graph_evidence,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _planned_rerank_candidate_limit(
    *,
    plan: QueryPlanV2,
    intent: Any,
    tier: RetrievalTier,
    configured_limit: int,
    final_top_k: int,
) -> int:
    """Bound cross-encoder work by query complexity without starving recall."""

    configured = max(1, int(configured_limit))
    final_k = max(1, int(final_top_k))
    obligation_count = max(1, len([probe for probe in plan.probes if probe.required]))
    if tier == RetrievalTier.qdrant_mongo_graph:
        desired = 34 + min(16, obligation_count * 4)
        ceiling = 52
    elif tier == RetrievalTier.qdrant_mongo:
        desired = 26 + min(12, obligation_count * 3)
        ceiling = 40
    else:
        desired = 18 + min(10, obligation_count * 2)
        ceiling = 28
    if intent.need == QueryNeed.SPECIFIC and plan.complexity == "simple":
        desired = max(final_k * 2, desired - 8)
    return max(final_k, min(configured, desired, ceiling))


def _tree_routing_lane_ids(lanes: list[Any]) -> list[str]:
    """Use full RAPTOR descent for required obligations, not advisory probes."""

    required = [
        lane.lane_id for lane in lanes if lane.role == "core" and bool(lane.required)
    ]
    if required:
        return required
    return [lane.lane_id for lane in lanes if lane.role in {"core", "original"}][:1]


def _planned_final_result_limit(
    *,
    plan: QueryPlanV2,
    intent: Any,
    tier: RetrievalTier,
    requested_limit: int,
    routed_document_count: int,
) -> int:
    """Size final context from answer obligations and routed breadth."""

    requested = max(1, int(requested_limit))
    required_probes = max(1, len([probe for probe in plan.probes if probe.required]))
    if plan.complexity == "simple" and intent.need == QueryNeed.SPECIFIC:
        floor = 8
    else:
        floor = 8 + min(8, (required_probes - 1) * 3)
    if intent.need == QueryNeed.BROAD:
        floor = max(floor, 12)
    floor = max(floor, min(6, max(0, int(routed_document_count))))
    ceiling = (
        24
        if tier == RetrievalTier.qdrant_mongo_graph
        else 20
        if tier == RetrievalTier.qdrant_mongo
        else 16
    )
    return min(ceiling, max(requested, floor))


def _filter_fast_grounded_candidates(
    chunks: list[SourceChunk],
    *,
    query: str = "",
) -> tuple[list[SourceChunk], int]:
    """Drop Fast-route fillers only when grounded evidence already exists.

    ``apply_query_grounding`` annotates candidates without changing the Fast
    route's RRF ordering. This fail-open cut prevents zero-match vector
    neighbors from becoming citations beside an exact named-concept hit.
    """

    def matched_count(chunk: SourceChunk) -> int:
        return int(
            ((chunk.metadata or {}).get("query_grounding") or {}).get(
                "matched_count", 0
            )
            or 0
        )

    grounded = [chunk for chunk in chunks if matched_count(chunk) > 0]
    if not grounded:
        return chunks, 0

    title_phrases = [
        " ".join(re.findall(r"[a-z0-9]+", match.group(1).lower()))
        for match in re.finditer(
            r"\b([A-Z][A-Za-z0-9'\-]*(?:\s+(?:to|of|the|and|for|in|on|"
            r"[A-Z][A-Za-z0-9'\-]*)){1,5})\b",
            str(query or ""),
        )
    ]

    def normalized_haystack(chunk: SourceChunk) -> str:
        return " ".join(
            re.findall(
                r"[a-z0-9]+",
                " ".join(
                    [
                        str(chunk.doc_name or ""),
                        " ".join(str(value) for value in (chunk.heading_path or [])),
                        str(chunk.text or ""),
                    ]
                ).lower(),
            )
        )

    if title_phrases:
        title_grounded = [
            chunk
            for chunk in grounded
            if any(phrase in normalized_haystack(chunk) for phrase in title_phrases)
        ]
        if title_grounded:
            return title_grounded, len(chunks) - len(title_grounded)

    def has_exact_matched_span(chunk: SourceChunk) -> bool:
        grounding = (chunk.metadata or {}).get("query_grounding") or {}
        matched = [
            str(value or "").replace("_", " ").strip().lower()
            for value in grounding.get("matched") or []
            if str(value or "").strip()
        ]
        haystack = normalized_haystack(chunk)
        phrases = [value for value in matched if len(value.split()) >= 2]
        if len(matched) >= 2:
            phrases.append(" ".join(matched))
        return any(
            " ".join(re.findall(r"[a-z0-9]+", phrase)) in haystack for phrase in phrases
        )

    exact = [chunk for chunk in grounded if has_exact_matched_span(chunk)]
    if exact:
        return exact, len(chunks) - len(exact)
    strongest_count = max(matched_count(chunk) for chunk in grounded)
    if strongest_count >= 2:
        grounded = [
            chunk for chunk in grounded if matched_count(chunk) == strongest_count
        ]
    return grounded, len(chunks) - len(grounded)


_PER_CORPUS_LIMIT = 20  # spec: 20 per corpus for round-robin
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
_EXTERNAL_SUFFICIENCY_MAX_CONCEPTS = 3


def invalidate_retrieval_cache() -> None:
    """Invalidate assembled retrieval results after artifact mutations."""

    _RETRIEVAL_CACHE.clear()


def _missing_concept_support_query(result: RetrievalResult) -> str | None:
    """Build one bounded support query from a failed final-context gate."""

    selection = (result.diagnostics or {}).get("selection") or {}
    sufficiency = selection.get("sufficiency") or {}
    if sufficiency.get("answerable") is not False:
        return None
    missing: list[str] = []
    for atom in sufficiency.get("missing_atoms") or []:
        value = str(atom or "")
        if not value.startswith("concept:"):
            continue
        term = value.removeprefix("concept:").replace("_", " ").strip()
        if term and term not in missing:
            missing.append(term)
        if len(missing) >= _EXTERNAL_SUFFICIENCY_MAX_CONCEPTS:
            break
    if not missing:
        return None

    # A blended repair query can accidentally satisfy a generic token while
    # missing the named concept that triggered repair. Prefer one curated
    # concept and fan it out through the shared alias vocabulary. Fall back to
    # the original compact multi-concept query for unknown vocabulary.
    curated = next(
        (term for term in missing if is_curated_concept(term.replace(" ", "_"))),
        None,
    )
    if curated:
        phrases = concept_support_phrases(curated.replace(" ", "_"))
        return " ".join(phrases) or curated
    return " ".join(missing)


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


def _document_anchor_limit_for(
    effective_tier: RetrievalTier, *, retrieval_k: int
) -> int:
    """Small source-title recall budget for hydrated tiers.

    This is metadata/Mongo-backed recall, so Fast Search must not use it.
    """
    if effective_tier == RetrievalTier.qdrant_only:
        return 0
    return 8 if retrieval_k >= 40 else 4


def _rerank_enabled_for_tier(
    requested: bool,
    tier: RetrievalTier,
) -> bool:
    """Reranking is a relevance policy independent of the storage tier."""

    return bool(requested)


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
            "cross_encoder_rerank": True,
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
            "cross_encoder_rerank": True,
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
        "cross_encoder_rerank": True,
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
    seen_chunk_ids: set[tuple[str, str]] = set()

    for fact in facts:
        chunk_id = (fact.chunk_id or "").strip()
        chunk_key = (str(fact.corpus_id or ""), chunk_id)
        if not chunk_id or chunk_key in seen_chunk_ids:
            continue
        seen_chunk_ids.add(chunk_key)
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
        refs = [
            (str(c.corpus_id or ""), str(c.chunk_id))
            for c in chunks
            if c.chunk_id and c.corpus_id
        ]
        noisy = {
            (str(doc["corpus_id"]), str(doc["chunk_id"]))
            async for doc in db["chunks"].find(
                {
                    "$or": [
                        {"corpus_id": corpus_id, "chunk_id": chunk_id}
                        for corpus_id, chunk_id in refs
                    ],
                    "chunk_kind": {"$in": list(NOISY_KINDS)},
                },
                {"_id": 0, "corpus_id": 1, "chunk_id": 1},
            )
        }
        if not noisy:
            return chunks
        logger.info(
            "fact-seed NOISY_KINDS filter: dropped %d citation evidence chunk(s)",
            len(noisy),
        )
        return [
            c
            for c in chunks
            if (str(c.corpus_id or ""), str(c.chunk_id or "")) not in noisy
        ]
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
            from services.storage.record_status import with_active_records

            docs = (
                await db["corpora"]
                .find(
                    with_active_records({"corpus_id": {"$in": corpus_ids}}),
                    {"corpus_id": 1},
                )
                .to_list(length=None)
            )
            existing = {d["corpus_id"] for d in docs}
            filtered = [c for c in corpus_ids if c in existing]
            dropped = [c for c in corpus_ids if c not in existing]
            if dropped:
                logger.warning(
                    "Dropping %d stale corpus_id(s) not in Mongo: %s",
                    len(dropped),
                    dropped,
                )
            return filtered, dropped
        except Exception as exc:
            logger.warning("Corpus existence check failed (%s) — keeping all ids", exc)
            return corpus_ids, []

    async def _corpus_artifact_epoch(self, corpus_ids: list[str] | None) -> tuple:
        """Return the durable readiness version used by retrieval cache keys."""

        if not corpus_ids:
            return ()
        try:
            from services.conversation import conversation_service

            db = conversation_service._db
            if db is None:
                return tuple((str(cid), "unknown") for cid in sorted(corpus_ids))
            rows = (
                await db["corpus_readiness"]
                .find(
                    {"_id": {"$in": list(corpus_ids)}},
                    {"_id": 1, "computed_at": 1, "schema_version": 1},
                )
                .to_list(length=None)
            )
            versions = {
                str(row.get("_id")): str(row.get("computed_at") or "unknown")
                for row in rows
            }
            return tuple(
                (str(cid), versions.get(str(cid), "unknown"))
                for cid in sorted(corpus_ids)
            )
        except Exception as exc:
            logger.warning("Corpus artifact epoch lookup failed: %s", exc)
            return tuple((str(cid), "unknown") for cid in sorted(corpus_ids))

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

            driver = getattr(ingestion_service, "neo4j_driver", None) or getattr(
                fact_retrieval, "_driver", None
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

            entity_limit = max(
                1, min(int(getattr(settings, "GRAPH_ENTITY_LIMIT", 8)), 50)
            )
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
                g
                for g in _groups
                if getattr(g, "key", "") not in _GENERIC_QUERY_CONCEPTS
            ]

            # Multi-corpus correctness: do not silently truncate graph fact
            # seeding to the first three corpora. Probe every selected corpus
            # concurrently, then select candidate entities round-robin so an
            # 8-entity budget cannot be monopolized by the first corpus.
            detect_sem = asyncio.Semaphore(8)

            async def _detect_for_corpus(cid: str) -> tuple[str, list[dict[str, Any]]]:
                async with detect_sem:
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
                        return cid, []
                    return cid, list(rows or [])

            detected = await asyncio.gather(
                *[_detect_for_corpus(str(cid)) for cid in corpus_ids]
            )
            candidates_by_corpus: dict[str, list[tuple[str, str]]] = {
                str(cid): [] for cid in corpus_ids
            }
            for cid, rows in detected:
                try:
                    bucket = candidates_by_corpus.setdefault(cid, [])
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
                        bucket.append((name, eid))
                except Exception as exc:
                    logger.warning(
                        "Graph fact seeding: candidate filtering failed for corpus=%s: %s",
                        cid,
                        exc,
                    )

            positions = {cid: 0 for cid in candidates_by_corpus}
            selected_corpora: set[str] = set()
            while True:
                if (
                    len(entity_names) >= entity_limit
                    and len(entity_ids) >= entity_limit
                ):
                    break
                progressed = False
                for cid in corpus_ids:
                    bucket = candidates_by_corpus.get(str(cid)) or []
                    while positions[str(cid)] < len(bucket):
                        name, eid = bucket[positions[str(cid)]]
                        positions[str(cid)] += 1
                        added = False
                        key = name.lower()
                        if (
                            key not in seen_entities
                            and len(entity_names) < entity_limit
                        ):
                            entity_names.append(name)
                            seen_entities.add(key)
                            added = True
                        if (
                            eid
                            and eid not in seen_ids
                            and len(entity_ids) < entity_limit
                        ):
                            entity_ids.append(eid)
                            seen_ids.add(eid)
                            added = True
                        if added:
                            selected_corpora.add(str(cid))
                            progressed = True
                            break
                    if (
                        len(entity_names) >= entity_limit
                        and len(entity_ids) >= entity_limit
                    ):
                        break
                if not progressed:
                    break

            if not entity_names and not entity_ids:
                return []

            graph_fact_limit = max(0, min(int(settings.GRAPH_FACT_SEED_LIMIT), 50))
            limit = max(
                0, min(int(fact_seed_limit or graph_fact_limit), graph_fact_limit)
            )
            if limit <= 0:
                return []
            facts = await fact_retrieval.retrieve_facts_for_entities(
                entity_names=entity_names[:entity_limit],
                entity_ids=entity_ids[:entity_limit],
                corpus_ids=corpus_ids,
                fact_types=None,
                limit=limit,
            )
            logger.info(
                "Graph fact seeding: corpus_seeds=%d/%d candidate_corpora=%d entities=%d ids=%d facts=%d entity_limit=%d fact_limit=%d",
                len(selected_corpora),
                len(corpus_ids),
                sum(1 for rows in candidates_by_corpus.values() if rows),
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
        if (
            cached is not None
            and (perf_counter() - cached[0]) < _EMBED_CONFIG_TTL_SECONDS
        ):
            return cached[1]
        try:
            from services.conversation import conversation_service

            db = conversation_service._db
            if db is None:
                return None
            from services.storage.record_status import with_active_records

            doc = await db["corpora"].find_one(
                with_active_records({"corpus_id": cid}),
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
            from services.storage.record_status import with_active_records

            corpus_docs = (
                await db["corpora"]
                .find(with_active_records({"corpus_id": {"$in": corpus_ids}}))
                .to_list(length=None)
            )

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

    async def _repair_cross_corpus_missing_concepts(
        self,
        result: RetrievalResult,
        request_kwargs: dict[str, Any],
    ) -> RetrievalResult:
        """Run one bounded Hybrid support pass when final context is incomplete.

        The in-pool selector can only repair with candidates it already has.
        This pass is deliberately narrow: multi-corpus, local search, reranking
        enabled, and missing concept atoms present. The repaired context is
        adopted only when the deterministic sufficiency score improves.
        """

        corpus_ids = list(request_kwargs.get("corpus_ids") or [])
        support_query = _missing_concept_support_query(result)
        if (
            len(corpus_ids) < 2
            or not support_query
            or bool(request_kwargs.get("support_profile", False))
            or str(request_kwargs.get("search_mode") or "local").lower() != "local"
            or not bool(request_kwargs.get("rerank_enabled", True))
            or result.effective_tier == RetrievalTier.qdrant_only
            or not result.chunks
        ):
            return result

        started = perf_counter()
        diagnostics = dict(result.diagnostics or {})
        before_selection = diagnostics.get("selection") or {}
        before_sufficiency = before_selection.get("sufficiency") or {}
        repair_meta: dict[str, Any] = {
            "attempted": True,
            "adopted": False,
            "support_query": support_query,
            "missing_atoms_before": list(before_sufficiency.get("missing_atoms") or []),
            "coverage_before": float(
                before_sufficiency.get("required_coverage") or 0.0
            ),
        }

        try:
            support_kwargs = dict(request_kwargs)
            support_kwargs.update(
                {
                    "query": support_query,
                    "ranking_query": support_query,
                    "retrieval_tier": RetrievalTier.qdrant_mongo,
                    "collections": None,
                    "retrieval_k": max(
                        24,
                        min(int(request_kwargs.get("retrieval_k") or 40), 40),
                    ),
                    "rerank_enabled": False,
                    "top_k_summary": 0,
                    "rerank_top_n": 16,
                    "neo4j_expansion_cap": 0,
                    "final_top_k": 6,
                    "fact_seed_limit": 0,
                    "search_mode": "local",
                    "support_profile": True,
                }
            )
            support = await self._retrieve_uncached(**support_kwargs)
            repair_meta["support_candidates"] = len(support.chunks)
            missing_concepts = [
                str(atom).removeprefix("concept:").strip().lower()
                for atom in before_sufficiency.get("missing_atoms") or []
                if str(atom).startswith("concept:")
            ]
            marked_support: list[SourceChunk] = []
            for support_rank, chunk in enumerate(support.chunks, start=1):
                copied = chunk.model_copy(deep=True)
                copied.metadata = dict(copied.metadata or {})
                copied.metadata["external_sufficiency_support"] = {
                    "query": support_query,
                    "rank": support_rank,
                    "missing_concepts": missing_concepts,
                    "admitted": False,
                }
                marked_support.append(copied)
            combined = merge_pools(result.chunks, marked_support)
            repair_meta["combined_candidates"] = len(combined)
            if len(combined) <= len(result.chunks):
                repair_meta["reason"] = "support_search_added_no_candidates"
                diagnostics["external_sufficiency_repair"] = repair_meta
                return result.model_copy(update={"diagnostics": diagnostics})

            ranking_query = str(
                request_kwargs.get("ranking_query") or request_kwargs.get("query") or ""
            )
            ranked = await reranker_service.rerank(ranking_query, combined)
            ranked = apply_query_grounding(
                ranked,
                query=ranking_query,
                tier=result.effective_tier,
                score_scale=settings.RERANKER_SCORE_SCALE,
            )
            # The final cross-encoder may demote a passage that is an exact
            # hit for the missing atom because it only explains one side of a
            # multi-part query. Reserve at most one such passage, and only when
            # it covers every missing concept from the support pass.
            support_candidates: list[SourceChunk] = []
            for chunk in ranked:
                metadata = dict(chunk.metadata or {})
                support_info = metadata.get("external_sufficiency_support")
                grounding = metadata.get("query_grounding")
                if not isinstance(support_info, dict) or not isinstance(
                    grounding, dict
                ):
                    continue
                matched = {
                    str(value).strip().lower()
                    for value in grounding.get("matched") or []
                    if str(value).strip()
                }
                if missing_concepts and set(missing_concepts) <= matched:
                    support_candidates.append(chunk)
            if support_candidates:
                support_candidates.sort(
                    key=lambda chunk: int(
                        (
                            (chunk.metadata or {}).get("external_sufficiency_support")
                            or {}
                        ).get("rank", 999)
                    )
                )
                reserved_id = support_candidates[0].chunk_id
                top_score = max(float(chunk.score or 0.0) for chunk in ranked)
                adjusted: list[SourceChunk] = []
                for chunk in ranked:
                    copied = chunk.model_copy(deep=True)
                    if copied.chunk_id == reserved_id:
                        copied.score = max(float(copied.score or 0.0), top_score * 0.45)
                        copied.metadata = dict(copied.metadata or {})
                        support_info = dict(
                            copied.metadata.get("external_sufficiency_support") or {}
                        )
                        support_info["admitted"] = True
                        copied.metadata["external_sufficiency_support"] = support_info
                        repair_meta["reserved_support_chunk_id"] = reserved_id
                    adjusted.append(copied)
                ranked = sorted(adjusted, key=lambda chunk: chunk.score, reverse=True)
            final_top_k = int(
                request_kwargs.get("final_top_k") or settings.DEFAULT_RETRIEVAL_K
            )
            diversity = select_with_diversity(
                ranked,
                final_top_k=final_top_k,
                intent=infer_retrieval_intent(ranking_query),
                tier=result.effective_tier,
                multi_corpus=True,
                selected_corpus_ids=corpus_ids,
                query=ranking_query,
            )
            after_selection = dict(diversity.diagnostics or {})
            after_sufficiency = after_selection.get("sufficiency") or {}
            coverage_after = float(after_sufficiency.get("required_coverage") or 0.0)
            coverage_before = float(before_sufficiency.get("required_coverage") or 0.0)
            missing_before = {
                str(atom) for atom in before_sufficiency.get("missing_atoms") or []
            }
            missing_after = {
                str(atom) for atom in after_sufficiency.get("missing_atoms") or []
            }
            resolved_concepts = sorted(
                atom
                for atom in missing_before - missing_after
                if atom.startswith("concept:")
            )
            improved = bool(coverage_after > coverage_before and resolved_concepts)
            repair_meta.update(
                {
                    "coverage_after": coverage_after,
                    "resolved_concepts": resolved_concepts,
                    "missing_atoms_after": list(
                        after_sufficiency.get("missing_atoms") or []
                    ),
                    "answerable_after": bool(
                        after_sufficiency.get("answerable", False)
                    ),
                    "duration_s": round(perf_counter() - started, 3),
                }
            )
            if not improved:
                repair_meta["reason"] = "support_search_did_not_improve_coverage"
                diagnostics["external_sufficiency_repair"] = repair_meta
                return result.model_copy(update={"diagnostics": diagnostics})

            repair_meta["adopted"] = True
            diagnostics["external_sufficiency_repair"] = repair_meta
            diagnostics["selection"] = after_selection
            counts = dict(diagnostics.get("counts") or {})
            counts["external_sufficiency_support"] = len(support.chunks)
            counts["candidates"] = len(diversity.candidates)
            diagnostics["counts"] = counts
            diagnostics["final_count"] = len(diversity.candidates)
            diagnostics["total_s"] = round(
                float(diagnostics.get("total_s") or 0.0)
                + float(repair_meta["duration_s"]),
                3,
            )
            return result.model_copy(
                update={
                    "chunks": diversity.candidates,
                    "diagnostics": diagnostics,
                }
            )
        except Exception as exc:
            logger.warning("External sufficiency repair skipped: %s", exc)
            repair_meta.update(
                {
                    "reason": "support_search_error",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "duration_s": round(perf_counter() - started, 3),
                }
            )
            diagnostics["external_sufficiency_repair"] = repair_meta
            return result.model_copy(update={"diagnostics": diagnostics})

    # ── main entry ─────────────────────────────────────────────────────────────

    async def retrieve_planned(
        self,
        *,
        plan: QueryPlanV2,
        corpus_ids: list[str] | None,
        retrieval_tier: RetrievalTier,
        collections: list[str] | None = None,
        retrieval_k: int | None = None,
        rerank_enabled: bool = True,
        top_k_summary: int | None = None,
        rerank_top_n: int | None = None,
        final_top_k: int | None = None,
        fact_seed_limit: int | None = None,
        similarity_threshold: float | None = None,
        neo4j_expansion_cap: int | None = None,
        max_corpora_per_query: int | None = None,
        search_mode: str = "local",
        disabled_lexicon_ids: list[str] | None = None,
        grounded_planner_route: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        """Execute QueryPlanV2 as one candidate-generation and rerank pass."""

        curation_query = query_plan_curation_query(plan)
        intent = infer_retrieval_intent(plan.standalone_query)
        intent = promote_compositional_intent(
            intent,
            complexity=plan.complexity,
            required_lane_count=sum(
                1 for probe in plan.probes if bool(getattr(probe, "required", False))
            ),
        )

        if (
            retrieval_tier == RetrievalTier.qdrant_only
            and requires_explicit_graph_evidence(plan.standalone_query)
        ):
            return RetrievalResult(
                chunks=[],
                requested_tier=retrieval_tier,
                effective_tier=retrieval_tier,
                diagnostics={
                    "query_plan_version": "query_plan.v2",
                    "status": "route_capability_mismatch",
                    "reason": "explicit_graph_evidence_requires_graph_route",
                    "original_query": plan.original_query,
                    "cache": {"hit": False, "key_version": "retrieval_v2"},
                },
            )

        if search_mode == "global":
            return await self._retrieve_uncached(
                query=plan.standalone_query,
                corpus_ids=corpus_ids,
                retrieval_tier=retrieval_tier,
                collections=collections,
                retrieval_k=retrieval_k,
                rerank_enabled=rerank_enabled,
                ranking_query=curation_query,
                top_k_summary=top_k_summary,
                rerank_top_n=rerank_top_n,
                final_top_k=final_top_k,
                fact_seed_limit=fact_seed_limit,
                search_mode=search_mode,
            )

        started = perf_counter()
        timings: dict[str, float] = {}
        lane_diagnostics: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        total_deadline = float(
            getattr(
                settings,
                (
                    "QUERY_PLAN_GRAPH_TOTAL_DEADLINE_SECONDS"
                    if retrieval_tier == RetrievalTier.qdrant_mongo_graph
                    else "QUERY_PLAN_HYBRID_TOTAL_DEADLINE_SECONDS"
                ),
                9.5 if retrieval_tier == RetrievalTier.qdrant_mongo_graph else 7.5,
            )
        )
        quality_first = bool(getattr(settings, "QUERY_PLAN_QUALITY_FIRST", True))

        def _stage_timeout(cap: float, *, reserve: float = 0.0) -> float:
            if quality_first:
                return max(0.05, float(cap))
            remaining = total_deadline - (perf_counter() - started) - reserve
            return max(0.05, min(float(cap), remaining))

        def _budget_remaining() -> float:
            return max(0.0, total_deadline - (perf_counter() - started))

        requested_corpus_ids = list(corpus_ids or [])
        try:
            corpus_ids, dropped = await asyncio.wait_for(
                self._filter_existing_corpora(corpus_ids),
                timeout=_stage_timeout(1.0, reserve=1.0),
            )
        except Exception as exc:
            corpus_ids, dropped = requested_corpus_ids, []
            failures.append(
                {
                    "lane_id": "setup",
                    "retriever": "corpus_filter",
                    "error": f"{type(exc).__name__}: {exc}"[:240],
                }
            )
        if (
            max_corpora_per_query is not None
            and max_corpora_per_query > 0
            and len(corpus_ids) > max_corpora_per_query
        ):
            corpus_ids = corpus_ids[:max_corpora_per_query]
        try:
            effective_tier, downgrade_reason = await asyncio.wait_for(
                self._enforce_strategy_intersection(retrieval_tier, corpus_ids),
                timeout=_stage_timeout(1.0, reserve=1.0),
            )
        except Exception as exc:
            effective_tier, downgrade_reason = retrieval_tier, None
            failures.append(
                {
                    "lane_id": "setup",
                    "retriever": "strategy_intersection",
                    "error": f"{type(exc).__name__}: {exc}"[:240],
                }
            )
        a_cols, b_cols = self._resolve_collections(
            effective_tier, corpus_ids, collections
        )
        lanes = list(query_plan_execution_lanes(plan))
        if not lanes:
            lanes = [plan.lanes[0]]
        vocabulary_lanes = list(query_plan_vocabulary_lanes(plan))
        required_lane_ids = [
            lane.lane_id for lane in lanes if lane.role == "core" and lane.required
        ]
        protected_lane_ids = [lane.lane_id for lane in lanes if lane.role == "original"]
        broad_document_routing = bool(
            len(required_lane_ids) >= 3
            or plan.complexity
            in {"comparative", "compositional", "dependent_multi_hop"}
            or plan.answer_shape
            in {"comparison", "enumeration", "relationship", "synthesis"}
        )
        document_route_limit = 12 if broad_document_routing else 6
        document_route_fetch = 24 if broad_document_routing else 12
        answer_lane_ids = list(answer_object_lane_ids(plan))
        repair_diagnostics: dict[str, Any] = {
            "max_rounds": int(plan.max_repair_rounds),
            "attempted_rounds": 0,
            "missing_lane_ids_before": [],
            "added_candidates": 0,
        }

        embed_started = perf_counter()
        try:
            embedding_config = await asyncio.wait_for(
                self._embedding_config_for_query(corpus_ids),
                timeout=_stage_timeout(1.0, reserve=1.0),
            )
        except Exception as exc:
            embedding_config = None
            failures.append(
                {
                    "lane_id": "setup",
                    "retriever": "embedding_config",
                    "error": f"{type(exc).__name__}: {exc}"[:240],
                }
            )
        try:
            embedding_texts = [
                *[lane.dense_text for lane in lanes],
                *[lane.dense_text for lane in vocabulary_lanes],
            ]
            embedded_vectors = await asyncio.wait_for(
                embed_queries(embedding_texts, embedding_config),
                timeout=_stage_timeout(
                    float(getattr(settings, "QUERY_PLAN_EMBED_DEADLINE_SECONDS", 5.0)),
                    reserve=2.0,
                ),
            )
        except Exception as exc:
            logger.warning(
                "QueryPlanV2 embedding degraded to lexical-only lanes: %s", exc
            )
            embedded_vectors = [None] * (len(lanes) + len(vocabulary_lanes))
            failures.append(
                {
                    "lane_id": "all",
                    "retriever": "embedding",
                    "error": f"{type(exc).__name__}: {exc}"[:240],
                }
            )
        vectors = embedded_vectors[: len(lanes)]
        vocabulary_vectors = embedded_vectors[len(lanes) :]
        timings["embed"] = perf_counter() - embed_started

        vocabulary_started = perf_counter()
        qdrant_client = None
        vocabulary_diagnostics: dict[str, Any] = {
            "version": VOCABULARY_RESOLVER_VERSION,
            "status": "skipped",
            "reason": "lexicon_projection_unavailable",
            "matches": [],
            "per_corpus": {},
            "rejected_expansions": [],
        }
        vocabulary_deadline_s = float(
            getattr(
                settings,
                "QUERY_PLAN_VOCABULARY_DEADLINE_SECONDS",
                15.0,
            )
        )
        try:
            from services.conversation import conversation_service
            from services.ingestion_service import ingestion_service

            qdrant_client = getattr(ingestion_service, "qdrant_client", None)
            if (
                bool(getattr(settings, "CORPUS_VOCABULARY_RESOLVER_ENABLED", True))
                and qdrant_client is not None
                and corpus_ids
            ):
                original_vector = next(
                    (
                        vector
                        for lane, vector in zip(lanes, vectors)
                        if lane.role == "original" and vector is not None
                    ),
                    next((vector for vector in vectors if vector is not None), None),
                )
                vocabulary_diagnostics = await asyncio.wait_for(
                    corpus_vocabulary_resolver.resolve(
                        query=plan.standalone_query,
                        corpus_ids=corpus_ids,
                        tier=effective_tier,
                        query_vector=original_vector,
                        qdrant_client=qdrant_client,
                        db=conversation_service._db,
                        neo4j_driver=getattr(ingestion_service, "neo4j_driver", None),
                        disabled_lexicon_ids=disabled_lexicon_ids,
                        excluded_terms=[
                            term
                            for constraint in plan.constraints
                            if constraint.operator == "exclude"
                            for term in constraint.terms
                        ],
                        query_lanes=[
                            {
                                "lane_id": lane.lane_id,
                                "query": lane.query or lane.dense_text,
                                "query_vector": vector,
                            }
                            for lane, vector in zip(
                                vocabulary_lanes,
                                vocabulary_vectors,
                            )
                            if vector is not None
                        ],
                    ),
                    timeout=_stage_timeout(
                        vocabulary_deadline_s,
                        reserve=1.0,
                    ),
                )
                extra_lanes, expansion_diagnostics = grounded_vocabulary_lanes(
                    plan,
                    vocabulary_diagnostics,
                )
                vocabulary_diagnostics["expansion"] = expansion_diagnostics
                if extra_lanes:
                    extra_vectors = await asyncio.wait_for(
                        embed_queries(
                            [lane.dense_text for lane in extra_lanes],
                            embedding_config,
                        ),
                        timeout=_stage_timeout(
                            float(
                                getattr(
                                    settings,
                                    "QUERY_PLAN_EMBED_DEADLINE_SECONDS",
                                    5.0,
                                )
                            ),
                            reserve=1.0,
                        ),
                    )
                    lanes.extend(extra_lanes)
                    vectors.extend(extra_vectors)
                planner_diagnostics = await run_grounded_planner(
                    conversation_service._db,
                    plan=plan,
                    resolution=vocabulary_diagnostics,
                    corpus_ids=list(corpus_ids),
                    route=grounded_planner_route,
                )
                planner_extra_lanes, planner_lane_lexicon_ids = grounded_planner_lanes(
                    planner_diagnostics,
                    vocabulary_diagnostics,
                )
                if planner_extra_lanes:
                    planner_vectors = await asyncio.wait_for(
                        embed_queries(
                            [lane.dense_text for lane in planner_extra_lanes],
                            embedding_config,
                        ),
                        timeout=_stage_timeout(
                            float(
                                getattr(
                                    settings,
                                    "QUERY_PLAN_EMBED_DEADLINE_SECONDS",
                                    5.0,
                                )
                            ),
                            reserve=1.0,
                        ),
                    )
                    original_vector = next(
                        (
                            vector
                            for lane, vector in zip(lanes, vectors)
                            if lane.role == "original" and vector is not None
                        ),
                        next(
                            (vector for vector in vectors if vector is not None), None
                        ),
                    )
                    (
                        planner_extra_lanes,
                        planner_vectors,
                        planner_lane_lexicon_ids,
                        planner_alignment,
                    ) = filter_aligned_planner_lanes(
                        planner_extra_lanes,
                        planner_vectors,
                        planner_lane_lexicon_ids,
                        original_vector=original_vector,
                        minimum_alignment=float(
                            getattr(
                                settings,
                                "GROUNDED_QUERY_PLANNER_MIN_ALIGNMENT",
                                0.45,
                            )
                        ),
                        step_back_minimum_alignment=float(
                            getattr(
                                settings,
                                "GROUNDED_QUERY_PLANNER_STEP_BACK_MIN_ALIGNMENT",
                                0.35,
                            )
                        ),
                    )
                    planner_diagnostics["semantic_alignment"] = planner_alignment
                    lanes.extend(planner_extra_lanes)
                    vectors.extend(planner_vectors)
                planner_diagnostics["lane_ids"] = [
                    lane.lane_id for lane in planner_extra_lanes
                ]
                planner_diagnostics["lane_lexicon_ids"] = planner_lane_lexicon_ids
                if planner_lane_lexicon_ids:
                    expansion = vocabulary_diagnostics.setdefault("expansion", {})
                    expansion.setdefault("lane_lexicon_ids", {}).update(
                        planner_lane_lexicon_ids
                    )
                vocabulary_diagnostics["grounded_planner"] = planner_diagnostics
                vocabulary_diagnostics["status"] = (
                    "expanded"
                    if extra_lanes or planner_extra_lanes
                    else "resolved_no_expansion"
                )
            elif not bool(
                getattr(settings, "CORPUS_VOCABULARY_RESOLVER_ENABLED", True)
            ):
                vocabulary_diagnostics["reason"] = "disabled_by_operator"
        except Exception as exc:
            vocabulary_diagnostics.update(
                {
                    "status": "degraded",
                    "reason": f"{type(exc).__name__}: {exc}"[:240],
                }
            )
            failures.append(
                {
                    "lane_id": "vocabulary",
                    "retriever": "corpus_vocabulary",
                    "error": f"{type(exc).__name__}: {exc}"[:240],
                }
            )
        timings["vocabulary_resolution"] = perf_counter() - vocabulary_started
        vectors_by_lane_id = {
            lane.lane_id: vector for lane, vector in zip(lanes, vectors)
        }

        routing_started = perf_counter()
        document_routes: dict[str, list[Any]] = {}
        document_routing_diagnostics: dict[str, Any] = {
            "enabled": False,
            "reason": "tier0_routing_disabled",
        }
        if bool(getattr(settings, "TIER0_ROUTING", False)):
            try:
                route_vectors = {
                    lane.lane_id: vectors_by_lane_id.get(lane.lane_id)
                    for lane in lanes
                    if lane.role == "core"
                }
                if not route_vectors and lanes:
                    route_vectors[lanes[0].lane_id] = vectors_by_lane_id.get(
                        lanes[0].lane_id
                    )
                document_routes, document_routing_diagnostics = await asyncio.wait_for(
                    tier0_document_router.route_lanes(
                        route_vectors,
                        corpus_ids,
                        per_lane_per_corpus=document_route_fetch,
                        max_per_lane=document_route_limit,
                        title_terms_by_lane=answer_object_title_terms(plan),
                    ),
                    timeout=_stage_timeout(1.25, reserve=1.0),
                )
            except Exception as exc:
                failures.append(
                    {
                        "lane_id": "document_routing",
                        "retriever": "tier0_document_summary",
                        "error": f"{type(exc).__name__}: {exc}"[:240],
                    }
                )
                document_routing_diagnostics = {
                    "enabled": True,
                    "status": "degraded",
                    "reason": f"{type(exc).__name__}: {exc}"[:240],
                }
            route_hints = grounded_document_route_hints(
                vocabulary_diagnostics,
                vocabulary_diagnostics.get("expansion") or {},
            )
            document_routes, grounded_route_rows = merge_grounded_document_route_hints(
                document_routes,
                route_hints,
                max_per_lane=document_route_limit,
            )
            if grounded_route_rows:
                document_routing_diagnostics[
                    "grounded_route_hints"
                ] = grounded_route_rows
                document_routing_diagnostics["grounded_route_hint_count"] = sum(
                    len(values) for values in grounded_route_rows.values()
                )
                document_routing_diagnostics["routed_doc_count"] = len(
                    {
                        (route.corpus_id, route.doc_id)
                        for values in document_routes.values()
                        for route in values
                    }
                )
                document_routing_diagnostics["routes"] = {
                    lane_id: [
                        {
                            "corpus_id": route.corpus_id,
                            "doc_id": route.doc_id,
                            "score": round(route.score, 4),
                            "title": route.title,
                            "concepts": list(route.concepts),
                            "section_ids": list(route.section_ids),
                            "grounded_hint": (
                                route.corpus_id,
                                route.doc_id,
                            )
                            in {
                                (
                                    str(hint.get("corpus_id") or ""),
                                    str(hint.get("doc_id") or ""),
                                )
                                for hint in grounded_route_rows.get(lane_id, [])
                            },
                        }
                        for route in values
                    ]
                    for lane_id, values in document_routes.items()
                }
        timings["document_routing"] = perf_counter() - routing_started

        tree_routing_started = perf_counter()
        summary_tree_routes: dict[str, list[Any]] = {}
        summary_tree_diagnostics: dict[str, Any] = {
            "enabled": False,
            "reason": "no_document_routes",
        }
        if document_routes:
            try:
                initial_tree_lane_ids = _tree_routing_lane_ids(lanes)
                summary_tree_routes, summary_tree_diagnostics = await asyncio.wait_for(
                    summary_tree_navigator.navigate(
                        lane_vectors={
                            lane.lane_id: vectors_by_lane_id.get(lane.lane_id)
                            for lane in lanes
                            if lane.lane_id in set(initial_tree_lane_ids)
                        },
                        document_routes=document_routes,
                        embedding_config=embedding_config,
                        qdrant_client=qdrant_client,
                    ),
                    timeout=_stage_timeout(
                        float(
                            getattr(
                                settings,
                                "QUERY_PLAN_TREE_ROUTING_DEADLINE_SECONDS",
                                6.0,
                            )
                        ),
                        reserve=1.0,
                    ),
                )
                summary_tree_diagnostics["initial_lane_ids"] = initial_tree_lane_ids
                summary_tree_diagnostics["advisory_lanes_use_document_routes"] = True
            except Exception as exc:
                failures.append(
                    {
                        "lane_id": "summary_tree_routing",
                        "retriever": "summary_tree",
                        "error": f"{type(exc).__name__}: {exc}"[:240],
                    }
                )
                summary_tree_diagnostics = {
                    "enabled": True,
                    "status": "degraded",
                    "reason": f"{type(exc).__name__}: {exc}"[:240],
                }

        # The hierarchy points already carry source-proven lexicon IDs. Resolve
        # only those bound cards, then execute novel corpus-native terms as
        # optional lanes. This is a deterministic second descent, not an LLM
        # rewrite and not another corpus-wide concept search.
        hierarchy_binding_diagnostics: dict[str, Any] = {
            "status": "skipped",
            "reason": "no_summary_tree_routes",
        }
        hierarchy_tree_routes = {
            lane_id: list(summary_tree_routes.get(lane_id) or [])
            for lane_id in required_lane_ids
            if summary_tree_routes.get(lane_id)
        }
        hierarchy_match_limit = min(
            4,
            max(2, len(hierarchy_tree_routes)) * max(1, len(corpus_ids)),
        )
        hierarchy_vocabulary_deadline_s = min(
            12.0,
            max(4.0, vocabulary_deadline_s * 0.8),
        )
        if hierarchy_tree_routes and qdrant_client is not None:
            try:
                (
                    hierarchy_matches,
                    hierarchy_binding_diagnostics,
                ) = await asyncio.wait_for(
                    hierarchy_bound_vocabulary_matches(
                        qdrant_client=qdrant_client,
                        summary_tree_routes=hierarchy_tree_routes,
                        lane_vectors=vectors_by_lane_id,
                        lane_queries={lane.lane_id: lane.query for lane in lanes},
                        existing_matches=list(
                            vocabulary_diagnostics.get("matches") or []
                        ),
                        disabled_lexicon_ids=disabled_lexicon_ids,
                        max_matches=hierarchy_match_limit,
                        max_per_lane_corpus=(
                            2 if len(hierarchy_tree_routes) == 1 else 1
                        ),
                    ),
                    timeout=_stage_timeout(
                        hierarchy_vocabulary_deadline_s,
                        reserve=1.0,
                    ),
                )
                hierarchy_binding_diagnostics[
                    "deadline_s"
                ] = hierarchy_vocabulary_deadline_s
                if hierarchy_matches:
                    existing_match_ids = {
                        str(row.get("lexicon_id") or "")
                        for row in (vocabulary_diagnostics.get("matches") or [])
                    }
                    novel_matches = [
                        row
                        for row in hierarchy_matches
                        if str(row.get("lexicon_id") or "") not in existing_match_ids
                    ]

                    matches_by_corpus: dict[str, list[dict[str, Any]]] = {}
                    for match in novel_matches:
                        match_corpus_id = str(match.get("corpus_id") or "")
                        if match_corpus_id:
                            matches_by_corpus.setdefault(match_corpus_id, []).append(
                                match
                            )
                    reference_results = await asyncio.gather(
                        *(
                            definition_reference_vocabulary_matches(
                                qdrant_client,
                                corpus_id=match_corpus_id,
                                seeds=corpus_matches,
                                disabled_lexicon_ids=disabled_lexicon_ids,
                                query_lanes=[
                                    {
                                        "lane_id": lane.lane_id,
                                        "query": lane.query or lane.dense_text,
                                    }
                                    for lane in lanes
                                ],
                                limit=2,
                            )
                            for match_corpus_id, corpus_matches in matches_by_corpus.items()
                        ),
                        return_exceptions=True,
                    )
                    hierarchy_reference_rejections: list[dict[str, Any]] = []
                    for result in reference_results:
                        if isinstance(result, BaseException):
                            continue
                        reference_matches, reference_rejections = result
                        for match in reference_matches:
                            lexicon_id = str(match.get("lexicon_id") or "")
                            if lexicon_id and lexicon_id not in existing_match_ids:
                                novel_matches.append(match)
                                existing_match_ids.add(lexicon_id)
                        hierarchy_reference_rejections.extend(reference_rejections)
                    hierarchy_binding_diagnostics["definition_reference_matches"] = sum(
                        1
                        for match in novel_matches
                        if str(match.get("applicability") or "")
                        == "corpus_definition_reference"
                    )
                    if hierarchy_reference_rejections:
                        vocabulary_diagnostics.setdefault(
                            "rejected_expansions", []
                        ).extend(hierarchy_reference_rejections[:8])
                    vocabulary_diagnostics[
                        "definition_reference_expansion_count"
                    ] = int(
                        vocabulary_diagnostics.get(
                            "definition_reference_expansion_count"
                        )
                        or 0
                    ) + int(
                        hierarchy_binding_diagnostics["definition_reference_matches"]
                    )
                    vocabulary_diagnostics.setdefault("matches", []).extend(
                        novel_matches
                    )
                    for match in novel_matches:
                        corpus_id = str(match.get("corpus_id") or "")
                        if not corpus_id:
                            continue
                        corpus_diagnostics = vocabulary_diagnostics.setdefault(
                            "per_corpus", {}
                        ).setdefault(corpus_id, {})
                        corpus_diagnostics.setdefault("matches", []).append(match)
                        corpus_diagnostics["match_count"] = len(
                            corpus_diagnostics.get("matches") or []
                        )
                        if (
                            str(match.get("applicability") or "")
                            == "corpus_definition_reference"
                        ):
                            corpus_diagnostics["definition_reference_match_count"] = (
                                int(
                                    corpus_diagnostics.get(
                                        "definition_reference_match_count"
                                    )
                                    or 0
                                )
                                + 1
                            )

                    # Route hints require document profiles. Reuse the already
                    # selected Tier-0 cards instead of issuing another store read.
                    profiles = vocabulary_diagnostics.setdefault(
                        "document_profiles", []
                    )
                    profile_keys = {
                        (
                            str(profile.get("corpus_id") or ""),
                            str(profile.get("doc_id") or ""),
                        )
                        for profile in profiles
                    }
                    for route_values in document_routes.values():
                        for route in route_values:
                            key = (str(route.corpus_id), str(route.doc_id))
                            if key in profile_keys:
                                continue
                            profiles.append(
                                {
                                    "corpus_id": str(route.corpus_id),
                                    "doc_id": str(route.doc_id),
                                    "title": str(route.title or ""),
                                    "summary": str(route.summary or ""),
                                    "concepts": list(route.concepts),
                                    "section_ids": list(route.section_ids),
                                    "node_type": "document",
                                    "store": "tier0_document_profile",
                                }
                            )
                            profile_keys.add(key)

                    all_grounded_lanes, hierarchy_expansion = grounded_vocabulary_lanes(
                        plan,
                        {"matches": novel_matches},
                        max_translation_lanes=hierarchy_match_limit,
                        max_translation_lanes_per_corpus=max(
                            1,
                            min(hierarchy_match_limit, len(hierarchy_tree_routes)),
                        ),
                    )
                    existing_lane_ids = {lane.lane_id for lane in lanes}
                    new_hierarchy_lanes = [
                        lane
                        for lane in all_grounded_lanes
                        if lane.lane_id not in existing_lane_ids
                    ]
                    if new_hierarchy_lanes:
                        new_vectors = await asyncio.wait_for(
                            embed_queries(
                                [lane.dense_text for lane in new_hierarchy_lanes],
                                embedding_config,
                            ),
                            timeout=_stage_timeout(
                                float(
                                    getattr(
                                        settings,
                                        "QUERY_PLAN_EMBED_DEADLINE_SECONDS",
                                        5.0,
                                    )
                                ),
                                reserve=1.0,
                            ),
                        )
                        lanes.extend(new_hierarchy_lanes)
                        vectors.extend(new_vectors)
                        vectors_by_lane_id.update(
                            {
                                lane.lane_id: vector
                                for lane, vector in zip(
                                    new_hierarchy_lanes,
                                    new_vectors,
                                    strict=True,
                                )
                            }
                        )

                        expansion = vocabulary_diagnostics.setdefault("expansion", {})
                        for field in (
                            "translation_lane_ids",
                            "step_back_lane_ids",
                            "introduced_lexicon_ids",
                        ):
                            expansion[field] = list(
                                dict.fromkeys(
                                    [
                                        *(expansion.get(field) or []),
                                        *(hierarchy_expansion.get(field) or []),
                                    ]
                                )
                            )
                        expansion.setdefault("lane_lexicon_ids", {}).update(
                            hierarchy_expansion.get("lane_lexicon_ids") or {}
                        )
                        expansion["required"] = False

                        route_hints = grounded_document_route_hints(
                            vocabulary_diagnostics,
                            expansion,
                        )
                        (
                            document_routes,
                            hierarchy_route_rows,
                        ) = merge_grounded_document_route_hints(
                            document_routes,
                            {
                                lane.lane_id: route_hints.get(lane.lane_id, [])
                                for lane in new_hierarchy_lanes
                            },
                            max_per_lane=document_route_limit,
                        )
                        hierarchy_binding_diagnostics["route_hints"] = sum(
                            len(values) for values in hierarchy_route_rows.values()
                        )
                        hierarchy_binding_diagnostics["new_lane_ids"] = [
                            lane.lane_id for lane in new_hierarchy_lanes
                        ]

                        (
                            second_tree_routes,
                            second_tree_diagnostics,
                        ) = await asyncio.wait_for(
                            summary_tree_navigator.navigate(
                                lane_vectors={
                                    lane.lane_id: vectors_by_lane_id.get(lane.lane_id)
                                    for lane in new_hierarchy_lanes
                                },
                                document_routes={
                                    lane.lane_id: document_routes.get(lane.lane_id, [])
                                    for lane in new_hierarchy_lanes
                                },
                                embedding_config=embedding_config,
                                qdrant_client=qdrant_client,
                            ),
                            timeout=_stage_timeout(
                                float(
                                    getattr(
                                        settings,
                                        "QUERY_PLAN_TREE_ROUTING_DEADLINE_SECONDS",
                                        6.0,
                                    )
                                ),
                                reserve=1.0,
                            ),
                        )
                        summary_tree_routes.update(second_tree_routes)
                        hierarchy_binding_diagnostics[
                            "second_tree"
                        ] = second_tree_diagnostics
                    vocabulary_diagnostics["status"] = "expanded"
            except Exception as exc:
                hierarchy_binding_diagnostics = {
                    "status": "degraded",
                    "reason": f"{type(exc).__name__}: {exc}"[:240],
                    "deadline_s": hierarchy_vocabulary_deadline_s,
                }
                failures.append(
                    {
                        "lane_id": "hierarchy_vocabulary",
                        "retriever": "hierarchy_bound_vocabulary",
                        "error": f"{type(exc).__name__}: {exc}"[:240],
                    }
                )
        vocabulary_diagnostics["hierarchy_binding"] = hierarchy_binding_diagnostics
        timings["summary_tree_routing"] = perf_counter() - tree_routing_started

        def _annotate_document_routes(
            chunks: list[SourceChunk], lane_id: str
        ) -> list[SourceChunk]:
            route_scores = {
                (str(route.corpus_id), str(route.doc_id)): float(route.score)
                for route in document_routes.get(lane_id, [])
            }
            if not route_scores:
                return chunks
            for chunk in chunks:
                route_score = route_scores.get(
                    (str(chunk.corpus_id or ""), str(chunk.doc_id or ""))
                )
                if route_score is None:
                    continue
                metadata = dict(chunk.metadata or {})
                routed_lanes = dict(metadata.get("document_route_lanes") or {})
                routed_lanes[lane_id] = max(
                    route_score,
                    float(routed_lanes.get(lane_id) or 0.0),
                )
                metadata["document_route_lanes"] = routed_lanes
                metadata["document_routed"] = True
                chunk.metadata = metadata
                marker = {
                    "retriever": "tier0_document_summary",
                    "lane_id": lane_id,
                    "route_score": round(route_score, 4),
                }
                provenance = list(chunk.provenance or [])
                if marker not in provenance:
                    provenance.append(marker)
                chunk.provenance = provenance
            return chunks

        requested_child_top_k = max(85, min(int(retrieval_k or 85), 128))
        requested_summary_top_k = max(20, min(int(top_k_summary or 24), 48))
        funnel_limits = adaptive_funnel_limits(
            intent,
            child_base=requested_child_top_k,
            summary_base=requested_summary_top_k,
        )
        child_top_k = max(32, min(funnel_limits.child_top_k, 120))
        summary_top_k = max(12, min(funnel_limits.summary_top_k, 48))
        lexical_top_k = max(12, min(child_top_k // 2, 32))
        if effective_tier == RetrievalTier.qdrant_only:
            # Focused retrieval remains Qdrant-only. It still uses the full
            # document/summary hierarchy and Qdrant dense+sparse fusion.
            lexical_top_k = 0

        async def _lane_pools(lane, vector) -> list[PlannedPool]:
            lane_started = perf_counter()
            routed_doc_refs = (
                [
                    (str(route.corpus_id), str(route.doc_id))
                    for route in document_routes.get(lane.lane_id, [])
                ]
                if lane.role == "core"
                else []
            )
            routed_doc_ids = [doc_id for _corpus_id, doc_id in routed_doc_refs]
            is_routed_core = bool(routed_doc_refs)
            tree_routes_for_lane = list(summary_tree_routes.get(lane.lane_id) or [])
            tree_parent_ids_by_doc = {
                (str(route.corpus_id), str(route.doc_id)): list(route.parent_ids)
                for route in tree_routes_for_lane
            }
            tree_parent_ids = list(
                dict.fromkeys(
                    parent_id
                    for route in tree_routes_for_lane
                    for parent_id in route.parent_ids
                )
            )
            if lane.role == "original":
                # The exact user wording is the recall safety lane. It remains
                # global and receives the configured broad first-stage budget;
                # derived probes may narrow through Tier-0 and the RAPTOR tree.
                lane_summary_top_k = requested_summary_top_k
                lane_lexical_top_k = lexical_top_k
                lane_child_top_k = requested_child_top_k
            else:
                lane_summary_top_k = summary_top_k if lane.role == "core" else 0
                lane_lexical_top_k = (
                    lexical_top_k if lane.role == "core" else min(2, lexical_top_k)
                )
                lane_child_top_k = (
                    child_top_k if lane.role == "core" else min(6, child_top_k)
                )
            fair_routed_documents = bool(lane.role == "core" and routed_doc_refs)

            async def _summary_candidates() -> list[SourceChunk]:
                if lane_summary_top_k <= 0 or vector is None:
                    return []
                if not fair_routed_documents:
                    return await funnel_a.search(
                        vector,
                        corpus_ids,
                        a_cols,
                        top_k=lane_summary_top_k,
                        fair_mode=True,
                        query_text=lane.query,
                        doc_ids=routed_doc_ids,
                        parent_ids=tree_parent_ids or None,
                    )
                per_document = max(
                    2,
                    (lane_summary_top_k + len(routed_doc_refs) - 1)
                    // len(routed_doc_refs),
                )
                rows = await asyncio.gather(
                    *(
                        funnel_a.search(
                            vector,
                            [route_corpus_id],
                            a_cols,
                            top_k=per_document,
                            fair_mode=True,
                            query_text=lane.query,
                            doc_ids=[doc_id],
                            parent_ids=(
                                tree_parent_ids_by_doc.get((route_corpus_id, doc_id))
                                or None
                            ),
                        )
                        for route_corpus_id, doc_id in routed_doc_refs
                    ),
                    return_exceptions=True,
                )
                chunks = [
                    chunk
                    for row in rows
                    if not isinstance(row, BaseException)
                    for chunk in (row or [])
                ]
                if not chunks:
                    error = next(
                        (row for row in rows if isinstance(row, BaseException)), None
                    )
                    if error is not None:
                        raise error
                return chunks

            async def _lexical_candidates() -> list[SourceChunk]:
                if lane_lexical_top_k <= 0:
                    return []
                if not fair_routed_documents:
                    return await lexical_retriever.search(
                        lane.query,
                        corpus_ids,
                        top_k=lane_lexical_top_k,
                        doc_ids=routed_doc_ids,
                    )
                per_document = max(
                    2,
                    (lane_lexical_top_k + len(routed_doc_refs) - 1)
                    // len(routed_doc_refs),
                )
                rows = await asyncio.gather(
                    *(
                        lexical_retriever.search(
                            lane.query,
                            [route_corpus_id],
                            top_k=per_document,
                            doc_ids=[doc_id],
                        )
                        for route_corpus_id, doc_id in routed_doc_refs
                    ),
                    return_exceptions=True,
                )
                chunks = [
                    chunk
                    for row in rows
                    if not isinstance(row, BaseException)
                    for chunk in (row or [])
                ]
                if not chunks:
                    error = next(
                        (row for row in rows if isinstance(row, BaseException)), None
                    )
                    if error is not None:
                        raise error
                return chunks

            summary_raw, lexical_raw = await asyncio.gather(
                (
                    asyncio.wait_for(
                        _summary_candidates(),
                        timeout=_stage_timeout(
                            float(
                                getattr(
                                    settings,
                                    "QUERY_PLAN_RETRIEVAL_DEADLINE_SECONDS",
                                    4.0,
                                )
                            ),
                            reserve=1.0,
                        ),
                    )
                    if lane_summary_top_k > 0 and vector is not None
                    else asyncio.sleep(0, result=[])
                ),
                (
                    asyncio.wait_for(
                        _lexical_candidates(),
                        timeout=_stage_timeout(
                            float(
                                getattr(
                                    settings,
                                    "QUERY_PLAN_RETRIEVAL_DEADLINE_SECONDS",
                                    4.0,
                                )
                            ),
                            reserve=1.0,
                        ),
                    )
                    if lane_lexical_top_k > 0
                    else asyncio.sleep(0, result=[])
                ),
                return_exceptions=True,
            )
            summary_chunks = (
                []
                if isinstance(summary_raw, BaseException)
                else list(summary_raw or [])
            )
            lexical_chunks = (
                []
                if isinstance(lexical_raw, BaseException)
                else list(lexical_raw or [])
            )
            if isinstance(summary_raw, BaseException):
                failures.append(
                    {
                        "lane_id": lane.lane_id,
                        "retriever": "summary",
                        "error": f"{type(summary_raw).__name__}: {summary_raw}"[:240],
                    }
                )
            if isinstance(lexical_raw, BaseException):
                failures.append(
                    {
                        "lane_id": lane.lane_id,
                        "retriever": "lexical",
                        "error": f"{type(lexical_raw).__name__}: {lexical_raw}"[:240],
                    }
                )
            routed_parent_ids = list(
                dict.fromkeys(
                    str(chunk.parent_id)
                    for chunk in summary_chunks
                    if str(chunk.parent_id or "").strip()
                )
            )
            child_raw: list[SourceChunk] | BaseException = []
            if vector is not None:
                try:
                    if fair_routed_documents:
                        per_document = max(
                            4,
                            (lane_child_top_k + len(routed_doc_refs) - 1)
                            // len(routed_doc_refs),
                        )
                        child_rows = await asyncio.wait_for(
                            asyncio.gather(
                                *(
                                    funnel_b.search(
                                        vector,
                                        [route_corpus_id],
                                        b_cols,
                                        top_k=per_document,
                                        query_text=lane.query,
                                        doc_ids=[doc_id],
                                        parent_ids=[
                                            str(chunk.parent_id)
                                            for chunk in summary_chunks
                                            if str(chunk.corpus_id or "")
                                            == route_corpus_id
                                            and str(chunk.doc_id or "") == str(doc_id)
                                            and str(chunk.parent_id or "").strip()
                                        ]
                                        or tree_parent_ids_by_doc.get(
                                            (route_corpus_id, doc_id)
                                        )
                                        or None,
                                    )
                                    for route_corpus_id, doc_id in routed_doc_refs
                                ),
                                return_exceptions=True,
                            ),
                            timeout=_stage_timeout(
                                float(
                                    getattr(
                                        settings,
                                        "QUERY_PLAN_RETRIEVAL_DEADLINE_SECONDS",
                                        4.0,
                                    )
                                ),
                                reserve=0.75,
                            ),
                        )
                        child_raw = [
                            chunk
                            for row in child_rows
                            if not isinstance(row, BaseException)
                            for chunk in (row or [])
                        ]
                        if not child_raw:
                            error = next(
                                (
                                    row
                                    for row in child_rows
                                    if isinstance(row, BaseException)
                                ),
                                None,
                            )
                            if error is not None:
                                raise error
                    else:
                        child_raw = await asyncio.wait_for(
                            funnel_b.search(
                                vector,
                                corpus_ids,
                                b_cols,
                                top_k=lane_child_top_k,
                                query_text=lane.query,
                                doc_ids=routed_doc_ids,
                                parent_ids=(
                                    (routed_parent_ids or tree_parent_ids)
                                    if is_routed_core
                                    else None
                                ),
                            ),
                            timeout=_stage_timeout(
                                float(
                                    getattr(
                                        settings,
                                        "QUERY_PLAN_RETRIEVAL_DEADLINE_SECONDS",
                                        4.0,
                                    )
                                ),
                                reserve=0.75,
                            ),
                        )
                    if is_routed_core and routed_parent_ids and not child_raw:
                        fallback_rows = await asyncio.gather(
                            *(
                                funnel_b.search(
                                    vector,
                                    [route_corpus_id],
                                    b_cols,
                                    top_k=min(8, lane_child_top_k),
                                    query_text=lane.query,
                                    doc_ids=[doc_id],
                                )
                                for route_corpus_id, doc_id in routed_doc_refs
                            )
                        )
                        child_raw = [
                            chunk for row in fallback_rows for chunk in (row or [])
                        ]
                except Exception as exc:
                    child_raw = exc

            retrievers = ("dense", "summary", "lexical")
            raw_results = (child_raw, summary_chunks, lexical_chunks)
            pools: list[PlannedPool] = []
            counts: dict[str, int] = {}
            for retriever_name, raw in zip(retrievers, raw_results):
                if isinstance(raw, BaseException):
                    failures.append(
                        {
                            "lane_id": lane.lane_id,
                            "retriever": retriever_name,
                            "error": f"{type(raw).__name__}: {raw}"[:240],
                        }
                    )
                    chunks: list[SourceChunk] = []
                else:
                    chunks = list(raw or [])
                chunks = _annotate_document_routes(chunks, lane.lane_id)
                counts[retriever_name] = len(chunks)
                pools.append(
                    PlannedPool(
                        lane_id=lane.lane_id,
                        retriever=retriever_name,
                        chunks=tuple(chunks),
                        # Fusion protects one exact-query candidate before the
                        # rerank cap. Semantic answerability still counts only
                        # required core lanes via required_lane_ids above.
                        required=(
                            lane.role == "original"
                            or (lane.role == "core" and lane.required)
                        ),
                        anchor_phrase=lane.phrase,
                        anchor_phrases=tuple(lane.support_phrases),
                        anchor_terms=tuple(lane.lexical_terms),
                    )
                )
            lane_diagnostics.append(
                {
                    "lane_id": lane.lane_id,
                    "role": lane.role,
                    "query": lane.query,
                    "routed_doc_ids": routed_doc_ids or [],
                    "routed_parent_ids": routed_parent_ids,
                    "tree_parent_ids": tree_parent_ids,
                    "descent": (
                        "document_tree_parent_child_fair"
                        if fair_routed_documents and tree_parent_ids
                        else (
                            "document_parent_child_fair"
                            if fair_routed_documents and routed_parent_ids
                            else (
                                "document_tree_parent_child"
                                if is_routed_core and tree_parent_ids
                                else (
                                    "document_parent_child"
                                    if is_routed_core and routed_parent_ids
                                    else (
                                        "document_child_fallback"
                                        if is_routed_core
                                        else "global_wildcard"
                                    )
                                )
                            )
                        )
                    ),
                    "counts": counts,
                    "duration_s": round(perf_counter() - lane_started, 3),
                }
            )
            return pools

        candidate_started = perf_counter()
        lane_pool_groups = await asyncio.gather(
            *[_lane_pools(lane, vector) for lane, vector in zip(lanes, vectors)]
        )
        pools = [pool for group in lane_pool_groups for pool in group]
        timings["candidate_generation"] = perf_counter() - candidate_started

        routed_document_count = int(
            document_routing_diagnostics.get("routed_doc_count") or 0
        )
        planned_final_top_k = _planned_final_result_limit(
            plan=plan,
            intent=intent,
            tier=effective_tier,
            requested_limit=int(final_top_k or settings.DEFAULT_RETRIEVAL_K),
            routed_document_count=routed_document_count,
        )
        rerank_cap = (
            int(getattr(settings, "QUERY_PLAN_GRAPH_RERANK_CANDIDATES", 80))
            if effective_tier == RetrievalTier.qdrant_mongo_graph
            else (
                int(getattr(settings, "QUERY_PLAN_HYBRID_RERANK_CANDIDATES", 64))
                if effective_tier == RetrievalTier.qdrant_mongo
                else int(getattr(settings, "QUERY_PLAN_FAST_RERANK_CANDIDATES", 48))
            )
        )
        rerank_cap = _planned_rerank_candidate_limit(
            plan=plan,
            intent=intent,
            tier=effective_tier,
            configured_limit=rerank_cap,
            final_top_k=planned_final_top_k,
        )
        if rerank_top_n is not None:
            if quality_first:
                rerank_cap = max(
                    rerank_cap,
                    min(128, max(1, int(rerank_top_n))),
                )
            else:
                rerank_cap = min(rerank_cap, max(1, int(rerank_top_n)))
        fused, fusion_diagnostics = fuse_planned_pools(
            pools,
            max_candidates=rerank_cap,
            corpus_ids=corpus_ids,
        )
        if similarity_threshold is not None and similarity_threshold > 0.0:
            before_threshold = len(fused)
            fused = [
                chunk
                for chunk in fused
                if float(chunk.score or 0.0) >= similarity_threshold
            ]
            fusion_diagnostics["similarity_threshold"] = {
                "value": float(similarity_threshold),
                "before": before_threshold,
                "after": len(fused),
            }

        facts: list[SourceFact] = []
        if effective_tier == RetrievalTier.qdrant_mongo_graph and fused:
            graph_started = perf_counter()
            try:
                from services.conversation import conversation_service

                facts_task = self._retrieve_graph_seed_facts(
                    plan.standalone_query,
                    corpus_ids,
                    fact_seed_limit=fact_seed_limit,
                )
                expansion_task = mode_a_expansion.expand(
                    fused,
                    corpus_ids,
                    limit=min(
                        (
                            max(0, int(neo4j_expansion_cap))
                            if neo4j_expansion_cap is not None
                            else 12
                        ),
                        rerank_cap,
                    ),
                    db=conversation_service._db,
                    query=plan.standalone_query,
                )
                facts_raw, expansion_raw = await asyncio.wait_for(
                    asyncio.gather(facts_task, expansion_task, return_exceptions=True),
                    timeout=_stage_timeout(
                        float(
                            getattr(settings, "QUERY_PLAN_GRAPH_DEADLINE_SECONDS", 4.0)
                        ),
                        reserve=1.0,
                    ),
                )
                if not isinstance(facts_raw, BaseException):
                    facts = list(facts_raw or [])
                if isinstance(expansion_raw, BaseException):
                    failures.append(
                        {
                            "lane_id": "graph",
                            "retriever": "graph",
                            "error": f"{type(expansion_raw).__name__}: {expansion_raw}"[
                                :240
                            ],
                        }
                    )
                    graph_chunks = []
                else:
                    graph_chunks = [
                        chunk
                        for chunk in list(expansion_raw or [])
                        if any(
                            str(item.get(key) or "").strip()
                            for item in (chunk.provenance or [])
                            if isinstance(item, dict)
                            for key in (
                                "entity",
                                "entity_id",
                                "predicate",
                                "relation_family",
                                "bridge_type",
                                "evidence_phrase",
                            )
                        )
                    ]
                fact_chunks = _fact_seed_chunks(facts)
                for lane in lanes:
                    if lane.role != "core":
                        continue
                    annotate_planned_lane_grounding(
                        graph_chunks,
                        lane_id=lane.lane_id,
                        anchor_phrase=lane.phrase,
                        anchor_phrases=tuple(lane.support_phrases),
                        anchor_terms=tuple(lane.lexical_terms),
                    )
                    annotate_planned_lane_grounding(
                        fact_chunks,
                        lane_id=lane.lane_id,
                        anchor_phrase=lane.phrase,
                        anchor_phrases=tuple(lane.support_phrases),
                        anchor_terms=tuple(lane.lexical_terms),
                    )
                required_base_pools = [
                    PlannedPool(
                        lane.lane_id,
                        "dense",
                        tuple(
                            chunk
                            for chunk in fused
                            if lane.lane_id
                            in set((chunk.metadata or {}).get("planned_lanes") or [])
                        ),
                        required=True,
                    )
                    for lane in lanes
                    if lane.role == "core"
                ]
                graph_pools = [
                    PlannedPool("graph", "graph", tuple(graph_chunks)),
                    PlannedPool("graph_facts", "graph", tuple(fact_chunks)),
                ]
                fused, graph_fusion = fuse_planned_pools(
                    [
                        PlannedPool("base", "dense", tuple(fused)),
                        *required_base_pools,
                        *graph_pools,
                    ],
                    max_candidates=rerank_cap,
                    corpus_ids=corpus_ids,
                )
                fusion_diagnostics["graph"] = graph_fusion
            except asyncio.TimeoutError:
                failures.append(
                    {
                        "lane_id": "graph",
                        "retriever": "graph",
                        "error": "deadline_exceeded",
                    }
                )
            timings["graph"] = perf_counter() - graph_started

        supported_before_repair = grounded_planned_lane_ids(
            fused,
            required_lane_ids,
        )
        missing_before_repair = [
            lane_id
            for lane_id in required_lane_ids
            if lane_id not in set(supported_before_repair)
        ]
        repair_diagnostics["missing_lane_ids_before"] = missing_before_repair
        if (
            missing_before_repair
            and int(plan.max_repair_rounds) > 0
            and (quality_first or _budget_remaining() > 0.4)
        ):
            repair_started = perf_counter()
            repair_diagnostics["attempted_rounds"] = 1
            lanes_by_id = {lane.lane_id: lane for lane in lanes}

            async def _repair_lane(lane_id: str) -> list[PlannedPool]:
                lane = lanes_by_id[lane_id]
                vector = vectors_by_lane_id.get(lane_id)
                routed_doc_refs = [
                    (str(route.corpus_id), str(route.doc_id))
                    for route in document_routes.get(lane_id, [])
                ]
                retrievers = ("dense", "summary", "lexical")

                async def _repair_dense() -> list[SourceChunk]:
                    if vector is None:
                        return []
                    if not routed_doc_refs:
                        return await funnel_b.search(
                            vector,
                            corpus_ids,
                            b_cols,
                            top_k=min(child_top_k, 12),
                            query_text=lane.query,
                        )
                    rows = await asyncio.gather(
                        *(
                            funnel_b.search(
                                vector,
                                [route_corpus_id],
                                b_cols,
                                top_k=min(child_top_k, 12),
                                query_text=lane.query,
                                doc_ids=[doc_id],
                            )
                            for route_corpus_id, doc_id in routed_doc_refs
                        )
                    )
                    return [chunk for row in rows for chunk in (row or [])]

                async def _repair_summary() -> list[SourceChunk]:
                    if summary_top_k <= 0 or vector is None:
                        return []
                    if not routed_doc_refs:
                        return await funnel_a.search(
                            vector,
                            corpus_ids,
                            a_cols,
                            top_k=min(summary_top_k, 8),
                            fair_mode=True,
                            query_text=lane.query,
                        )
                    rows = await asyncio.gather(
                        *(
                            funnel_a.search(
                                vector,
                                [route_corpus_id],
                                a_cols,
                                top_k=min(summary_top_k, 8),
                                fair_mode=True,
                                query_text=lane.query,
                                doc_ids=[doc_id],
                            )
                            for route_corpus_id, doc_id in routed_doc_refs
                        )
                    )
                    return [chunk for row in rows for chunk in (row or [])]

                async def _repair_lexical() -> list[SourceChunk]:
                    if lexical_top_k <= 0:
                        return []
                    if not routed_doc_refs:
                        return await lexical_retriever.search(
                            lane.query,
                            corpus_ids,
                            top_k=min(16, max(8, lexical_top_k * 2)),
                        )
                    rows = await asyncio.gather(
                        *(
                            lexical_retriever.search(
                                lane.query,
                                [route_corpus_id],
                                top_k=min(16, max(8, lexical_top_k * 2)),
                                doc_ids=[doc_id],
                            )
                            for route_corpus_id, doc_id in routed_doc_refs
                        )
                    )
                    return [chunk for row in rows for chunk in (row or [])]

                try:
                    raw_results = await asyncio.wait_for(
                        asyncio.gather(
                            _repair_dense(),
                            _repair_summary(),
                            _repair_lexical(),
                            return_exceptions=True,
                        ),
                        timeout=_stage_timeout(
                            float(
                                getattr(
                                    settings,
                                    "QUERY_PLAN_REPAIR_DEADLINE_SECONDS",
                                    1.25,
                                )
                            ),
                            reserve=0.3,
                        ),
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "lane_id": lane_id,
                            "retriever": "bounded_repair",
                            "error": f"{type(exc).__name__}: {exc}"[:240],
                        }
                    )
                    raw_results = ([], [], [])

                repair_pools: list[PlannedPool] = []
                for retriever_name, raw in zip(retrievers, raw_results):
                    if isinstance(raw, BaseException):
                        failures.append(
                            {
                                "lane_id": lane_id,
                                "retriever": f"bounded_repair_{retriever_name}",
                                "error": f"{type(raw).__name__}: {raw}"[:240],
                            }
                        )
                        chunks: list[SourceChunk] = []
                    else:
                        chunks = list(raw or [])
                    chunks = _annotate_document_routes(chunks, lane_id)
                    repair_pools.append(
                        PlannedPool(
                            lane_id=lane_id,
                            retriever=retriever_name,
                            chunks=tuple(chunks),
                            required=True,
                            anchor_phrase=lane.phrase,
                            anchor_phrases=tuple(lane.support_phrases),
                            anchor_terms=tuple(lane.lexical_terms),
                        )
                    )
                return repair_pools

            repair_pool_groups = await asyncio.gather(
                *[_repair_lane(lane_id) for lane_id in missing_before_repair]
            )
            repair_pools = [pool for group in repair_pool_groups for pool in group]
            repair_diagnostics["added_candidates"] = sum(
                len(pool.chunks) for pool in repair_pools
            )
            if any(pool.chunks for pool in repair_pools):
                fused, repair_fusion = fuse_planned_pools(
                    [PlannedPool("base", "dense", tuple(fused)), *repair_pools],
                    max_candidates=rerank_cap,
                    corpus_ids=corpus_ids,
                )
                fusion_diagnostics["repair"] = repair_fusion
            timings["repair"] = perf_counter() - repair_started

        identity_started = perf_counter()
        if effective_tier != RetrievalTier.qdrant_only:
            try:
                fused = await asyncio.wait_for(
                    attach_document_identities(fused, corpus_ids),
                    timeout=_stage_timeout(
                        float(
                            getattr(
                                settings, "QUERY_PLAN_IDENTITY_DEADLINE_SECONDS", 2.0
                            )
                        ),
                        reserve=0.8,
                    ),
                )
            except Exception as exc:
                failures.append(
                    {
                        "lane_id": "final",
                        "retriever": "document_identity",
                        "error": f"{type(exc).__name__}: {exc}"[:240],
                    }
                )
        fused, duplicate_count = dedupe_cross_corpus_evidence(fused)
        pre_rerank_document_drops = 0
        pre_rerank_max_per_document = (
            6 if plan.complexity in {"compositional", "dependent_multi_hop"} else 4
        )
        if intent.need == QueryNeed.BROAD:
            fused, pre_rerank_document_drops = limit_candidates_per_document(
                fused,
                max_candidates=rerank_cap,
                max_per_document=pre_rerank_max_per_document,
                required_lane_ids=required_lane_ids,
                preferred_route_lane_ids=answer_lane_ids,
                protected_lane_ids=protected_lane_ids,
            )
        fused = fused[:rerank_cap]
        fusion_diagnostics["pre_rerank_document_cap"] = {
            "enabled": intent.need == QueryNeed.BROAD,
            "max_per_document": (
                pre_rerank_max_per_document if intent.need == QueryNeed.BROAD else None
            ),
            "dropped": pre_rerank_document_drops,
            "selected_candidates": len(fused),
        }
        timings["identity_dedupe"] = perf_counter() - identity_started

        rerank_started = perf_counter()
        if effective_tier != RetrievalTier.qdrant_only:
            try:
                fused = await asyncio.wait_for(
                    hydrate_rerank_texts(fused, corpus_ids),
                    timeout=_stage_timeout(
                        float(
                            getattr(
                                settings, "QUERY_PLAN_HYDRATE_DEADLINE_SECONDS", 2.0
                            )
                        ),
                        reserve=0.6,
                    ),
                )
            except Exception as exc:
                failures.append(
                    {
                        "lane_id": "final",
                        "retriever": "rerank_text_hydration",
                        "error": f"{type(exc).__name__}: {exc}"[:240],
                    }
                )
        fused, hydrated_duplicate_count = dedupe_cross_corpus_evidence(fused)
        separator_only_count = sum(
            1 for chunk in fused if is_separator_only_text(chunk.text)
        )
        if separator_only_count:
            fused = [chunk for chunk in fused if not is_separator_only_text(chunk.text)]
        fusion_diagnostics["structural_noise_filter"] = {
            "separator_only_dropped": separator_only_count,
            "remaining_candidates": len(fused),
        }
        reranker_diagnostics: dict[str, Any]
        if rerank_enabled and fused:
            try:
                ranked = await asyncio.wait_for(
                    reranker_service.rerank(curation_query, fused),
                    timeout=_stage_timeout(
                        float(
                            getattr(settings, "QUERY_PLAN_RERANK_DEADLINE_SECONDS", 6.0)
                        ),
                        reserve=0.35,
                    ),
                )
                reranker_diagnostics = reranker_service.diagnostics()
            except Exception as exc:
                failures.append(
                    {
                        "lane_id": "final",
                        "retriever": "reranker",
                        "error": f"{type(exc).__name__}: {exc}"[:240],
                    }
                )
                ranked = fused
                reranker_diagnostics = {
                    "status": "deadline_fallback_rank_fusion",
                    "fallback": True,
                    "candidate_count": len(fused),
                    "error": f"{type(exc).__name__}: {exc}"[:240],
                }
        else:
            ranked = fused
            reranker_diagnostics = {
                "status": "skipped_by_request",
                "candidate_count": len(fused),
            }
        timings["rerank"] = perf_counter() - rerank_started

        ranked = apply_query_grounding(
            ranked,
            query=curation_query,
            tier=effective_tier,
            score_scale=settings.RERANKER_SCORE_SCALE,
        )
        translation_lane_targets = grounded_translation_lane_targets(
            vocabulary_diagnostics,
            vocabulary_diagnostics.get("expansion") or {},
            required_lane_ids=required_lane_ids,
        )
        vocabulary_diagnostics[
            "required_lane_propagation"
        ] = propagate_grounded_lane_aliases(ranked, translation_lane_targets)
        ranked, grounding_filter_diagnostics = filter_grounded_planned_candidates(
            ranked,
            required_lane_ids,
            selected_corpus_ids=corpus_ids,
            protected_lane_ids=protected_lane_ids,
        )
        diversity = select_with_diversity(
            ranked,
            final_top_k=planned_final_top_k,
            intent=intent,
            tier=effective_tier,
            multi_corpus=bool(corpus_ids and len(corpus_ids) > 1),
            selected_corpus_ids=corpus_ids or [],
            query=plan.standalone_query,
        )
        final_limit = planned_final_top_k
        preferred_candidates = diversity.candidates
        enumeration_diagnostics: dict[str, object] = {"applied": False}
        if answer_lane_ids:
            (
                preferred_candidates,
                enumeration_diagnostics,
            ) = prioritize_enumeration_candidates(
                ranked,
                diversity.candidates,
                answer_lane_ids=answer_lane_ids,
                required_lane_ids=required_lane_ids,
                max_candidates=final_limit,
            )
        route_count = routed_document_count
        if answer_lane_ids or intent.need == QueryNeed.BROAD:
            routed_document_budget = min(route_count, max(2, final_limit // 2))
        elif intent.need == QueryNeed.SPECIFIC:
            routed_document_budget = min(route_count, 2)
        else:
            routed_document_budget = min(route_count, max(2, final_limit // 3))
        finalist_candidates, reservation_diagnostics = reserve_planned_finalists(
            ranked,
            preferred_candidates,
            required_lane_ids=required_lane_ids,
            corpus_ids=corpus_ids,
            max_candidates=final_limit,
            max_per_document=(
                1 if intent.need == QueryNeed.BROAD and not answer_lane_ids else None
            ),
            routed_document_budget=routed_document_budget,
            preferred_route_lane_ids=answer_lane_ids,
            protected_lane_ids=protected_lane_ids,
        )
        hydrate_started = perf_counter()
        if effective_tier == RetrievalTier.qdrant_only:
            finalists = [
                chunk for chunk in finalist_candidates if str(chunk.text or "").strip()
            ]
        else:
            try:
                finalists = await asyncio.wait_for(
                    hydrate_chunks(
                        finalist_candidates, corpus_ids, query=plan.standalone_query
                    ),
                    timeout=_stage_timeout(
                        float(
                            getattr(
                                settings, "QUERY_PLAN_HYDRATE_DEADLINE_SECONDS", 2.0
                            )
                        )
                    ),
                )
            except Exception as exc:
                failures.append(
                    {
                        "lane_id": "final",
                        "retriever": "finalist_hydration",
                        "error": f"{type(exc).__name__}: {exc}"[:240],
                    }
                )
                finalists = [
                    chunk
                    for chunk in finalist_candidates
                    if str(chunk.text or "").strip()
                ]
        if plan.answer_shape == "enumeration":
            finalists, enumeration_duplicate_count = dedupe_enumeration_finalists(
                finalists,
                answer_lane_ids=answer_lane_ids,
            )
            finalists = order_enumeration_finalists(
                finalists,
                answer_lane_ids=answer_lane_ids,
            )
            parent_duplicate_count = enumeration_duplicate_count
            document_lane_duplicate_count = 0
        else:
            finalists, parent_duplicate_count = dedupe_parent_finalists(finalists)
            finalists, document_lane_duplicate_count = dedupe_document_lane_finalists(
                finalists
            )
        timings["hydrate_finalists"] = perf_counter() - hydrate_started
        supported_lane_ids = grounded_planned_lane_ids(
            finalists,
            required_lane_ids,
        )
        required_lane_coverage = (
            len(supported_lane_ids) / len(required_lane_ids)
            if required_lane_ids
            else 1.0
        )
        selection_diagnostics = dict(diversity.diagnostics or {})
        scoring_sufficiency = selection_diagnostics.get("sufficiency")
        supported_lane_set = set(supported_lane_ids)
        # P0.4 — separate lane COVERAGE (telemetry; includes the synthetic
        # fallback probe) from ANSWERABILITY (the refusal signal; never keyed
        # to internal plumbing). The synthetic fallback lane may drive
        # retrieval and repair, but an undecomposed query must be judged by
        # evidence-atom sufficiency, not by whether one catch-all lane cleared
        # a grounding threshold.
        refusal_lane_ids = [
            lane_id for lane_id in required_lane_ids if lane_id != FALLBACK_PROBE_ID
        ]
        synthetic_lane_ids = [
            lane_id for lane_id in required_lane_ids if lane_id == FALLBACK_PROBE_ID
        ]
        selection_diagnostics["lane_coverage"] = {
            "required_lane_ids": required_lane_ids,
            "supported_lane_ids": supported_lane_ids,
            "coverage": round(required_lane_coverage, 4),
            "synthetic_lane_ids": synthetic_lane_ids,
        }
        if isinstance(scoring_sufficiency, dict):
            selection_diagnostics["scoring_sufficiency"] = scoring_sufficiency
        if refusal_lane_ids:
            required_lane_atoms = [f"concept:{lane_id}" for lane_id in refusal_lane_ids]
            covered_lane_atoms = [
                f"concept:{lane_id}"
                for lane_id in refusal_lane_ids
                if lane_id in supported_lane_set
            ]
            missing_lane_atoms = [
                f"concept:{lane_id}"
                for lane_id in refusal_lane_ids
                if lane_id not in supported_lane_set
            ]
            refusal_coverage = len(covered_lane_atoms) / len(required_lane_atoms)
            selection_diagnostics["sufficiency"] = {
                "required_atoms": required_lane_atoms,
                "covered_required_atoms": covered_lane_atoms,
                "missing_atoms": missing_lane_atoms,
                "missing_critical_atoms": missing_lane_atoms,
                "required_coverage": round(refusal_coverage, 4),
                "answerable": bool(finalists) and refusal_coverage >= 1.0,
                "source": "query_plan_required_lanes",
            }
        elif isinstance(scoring_sufficiency, dict):
            # Undecomposed query: judge by the strict evidence-atom gate that
            # already drove sufficiency repair, so negative controls still
            # fail closed while grounded broad questions answer.
            selection_diagnostics["sufficiency"] = {
                "required_atoms": list(scoring_sufficiency.get("required_atoms") or []),
                "covered_required_atoms": list(
                    scoring_sufficiency.get("covered_required_atoms") or []
                ),
                "missing_atoms": list(scoring_sufficiency.get("missing_atoms") or []),
                "missing_critical_atoms": list(
                    scoring_sufficiency.get("missing_critical_atoms") or []
                ),
                "required_coverage": float(
                    scoring_sufficiency.get("required_coverage") or 0.0
                ),
                "answerable": bool(finalists)
                and bool(scoring_sufficiency.get("answerable")),
                "source": "evidence_atom_sufficiency",
            }
        else:
            selection_diagnostics["sufficiency"] = {
                "required_atoms": [],
                "covered_required_atoms": [],
                "missing_atoms": [],
                "missing_critical_atoms": [],
                "required_coverage": 1.0 if finalists else 0.0,
                "answerable": bool(finalists),
                "source": "evidence_presence",
            }
        corpus_distribution: dict[str, int] = {}
        document_distribution: dict[str, int] = {}
        predicates_used: set[str] = set()
        for chunk in finalists:
            corpus_key = str(chunk.corpus_id or "unknown")
            document_key = (
                f"{chunk.corpus_id}|{chunk.doc_id}"
                if chunk.corpus_id and chunk.doc_id
                else str(chunk.doc_id or "unknown")
            )
            corpus_distribution[corpus_key] = corpus_distribution.get(corpus_key, 0) + 1
            document_distribution[document_key] = (
                document_distribution.get(document_key, 0) + 1
            )
            for item in chunk.provenance or []:
                if isinstance(item, dict) and str(item.get("predicate") or "").strip():
                    predicates_used.add(str(item["predicate"]).strip())
        reranker_diagnostics = dict(reranker_diagnostics)
        reranker_diagnostics["execution_s"] = round(timings.get("rerank", 0.0), 3)
        total_s = perf_counter() - started
        final_source_tiers: dict[str, int] = {}
        for chunk in finalists:
            source_tier = str(chunk.source_tier or "unknown")
            final_source_tiers[source_tier] = final_source_tiers.get(source_tier, 0) + 1

        def _distinct_docs(chunks: list[SourceChunk]) -> int:
            return len(
                {
                    (str(chunk.corpus_id or ""), str(chunk.doc_id))
                    for chunk in chunks
                    if chunk.doc_id
                }
            )

        max_doc_share_final = (
            round(max(document_distribution.values()) / len(finalists), 4)
            if finalists and document_distribution
            else 0.0
        )
        planned_counts = {
            "funnel_a": sum(
                len(pool.chunks) for pool in pools if pool.retriever == "summary"
            ),
            "funnel_b": sum(
                len(pool.chunks) for pool in pools if pool.retriever == "dense"
            ),
            "lexical": sum(
                len(pool.chunks) for pool in pools if pool.retriever == "lexical"
            ),
            "document_anchor": sum(
                len(pool.chunks)
                for pool in pools
                if pool.retriever == "document_anchor"
            ),
            "document_routes": int(
                document_routing_diagnostics.get("routed_doc_count") or 0
            ),
            "facts": len(facts),
            "fact_seed_chunks": sum(
                1 for chunk in fused if str(chunk.source_tier or "") == "fact_seed"
            ),
            "graph_expanded": sum(
                1
                for chunk in fused
                if str(chunk.source_tier or "") in {"graph", "graph_expanded"}
            ),
            "merged_initial": int(
                fusion_diagnostics.get("unique_candidates") or len(fused)
            ),
            "ranked": len(ranked),
            "ranked_query_grounded": len(ranked),
            "distinct_docs_merged": _distinct_docs(fused),
            "distinct_docs_in_pool": _distinct_docs(ranked),
        }
        diagnostics = {
            "status": "query_plan_v2_degraded" if failures else "query_plan_v2",
            "query_plan_version": plan.version,
            "complexity": plan.complexity,
            "answer_shape": plan.answer_shape,
            "constraints": [
                {
                    "constraint_id": constraint.constraint_id,
                    "operator": constraint.operator,
                    "kind": constraint.kind,
                    "text": constraint.text,
                    "terms": list(constraint.terms),
                }
                for constraint in plan.constraints
            ],
            "execution_batches": [
                list(batch) for batch in query_plan_execution_batches(plan)
            ],
            "original_query": plan.original_query,
            "standalone_query": plan.standalone_query,
            "curation_query": curation_query,
            "probes": [
                {
                    "probe_id": probe.probe_id,
                    "question": probe.question,
                    "answer_type": probe.answer_type,
                    "role": probe.role,
                    "required": probe.required,
                    "concepts": list(probe.concepts),
                    "constraints": list(probe.constraints),
                    "depends_on": list(probe.depends_on),
                }
                for probe in plan.probes
            ],
            "detected_phrases": list(plan.concepts),
            "detected_entities": list(plan.concepts),
            "requested_tier": getattr(retrieval_tier, "value", retrieval_tier),
            "effective_tier": getattr(effective_tier, "value", effective_tier),
            "store_contract": _retrieval_store_contract(effective_tier),
            "search_mode": search_mode,
            "intent": {
                "need": getattr(intent.need, "value", intent.need),
                "broad_score": intent.broad_score,
                "specific_score": intent.specific_score,
                "child_ratio": intent.child_ratio,
                "summary_ratio": intent.summary_ratio,
            },
            "limits": {
                "child_top_k": child_top_k,
                "summary_top_k": summary_top_k,
                "requested_final_top_k": int(
                    final_top_k or settings.DEFAULT_RETRIEVAL_K
                ),
                "final_top_k": planned_final_top_k,
                "rerank_candidates": rerank_cap,
                "rerank_enabled": bool(rerank_enabled),
                "similarity_threshold": similarity_threshold,
                "neo4j_expansion_cap": neo4j_expansion_cap,
                "max_corpora_per_query": max_corpora_per_query,
            },
            "counts": planned_counts,
            "dropped_corpus_ids": dropped,
            "lanes": lane_diagnostics,
            "lane_failures": failures,
            "fusion": fusion_diagnostics,
            "dedupe": {
                "pre_hydration": duplicate_count,
                "post_hydration": hydrated_duplicate_count,
                "parent_finalists": parent_duplicate_count,
                "document_lanes": document_lane_duplicate_count,
            },
            "reranker": reranker_diagnostics,
            "selection": selection_diagnostics,
            "enumeration_selection": enumeration_diagnostics,
            "grounding_filter": grounding_filter_diagnostics,
            "reservations": reservation_diagnostics,
            "cache": {"hit": False, "key_version": "retrieval_v2"},
            "required_concept_coverage": {
                # Refusal-relevant lanes only: the synthetic fallback probe is
                # retrieval plumbing and must not become a chat-gate critical
                # atom (P0.4). Full lane telemetry incl. the synthetic lane
                # lives in selection.lane_coverage.
                "required_lane_ids": refusal_lane_ids,
                "supported_lane_ids": [
                    lane_id
                    for lane_id in supported_lane_ids
                    if lane_id != FALLBACK_PROBE_ID
                ],
                "coverage": round(
                    (
                        sum(
                            1
                            for lane_id in refusal_lane_ids
                            if lane_id in supported_lane_set
                        )
                        / len(refusal_lane_ids)
                    )
                    if refusal_lane_ids
                    else 1.0,
                    3,
                ),
                "synthetic_lane_ids": synthetic_lane_ids,
            },
            "protected_recall_lanes": protected_lane_ids,
            "repair": {
                **repair_diagnostics,
                "decision": (
                    "not_needed_required_lanes_reserved"
                    if not missing_before_repair
                    else (
                        "repair_recovered_required_lanes"
                        if required_lane_coverage >= 1.0
                        else "repair_exhausted_required_lanes_unsupported"
                    )
                ),
            },
            "final_distribution": {
                "corpora": corpus_distribution,
                "documents": document_distribution,
            },
            "final_source_tiers": final_source_tiers,
            "document_routing": document_routing_diagnostics,
            "summary_tree_routing": summary_tree_diagnostics,
            "vocabulary_resolution": vocabulary_diagnostics,
            "unique_docs_final": len(document_distribution),
            "max_doc_share_final": max_doc_share_final,
            "graph_evidence": {
                "facts_used": len(facts),
                "fact_types": sorted(
                    {
                        str(fact.fact_type)
                        for fact in facts
                        if str(fact.fact_type or "").strip()
                    }
                ),
                "predicates_used": sorted(predicates_used),
            },
            "timings_s": {key: round(value, 3) for key, value in timings.items()},
            "quality_first": quality_first,
            "total_deadline_s": round(total_deadline, 3),
            "budget_remaining_s": round(_budget_remaining(), 3),
            "total_s": round(total_s, 3),
            "final_count": len(finalists),
        }
        logger.info(
            "QueryPlanV2 timings total=%.3fs budget=%.3fs remaining=%.3fs stages=%s failures=%s",
            total_s,
            total_deadline,
            _budget_remaining(),
            diagnostics["timings_s"],
            [f"{item['retriever']}:{item['error']}" for item in failures],
        )
        return RetrievalResult(
            chunks=finalists,
            facts=facts,
            requested_tier=retrieval_tier,
            effective_tier=effective_tier,
            downgrade_reason=downgrade_reason,
            diagnostics=diagnostics,
        )

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
                artifact_epoch = await self._corpus_artifact_epoch(corpus_ids)
                key = hash_key(
                    "retrieval_v2",
                    artifact_epoch,
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
        if not args:
            result = await self._repair_cross_corpus_missing_concepts(result, kwargs)
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
        cross-encoder call entirely. Fast Search always skips the cross-encoder
        because its qdrant_only contract is the latency-oriented route.
        """
        requested_rerank_enabled = bool(rerank_enabled)
        rerank_enabled = _rerank_enabled_for_tier(
            requested_rerank_enabled,
            retrieval_tier,
        )
        single_limit = retrieval_k if retrieval_k is not None else _SINGLE_CORPUS_LIMIT
        rank_query = ranking_query or query
        retrieval_intent = infer_retrieval_intent(rank_query)
        summary_base = (
            top_k_summary if top_k_summary is not None else _DEFAULT_SUMMARY_LIMIT
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
                    f"{name}:{dur:.2f}s"
                    for name, dur in sorted(funnel_durations.items())
                )
                or "none",
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
            doc_counts: dict[tuple[str, str], int] = {}
            for chunk in chunks or []:
                doc_id = getattr(chunk, "doc_id", None)
                if not doc_id:
                    continue
                key = (str(chunk.corpus_id or ""), str(doc_id))
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
                    "final_top_k": (
                        final_top_k
                        if final_top_k is not None
                        else settings.DEFAULT_RETRIEVAL_K
                    ),
                    "rerank_requested": requested_rerank_enabled,
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
        if (
            effective_tier == RetrievalTier.qdrant_mongo_graph
            and settings.NEO4J_ENABLED
        ):
            # Self-heal the graph-analytics cache (bridges / analogies / transfer
            # candidates). Non-blocking and signature-driven: any staleness — from
            # ingest, delete, backfill, dedup, or a lost post-ingest warm — repairs
            # itself for the next graph query. The query never waits on it.
            for _cid in corpus_ids or []:
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
                        ranked = sorted(
                            fallback_pool, key=lambda x: x.score, reverse=True
                        )
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
        a_cols, b_cols = self._resolve_collections(
            effective_tier, corpus_ids, collections
        )

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
                        rank_query,
                        a_results_global,
                    )
                except Exception as exc:
                    logger.warning(
                        "global-mode rerank failed, score-sorting: %s",
                        exc,
                    )
                    a_results_global = sorted(
                        a_results_global,
                        key=lambda c: c.score,
                        reverse=True,
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
                    counts["global_rerank_tail_trimmed"] = len(a_results_global) - len(
                        trimmed_global
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

        # Q2/U2 — payload soft-prefilter inputs, derived ONCE from the RAW
        # query (rank_query/HyDE prose would emit junk grams). Empty lists
        # disable the should-filter entirely inside funnel B.
        soft_concepts: list[str] = []
        soft_entity_ids: list[str] = []
        prefilter_min = 0
        if bool(getattr(settings, "PAYLOAD_SOFT_PREFILTER", True)):
            from services.retriever.prefilter import query_payload_terms

            soft_concepts, soft_entity_ids = query_payload_terms(query)
            prefilter_min = int(getattr(settings, "PAYLOAD_PREFILTER_MIN_RESULTS", 8))

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
                    concept_terms=soft_concepts,
                    entity_ids=soft_entity_ids,
                    min_filtered=prefilter_min,
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
            if (
                funnel_limits.summary_top_k > 0
                and effective_tier == RetrievalTier.qdrant_only
            ):
                a_task = asyncio.wait_for(
                    a_task,
                    timeout=float(settings.FAST_SUMMARY_DEADLINE_SECONDS),
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
                _unwrap_funnel_result(r, f"funnel_b[{i}]") for i, r in enumerate(raw_b)
            ]
            b_results: list[SourceChunk] = [c for pool in per_corpus_b for c in pool]
        else:
            a_kwargs = {
                "top_k": funnel_limits.summary_top_k,
                "query_text": rank_query,
            }
            if funnel_limits.summary_top_k > 0:
                a_task = funnel_a.search(
                    query_vector,
                    corpus_ids,
                    a_cols,
                    **a_kwargs,
                )
                if effective_tier == RetrievalTier.qdrant_only:
                    a_task = asyncio.wait_for(
                        a_task,
                        timeout=float(settings.FAST_SUMMARY_DEADLINE_SECONDS),
                    )
            else:
                a_task = asyncio.sleep(0, result=[])
            # Same partial-failure safety as the multi-corpus branch above.
            raw_a, raw_b, raw_lex, raw_doc_anchor = await asyncio.gather(
                _timed_funnel("a", a_task),
                _timed_funnel(
                    "b",
                    funnel_b.search(
                        query_vector,
                        corpus_ids,
                        b_cols,
                        top_k=funnel_limits.child_top_k,
                        query_text=rank_query,
                        concept_terms=soft_concepts,
                        entity_ids=soft_entity_ids,
                        min_filtered=prefilter_min,
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

        def _result(
            chunks: list[SourceChunk], *, status: str = "result"
        ) -> RetrievalResult:
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
            "b": 1.0,  # dense/hybrid children — the semantic core
            "anchor": 0.9,  # title-anchored recall
            "lex": 0.8,  # sparse/lexical recall
            "graph": 0.9,  # Mode A expansion (added later)
            "a": 0.7,  # summaries
            "fact": 0.9,  # fact-seed evidence
        }
        _lane_ranks: dict[str, dict[str, int]] = {}

        def _record_lane_ranks(
            lane: str,
            chunks_in_lane: list[SourceChunk],
            *,
            per_corpus: bool = False,
        ) -> None:
            table = _lane_ranks.setdefault(lane, {})
            rank_groups: list[list[SourceChunk]]
            if per_corpus:
                grouped: dict[str, list[SourceChunk]] = {}
                for _c in chunks_in_lane:
                    _cid = str(getattr(_c, "corpus_id", "") or "__unknown__")
                    grouped.setdefault(_cid, []).append(_c)
                rank_groups = list(grouped.values())
            else:
                rank_groups = [chunks_in_lane]
            for _group in rank_groups:
                for _rank, _c in enumerate(_group):
                    _content_id = str(_c.chunk_id or _c.parent_id or "")
                    _key = (
                        f"{_c.corpus_id}|{_content_id}"
                        if _c.corpus_id and _content_id
                        else _content_id
                    )
                    if _key and (_key not in table or _rank < table[_key]):
                        table[_key] = _rank

        def _rrf_fused(chunk: SourceChunk) -> float:
            _content_id = str(chunk.chunk_id or chunk.parent_id or "")
            _parent_id = str(chunk.parent_id or "")
            _key = (
                f"{chunk.corpus_id}|{_content_id}"
                if chunk.corpus_id and _content_id
                else _content_id
            )
            _pkey = (
                f"{chunk.corpus_id}|{_parent_id}"
                if chunk.corpus_id and _parent_id
                else _parent_id
            )
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
                    c.corpus_id or "",
                    c.doc_id or "",
                    c.chunk_id or "",
                ),
            )

        _record_lane_ranks("fact", fact_seed_chunks, per_corpus=multi)
        _record_lane_ranks("a", a_results, per_corpus=multi)
        _record_lane_ranks("b", b_results, per_corpus=multi)
        _record_lane_ranks("lex", lexical_results, per_corpus=multi)
        _record_lane_ranks("anchor", document_anchor_results, per_corpus=multi)
        if multi:
            counts["pool_rank_fusion_per_corpus"] = 1

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
            {
                (str(c.corpus_id or ""), str(c.doc_id))
                for c in merged
                if getattr(c, "doc_id", None)
            }
        )
        _add_timing("merge", phase_started)
        if not merged:
            _log_timings("empty_after_merge", 0)
            return _result([], status="empty_after_merge")

        # Q2/U2 — semantic_chunk_type <-> query-operator RANK-ONLY bonus
        # (additive, tiny, pre-rerank; the cross-encoder re-scores its pool
        # so this only steers POOL SELECTION toward answer-shaped chunks).
        sem_bonus = float(getattr(settings, "SEMANTIC_TYPE_RANK_BONUS", 0.03) or 0.0)
        if sem_bonus > 0:
            from services.retriever.prefilter import query_operator, semantic_rank_bonus

            _op = query_operator(query)
            if _op:
                boosted = 0
                for c in merged:
                    b = semantic_rank_bonus(
                        _op,
                        (getattr(c, "metadata", None) or {}).get("semantic_chunk_type"),
                        bonus=sem_bonus,
                    )
                    if b:
                        c.score = min(1.0, float(c.score or 0.0) + b)
                        boosted += 1
                if boosted:
                    merged.sort(key=lambda x: x.score, reverse=True)
                    counts["semantic_type_boosted"] = boosted
        # Q4 H4 — mechanisms[]-overlap bridge bonus (rank-only, cross-doc
        # only; inert until promoted mechanisms exist on payloads).
        _cd_mode = str(getattr(settings, "CROSS_DOMAIN_EMPHASIS", "balanced"))
        if _cd_mode != "off":
            from services.retriever.cross_domain import mechanisms_overlap_bonus

            _mech_boosted = mechanisms_overlap_bonus(merged, mode=_cd_mode)
            if _mech_boosted:
                merged.sort(key=lambda x: x.score, reverse=True)
                counts["mechanisms_boosted"] = _mech_boosted

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
        if (
            effective_tier == RetrievalTier.qdrant_mongo_graph
            and settings.NEO4J_ENABLED
        ):
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
                effective_expansion_cap = min(
                    requested_expansion, graph_expansion_limit
                )
                expand_kwargs = (
                    {
                        "limit": effective_expansion_cap,
                        "seed_limit": getattr(settings, "GRAPH_SEED_CHUNKS", 8),
                        # P2 A1 — the RAW user query drives entity linking
                        # (ranking_query/HyDE prose would slug to junk grams)
                        "query": query,
                    }
                    if effective_expansion_cap > 0
                    else {
                        "limit": 0,
                        "seed_limit": getattr(settings, "GRAPH_SEED_CHUNKS", 8),
                    }
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
                    getattr(settings, "GRAPH_EXPANSION_TIMEOUT_SECONDS", 4.0) or 4.0
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
                    _record_lane_ranks("graph", expanded, per_corpus=multi)
                    merged = merge_pools(merged, expanded)
                    counts["merged_after_graph"] = len(merged)
            except asyncio.TimeoutError:
                counts["graph_expansion_timed_out"] = 1
                logger.warning(
                    "Mode A expansion timed out after %.2fs, continuing with hybrid seeds",
                    float(
                        getattr(settings, "GRAPH_EXPANSION_TIMEOUT_SECONDS", 4.0) or 4.0
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
        if effective_tier == RetrievalTier.qdrant_mongo_graph and merged:
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

        if effective_tier in (
            RetrievalTier.qdrant_mongo,
            RetrievalTier.qdrant_mongo_graph,
        ):
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
                "status": (
                    "skipped_fast_tier"
                    if requested_rerank_enabled
                    and effective_tier == RetrievalTier.qdrant_only
                    else "skipped_by_request"
                ),
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
        _pool_doc_ids = {
            (str(c.corpus_id or ""), str(c.doc_id))
            for c in ranked
            if getattr(c, "doc_id", None)
        }
        counts["distinct_docs_in_pool"] = len(_pool_doc_ids)
        diversity = select_with_diversity(
            ranked,
            final_top_k=effective_final_k,
            intent=retrieval_intent,
            tier=effective_tier,
            multi_corpus=multi,
            selected_corpus_ids=corpus_ids or [],
            query=rank_query,
        )
        selection_diagnostics = dict(diversity.diagnostics or {})
        candidates = diversity.candidates
        # Q4 H4 — distinct-DOMAIN reserve: when the whole final cut shares
        # the top chunk's domain on a breadth-shaped query, the best
        # different-domain candidate takes the LAST slot. Rank-only; inert
        # on corpora without promoted/denormalized domain payloads.
        if str(getattr(settings, "CROSS_DOMAIN_EMPHASIS", "balanced")) != "off":
            from services.retriever.cross_domain import domain_reserve_swap

            candidates, _swapped_domain = domain_reserve_swap(
                candidates,
                ranked,
                mode=str(getattr(settings, "CROSS_DOMAIN_EMPHASIS", "balanced")),
                broad=retrieval_intent.need == QueryNeed.BROAD,
                balanced_intent=retrieval_intent.need == QueryNeed.BALANCED,
            )
            if _swapped_domain:
                counts["domain_reserve_swap"] = 1
                logger.info(
                    "Q4 domain reserve: last slot -> %s (top domain was uniform)",
                    _swapped_domain,
                )
        counts["candidates"] = len(candidates)
        counts["diversity_added"] = diversity.added
        if effective_tier == RetrievalTier.qdrant_only:
            candidates, fast_grounding_dropped = _filter_fast_grounded_candidates(
                candidates,
                query=rank_query,
            )
            if fast_grounding_dropped:
                counts["fast_ungrounded_dropped"] = fast_grounding_dropped
                counts["candidates"] = len(candidates)
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
        if effective_tier in (
            RetrievalTier.qdrant_mongo,
            RetrievalTier.qdrant_mongo_graph,
        ):
            phase_started = perf_counter()
            try:
                hydrated = await hydrate_chunks(
                    candidates, corpus_ids, query=rank_query
                )
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
                            len(packet.items),
                            packet.used_tokens,
                            packet.budget_tokens,
                            packet.packet_hash[:12],
                        )
                _log_timings("ok_hydrated", len(hydrated))
                return result
            except Exception as exc:
                _add_timing("hydrate", phase_started)
                logger.warning("Hydration failed, returning unhydrated: %s", exc)

        _log_timings("ok_unhydrated", len(candidates))
        return _result(candidates, status="ok_unhydrated")


retriever_orchestrator = RetrieverOrchestrator()
