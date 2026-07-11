"""Deterministic, phrase-aware retrieval planning.

QueryPlanV2 is intentionally model-free. It preserves named concepts as a
single retrieval unit, exposes explicit comparison/relationship operators, and
keeps the original user query as the mandatory recall lane. The plan is small
enough to build on every request and serializable for trace/evaluation data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from services.retriever.query_semantics import (
    CONCEPT_ALIASES,
    ConceptGroup,
    clean_text,
    concept_groups,
    concept_support_phrases,
    lexical_terms,
    required_operator_atoms,
)


QueryComplexity = Literal[
    "simple",
    "compositional",
    "comparative",
    "dependent_multi_hop",
]
QueryLaneRole = Literal["original", "core", "bridge", "background"]

_DESCRIPTOR_RE = re.compile(
    r"\b((?:[A-Za-z][A-Za-z0-9'\-]*\s+){1,4})"
    r"(strategy|framework|model|method|mechanism|principles|positioning|messag(?:e|es|ing))\b",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9'\-]*(?:\s+(?:to|of|the|and|for|in|on|[A-Z][A-Za-z0-9'\-]*)){1,5})\b"
)
_QUOTE_RE = re.compile(r"[\"']([^\"']{3,100})[\"']")
_REGULATORY_RE = re.compile(
    r"\b((?:exact\s+)?(?:\d{4}\s+)?(?:[A-Za-z][A-Za-z0-9'\-]*\s+){0,2}"
    r"(?:law|regulation|policy))\b",
    re.IGNORECASE,
)
_TARGET_RE = re.compile(
    r"\bfor\s+((?:an?\s+|the\s+)?(?:[A-Za-z][A-Za-z0-9'\-]*\s+){1,3}"
    r"[A-Za-z][A-Za-z0-9'\-]*)$",
    re.IGNORECASE,
)
_COMMAND_SUBJECT_RE = re.compile(
    r"\b(?:understanding|knowledge|comprehension)\s+"
    r"(?:(?:of|about|in|on)\s+)?"
    r"((?:[A-Za-z][A-Za-z0-9'\-]*\s+){0,4}[A-Za-z][A-Za-z0-9'\-]*)$",
    re.IGNORECASE,
)
_DEPENDENCY_RE = re.compile(
    r"\b(?:then|before|after|depends?\s+on|using\s+the\s+result|based\s+on)\b",
    re.IGNORECASE,
)
_EXPLICIT_MULTI_RE = re.compile(
    r"\b(?:compare|contrast|versus|vs\.?|combine|relationship|relate|between)\b",
    re.IGNORECASE,
)
_ANSWER_OBJECT_RE = re.compile(
    r"\b(?:what|which|name|list|identify|recommend)\s+"
    r"(books?|authors?|people|experts?|tools?|models?|frameworks?|methods?|"
    r"strategies|examples?|products?|companies|organizations?|documents?|sources?)\b",
    re.IGNORECASE,
)
_FOLLOWUP_PREFIX_RE = re.compile(
    r"^(?:no[,:]?\s+|actually[,:]?\s+|instead[,:]?\s+|"
    r"and\s+|also\s+|what\s+about\s+)",
    re.IGNORECASE,
)
_ANSWER_OBJECT_SUPPORT: dict[str, tuple[str, ...]] = {
    "book": ("book titles", "authors", "book recommendations", "lessons", "principles"),
    "books": ("book titles", "authors", "book recommendations", "lessons", "principles"),
    "author": ("book authors", "written by", "books"),
    "authors": ("book authors", "written by", "books"),
    "tool": ("tools", "use cases", "applications"),
    "tools": ("tools", "use cases", "applications"),
    "example": ("examples", "case studies", "applications"),
    "examples": ("examples", "case studies", "applications"),
}
_GENERIC_LANE_TERMS = {
    "assess",
    "assessment",
    "brand",
    "combine",
    "comparison",
    "establish",
    "ecommerce",
    "evaluate",
    "exact",
    "find",
    "graph",
    "help",
    "helped",
    "helps",
    "html",
    "offer",
    "offers",
    "out",
    "product",
    "require",
    "requires",
    "strategy",
    "test",
    "quiz",
    "understanding",
    "framework",
    "model",
    "method",
    "no",
    "principles",
}

_PHRASE_BOUNDARIES = {"between", "versus", "vs", "with"}

_PHRASE_LEADERS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "combine",
    "compare",
    "contrast",
    "do",
    "does",
    "evaluate",
    "exact",
    "find",
    "for",
    "from",
    "how",
    "is",
    "it",
    "my",
    "relate",
    "require",
    "requires",
    "then",
    "the",
    "to",
    "understanding",
    "use",
    "using",
    "what",
    "with",
}


@dataclass(frozen=True)
class QueryLane:
    lane_id: str
    role: QueryLaneRole
    query: str
    dense_text: str
    lexical_terms: tuple[str, ...]
    required: bool = True
    depends_on: tuple[str, ...] = ()
    phrase: str | None = None
    support_phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPlanV2:
    version: str
    original_query: str
    standalone_query: str
    complexity: QueryComplexity
    concepts: tuple[str, ...]
    operators: tuple[str, ...]
    lanes: tuple[QueryLane, ...]
    corpus_ids: tuple[str, ...] = ()
    max_repair_rounds: int = 1


def _normalize_phrase(value: str) -> str:
    return " ".join(str(value or "").strip(" ,.:;!?()[]{}").split())


def _slug(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    return "_".join(tokens[:6])[:64]


def _best_surface(group: ConceptGroup, query: str) -> str:
    haystack = clean_text(query)
    matches = [
        alias
        for alias in (group.key.replace("_", " "), *group.aliases)
        if clean_text(alias) in haystack
    ]
    if matches:
        return max(matches, key=lambda item: (len(item.split()), len(item)))
    return group.key.replace("_", " ")


def _strip_phrase_leaders(value: str) -> str:
    words = _normalize_phrase(value).split()
    while words and words[0].lower() in _PHRASE_LEADERS:
        words.pop(0)
    return " ".join(words)


def _looks_like_uppercase_command(value: str) -> bool:
    """Reject shouted imperative clauses that the title regex can misread."""

    words = [word for word in _normalize_phrase(value).split() if word.isalpha()]
    return len(words) >= 4 and all(word.isupper() for word in words)


def _phrase_candidates(query: str, groups: list[ConceptGroup]) -> list[str]:
    candidates: list[str] = []
    candidates.extend(match.group(1) for match in _ANSWER_OBJECT_RE.finditer(query))
    candidates.extend(match.group(1) for match in _QUOTE_RE.finditer(query))
    candidates.extend(
        phrase
        for match in _TITLE_RE.finditer(query)
        if not _looks_like_uppercase_command(match.group(1))
        if (phrase := _strip_phrase_leaders(match.group(1)))
    )
    candidates.extend(
        phrase
        for match in _REGULATORY_RE.finditer(query)
        if (phrase := _strip_phrase_leaders(match.group(1)))
    )
    for match in _TARGET_RE.finditer(query):
        target = _strip_phrase_leaders(match.group(1))
        target_terms = set(lexical_terms(target))
        if target_terms and not target_terms <= _GENERIC_LANE_TERMS:
            candidates.append(target)
    for match in _COMMAND_SUBJECT_RE.finditer(query):
        subject = _strip_phrase_leaders(match.group(1))
        subject_terms = set(lexical_terms(subject))
        if subject_terms and not subject_terms <= _GENERIC_LANE_TERMS:
            candidates.append(subject)
    for match in _DESCRIPTOR_RE.finditer(query):
        prefix = _strip_phrase_leaders(match.group(1))
        # A descriptor phrase may begin after an operator/preposition. Keep the
        # final meaningful words so "combine Purple Ocean strategy" becomes
        # "Purple Ocean strategy", not an operator-shaped lane.
        words = prefix.split()
        while words and words[0].lower() in _PHRASE_LEADERS:
            words.pop(0)
        boundary_indexes = [
            index
            for index, word in enumerate(words)
            if word.lower().rstrip(".") in _PHRASE_BOUNDARIES
        ]
        if boundary_indexes and boundary_indexes[-1] < len(words) - 1:
            words = words[boundary_indexes[-1] + 1 :]
        if words:
            candidates.append(" ".join([*words[-4:], match.group(2)]))

    # Curated aliases are stable semantic phrases. Prefer the longest surface
    # form present in the user query (e.g. "Made to Stick").
    candidates.extend(
        _best_surface(group, query) for group in groups if group.key in CONCEPT_ALIASES
    )

    # Common morphology that the curated alias table deliberately keeps small.
    for match in re.finditer(r"\bsticky\s+messag(?:e|es|ing)\b", query, re.I):
        candidates.append(match.group(0))

    seen: set[str] = set()
    output: list[str] = []
    for candidate in candidates:
        phrase = _normalize_phrase(candidate)
        key = clean_text(phrase).strip()
        if not key or key in seen:
            continue
        if len(phrase.split()) == 1 and key in _GENERIC_LANE_TERMS:
            continue
        seen.add(key)
        output.append(phrase)
    ordered = sorted(
        output,
        key=lambda item: (len(item.split()), len(item)),
        reverse=True,
    )
    selected: list[str] = []
    selected_keys: list[str] = []
    for phrase in ordered:
        key = clean_text(phrase).strip()
        if any(f" {key} " in f" {existing} " for existing in selected_keys):
            continue
        selected.append(phrase)
        selected_keys.append(key)
    return selected


def _overlaps_phrase(surface: str, phrases: list[str]) -> bool:
    surface_tokens = set(lexical_terms(surface))
    if not surface_tokens:
        return False
    for phrase in phrases:
        phrase_tokens = set(lexical_terms(phrase))
        if surface_tokens & phrase_tokens:
            return True
    return False


def _stem_token(value: str) -> str:
    token = value.lower()
    for suffix in ("ing", "es", "s", "e"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _curated_group_for_concept(
    concept: str,
    groups: list[ConceptGroup],
) -> ConceptGroup | None:
    concept_tokens = {_stem_token(token) for token in lexical_terms(concept)}
    matches: list[tuple[int, ConceptGroup]] = []
    candidate_groups = list(groups)
    seen_group_keys = {group.key for group in candidate_groups}
    candidate_groups.extend(
        ConceptGroup(key=key, aliases=aliases)
        for key, aliases in CONCEPT_ALIASES.items()
        if key not in seen_group_keys
    )
    for group in candidate_groups:
        if group.key not in CONCEPT_ALIASES:
            continue
        best_overlap = 0
        for alias in group.aliases:
            alias_tokens = {_stem_token(token) for token in lexical_terms(alias)}
            # Single-token aliases such as "ocean" are too ambiguous to map a
            # phrase like Purple Ocean onto an unrelated curated framework.
            if len(alias_tokens) < 2:
                continue
            overlap = len(alias_tokens & concept_tokens)
            if overlap == len(alias_tokens) or overlap >= 2:
                best_overlap = max(best_overlap, overlap)
        if best_overlap:
            matches.append((best_overlap, group))
    return max(matches, key=lambda item: item[0], default=(0, None))[1]


def _lexical_recall_phrases(
    concept: str,
    groups: list[ConceptGroup],
) -> tuple[str, ...]:
    answer_object_phrases = _ANSWER_OBJECT_SUPPORT.get(clean_text(concept).strip())
    if answer_object_phrases:
        return tuple(dict.fromkeys((concept, *answer_object_phrases)))
    group = _curated_group_for_concept(concept, groups)
    if group is None:
        return (concept,)
    phrases = [concept, *concept_support_phrases(group.key, max_phrases=4)]
    return tuple(dict.fromkeys(phrase for phrase in phrases if phrase))


def _complexity(
    query: str, lane_count: int, operators: tuple[str, ...]
) -> QueryComplexity:
    if _DEPENDENCY_RE.search(query) and lane_count > 1:
        return "dependent_multi_hop"
    if "relationship" in operators or _EXPLICIT_MULTI_RE.search(query):
        return "comparative"
    if lane_count > 1:
        return "compositional"
    return "simple"


def _collapse_attribution_concepts(
    query: str,
    concepts: list[str],
    groups: list[ConceptGroup],
) -> list[str]:
    """Treat a named ``according to`` source as the retrieval authority.

    In questions such as "what makes a message sticky according to Made to
    Stick", the surrounding property words describe what to extract from the
    named source. They are not independent evidence lanes. Keeping them as
    lanes turns ordinary words such as ``sticky`` into false domain concepts.
    Comparative and dependent queries retain every semantic side.
    """
    if not re.search(r"\baccording\s+to\b", query, re.IGNORECASE):
        return concepts
    if _EXPLICIT_MULTI_RE.search(query) or _DEPENDENCY_RE.search(query):
        return concepts
    attributed = [
        concept
        for concept in concepts
        if _curated_group_for_concept(concept, groups) is not None
    ]
    if not attributed:
        return concepts
    return [max(attributed, key=lambda item: (len(item.split()), len(item)))]


def _decompose_command_subject(query: str, concepts: list[str]) -> list[str]:
    """Split a cross-domain ``domain + acronym`` command subject into lanes.

    This is deliberately narrow: branded phrases and normal title-cased
    concepts remain intact, while subjects such as ``ECOMMERCE AI`` or
    ``healthcare NLP`` get one evidence lane per domain side.
    """

    match = _COMMAND_SUBJECT_RE.search(query)
    if not match:
        return concepts
    subject = _normalize_phrase(match.group(1))
    words = subject.split()
    if len(words) != 2 or len(words[0]) < 5 or len(words[1]) > 4:
        return concepts
    subject_key = clean_text(subject).strip()
    if not any(clean_text(concept).strip() == subject_key for concept in concepts):
        return concepts
    decomposed: list[str] = []
    for concept in concepts:
        if clean_text(concept).strip() == subject_key:
            decomposed.extend(words)
        else:
            decomposed.append(concept)
    return list(dict.fromkeys(decomposed))


def _message_value(message: Any, field: str) -> str:
    if isinstance(message, dict):
        return str(message.get(field) or "")
    return str(getattr(message, field, "") or "")


def contextualize_followup_query(
    query: str,
    recent_messages: Iterable[Any] | None,
) -> str:
    """Build a deterministic standalone retrieval query for terse follow-ups.

    The answer model still receives the user's exact message. This rewrite is
    retrieval-only and activates only for short/elliptical turns, avoiding an
    extra model call while preventing fragments such as ``no authors`` from
    being embedded without the subject established by the previous user turn.
    """

    current = _normalize_phrase(query)
    if not current:
        return current
    tokens = re.findall(r"[A-Za-z0-9']+", current)
    contextual = len(tokens) <= 6 or bool(_FOLLOWUP_PREFIX_RE.match(current))
    if not contextual:
        return current

    previous_user = ""
    for message in reversed(list(recent_messages or [])):
        if _message_value(message, "role").lower() != "user":
            continue
        candidate = _normalize_phrase(_message_value(message, "content"))
        if candidate and candidate.casefold() != current.casefold():
            previous_user = candidate
            break
    if not previous_user:
        return current

    focus = _FOLLOWUP_PREFIX_RE.sub("", current).strip() or current
    return _normalize_phrase(f"{previous_user}; {focus}")


def build_query_plan_v2(
    query: str,
    *,
    corpus_ids: list[str] | tuple[str, ...] | None = None,
    max_core_lanes: int = 4,
    standalone_query: str | None = None,
) -> QueryPlanV2:
    """Build a bounded phrase-aware plan without an LLM call."""

    original = _normalize_phrase(query)
    standalone = _normalize_phrase(standalone_query or original)
    groups = concept_groups(standalone, max_groups=max_core_lanes + 4)
    phrases = _phrase_candidates(standalone, groups)

    concepts: list[str] = []
    for phrase in phrases:
        if phrase.lower() not in {item.lower() for item in concepts}:
            concepts.append(phrase)

    # Preserve useful bare concepts only when they are not already represented
    # by a phrase. This is the guard against Purple/Ocean fragmentation.
    for group in groups:
        surface = _best_surface(group, standalone)
        key = clean_text(surface).strip()
        if (
            key in _GENERIC_LANE_TERMS
            or key in _PHRASE_LEADERS
            or _overlaps_phrase(surface, concepts)
        ):
            continue
        concepts.append(surface)
        if len(concepts) >= max_core_lanes:
            break
    concepts = _collapse_attribution_concepts(
        standalone,
        concepts[:max_core_lanes],
        groups,
    )
    concepts = _decompose_command_subject(standalone, concepts)[:max_core_lanes]
    deduplicated_concepts: list[str] = []
    seen_concepts: set[str] = set()
    for concept in concepts:
        key = clean_text(concept).strip()
        if not key or key in seen_concepts:
            continue
        deduplicated_concepts.append(concept)
        seen_concepts.add(key)
    concepts = deduplicated_concepts[:max_core_lanes]

    operators = tuple(sorted(required_operator_atoms(standalone)))
    lanes: list[QueryLane] = [
        QueryLane(
            lane_id="original",
            role="original",
            query=standalone,
            dense_text=standalone,
            lexical_terms=tuple(lexical_terms(standalone)[:16]),
            phrase=standalone,
            support_phrases=(standalone,),
        )
    ]
    for index, concept in enumerate(concepts):
        lane_id = _slug(concept) or f"concept_{index + 1}"
        support_phrases = _lexical_recall_phrases(concept, groups)
        recall_query = " ".join(support_phrases)
        lanes.append(
            QueryLane(
                lane_id=lane_id,
                role="core",
                query=recall_query,
                dense_text=concept,
                lexical_terms=tuple(lexical_terms(concept)[:12]),
                phrase=concept,
                support_phrases=support_phrases,
            )
        )

    if len(concepts) > 1 and (
        "relationship" in operators or _EXPLICIT_MULTI_RE.search(standalone)
    ):
        lanes.append(
            QueryLane(
                lane_id="bridge",
                role="bridge",
                query=standalone,
                dense_text=standalone,
                lexical_terms=tuple(lexical_terms(standalone)[:16]),
                required=False,
                depends_on=tuple(lane.lane_id for lane in lanes if lane.role == "core"),
            )
        )

    return QueryPlanV2(
        version="query_plan.v2",
        original_query=original,
        standalone_query=standalone,
        complexity=_complexity(standalone, len(concepts), operators),
        concepts=tuple(concepts),
        operators=operators,
        lanes=tuple(lanes),
        corpus_ids=tuple(str(item) for item in (corpus_ids or ())),
    )


def query_plan_curation_query(plan: QueryPlanV2) -> str:
    """Return the semantic subject used for reranking and answerability.

    Simple imperative requests often contain output-format and interaction
    words (for example, "create an HTML test about X"). The original lane is
    still searched for recall, but those command words must not become required
    evidence atoms. Comparative/relational plans retain their full wording.
    """

    if (
        plan.complexity in {"simple", "compositional"}
        and plan.concepts
        and not plan.operators
    ):
        phrases: list[str] = []
        for concept in plan.concepts:
            phrases.append(concept)
            phrases.extend(_ANSWER_OBJECT_SUPPORT.get(clean_text(concept).strip(), ())[:4])
        return " ".join(dict.fromkeys(phrases))
    return plan.original_query


def query_plan_to_dict(plan: QueryPlanV2) -> dict[str, object]:
    return {
        "version": plan.version,
        "original_query": plan.original_query,
        "standalone_query": plan.standalone_query,
        "complexity": plan.complexity,
        "concepts": list(plan.concepts),
        "operators": list(plan.operators),
        "corpus_ids": list(plan.corpus_ids),
        "max_repair_rounds": plan.max_repair_rounds,
        "lanes": [
            {
                "lane_id": lane.lane_id,
                "role": lane.role,
                "query": lane.query,
                "dense_text": lane.dense_text,
                "lexical_terms": list(lane.lexical_terms),
                "required": lane.required,
                "depends_on": list(lane.depends_on),
                "phrase": lane.phrase,
                "support_phrases": list(lane.support_phrases),
            }
            for lane in plan.lanes
        ],
    }


def query_plan_evidence_sides(plan: QueryPlanV2) -> list[dict[str, object]]:
    """Convert core lanes to the existing evidence-plan input contract."""

    return [
        {
            "name": lane.lane_id,
            "label": lane.phrase or lane.query,
            "query": lane.query,
            "search_terms": [lane.phrase or lane.query, *lane.lexical_terms],
        }
        for lane in plan.lanes
        if lane.role == "core"
    ]
