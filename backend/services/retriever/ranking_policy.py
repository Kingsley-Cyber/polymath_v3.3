"""Candidate weighting and final-source diversity policy."""

from __future__ import annotations

from dataclasses import dataclass

from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import QueryNeed, RetrievalIntent
from services.retriever.query_grounding import (
    chunk_concept_hits,
    concept_groups,
)

# ── Phase 1: candidate-collapse hygiene ───────────────────────────────────
# Intent-adaptive distinct-document breadth — a focused (SPECIFIC) query may
# concentrate on a few authoritative sources; a broad/thematic one should fan
# out. The per-document chunk ceiling is derived from this
# (ceil(final_top_k / breadth), floored at 1), so no single document can
# collapse the context window (the "6 of 9 chunks from one book" pathology).
_DISTINCT_DOC_BREADTH: dict[QueryNeed, int] = {
    QueryNeed.SPECIFIC: 4,
    QueryNeed.BALANCED: 6,
    QueryNeed.BROAD: 8,
}
# Relative (pool-derived) noise floor on the MAIN selection for hydrated tiers:
# a non-graph chunk scoring below this fraction of the top score is the kind of
# lexical-rescued junk (a ~0.02 cross-encoder chunk lifted to ~0.18 by the
# query-grounding per-word bonus) the LLM should not see. Graph-provenance
# chunks are exempt (their value is relational); MIN_KEEP never strands a pool.
_MAIN_FLOOR_RATIO: float = 0.25
_MAIN_ABS_FLOOR: float = 0.10
_MAIN_MIN_KEEP: int = 3


@dataclass(frozen=True)
class DiversityResult:
    candidates: list[SourceChunk]
    added: int


def _provenance_retrievers(chunk: SourceChunk) -> set[str]:
    values: set[str] = set()
    for item in chunk.provenance or []:
        retriever = item.get("retriever")
        if retriever:
            values.add(str(retriever))
    return values


def _document_anchor_confidence(chunk: SourceChunk) -> float:
    best = 0.0
    source_tier = (chunk.source_tier or "").lower()
    if "document_anchor" in source_tier:
        best = 0.75
    for item in chunk.provenance or []:
        if item.get("retriever") != "document_anchor":
            continue
        try:
            best = max(best, float(item.get("document_score") or 0.0))
        except (TypeError, ValueError):
            continue
    return best


def _is_confident_document_anchor(chunk: SourceChunk) -> bool:
    return _document_anchor_confidence(chunk) >= 0.75


def _is_graph_expansion(chunk: SourceChunk) -> bool:
    """A chunk surfaced by Neo4j Mode A/B expansion (graph_mode_a/_bridge/_b).

    These are demoted by the text-similarity cross-encoder because their value
    is relational, not lexical, so the diversity pass reserves slots for them.
    """
    return (chunk.source_tier or "").lower().startswith("graph_mode")


def candidate_kind(chunk: SourceChunk) -> str:
    """Classify a candidate by retrieval lane."""
    source_tier = (chunk.source_tier or "").lower()
    retrievers = _provenance_retrievers(chunk)

    if source_tier == "graph_fact_seed" or "neo4j_fact" in retrievers:
        return "fact"
    if "+lexical" in source_tier or "lexical" in retrievers or "qdrant_sparse" in retrievers:
        return "lexical"
    if chunk.summary and (chunk.text == chunk.summary or chunk.chunk_id.endswith("_summary")):
        return "summary"
    return "child"


def _weight_for(kind: str, intent: RetrievalIntent, tier: RetrievalTier) -> float:
    if tier == RetrievalTier.qdrant_only:
        weights = {
            QueryNeed.SPECIFIC: {"child": 1.08, "summary": 0.94},
            QueryNeed.BALANCED: {"child": 1.00, "summary": 1.02},
            QueryNeed.BROAD: {"child": 0.96, "summary": 1.10},
        }
    else:
        weights = {
            QueryNeed.SPECIFIC: {
                "child": 1.06,
                "summary": 0.95,
                "lexical": 1.12,
                "fact": 1.04,
            },
            QueryNeed.BALANCED: {
                "child": 1.00,
                "summary": 1.04,
                "lexical": 1.06,
                "fact": 1.04,
            },
            QueryNeed.BROAD: {
                "child": 0.96,
                "summary": 1.12,
                "lexical": 1.02,
                "fact": 1.06,
            },
        }
    return weights.get(intent.need, {}).get(kind, 1.0)


def apply_candidate_weights(
    chunks: list[SourceChunk],
    *,
    intent: RetrievalIntent,
    tier: RetrievalTier,
) -> list[SourceChunk]:
    """Return score-adjusted copies using conservative lane weights."""
    weighted: list[SourceChunk] = []
    for chunk in chunks:
        copied = chunk.model_copy()
        copied.score = float(copied.score or 0.0) * _weight_for(
            candidate_kind(copied),
            intent,
            tier,
        )
        weighted.append(copied)
    weighted.sort(
        key=lambda c: (
            -float(c.score or 0.0),
            c.parent_id or "",
            c.doc_id or "",
            c.chunk_id or "",
        )
    )
    return weighted


def _grounded_score(
    score: float,
    *,
    hits: int,
    total: int,
    score_scale: str | None,
) -> float:
    """Conservative score adjustment for query-concept coverage.

    Bounded rerankers can emit 0..1 scores that look authoritative even when
    the sidecar failed and the original lexical score was preserved. Complete
    query-concept coverage gets a small lift; partial/no coverage gets demoted.
    """
    if total <= 0:
        return score

    scale = (score_scale or "").lower()
    bounded = scale in {"probability", "cosine"} or 0.0 <= score <= 1.0
    coverage = hits / total

    if bounded:
        if hits <= 0:
            return round(max(0.0, score * 0.30), 4)
        if hits < total:
            multiplier = 0.62 + (0.18 * coverage)
            return round(min(1.0, max(0.0, score * multiplier + 0.04 * hits)), 4)
        return round(min(1.0, max(0.0, score * 1.04 + 0.12)), 4)

    if hits <= 0:
        return round(score - 1.25, 4)
    if hits < total:
        return round(score - (0.65 * (1.0 - coverage)) + (0.10 * hits), 4)
    return round(score + 0.75 + (0.10 * min(hits, 3)), 4)


def apply_query_grounding(
    chunks: list[SourceChunk],
    *,
    query: str,
    tier: RetrievalTier,
    score_scale: str | None = None,
) -> list[SourceChunk]:
    """Prefer final evidence that covers the user's core query concepts.

    This does not add any new store to a retrieval tier. It only reorders and
    lightly rescales the candidates already retrieved by that tier, using a
    deterministic concept coverage pass. If no candidate covers any extracted
    query concept, the original ordering is preserved.
    """
    if len(chunks) <= 1:
        return chunks

    groups = concept_groups(query)
    if not groups:
        return chunks

    scored: list[tuple[SourceChunk, int, tuple[str, ...]]] = []
    group_counts: dict[str, int] = {group.key: 0 for group in groups}
    for chunk in chunks:
        hits, matched = chunk_concept_hits(chunk, groups)
        for key in matched:
            group_counts[key] = group_counts.get(key, 0) + 1
        scored.append((chunk, hits, matched))

    max_hits = max((hits for _, hits, _ in scored), default=0)
    if max_hits <= 0:
        return chunks

    total = len(groups)
    grounded: list[tuple[SourceChunk, int, float]] = []
    for chunk, hits, matched in scored:
        copied = chunk.model_copy()
        original_score = float(copied.score or 0.0)
        rarity = 0.0
        for key in matched:
            count = max(group_counts.get(key, 0), 1)
            rarity += 1.0 / count
        if total > 1:
            rarity = min(1.0, rarity)
        else:
            rarity = 0.0
        copied.score = _grounded_score(
            original_score,
            hits=hits,
            total=total,
            score_scale=score_scale,
        )
        copied.metadata = dict(copied.metadata or {})
        copied.metadata["query_grounding"] = {
            "concept_count": total,
            "matched_count": hits,
            "matched": list(matched),
            "original_score": original_score,
            "adjusted_score": copied.score,
            "tier": tier.value if hasattr(tier, "value") else str(tier),
        }
        grounded.append((copied, hits, rarity))

    grounded.sort(
        key=lambda item: (
            -item[1],
            -item[2],
            -float(item[0].score or 0.0),
            item[0].parent_id or "",
            item[0].doc_id or "",
            item[0].chunk_id or "",
        )
    )
    return [chunk for chunk, _, _ in grounded]


def _candidate_identity(chunk: SourceChunk) -> tuple[str, str, str]:
    return (
        chunk.parent_id or chunk.chunk_id or "",
        chunk.doc_id or "",
        " / ".join(chunk.heading_path or []),
    )


def _passes_diversity_threshold(candidate: SourceChunk, top_score: float) -> bool:
    score = float(candidate.score or 0.0)
    if 0.0 <= top_score <= 1.0:
        return score >= max(0.35, top_score * 0.80)
    return score >= top_score - 1.25


def _per_doc_cap_for(intent: RetrievalIntent, final_top_k: int) -> int:
    """Max chunks a single document may contribute to the final set.

    Derived from intent-adaptive distinct-document breadth:
    cap = ceil(final_top_k / breadth), floored at 1. final_top_k=8 →
    SPECIFIC:2, BALANCED:2, BROAD:1.
    """
    breadth = max(1, int(_DISTINCT_DOC_BREADTH.get(intent.need, 6)))
    return max(1, -(-int(final_top_k) // breadth))


def select_with_diversity(
    ranked: list[SourceChunk],
    *,
    final_top_k: int,
    intent: RetrievalIntent,
    tier: RetrievalTier,
    multi_corpus: bool = False,
) -> DiversityResult:
    """Return final candidates plus up to two strong diverse extras.

    Diversity is only for hydrated tiers. Vector Base remains strict top-k so
    its baseline behavior stays simple and easy to compare. For hydrated tiers
    the main selection enforces a per-document ceiling (anti candidate-collapse)
    and a relative noise floor (kills the lexical-rescued junk tail); graph-
    provenance chunks are exempt from the floor and governed by the graph
    reservation, and a MIN_KEEP guard never strands the pool.
    """
    final_top_k = max(1, int(final_top_k))

    # Vector Base stays strict top-k for simple, comparable baseline behavior.
    if tier == RetrievalTier.qdrant_only:
        return DiversityResult(candidates=list(ranked[:final_top_k]), added=0)
    if not ranked:
        return DiversityResult(candidates=[], added=0)

    per_doc_cap = _per_doc_cap_for(intent, final_top_k)
    top_score = float(ranked[0].score or 0.0)
    bounded = 0.0 <= top_score <= 1.0
    rel_floor = (
        max(_MAIN_ABS_FLOOR, top_score * _MAIN_FLOOR_RATIO) if bounded else 0.0
    )

    selected: list[SourceChunk] = []
    doc_counts: dict[str, int] = {}
    chosen_idx: set[int] = set()

    def _doc_has_room(chunk: SourceChunk) -> bool:
        doc = str(chunk.doc_id or "")
        return (not doc) or doc_counts.get(doc, 0) < per_doc_cap

    def _take(idx: int, chunk: SourceChunk) -> None:
        selected.append(chunk)
        chosen_idx.add(idx)
        doc = str(chunk.doc_id or "")
        if doc:
            doc_counts[doc] = doc_counts.get(doc, 0) + 1

    # Main selection: walk ranked best-first, skipping sub-floor non-graph junk
    # and documents already at the per-doc ceiling — so a lower-ranked chunk
    # from an under-represented document can take the slot a dominant document
    # would otherwise have collapsed.
    for idx, candidate in enumerate(ranked):
        if len(selected) >= final_top_k:
            break
        if (
            not _is_graph_expansion(candidate)
            and bounded
            and float(candidate.score or 0.0) < rel_floor
        ):
            continue
        if not _doc_has_room(candidate):
            continue
        _take(idx, candidate)

    # MIN_KEEP guard — if the cap/floor over-pruned, backfill the highest-ranked
    # excluded chunks (ignoring cap/floor) so the answer is never starved.
    min_keep = min(final_top_k, _MAIN_MIN_KEEP)
    if len(selected) < min_keep:
        for idx, candidate in enumerate(ranked):
            if len(selected) >= min_keep:
                break
            if idx in chosen_idx:
                continue
            _take(idx, candidate)

    max_extra = 2 if (intent.need == QueryNeed.BROAD or multi_corpus) else 1
    if max_extra <= 0 or not selected:
        return DiversityResult(candidates=selected, added=0)

    selected_identities = {_candidate_identity(chunk) for chunk in selected}
    selected_parents = {chunk.parent_id or chunk.chunk_id for chunk in selected}
    added = 0

    # Graph tier: the cross-encoder ranks pure text similarity, which
    # systematically demotes graph-expanded neighbors (their value is the
    # relationship, not lexical overlap). Reserve a small dedicated budget for
    # graph-provenance chunks MISSING from the natural top-k so graph context
    # reaches the LLM — exempt from the per-doc ceiling, since surfacing the
    # relation is the whole point of the tier.
    if tier == RetrievalTier.qdrant_mongo_graph:
        graph_reserve = 2 if intent.need == QueryNeed.BROAD else 1
        graph_in_topk = sum(1 for c in selected if _is_graph_expansion(c))
        graph_need = max(0, graph_reserve - graph_in_topk)
        for idx, candidate in enumerate(ranked):
            if graph_need <= 0:
                break
            if idx in chosen_idx or not _is_graph_expansion(candidate):
                continue
            identity = _candidate_identity(candidate)
            parent_key = candidate.parent_id or candidate.chunk_id
            if identity in selected_identities or parent_key in selected_parents:
                continue
            selected.append(candidate)
            chosen_idx.add(idx)
            selected_identities.add(identity)
            selected_parents.add(parent_key)
            graph_need -= 1
            added += 1

    # Confident document-anchor extras — narrow but high-confidence title hits
    # the cross-encoder demotes. Respect the per-doc ceiling.
    for idx, candidate in enumerate(ranked):
        if added >= max_extra:
            break
        if idx in chosen_idx or not _is_confident_document_anchor(candidate):
            continue
        identity = _candidate_identity(candidate)
        parent_key = candidate.parent_id or candidate.chunk_id
        if identity in selected_identities or parent_key in selected_parents:
            continue
        if not _doc_has_room(candidate):
            continue
        _take(idx, candidate)
        selected_identities.add(identity)
        selected_parents.add(parent_key)
        added += 1

    # Strong diverse extras — respect the per-doc ceiling.
    for idx, candidate in enumerate(ranked):
        if added >= max_extra:
            break
        if idx in chosen_idx:
            continue
        identity = _candidate_identity(candidate)
        parent_key = candidate.parent_id or candidate.chunk_id
        if identity in selected_identities or parent_key in selected_parents:
            continue
        if not _passes_diversity_threshold(candidate, top_score):
            continue
        if not _doc_has_room(candidate):
            continue
        _take(idx, candidate)
        selected_identities.add(identity)
        selected_parents.add(parent_key)
        added += 1

    return DiversityResult(candidates=selected, added=added)
