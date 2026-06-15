"""Deterministic query-concept coverage helpers for retrieval.

These helpers are intentionally lexical and bounded. They do not decide what
to answer; they only keep the evidence packet honest when vector/rerank scores
surface candidates that do not touch the user's core concepts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from models.schemas import SourceChunk
from services.facets.runtime import metadata_facet_terms

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]{1,}")

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "could",
        "define",
        "describe",
        "did",
        "do",
        "does",
        "explain",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "of",
        "on",
        "or",
        "overview",
        "relation",
        "relationship",
        "role",
        "show",
        "tell",
        "that",
        "the",
        "their",
        "this",
        "to",
        "use",
        "uses",
        "using",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)

_ALIASES: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "artificial intelligence"),
    "db": ("db", "database"),
    "llm": ("llm", "large language model", "large language models"),
    "ml": ("ml", "machine learning"),
    "nlp": ("nlp", "natural language processing"),
    "rag": (
        "rag",
        "retrieval augmented generation",
        "retrieval-augmented generation",
    ),
    "sql": ("sql", "structured query language"),
}


@dataclass(frozen=True)
class ConceptGroup:
    """A query concept plus deterministic surface-form alternatives."""

    key: str
    aliases: tuple[str, ...]


def _token_variants(token: str) -> tuple[str, ...]:
    variants = [token]
    if len(token) > 3 and token.endswith("s"):
        variants.append(token[:-1])
    elif len(token) > 3:
        variants.append(f"{token}s")
    return tuple(dict.fromkeys(variants))


def concept_groups(query: str, *, max_groups: int = 8) -> list[ConceptGroup]:
    """Extract the user-query concepts retrieval should keep covered."""

    groups: list[ConceptGroup] = []
    seen: set[str] = set()

    for raw in _TOKEN_RE.findall(query or ""):
        token = raw.lower().strip("-_'")
        if len(token) < 2 or token in _STOP_WORDS or token in seen:
            continue
        aliases = _ALIASES.get(token) or _token_variants(token)
        groups.append(ConceptGroup(key=token, aliases=aliases))
        seen.add(token)
        if len(groups) >= max_groups:
            break

    return groups


def _clean_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return f" {' '.join(value.split())} "


def _alias_matches(alias: str, haystack: str) -> bool:
    alias_clean = _clean_text(alias)
    if not alias_clean.strip():
        return False
    return alias_clean in haystack


def group_matches_text(group: ConceptGroup, text: str) -> bool:
    """Return true when any alias for a concept appears in normalized text."""

    haystack = _clean_text(text)
    return any(_alias_matches(alias, haystack) for alias in group.aliases)


def chunk_grounding_text(chunk: SourceChunk) -> str:
    """Build a compact, metadata-aware haystack for final-source grounding."""

    metadata = chunk.metadata or {}
    facet_terms = metadata_facet_terms(metadata)
    provenance_terms: list[str] = []
    for item in chunk.provenance or []:
        provenance_terms.extend(
            str(item.get(key) or "")
            for key in (
                "entity",
                "surface_form",
                "predicate",
                "relation_family",
                "domain_type",
                "canonical_family",
                "entity_type",
            )
        )

    parts: Iterable[str] = (
        chunk.text or "",
        chunk.summary or "",
        chunk.doc_name or "",
        chunk.doc_id or "",
        " ".join(chunk.heading_path or []),
        " ".join(facet_terms),
        " ".join(provenance_terms),
    )
    return "\n".join(part for part in parts if part)


def chunk_concept_hits(
    chunk: SourceChunk,
    groups: list[ConceptGroup],
) -> tuple[int, tuple[str, ...]]:
    """Return the number and keys of query concepts covered by a chunk."""

    if not groups:
        return 0, ()
    text = chunk_grounding_text(chunk)
    matched = tuple(group.key for group in groups if group_matches_text(group, text))
    return len(matched), matched
