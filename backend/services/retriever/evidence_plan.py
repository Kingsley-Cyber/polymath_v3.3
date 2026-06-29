"""Question-shaped evidence planning for chat retrieval.

The ranking layer can decide which chunks are best, but it should not be the
only place that decides what *kinds* of evidence the answer needs. This module
turns relationship/comparison questions into explicit evidence lanes before the
chat model sees context.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.retriever.query_semantics import (
    ConceptGroup,
    clean_text,
    concept_groups,
    required_operator_atoms,
)


_MAX_LANE_ALIASES = 14


@dataclass(frozen=True)
class EvidenceLane:
    """One required side of a multi-concept evidence plan."""

    name: str
    label: str
    concept_key: str
    aliases: tuple[str, ...]
    search_terms: tuple[str, ...]
    query: str
    required: bool = True
    min_sources: int = 1


@dataclass(frozen=True)
class EvidencePlan:
    """Deterministic retrieval contract derived from the user question."""

    mode: str
    reason: str
    operators: tuple[str, ...]
    lanes: tuple[EvidenceLane, ...]
    bridge_query: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.lanes)

    @property
    def required_lanes(self) -> tuple[EvidenceLane, ...]:
        return tuple(lane for lane in self.lanes if lane.required)


def _dedupe_terms(terms: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        text = " ".join(str(term or "").replace("_", " ").split()).strip()
        if not text:
            continue
        key = clean_text(text).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return tuple(out)


def _collapse_overlapping_groups(groups: list[ConceptGroup]) -> list[ConceptGroup]:
    """Prefer a phrase-level concept over its raw token duplicate.

    Example: "different personality" produces the phrase concept
    ``personality_framework`` and the raw token ``personality``. For evidence
    planning those are not two independent lanes; the framework lane is the
    source-backed personality side.
    """

    shadowed: set[str] = set()
    keys = {group.key for group in groups}
    for key in keys:
        if key.endswith("_framework"):
            shadowed.add(key[: -len("_framework")])
    return [group for group in groups if group.key not in shadowed]


def _lane_search_terms(group: ConceptGroup) -> tuple[str, ...]:
    if group.key == "personality_framework":
        terms = [
            "personality framework",
            "personality test",
            "personality type",
            "personality assessment",
            "four tendencies",
            "handbook of personality",
            "the handbook of personality",
            "personality handbook",
            "big five",
            "myers briggs",
            "mbti",
            "enneagram",
            "gretchen rubin",
            "rubin four tendencies",
            *group.aliases,
        ]
    else:
        terms = [group.key.replace("_", " "), *group.aliases]
    return _dedupe_terms(terms)[:_MAX_LANE_ALIASES]


def _lane_from_group(group: ConceptGroup) -> EvidenceLane:
    search_terms = _lane_search_terms(group)
    aliases = _dedupe_terms([group.key.replace("_", " "), *group.aliases, *search_terms])
    query_terms = search_terms[:12] or aliases[:12]
    return EvidenceLane(
        name=group.key,
        label=group.key.replace("_", " "),
        concept_key=group.key,
        aliases=aliases,
        search_terms=search_terms,
        query=" ".join(query_terms),
    )


def build_evidence_plan(
    query: str | None,
    *,
    max_lanes: int = 4,
) -> EvidencePlan:
    """Build the evidence contract for a user query.

    Every query with detectable concepts gets a plan. Multi-concept queries
    become separate lanes even when the user did not write an explicit
    "compare/relate" operator; otherwise a single blended retrieval can still
    overfit the most common concept and go blind to the rest.
    """

    query_text = query or ""
    operators = tuple(sorted(required_operator_atoms(query_text)))
    groups = _collapse_overlapping_groups(
        concept_groups(query_text, max_groups=max(2, max_lanes + 2))
    )
    if not groups:
        return EvidencePlan(
            mode="unstructured",
            reason="no_stable_query_concepts_detected",
            operators=operators,
            lanes=(),
            bridge_query=query_text,
        )
    if len(groups) < 2:
        lanes = tuple(_lane_from_group(group) for group in groups[:1])
        return EvidencePlan(
            mode="single_concept",
            reason="single_query_concept",
            operators=operators,
            lanes=lanes,
            bridge_query=query_text,
        )
    lanes = tuple(_lane_from_group(group) for group in groups[:max_lanes])
    return EvidencePlan(
        mode=(
            "multi_concept_relationship"
            if "relationship" in operators
            else "multi_concept"
        ),
        reason=(
            "relationship_query_requires_each_side_as_evidence"
            if "relationship" in operators
            else "query_decomposed_into_semantic_concept_lanes"
        ),
        operators=operators,
        lanes=lanes,
        bridge_query=query_text,
    )


def evidence_plan_to_dict(plan: EvidencePlan) -> dict[str, object]:
    return {
        "active": plan.active,
        "mode": plan.mode,
        "reason": plan.reason,
        "operators": list(plan.operators),
        "bridge_query": plan.bridge_query,
        "required_lane_count": len(plan.required_lanes),
        "lanes": [
            {
                "name": lane.name,
                "label": lane.label,
                "concept_key": lane.concept_key,
                "aliases": list(lane.aliases[:_MAX_LANE_ALIASES]),
                "search_terms": list(lane.search_terms[:_MAX_LANE_ALIASES]),
                "query": lane.query,
                "required": lane.required,
                "min_sources": lane.min_sources,
            }
            for lane in plan.lanes
        ],
    }


def evidence_lane_matches_text(lane: EvidenceLane, text: str | None) -> bool:
    haystack = clean_text(text or "")
    return any(clean_text(alias) in haystack for alias in lane.aliases)
