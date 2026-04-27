"""
Section classifier — tag each parent chunk with a `ChunkKind` based on its
heading path, so downstream phases can:

  • skip Ghost B extraction on TOC / bibliography / index / appendix / front-
    matter / back-matter (saves LLM tokens — the dominant per-chunk cost),
  • exclude noise from default retrieval (Qdrant `must_not` filter),
  • still keep the chunks indexed in Qdrant so opt-in citation / TOC queries
    remain possible.

Modeled on Unstructured-IO's `ElementType` constants pattern
(`unstructured/documents/elements.py:640`), but the semantic-role taxonomy
("body", "toc", "bibliography", …) is Polymath-specific. Unstructured's
class hierarchy classifies *structural* element types (Title, Footer, …);
this module classifies what role a chunk plays in the document.

Defaults to `body` whenever no rule matches, so legacy chunks without
`chunk_kind` set behave exactly like the bulk of normal content. The
retrieval filter must use `must_not in {noisy kinds}` (not `must == body`)
to keep that backwards-compatible.
"""
from __future__ import annotations

import re
from typing import Iterable

# ─── Taxonomy ───────────────────────────────────────────────────────────────
# Kept as plain strings (not an Enum) so they serialize as-is into Mongo
# documents and Qdrant payloads with no custom encoder. Mirrors the
# Unstructured `ElementType` pattern (string constants on a class).


class ChunkKind:
    BODY = "body"
    TOC = "toc"
    BIBLIOGRAPHY = "bibliography"
    INDEX = "index"
    APPENDIX = "appendix"
    FRONT_MATTER = "front_matter"
    BACK_MATTER = "back_matter"


ALL_KINDS: tuple[str, ...] = (
    ChunkKind.BODY,
    ChunkKind.TOC,
    ChunkKind.BIBLIOGRAPHY,
    ChunkKind.INDEX,
    ChunkKind.APPENDIX,
    ChunkKind.FRONT_MATTER,
    ChunkKind.BACK_MATTER,
)

# Kinds the default retrieval filter excludes. The retriever keeps `body`
# AND any chunk where `chunk_kind` is missing entirely (legacy data).
NOISY_KINDS: tuple[str, ...] = tuple(k for k in ALL_KINDS if k != ChunkKind.BODY)

# Kinds for which Ghost B extraction is skipped at ingest. Identical to
# NOISY_KINDS today — exposed as a separate name so the two policies can
# diverge later (e.g. extract from appendix but not from biblio).
GHOST_B_SKIP_KINDS: frozenset[str] = frozenset(NOISY_KINDS)


# ─── Heading-text classification rules ──────────────────────────────────────
# Each rule = (compiled_regex, kind). Order matters: first match wins, so
# the more specific patterns appear before catch-alls. Patterns are matched
# against a normalized version of the *first* heading-path segment (which
# is typically the section / chapter title in Docling output for tier_a/b/b+).
# Case-insensitive. Anchored at the start to reduce false positives (e.g.
# don't classify "References to the Linnaean system" as bibliography).

_RULES: list[tuple[re.Pattern[str], str]] = [
    # Bibliography / works cited — most distinctive, check first
    (re.compile(r"^(bibliograph(y|ies?)|works?\s+cited|citations?|further\s+reading)\b"), ChunkKind.BIBLIOGRAPHY),
    (re.compile(r"^references?\b(?!\s+to\s)"), ChunkKind.BIBLIOGRAPHY),  # "References" but not "References to ..."

    # Table of contents
    (re.compile(r"^(table\s+of\s+contents|contents)\b"), ChunkKind.TOC),
    (re.compile(r"^(list\s+of\s+(figures|tables|illustrations|abbreviations))\b"), ChunkKind.TOC),

    # Index
    (re.compile(r"^(index|subject\s+index|name\s+index)\b"), ChunkKind.INDEX),

    # Appendix — keep separate from back_matter so users can opt to include
    (re.compile(r"^(appendix|appendices)\b"), ChunkKind.APPENDIX),

    # Front matter
    (re.compile(r"^(copyright|colophon|imprint)\b"), ChunkKind.FRONT_MATTER),
    (re.compile(r"^(preface|foreword|prologue|introduction\s*[:.\-—]?\s*$)"), ChunkKind.FRONT_MATTER),
    (re.compile(r"^(dedication|epigraph)\b"), ChunkKind.FRONT_MATTER),
    (re.compile(r"^(acknowledg(e)?ments?)\b"), ChunkKind.FRONT_MATTER),
    (re.compile(r"^(about\s+(the\s+)?(authors?|editors?|contributors?))\b"), ChunkKind.FRONT_MATTER),

    # Back matter
    (re.compile(r"^(glossary|errata|notes?\b\s*$|endnotes?)\b"), ChunkKind.BACK_MATTER),
    (re.compile(r"^(epilogue|afterword|postscript|coda)\b"), ChunkKind.BACK_MATTER),
]

# Heading prefixes Docling sometimes prepends (HTML chapter anchors, style
# annotations). Stripped before matching so e.g.
#   "[]{#ch01s05.html_ch01lvl1sec12}References {.title}"  → "References"
_DOCLING_PREFIX_RE = re.compile(r"^\s*(\[\]\{[^}]*\}|\{[^}]*\})\s*")
_DOCLING_SUFFIX_RE = re.compile(r"\s*\{[^}]*\}\s*$")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_heading(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _DOCLING_PREFIX_RE.sub("", text)
    cleaned = _DOCLING_SUFFIX_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned.lower()


def classify_heading(heading_path: Iterable[str] | None) -> str:
    """Return the `ChunkKind` for a given heading path. Defaults to BODY.

    Walks the heading path top-down — the first segment carries the strongest
    signal in book corpora (Chapter / Section / "References" …). Earlier
    segments override later ones; once we hit BODY we stop searching, since
    a body section can contain a sub-section literally titled e.g.
    "Appendix B" that shouldn't reclassify the parent.
    """
    if not heading_path:
        return ChunkKind.BODY
    for raw in heading_path:
        normalized = _normalize_heading(raw)
        if not normalized:
            continue
        for rule_re, kind in _RULES:
            if rule_re.match(normalized):
                return kind
        # First non-empty heading didn't match → caller's a regular body
        # section. Don't keep walking deeper headings.
        return ChunkKind.BODY
    return ChunkKind.BODY


def is_noisy(kind: str | None) -> bool:
    """True if this kind is excluded from default retrieval."""
    return bool(kind) and kind in NOISY_KINDS


def should_skip_ghost_b(kind: str | None) -> bool:
    """True if Ghost B extraction should be skipped on chunks of this kind."""
    return bool(kind) and kind in GHOST_B_SKIP_KINDS
