"""Deterministic guards for text that carries no retrievable semantics."""

from __future__ import annotations


_SEPARATOR_CHARS = frozenset("|-:+_=*~.\u2013\u2014\u2500\u2501")


def is_separator_only_text(value: str | None) -> bool:
    """Return True for markdown/table/rule separators with no content.

    This is intentionally narrower than an alphanumeric-ratio heuristic. Code,
    equations, and compact identifiers may contain little prose but still carry
    evidence; a row made only from table/rule punctuation never does.
    """

    text = str(value or "").strip()
    if not text:
        return True
    compact = "".join(character for character in text if not character.isspace())
    if not compact or any(character.isalnum() for character in compact):
        return False
    return all(character in _SEPARATOR_CHARS for character in compact)
