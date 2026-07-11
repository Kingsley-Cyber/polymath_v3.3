"""Deterministic retrieval-intent policy.

This module deliberately uses plain heuristics instead of an LLM so the same
query always produces the same retrieval mix. It controls child-vs-summary
budgets and later ranking/diversity policy without adding any new data store
to Fast Search or Hybrid Search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from services.retriever.search_mode import (
    _GLOBAL_MARKERS,
    _LOCAL_MARKERS,
    _count_phrase_hits,
    _has_code_entity,
)
from services.retriever.query_semantics import (
    RELATIONSHIP_MARKERS,
    has_marker,
)

_QUOTE_RE = re.compile(r"(['\"])(?:(?=(\\?))\2.)*?\1")
_MULTI_DOC_MARKERS: tuple[str, ...] = (
    "across",
    "compare",
    "contrast",
    "differences",
    "similarities",
    "themes",
    "patterns",
    "documents",
    "sources",
    "corpora",
    "library",
)
_ASSESSMENT_MARKERS: tuple[str, ...] = (
    "test my understanding",
    "test out my understanding",
    "quiz me",
    "assess my knowledge",
    "assess my understanding",
    "test my knowledge",
)


class QueryNeed(str, Enum):
    BROAD = "broad"
    BALANCED = "balanced"
    SPECIFIC = "specific"


@dataclass(frozen=True)
class RetrievalIntent:
    need: QueryNeed
    broad_score: int
    specific_score: int
    child_ratio: float
    summary_ratio: float


@dataclass(frozen=True)
class FunnelLimits:
    child_top_k: int
    summary_top_k: int


_RATIOS: dict[QueryNeed, tuple[float, float]] = {
    QueryNeed.SPECIFIC: (0.80, 0.20),
    QueryNeed.BALANCED: (0.65, 0.35),
    QueryNeed.BROAD: (0.50, 0.50),
}


def _normalize_query(query: str) -> str:
    return " ".join((query or "").lower().split())


def infer_retrieval_intent(query: str) -> RetrievalIntent:
    """Return deterministic child/summary intent for a query."""
    normalized = _normalize_query(query)
    if not normalized:
        child_ratio, summary_ratio = _RATIOS[QueryNeed.BALANCED]
        return RetrievalIntent(
            need=QueryNeed.BALANCED,
            broad_score=0,
            specific_score=0,
            child_ratio=child_ratio,
            summary_ratio=summary_ratio,
        )

    broad_score = 2 * _count_phrase_hits(normalized, _GLOBAL_MARKERS)
    specific_score = 2 * _count_phrase_hits(normalized, _LOCAL_MARKERS)

    if any(marker in normalized for marker in _MULTI_DOC_MARKERS):
        broad_score += 1
    if any(marker in normalized for marker in _ASSESSMENT_MARKERS):
        # Assessment generation should sample the subject broadly enough to
        # create representative questions, not collapse onto one passage.
        broad_score += 5
    if has_marker(normalized, RELATIONSHIP_MARKERS):
        # Relationship questions usually need evidence from both sides of
        # the relation. Balance them against "how does" local markers unless
        # a code/entity or quoted exact-match signal makes the query specific.
        broad_score += 2
    if _has_code_entity(query):
        specific_score += 3
    if _QUOTE_RE.search(query or ""):
        specific_score += 2

    if broad_score >= specific_score + 2:
        need = QueryNeed.BROAD
    elif specific_score >= broad_score + 1:
        need = QueryNeed.SPECIFIC
    else:
        need = QueryNeed.BALANCED

    child_ratio, summary_ratio = _RATIOS[need]
    return RetrievalIntent(
        need=need,
        broad_score=broad_score,
        specific_score=specific_score,
        child_ratio=child_ratio,
        summary_ratio=summary_ratio,
    )


def promote_compositional_intent(
    intent: RetrievalIntent,
    *,
    complexity: str,
    required_lane_count: int,
) -> RetrievalIntent:
    """Make independent retrieval obligations broad even when wording is local.

    A question containing several ``how`` clauses is lexically classified as
    specific, but each clause needs its own evidence neighborhood. QueryPlanV2
    has already established that structure, so it is the stronger signal for
    final breadth and hierarchy budgets.
    """

    if (
        required_lane_count <= 1
        or complexity not in {"compositional", "comparative", "dependent_multi_hop"}
        or intent.need == QueryNeed.BROAD
    ):
        return intent
    child_ratio, summary_ratio = _RATIOS[QueryNeed.BROAD]
    return RetrievalIntent(
        need=QueryNeed.BROAD,
        broad_score=max(intent.broad_score, intent.specific_score + 2),
        specific_score=intent.specific_score,
        child_ratio=child_ratio,
        summary_ratio=summary_ratio,
    )


def adaptive_funnel_limits(
    intent: RetrievalIntent,
    *,
    child_base: int,
    summary_base: int,
) -> FunnelLimits:
    """Split the candidate budget into deterministic child/summary top_k."""
    child_base = max(1, int(child_base))
    summary_base = max(1, int(summary_base))
    total = max(2, child_base + summary_base)

    child_top_k = max(1, round(total * intent.child_ratio))
    summary_top_k = max(1, total - child_top_k)
    return FunnelLimits(child_top_k=child_top_k, summary_top_k=summary_top_k)
