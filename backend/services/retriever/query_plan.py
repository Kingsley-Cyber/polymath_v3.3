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
ProbeRole = Literal["primary", "support", "bridge"]
AnswerShape = Literal[
    "single_fact",
    "enumeration",
    "comparison",
    "relationship",
    "procedure",
    "synthesis",
]

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
_PROCEDURE_INTENT_RE = re.compile(
    r"\b(?:how\s+(?:do|should|can)\s+i|where\s+do\s+i\s+(?:begin|start)|"
    r"what\s+do\s+i\s+do|day\s*(?:0|zero)|first\s+steps?|getting\s+started|"
    r"start(?:ing)?\s+from\s+scratch)\b",
    re.IGNORECASE,
)
_JUSTIFICATION_RE = re.compile(
    r"\b(?:and\s+)?why\b|\breasons?\b|\bwhy\s+(?:it|they|these)\b",
    re.IGNORECASE,
)
_BEGINNER_RE = re.compile(
    r"\b(?:complete\s+beginner|beginner|newcomer|starting\s+from\s+scratch|day\s*(?:0|zero))\b",
    re.IGNORECASE,
)
_DAY_ZERO_RE = re.compile(r"\bday\s*(?:0|zero)\b", re.IGNORECASE)
_FOLLOWUP_PREFIX_RE = re.compile(
    r"^(?:no[,:]?\s+|actually[,:]?\s+|instead[,:]?\s+|"
    r"and\s+|also\s+|what\s+about\s+)",
    re.IGNORECASE,
)
_REFERENTIAL_FOLLOWUP_RE = re.compile(
    r"\b(?:this|that|these|those|it|its|they|their|them|the\s+(?:product|"
    r"audience|answer|idea|concept|example|scene|video|prompt|strategy|"
    r"framework|document|book|ad|advertisement)|above|earlier|previous)\b",
    re.IGNORECASE,
)
_QUESTION_OBLIGATION_SPLIT_RE = re.compile(
    r"\s+(?:and|then)\s+(?=(?:how|what|why|which|who|where|when|can|"
    r"should|would|could|is|are|do|does)\b)",
    re.IGNORECASE,
)
_QUESTION_OBLIGATION_START_RE = re.compile(
    r"^(?:how|what|why|which|who|where|when|can|should|would|could|is|are|"
    r"do|does|identify|find|create|write|design|explain|compare|recommend|"
    r"list|assess|evaluate)\b",
    re.IGNORECASE,
)
_COORDINATED_OBJECTIVE_VERBS = (
    "apply",
    "attract",
    "build",
    "choose",
    "communicate",
    "create",
    "demonstrate",
    "design",
    "explain",
    "find",
    "hook",
    "identify",
    "make",
    "measure",
    "open",
    "persuade",
    "position",
    "predict",
    "prompt",
    "show",
    "stage",
    "start",
    "structure",
    "target",
    "test",
    "use",
    "write",
)
_COORDINATED_OBJECTIVE_HEAD_RE = re.compile(
    r"^(?P<operator>how\s+(?:should|can|would|could|do|does)\s+)"
    r"(?P<subject>.+?)\s+"
    r"(?P<objectives>(?:" + "|".join(_COORDINATED_OBJECTIVE_VERBS) + r")\b.+)$",
    re.IGNORECASE,
)
_COORDINATED_OBJECTIVE_SPLIT_RE = re.compile(
    r"(?:,\s*(?:and\s+)?|\s+and\s+)"
    r"(?=(?:" + "|".join(_COORDINATED_OBJECTIVE_VERBS) + r")\b)",
    re.IGNORECASE,
)
_ANSWER_OBJECT_SUPPORT: dict[str, tuple[str, ...]] = {
    "book": ("book titles", "authors", "book recommendations", "lessons", "principles"),
    "books": (
        "book titles",
        "authors",
        "book recommendations",
        "lessons",
        "principles",
    ),
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
    "if",
    "so",
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
    "if",
    "it",
    "my",
    "relate",
    "require",
    "requires",
    "then",
    "the",
    "so",
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
class RetrievalProbe:
    """One independently meaningful retrieval obligation.

    A probe is deliberately a complete question rather than a keyword lane.
    ``concepts`` and ``constraints`` remain structured so fusion can measure
    coverage without parsing the generated question back into fragments.
    """

    probe_id: str
    question: str
    answer_type: AnswerShape
    role: ProbeRole = "primary"
    required: bool = True
    concepts: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPlanV2:
    version: str
    original_query: str
    standalone_query: str
    complexity: QueryComplexity
    concepts: tuple[str, ...]
    operators: tuple[str, ...]
    lanes: tuple[QueryLane, ...]
    probes: tuple[RetrievalProbe, ...] = ()
    answer_shape: AnswerShape = "single_fact"
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
    if not words or not all(word.isupper() for word in words):
        return False
    normalized = {word.lower() for word in words}
    return len(words) >= 4 or normalized <= _PHRASE_LEADERS


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


def answer_object_lane_ids(plan: QueryPlanV2) -> tuple[str, ...]:
    """Return core lanes that describe the objects the answer must enumerate."""

    if plan.probes:
        return tuple(
            probe.probe_id
            for probe in plan.probes
            if probe.answer_type == "enumeration"
        )
    if plan.answer_shape != "enumeration":
        return ()
    return tuple(
        lane.lane_id
        for lane in plan.lanes
        if lane.role == "core"
        and clean_text(lane.phrase or "").strip() in _ANSWER_OBJECT_SUPPORT
    )


def answer_object_title_terms(plan: QueryPlanV2) -> dict[str, tuple[str, ...]]:
    """Expose conservative title terms for top-down answer-object routing."""

    answer_lanes = set(answer_object_lane_ids(plan))
    execution_lanes = query_plan_execution_lanes(plan)
    return {
        lane.lane_id: tuple(
            dict.fromkeys(
                term
                for term in re.findall(
                    r"[a-z0-9]+", (lane.phrase or lane.lane_id).lower()
                )
                if len(term) >= 3
            )
        )
        for lane in execution_lanes
        if lane.lane_id in answer_lanes
    }


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


def _answer_shape(query: str, complexity: QueryComplexity) -> AnswerShape:
    normalized = clean_text(query).strip()
    if _ANSWER_OBJECT_RE.search(query or "") and _PROCEDURE_INTENT_RE.search(
        query or ""
    ):
        return "synthesis"
    if _ANSWER_OBJECT_RE.search(query or ""):
        return "enumeration"
    if complexity == "comparative" or re.search(
        r"\b(?:compare|contrast|versus|vs)\b", normalized
    ):
        return "comparison"
    if re.search(r"\b(?:relationship|relate|between|connect|link)\b", normalized):
        return "relationship"
    if normalized.startswith("how ") or re.search(
        r"\b(?:steps|procedure|process)\b", normalized
    ):
        return "procedure"
    if complexity in {"compositional", "dependent_multi_hop"}:
        return "synthesis"
    return "single_fact"


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
    contextual = (
        len(tokens) <= 6
        or bool(_FOLLOWUP_PREFIX_RE.match(current))
        or bool(_REFERENTIAL_FOLLOWUP_RE.search(current))
    )
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


def _question_obligation_clauses(query: str) -> tuple[str, ...]:
    """Split compound requests into complete retrieval questions.

    This is intentionally narrower than generic sentence splitting. It only
    creates another obligation at a sentence boundary or before a new
    question operator (``and how``, ``and why``, and similar forms), so named
    concepts and ordinary noun conjunctions remain intact.
    """

    clauses: list[str] = []
    for sentence in re.split(r"[.!?;]+", str(query or "")):
        normalized = _normalize_phrase(sentence)
        if not normalized:
            continue
        coordinated = _COORDINATED_OBJECTIVE_HEAD_RE.match(normalized)
        if coordinated:
            objectives = [
                _normalize_phrase(value)
                for value in _COORDINATED_OBJECTIVE_SPLIT_RE.split(
                    coordinated.group("objectives")
                )
                if _normalize_phrase(value)
            ]
            if len(objectives) >= 2:
                prefix = coordinated.group("operator") + coordinated.group("subject")
                clauses.extend(
                    _normalize_phrase(f"{prefix} {objective}")
                    for objective in objectives
                )
                continue
        clauses.extend(
            part
            for part in (
                _normalize_phrase(value)
                for value in _QUESTION_OBLIGATION_SPLIT_RE.split(normalized)
            )
            if part and _QUESTION_OBLIGATION_START_RE.match(part)
        )
    if len(clauses) < 2:
        return ()
    return tuple(dict.fromkeys(clauses[:4]))


def _clause_concepts(clause: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Return coverage atoms for one obligation without changing its wording."""

    clause_terms = tuple(lexical_terms(clause))
    clause_term_set = set(clause_terms)
    matched = [
        concept for concept in fallback if set(lexical_terms(concept)) & clause_term_set
    ]
    if matched:
        return tuple(dict.fromkeys(matched))
    return tuple(
        term
        for term in clause_terms
        if term not in _GENERIC_LANE_TERMS and len(term) >= 3
    )[:6]


def _probe_constraints(query: str) -> tuple[str, ...]:
    constraints: list[str] = []
    if _BEGINNER_RE.search(query):
        constraints.append("beginner")
    if _DAY_ZERO_RE.search(query):
        constraints.append("Day 0")
    return tuple(constraints)


def _probe_topic_concepts(concepts: list[str]) -> tuple[str, ...]:
    return tuple(
        concept
        for concept in concepts
        if clean_text(concept).strip() not in _ANSWER_OBJECT_SUPPORT
        and clean_text(concept).strip() not in _GENERIC_LANE_TERMS
    )


def _build_retrieval_probes(
    query: str,
    concepts: list[str],
    *,
    complexity: QueryComplexity,
    answer_shape: AnswerShape,
) -> tuple[RetrievalProbe, ...]:
    """Turn user obligations into bounded, independently useful questions."""

    constraints = _probe_constraints(query)
    topic_concepts = _probe_topic_concepts(concepts)
    topic = " and ".join(topic_concepts[:2]).strip()
    object_match = _ANSWER_OBJECT_RE.search(query)
    answer_object = _normalize_phrase(object_match.group(1)) if object_match else ""
    is_procedure = bool(_PROCEDURE_INTENT_RE.search(query))
    is_enumeration = bool(answer_object)
    probes: list[RetrievalProbe] = []

    obligation_clauses = _question_obligation_clauses(query)
    if obligation_clauses and not is_enumeration:
        for index, clause in enumerate(obligation_clauses):
            clause_concepts = _clause_concepts(clause, topic_concepts)
            clause_shape = _answer_shape(clause, "simple")
            probes.append(
                RetrievalProbe(
                    probe_id=(
                        _slug(" ".join(lexical_terms(clause)))
                        or f"objective_{index + 1}"
                    ),
                    question=clause,
                    answer_type=(
                        "synthesis" if clause_shape == "single_fact" else clause_shape
                    ),
                    role="primary" if index == 0 else "support",
                    required=True,
                    concepts=clause_concepts,
                    constraints=constraints,
                )
            )

    if is_procedure and not probes:
        audience = (
            "a beginner"
            if "beginner" in constraints or "Day 0" in constraints
            else "someone"
        )
        timing = " on Day 0" if "Day 0" in constraints else ""
        subject = topic or "this task"
        probes.append(
            RetrievalProbe(
                probe_id="day_zero_steps"
                if "Day 0" in constraints
                else "starting_steps",
                question=f"What steps should {audience} take{timing} to start {subject}?",
                answer_type="procedure",
                concepts=topic_concepts,
                constraints=constraints,
            )
        )

    if is_enumeration:
        subject = f" for {topic}" if topic else ""
        is_beginner = "beginner" in constraints or "Day 0" in constraints
        normalized_object = clean_text(answer_object).strip() or "items"
        enumeration_question = (
            f"Which {answer_object} does the corpus recommend to a beginner"
            f" starting {topic}?"
            if is_beginner and topic
            else f"Which {answer_object} does the corpus recommend{subject}?"
        )
        probes.append(
            RetrievalProbe(
                probe_id=(
                    f"beginner_{_slug(normalized_object)}"
                    if is_beginner
                    else _slug(normalized_object)
                ),
                question=enumeration_question,
                answer_type="enumeration",
                concepts=tuple(dict.fromkeys((answer_object, *topic_concepts))),
                constraints=constraints,
            )
        )
        if _JUSTIFICATION_RE.search(query):
            justification_question = (
                f"Why are the recommended {answer_object} useful to a beginner"
                f" starting {topic}?"
                if is_beginner and topic
                else f"Why are the recommended {answer_object} useful{subject}?"
            )
            probes.append(
                RetrievalProbe(
                    probe_id=f"{_slug(normalized_object)}_justification",
                    question=justification_question,
                    answer_type="synthesis",
                    role="support",
                    required=True,
                    concepts=tuple(dict.fromkeys((answer_object, *topic_concepts))),
                    constraints=constraints,
                    depends_on=(probes[-1].probe_id,),
                )
            )

    if not probes and complexity in {"comparative", "dependent_multi_hop"}:
        for concept in concepts[:3]:
            probes.append(
                RetrievalProbe(
                    probe_id=_slug(concept),
                    question=f"What does the corpus establish about {concept}?",
                    answer_type="single_fact",
                    concepts=(concept,),
                )
            )
        if len(probes) > 1:
            probes.append(
                RetrievalProbe(
                    probe_id="relationship",
                    question=_normalize_phrase(query) + "?",
                    answer_type=(
                        "relationship"
                        if answer_shape == "relationship"
                        else "comparison"
                    ),
                    role="bridge",
                    required=True,
                    concepts=tuple(concepts[:3]),
                    depends_on=tuple(probe.probe_id for probe in probes),
                )
            )

    if not probes:
        probes.append(
            RetrievalProbe(
                probe_id="primary",
                question=_normalize_phrase(query) + "?",
                answer_type=answer_shape,
                concepts=tuple(concepts),
                constraints=constraints,
            )
        )

    output: list[RetrievalProbe] = []
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    for probe in probes[:4]:
        question = _normalize_phrase(probe.question)
        if question and question[0].islower():
            question = question[0].upper() + question[1:]
        question += "?"
        question_key = clean_text(question).strip()
        probe_id = probe.probe_id or f"probe_{len(output) + 1}"
        if not question_key or question_key in seen_questions or probe_id in seen_ids:
            continue
        # No keyword fragments: every executable probe must be a sentence-like
        # question with a subject beyond request scaffolding.
        if len(re.findall(r"[A-Za-z0-9]+", question)) < 4:
            continue
        output.append(
            RetrievalProbe(
                probe_id=probe_id,
                question=question,
                answer_type=probe.answer_type,
                role=probe.role,
                required=probe.required,
                concepts=probe.concepts,
                constraints=probe.constraints,
                depends_on=probe.depends_on,
            )
        )
        seen_ids.add(probe_id)
        seen_questions.add(question_key)
    return tuple(output)


def query_plan_execution_lanes(plan: QueryPlanV2) -> tuple[QueryLane, ...]:
    """Compile complete probes into the legacy lane executor contract."""

    original_lane = next(
        (lane for lane in plan.lanes if lane.role == "original"), plan.lanes[0]
    )
    if not plan.probes:
        return tuple(lane for lane in plan.lanes if lane.role in {"original", "core"})

    lanes: list[QueryLane] = [original_lane]
    groups = concept_groups(plan.standalone_query, max_groups=8)
    for probe in plan.probes:
        concepts = tuple(concept for concept in probe.concepts if concept)
        answer_object = next(
            (
                concept
                for concept in concepts
                if clean_text(concept).strip() in _ANSWER_OBJECT_SUPPORT
            ),
            None,
        )
        anchor = answer_object or next(iter(concepts), probe.question)
        grounding_concepts = (answer_object,) if answer_object else concepts
        support_phrases: list[str] = []
        for concept in concepts:
            support_phrases.extend(_lexical_recall_phrases(concept, groups))
        support_phrases.extend(probe.constraints)
        dense_text = (
            " ".join(_lexical_recall_phrases(answer_object, groups))
            if answer_object
            else probe.question
        )
        if plan.standalone_query != plan.original_query and not answer_object:
            dense_text = f"{probe.question} Context: {plan.standalone_query}"
        lanes.append(
            QueryLane(
                lane_id=probe.probe_id,
                # The executor treats every required probe as a core evidence
                # lane. Probe.role still records whether it is a primary,
                # support, or bridge obligation for diagnostics/synthesis.
                role="core",
                query=probe.question,
                # Keep the executable probe as a complete question while
                # giving document routing an answer-object representation.
                # Otherwise topic words (for example, dropshipping) dominate
                # and route a books probe to generic topic documents.
                dense_text=dense_text,
                lexical_terms=tuple(
                    dict.fromkeys(
                        re.findall(
                            r"[a-z0-9]+",
                            " ".join(grounding_concepts).lower(),
                        )
                    )
                )[:16],
                required=probe.required,
                depends_on=probe.depends_on,
                phrase=anchor,
                support_phrases=tuple(dict.fromkeys(support_phrases or [anchor])),
            )
        )
    return tuple(lanes)


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
        is_answer_object = clean_text(concept).strip() in _ANSWER_OBJECT_SUPPORT
        lanes.append(
            QueryLane(
                lane_id=lane_id,
                role="core",
                query=recall_query,
                # Bare answer nouns are weak document-routing vectors. Add
                # the requested object shape so titles/authors/list documents
                # separate from generic prose that merely mentions the noun.
                dense_text=recall_query if is_answer_object else concept,
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

    complexity = _complexity(standalone, len(concepts), operators)
    answer_shape = _answer_shape(standalone, complexity)
    probes = _build_retrieval_probes(
        original,
        concepts,
        complexity=complexity,
        answer_shape=answer_shape,
    )
    return QueryPlanV2(
        version="query_plan.v2",
        original_query=original,
        standalone_query=standalone,
        complexity=complexity,
        concepts=tuple(concepts),
        operators=operators,
        lanes=tuple(lanes),
        probes=probes,
        answer_shape=answer_shape,
        corpus_ids=tuple(str(item) for item in (corpus_ids or ())),
    )


def query_plan_curation_query(plan: QueryPlanV2) -> str:
    """Return the semantic subject used for reranking and answerability.

    Simple imperative requests often contain output-format and interaction
    words (for example, "create an HTML test about X"). The original lane is
    still searched for recall, but those command words must not become required
    evidence atoms. Comparative/relational plans retain their full wording.
    """

    if plan.standalone_query != plan.original_query:
        return plan.standalone_query
    if len(plan.probes) > 1:
        # A single concept concatenation previously converted compositional
        # questions into fragments. Preserve the user's complete formulation
        # for the one cross-encoder pass; individual probes already drive
        # candidate generation and required-coverage reservations.
        return plan.standalone_query
    if (
        plan.complexity in {"simple", "compositional"}
        and plan.concepts
        and not plan.operators
    ):
        phrases: list[str] = []
        for concept in plan.concepts:
            phrases.append(concept)
            phrases.extend(
                _ANSWER_OBJECT_SUPPORT.get(clean_text(concept).strip(), ())[:4]
            )
        return " ".join(dict.fromkeys(phrases))
    return plan.original_query


def query_plan_to_dict(plan: QueryPlanV2) -> dict[str, object]:
    return {
        "version": plan.version,
        "original_query": plan.original_query,
        "standalone_query": plan.standalone_query,
        "complexity": plan.complexity,
        "answer_shape": plan.answer_shape,
        "concepts": list(plan.concepts),
        "operators": list(plan.operators),
        "corpus_ids": list(plan.corpus_ids),
        "max_repair_rounds": plan.max_repair_rounds,
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

    if plan.probes:
        return [
            {
                "name": probe.probe_id,
                "label": probe.question,
                "query": probe.question,
                "search_terms": [
                    probe.question,
                    *probe.concepts,
                    *probe.constraints,
                ],
            }
            for probe in plan.probes
            if probe.required
        ]
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
