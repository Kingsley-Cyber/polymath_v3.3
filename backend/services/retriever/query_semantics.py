"""Shared query semantics for retrieval.

The retriever uses the same query interpretation in lexical recall,
query-grounded ranking, intent selection, and final answerability checks.
Keeping those rules here prevents one stage from treating an operator such as
"correlate" as a content keyword while another stage treats it as a relation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]{1,}")

BASE_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "actually",
        "across",
        "after",
        "again",
        "against",
        "also",
        "all",
        "an",
        "and",
        "are",
        "as",
        "at",
        "basically",
        "be",
        "because",
        "before",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "during",
        "each",
        "essentially",
        "for",
        "full",
        "from",
        "further",
        "give",
        "has",
        "have",
        "having",
        "here",
        "hers",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "of",
        "on",
        "only",
        "or",
        "other",
        "ours",
        "over",
        "role",
        "same",
        "should",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "under",
        "until",
        "very",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "your",
        "book",
        "books",
        "corpus",
        "document",
        "documents",
        "library",
        "libraries",
        "source",
        "sources",
        # possessive pronouns + generic scope modifiers ("across MY ENTIRE
        # library", "the OVERALL picture"). Function words with no answer-bearing
        # signal — they must never survive as a concept/lane or a lexical anchor.
        "my",
        "our",
        "entire",
        "overall",
    }
)

RELATIONSHIP_TERMS: frozenset[str] = frozenset(
    {
        "associate",
        "associated",
        "association",
        "between",
        "connect",
        "connected",
        "connection",
        "correlate",
        "correlated",
        "correlates",
        "correlation",
        "interplay",
        "link",
        "linked",
        "relationship",
        "relation",
        "relate",
        "related",
    }
)

COMPARISON_TERMS: frozenset[str] = frozenset(
    {
        "compare",
        "compared",
        "comparison",
        "contrast",
        "differences",
        "different",
        "similarities",
        "versus",
        "vs",
    }
)

DEFINITION_TERMS: frozenset[str] = frozenset(
    {
        "define",
        "definition",
        "meaning",
        "overview",
        "explain",
        "describe",
        "show",
        "tell",
    }
)

PROCEDURE_TERMS: frozenset[str] = frozenset(
    {
        "process",
        "procedure",
        "steps",
        "workflow",
    }
)

METHOD_TERMS: frozenset[str] = frozenset(
    {
        "application",
        "applications",
        "example",
        "examples",
        "include",
        "task",
        "tasks",
    }
)

# These words can be useful in prose, but they are too broad to act as
# retrieval anchors. They should not satisfy "query concept coverage" by
# themselves or drag random demographic/statistical passages into the pool.
GENERIC_CONTEXT_TERMS: frozenset[str] = frozenset(
    {
        "dating",
        "man",
        "men",
        "people",
        "person",
        "woman",
        "women",
    }
)

# Abstract scaffolding nouns. They are meaningful for *lexical* recall, but they
# must not become a standalone evidence LANE: "analysis" alone embeds near "data
# analysis", so an arbitrary technical book gets pulled into a psychology side.
# A token here only anchors a lane when it is part of a curated multi-word
# concept (CONCEPT_ALIASES); on its own it is skipped during lane building.
GENERIC_CONCEPT_TOKENS: frozenset[str] = frozenset(
    {
        # generic quantifiers / interrogatives / common verbs — question
        # scaffolding, never the evidence anchor (e.g. "how MANY eggs", "what
        # HAPPENS when X READS above Y"). Leaving them as required concepts makes
        # the answerability gate refuse even when the answer is in the chunk.
        "many",
        "much",
        "several",
        "ever",
        "been",
        "happen",
        "happens",
        "happened",
        "occur",
        "occurs",
        "occurred",
        "read",
        "reads",
        "make",
        "makes",
        "made",
        "analysis",
        "analyses",
        "approach",
        "approaches",
        "aspect",
        "aspects",
        "area",
        "areas",
        "concept",
        "concepts",
        "element",
        "elements",
        "factor",
        "factors",
        "framework",
        "frameworks",
        "idea",
        "ideas",
        "method",
        "methods",
        "methodology",
        "notion",
        "perspective",
        "perspectives",
        "principle",
        "principles",
        "strategies",
        "strategy",
        "tactic",
        "tactics",
        "technique",
        "techniques",
        "theory",
        "theories",
        "thing",
        "things",
        "topic",
        "topics",
        "spectrum",
        # thematic-survey scaffolding — "what are the MAJOR RECURRING THEMES
        # across my library". These describe the SHAPE of a broad overview
        # request, never a substantive evidence anchor, so they must not
        # decompose into standalone lanes (which the answerability gate would
        # then enforce on stopword overlap, dropping otherwise-valid breadth).
        "theme",
        "themes",
        "thematic",
        "major",
        "minor",
        "recurring",
        "overarching",
        "commonality",
        "commonalities",
        "motif",
        "motifs",
        "throughline",
        "throughlines",
        "type",
        "types",
        "vulnerabilities",
        "vulnerability",
        "vulnerable",
        "way",
        "ways",
    }
)

CONCEPT_ONLY_STOP_WORDS: frozenset[str] = frozenset(
    {
        "use",
        "uses",
        "using",
    }
)

OPERATOR_TERMS: frozenset[str] = (
    RELATIONSHIP_TERMS
    | COMPARISON_TERMS
    | DEFINITION_TERMS
    | PROCEDURE_TERMS
    | METHOD_TERMS
)
CONCEPT_STOP_WORDS: frozenset[str] = (
    BASE_STOP_WORDS | OPERATOR_TERMS | GENERIC_CONTEXT_TERMS | CONCEPT_ONLY_STOP_WORDS
)
LEXICAL_STOP_WORDS: frozenset[str] = (
    BASE_STOP_WORDS | OPERATOR_TERMS | GENERIC_CONTEXT_TERMS
)
TOKEN_DISPLAY_STOP_WORDS: frozenset[str] = BASE_STOP_WORDS

RELATIONSHIP_MARKERS: tuple[str, ...] = tuple(sorted(RELATIONSHIP_TERMS | COMPARISON_TERMS))
DEFINITION_MARKERS: tuple[str, ...] = (
    "what is",
    "what are",
    "define",
    "definition",
    "meaning of",
    "stands for",
)
PROCEDURE_MARKERS: tuple[str, ...] = (
    "how to",
    "process",
    "procedure",
    "steps",
    "workflow",
)
METHOD_MARKERS: tuple[str, ...] = (
    "application",
    "applications",
    "example",
    "examples",
    "include",
    "task",
    "tasks",
)

CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "artificial intelligence"),
    "db": ("db", "database"),
    "llm": ("llm", "large language model", "large language models"),
    "ml": ("ml", "machine learning"),
    "nlp": ("nlp", "natural language processing"),
    "personality": (
        "personality",
        "character",
        "character trait",
        "character traits",
        "profile",
        "profiles",
        "temperament",
        "trait",
        "traits",
        "type",
        "types",
    ),
    "personality_framework": (
        "personality framework",
        "personality frameworks",
        "personality test",
        "personality tests",
        "personality assessment",
        "personality assessments",
        "personality inventory",
        "personality inventories",
        "personality type",
        "personality types",
        "personality trait",
        "personality traits",
        "personality scale",
        "personality scales",
        "personality questionnaire",
        "personality questionnaires",
        "four tendencies",
        "big five",
        "ocean",
        "myers briggs",
        "mbti",
        "enneagram",
        "temperament theory",
        "personality typology",
        "handbook of personality",
        "the handbook of personality",
        "personality handbook",
    ),
    "rag": (
        "rag",
        "retrieval augmented generation",
        "retrieval-augmented generation",
    ),
    "seduction": ("seduction", "seductive", "seduce", "seducer", "seducers"),
    "sql": ("sql", "structured query language"),
}


@dataclass(frozen=True)
class ConceptGroup:
    """A query concept plus deterministic surface-form alternatives."""

    key: str
    aliases: tuple[str, ...]


def clean_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return f" {' '.join(value.split())} "


def normalize_query(query: str) -> str:
    return " ".join((query or "").lower().split())


def query_tokens(query: str, *, stop_words: frozenset[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in TOKEN_RE.findall(query or ""):
        token = raw.lower().strip("-_'")
        if len(token) < 2 or token in stop_words or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def token_variants(token: str) -> tuple[str, ...]:
    variants = [token]
    if len(token) > 3 and token.endswith("s"):
        variants.append(token[:-1])
    elif len(token) > 3:
        variants.append(f"{token}s")
    return tuple(dict.fromkeys(variants))


def _phrase_concept_groups(query: str) -> list[ConceptGroup]:
    haystack = clean_text(query)
    groups: list[ConceptGroup] = []
    seen: set[str] = set()
    personality_framework_markers = (
        "personality test",
        "personality tests",
        "personality type",
        "personality types",
        "personality trait",
        "personality traits",
        "personality framework",
        "personality frameworks",
        "personality assessment",
        "personality inventory",
        "personality inventories",
        "different personality",
        "different personalities",
        "four tendencies",
        "big five",
        "myers briggs",
        "mbti",
        "enneagram",
    )
    if any(alias_matches(marker, haystack) for marker in personality_framework_markers):
        groups.append(
            ConceptGroup(
                key="personality_framework",
                aliases=CONCEPT_ALIASES["personality_framework"],
            )
        )
        seen.add("personality_framework")
    for key, aliases in CONCEPT_ALIASES.items():
        if key in seen or key == "personality_framework":
            continue
        detection_aliases: list[str] = [key]
        for alias in aliases:
            alias_tokens = alias.split()
            if key == "personality" and alias not in {
                "personality",
                "character trait",
                "character traits",
            }:
                continue
            if len(alias_tokens) >= 2:
                detection_aliases.append(alias)
        if any(alias_matches(alias, haystack) for alias in detection_aliases):
            groups.append(ConceptGroup(key=key, aliases=aliases))
            seen.add(key)
    return groups


def concept_groups(query: str, *, max_groups: int = 8) -> list[ConceptGroup]:
    groups: list[ConceptGroup] = _phrase_concept_groups(query)
    seen = {group.key for group in groups}
    haystack = clean_text(query)
    covered_tokens: set[str] = set()
    for group in groups:
        for alias in group.aliases:
            if alias_matches(alias, haystack):
                covered_tokens.update(
                    query_tokens(alias, stop_words=frozenset())
                )
                break
    normalized = normalize_query(query)
    for token in query_tokens(query, stop_words=CONCEPT_STOP_WORDS):
        if token in covered_tokens:
            continue
        # In phrases like "the art of seduction", "art" is part of a title /
        # expression, not an evidence concept that should compete with the
        # substantive side of the question.
        if token == "art" and "art of" in normalized:
            continue
        # Abstract scaffolding nouns ("analysis", "approach", "theory") don't
        # identify a document set on their own and embed near unrelated
        # technical content, so they must not become a standalone evidence lane.
        # They still anchor a lane when curated (a named multi-word concept).
        if token in GENERIC_CONCEPT_TOKENS and token not in CONCEPT_ALIASES:
            continue
        if token in seen:
            continue
        aliases = CONCEPT_ALIASES.get(token) or token_variants(token)
        groups.append(ConceptGroup(key=token, aliases=aliases))
        seen.add(token)
        if len(groups) >= max_groups:
            break
    return groups


def lexical_terms(query: str) -> list[str]:
    return query_tokens(query, stop_words=LEXICAL_STOP_WORDS)


def token_display_terms(text: str) -> set[str]:
    return set(query_tokens(text, stop_words=TOKEN_DISPLAY_STOP_WORDS))


def alias_matches(alias: str, haystack: str) -> bool:
    alias_clean = clean_text(alias)
    if not alias_clean.strip():
        return False
    return alias_clean in haystack


def group_matches_text(group: ConceptGroup, text: str) -> bool:
    haystack = clean_text(text)
    return any(alias_matches(alias, haystack) for alias in group.aliases)


def has_marker(query: str, markers: tuple[str, ...] | frozenset[str]) -> bool:
    haystack = clean_text(query)
    return any(alias_matches(marker, haystack) for marker in markers)


def required_operator_atoms(query: str | None) -> set[str]:
    q = query or ""
    required: set[str] = set()
    if has_marker(q, DEFINITION_MARKERS):
        required.add("definition")
    if has_marker(q, RELATIONSHIP_MARKERS):
        required.add("relationship")
    if has_marker(q, METHOD_MARKERS):
        required.add("methods_tasks")
    if has_marker(q, PROCEDURE_MARKERS):
        required.add("procedure")
    return required


def is_curated_concept(key: str | None) -> bool:
    """True when a concept key is a curated alias entry (a strong, named concept)
    rather than a bare query token. Used to decide whether a deterministic
    evidence plan is strong enough or should be re-decomposed (LLM path)."""

    return str(key or "") in CONCEPT_ALIASES


def required_atoms_for_query(query: str | None, *, max_concepts: int = 4) -> set[str]:
    required = {
        f"concept:{group.key}"
        for group in concept_groups(query or "", max_groups=max_concepts)
    }
    required.update(required_operator_atoms(query))
    return required


_SIDE_SPLIT_SEPARATORS: tuple[str, ...] = (
    " versus ",
    " vs. ",
    " vs ",
    " compared with ",
    " compared to ",
    " contrasted with ",
)


def split_query_sides(query: str | None, *, max_sides: int = 2) -> list[dict]:
    """Heuristic, no-LLM split of a clearly binary question into source sides.

    Handles "relationship/difference between A and B" and "A versus / compared
    to B". Returns side dicts ``{name, label, search_terms, query}`` only when a
    clean two-part split is found; otherwise ``[]``. Deliberately conservative:
    it does NOT split on a bare "and"/"with" (which would mangle ordinary
    questions) — the curated ``concept_groups`` path already covers those. This
    is the no-LLM half of the generalization: an arbitrary "X vs Y" over books
    the alias table has never seen still decomposes into two grounded sides.
    """

    normalized = normalize_query(query)
    if not normalized:
        return []

    parts: list[str] = []
    between = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+)$", normalized)
    if between:
        parts = [between.group(1), between.group(2)]
    else:
        for sep in _SIDE_SPLIT_SEPARATORS:
            if sep in normalized:
                left, _, right = normalized.partition(sep)
                parts = [left, right]
                break
    if len(parts) < 2:
        return []

    sides: list[dict] = []
    seen: set[str] = set()
    for part in parts[:max_sides]:
        tokens = query_tokens(part, stop_words=CONCEPT_STOP_WORDS)
        if not tokens:
            continue
        phrase = " ".join(part.split()).strip()
        name = "_".join(tokens[:3])
        if not name or name in seen:
            continue
        seen.add(name)
        terms: list[str] = []
        if phrase:
            terms.append(phrase)
        terms.extend(tokens[:6])
        sides.append(
            {
                "name": name,
                "label": phrase or name.replace("_", " "),
                "search_terms": terms,
                "query": phrase or " ".join(tokens[:6]),
            }
        )
    return sides if len(sides) >= 2 else []
