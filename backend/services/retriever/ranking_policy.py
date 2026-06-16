"""Candidate weighting and final-source diversity policy."""

from __future__ import annotations

from dataclasses import dataclass

from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import QueryNeed, RetrievalIntent
from services.retriever.query_grounding import (
    chunk_concept_hits,
    concept_groups,
)


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
    its baseline behavior stays simple and easy to compare.
    """
    final_top_k = max(1, int(final_top_k))
    selected = list(ranked[:final_top_k])
    if tier == RetrievalTier.qdrant_only or len(ranked) <= final_top_k:
        return DiversityResult(candidates=selected, added=0)

    max_extra = 2 if (intent.need == QueryNeed.BROAD or multi_corpus) else 1
    if max_extra <= 0 or not selected:
        return DiversityResult(candidates=selected, added=0)

    top_score = float(selected[0].score or 0.0)
    selected_identities = {_candidate_identity(chunk) for chunk in selected}
    selected_parents = {chunk.parent_id or chunk.chunk_id for chunk in selected}

    added = 0

    # Graph tier: the cross-encoder ranks pure text similarity, which
    # systematically demotes graph-expanded neighbors (their value is the
    # relationship, not lexical overlap with the query). Without a carve-out the
    # expensive expansion is reranked away and the tier collapses toward Hybrid.
    # Reserve a small, dedicated number of extra slots for graph-provenance
    # chunks — only as many as are MISSING from the natural top-k — so the
    # graph context actually reaches the LLM. This runs first and uses its own
    # budget (separate from max_extra) so it cannot be crowded out.
    if tier == RetrievalTier.qdrant_mongo_graph:
        graph_reserve = 2 if intent.need == QueryNeed.BROAD else 1
        graph_in_topk = sum(1 for c in selected if _is_graph_expansion(c))
        graph_need = max(0, graph_reserve - graph_in_topk)
        for candidate in ranked[final_top_k:]:
            if graph_need <= 0:
                break
            if not _is_graph_expansion(candidate):
                continue
            identity = _candidate_identity(candidate)
            parent_key = candidate.parent_id or candidate.chunk_id
            if identity in selected_identities or parent_key in selected_parents:
                continue
            selected.append(candidate)
            selected_identities.add(identity)
            selected_parents.add(parent_key)
            graph_need -= 1
            added += 1

    # Source-constrained queries can produce high-confidence document-anchor
    # candidates that cross-encoders still demote because the candidate text is
    # narrow. Let at most two through when the document-title match was strong.
    for candidate in ranked[final_top_k:]:
        if added >= max_extra:
            break
        if not _is_confident_document_anchor(candidate):
            continue
        identity = _candidate_identity(candidate)
        parent_key = candidate.parent_id or candidate.chunk_id
        if identity in selected_identities or parent_key in selected_parents:
            continue
        selected.append(candidate)
        selected_identities.add(identity)
        selected_parents.add(parent_key)
        added += 1

    for candidate in ranked[final_top_k:]:
        if added >= max_extra:
            break
        identity = _candidate_identity(candidate)
        parent_key = candidate.parent_id or candidate.chunk_id
        if identity in selected_identities or parent_key in selected_parents:
            continue
        if not _passes_diversity_threshold(candidate, top_score):
            continue
        selected.append(candidate)
        selected_identities.add(identity)
        selected_parents.add(parent_key)
        added += 1

    return DiversityResult(candidates=selected, added=added)
