"""Deterministic entity cleaning shared by ingestion and graph query.

The graph UI should not depend on query-time filtering alone. These helpers are
used at write time to prevent extraction debris from becoming Entity nodes, and
again at query time as a safety net for older data.
"""

from __future__ import annotations

import re


GRAPH_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "has", "have", "in", "is", "it", "its", "of", "on", "or", "that", "the",
        "this", "to", "was", "were", "what", "when", "where", "which", "who",
        "why", "will", "with", "how", "do", "does", "did", "about", "between",
        "vs", "versus", "compared",
    }
)

JUNK_ENTITY_EXACT_LOWER = frozenset(
    {
        *GRAPH_STOP_WORDS,
        "set",
        "sets",
        "entity",
        "entities",
        "concept",
        "concepts",
        "object",
        "objects",
        "item",
        "items",
        "thing",
        "things",
        "person",
        "people",
        "organization",
        "organizations",
        "organisations",
        "organisation",
        "product",
        "products",
        "method",
        "event",
        "location",
        "artifact",
        "software",
        "standard",
        "rule",
        "law",
        "laws",
        "time",
        "reference",
        "user",
        "users",
        "index",
        "the book",
        "left",
        "middle",
        "right",
        "up",
        "down",
        "inlineequation",
        "equationcontent",
        "equationwrapper",
        "chapter",
        "section",
        "figure",
        "table",
        "page",
        "appendix",
    }
)

JUNK_ENTITY_NAME_PATTERN = (
    r"^(?:"
    r"\[[0-9]+\]|"
    r"[0-9]+|"
    r"[a-z]|"
    r"[0-9]+\s+(?:and|or)\s+[a-z0-9]+|"
    r"(?:chapter|section|figure|table|page|appendix|part|rule)\s+[0-9ivxlcdm]+"
    r")$"
)
JUNK_ENTITY_NAME_RE = re.compile(JUNK_ENTITY_NAME_PATTERN)


def normalize_entity_surface(name: str | None) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower()).strip()


def is_junk_entity_name(name: str | None) -> bool:
    """Return True for deterministic graph-junk surface forms."""
    low = normalize_entity_surface(name)
    return (
        not low
        or low in JUNK_ENTITY_EXACT_LOWER
        or bool(JUNK_ENTITY_NAME_RE.match(low))
    )


def is_junk_extracted_entity(
    canonical_name: str | None,
    surface_form: str | None = None,
) -> bool:
    """Return True when every available extracted surface is graph junk.

    Checking both canonical_name and surface_form avoids over-dropping names
    such as "C++" whose storage canonicalization may be lossy, while still
    filtering obvious debris like "And", "0 and x2", and "Rule 3".
    """
    surfaces = [
        value for value in (canonical_name, surface_form)
        if str(value or "").strip()
    ]
    return not surfaces or all(is_junk_entity_name(value) for value in surfaces)
