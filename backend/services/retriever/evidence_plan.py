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
    if group.key in {"personality", "personality_framework"}:
        terms = [
            "personality framework",
            "personality test",
            "personality type",
            "personality assessment",
            "personality traits",
            "personality development",
            "personality inventory",
            "four tendencies",
            "handbook of personality",
            "the handbook of personality",
            "personality handbook",
            "handbook of personality assessment",
            "art and science of personality development",
            "big five",
            "myers briggs",
            "mbti",
            "enneagram",
            "gifts differing",
            "gretchen rubin",
            "rubin four tendencies",
            *[
                alias
                for alias in group.aliases
                if alias not in {"type", "types", "profile", "profiles"}
            ],
        ]
    else:
        terms = [group.key.replace("_", " "), *group.aliases]
    return _dedupe_terms(terms)[:_MAX_LANE_ALIASES]


def _lane_from_group(group: ConceptGroup, *, min_sources: int = 1) -> EvidenceLane:
    search_terms = _lane_search_terms(group)
    aliases = _dedupe_terms(
        [group.key.replace("_", " "), *group.aliases, *search_terms]
    )
    query_terms = search_terms[:12] or aliases[:12]
    return EvidenceLane(
        name=group.key,
        label=group.key.replace("_", " "),
        concept_key=group.key,
        aliases=aliases,
        search_terms=search_terms,
        query=" ".join(query_terms),
        min_sources=min_sources,
    )


# Multi-concept queries reserve >=2 distinct-document sources per side. One
# chunk is a quote, not a lane of evidence; requiring two distinct documents is
# what stops a title-matching book from satisfying the *other* side by itself.
MULTI_CONCEPT_MIN_SOURCES = 2


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
    lanes = tuple(
        _lane_from_group(group, min_sources=MULTI_CONCEPT_MIN_SOURCES)
        for group in groups[:max_lanes]
    )
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


def _slug(value: str) -> str:
    text = clean_text(value).strip()
    return "_".join(text.split())[:48]


def build_evidence_plan_from_sides(
    query: str | None,
    sides: list[dict] | tuple[dict, ...],
    *,
    max_lanes: int = 4,
    allow_single: bool = False,
) -> EvidencePlan:
    """Build a multi-side plan from externally-provided sides (E: generalization).

    ``sides`` come from the no-LLM heuristic splitter or the optional LLM
    decomposer. Each side is a dict with a ``name``/``label`` and a list of
    ``search_terms`` (and optionally a ``query``). This lets an *arbitrary*
    multi-document question get the same per-side, distinct-document allocation
    as the curated concepts — the lanes are still grounded to real documents
    downstream via the ingestion-metadata hints. Normal callers fall back to
    the deterministic plan when fewer than two usable sides are provided;
    QueryPlanV2 may opt into preserving one complete objective.
    """

    lanes: list[EvidenceLane] = []
    seen: set[str] = set()
    for side in list(sides or [])[:max_lanes]:
        if not isinstance(side, dict):
            continue
        name = _slug(str(side.get("name") or side.get("label") or ""))
        if not name or name in seen:
            continue
        raw_terms = side.get("search_terms") or side.get("aliases") or []
        if isinstance(raw_terms, str):
            raw_terms = [raw_terms]
        if not raw_terms and side.get("query"):
            raw_terms = [side["query"]]
        search_terms = _dedupe_terms([*raw_terms])[:_MAX_LANE_ALIASES]
        if not search_terms:
            continue
        aliases = _dedupe_terms(
            [name.replace("_", " "), *(side.get("aliases") or []), *search_terms]
        )
        query_terms = search_terms[:12] or aliases[:12]
        lanes.append(
            EvidenceLane(
                name=name,
                label=str(side.get("label") or name.replace("_", " ")),
                concept_key=name,
                aliases=aliases,
                search_terms=search_terms,
                query=str(side.get("query") or " ".join(query_terms)),
                min_sources=MULTI_CONCEPT_MIN_SOURCES,
            )
        )
        seen.add(name)

    if len(lanes) < (1 if allow_single else 2):
        return build_evidence_plan(query, max_lanes=max_lanes)

    operators = tuple(sorted(required_operator_atoms(query or "")))
    relationship = "relationship" in operators and len(lanes) > 1
    return EvidencePlan(
        mode=(
            "multi_concept_relationship"
            if relationship
            else "multi_concept_sourced"
            if len(lanes) > 1
            else "single_objective_sourced"
        ),
        reason=(
            "relationship_query_decomposed_into_source_sides"
            if relationship
            else "query_decomposed_into_source_sides"
        ),
        operators=operators,
        lanes=tuple(lanes),
        bridge_query=query or "",
    )


def parse_llm_sides(text: str | None) -> list[dict]:
    """Parse an LLM decomposition reply into side dicts; tolerant and safe.

    Accepts a JSON object ``{"sides": [...]}`` or a bare JSON array, possibly
    wrapped in prose / code fences. Returns ``[]`` on anything unparseable so a
    malformed model reply never breaks retrieval.
    """

    import json
    import re

    raw = (text or "").strip()
    if not raw:
        return []
    match = re.search(r"\{.*\}|\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except Exception:
        return []
    sides_raw = data.get("sides") if isinstance(data, dict) else data
    if not isinstance(sides_raw, list):
        return []
    out: list[dict] = []
    for item in sides_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("label") or "").strip()
        terms = item.get("search_terms") or item.get("terms") or []
        if isinstance(terms, str):
            terms = [terms]
        terms = [str(t).strip() for t in terms if str(t).strip()]
        if not name or not terms:
            continue
        out.append(
            {
                "name": name,
                "label": str(item.get("label") or name),
                "search_terms": terms,
                "query": str(item.get("query") or " ".join(terms)),
            }
        )
    return out
