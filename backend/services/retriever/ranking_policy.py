"""Candidate weighting and final-source diversity policy."""

from __future__ import annotations

from dataclasses import dataclass

from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import QueryNeed, RetrievalIntent


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
