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
    CODE = "code"
    TABLE = "table"
    LINKS = "links"


ALL_KINDS: tuple[str, ...] = (
    ChunkKind.BODY,
    ChunkKind.TOC,
    ChunkKind.BIBLIOGRAPHY,
    ChunkKind.INDEX,
    ChunkKind.APPENDIX,
    ChunkKind.FRONT_MATTER,
    ChunkKind.BACK_MATTER,
    ChunkKind.CODE,
    ChunkKind.TABLE,
    ChunkKind.LINKS,
)

# Kinds the default retrieval filter excludes. CODE is first-class
# retrievable content (programming-textbook listings, source files), and TABLE
# is first-class structured evidence. Both are kept alongside BODY. The
# retriever also keeps any chunk where
# `chunk_kind` is missing entirely (legacy data).
_RETRIEVABLE: tuple[str, ...] = (ChunkKind.BODY, ChunkKind.CODE, ChunkKind.TABLE)
NOISY_KINDS: tuple[str, ...] = tuple(k for k in ALL_KINDS if k not in _RETRIEVABLE)

# Kinds for which Ghost B extraction is skipped at ingest. CODE joins the
# noisy kinds here even though it's retrievable — Ghost B's universal
# schema hallucinates Method/Artifact entities on raw code fragments.
# Deterministic AST extraction will replace Ghost B for code chunks in a
# later phase.
GHOST_B_SKIP_KINDS: frozenset[str] = frozenset(list(NOISY_KINDS) + [ChunkKind.CODE])


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
    # NOTE: "introduction" intentionally NOT here — in academic papers and
    # most books the Introduction is the substantive first section, not
    # preface material. Without positional context (page < frontmatter cutoff,
    # or a publisher-specific "front_matter" docling tag) we keep it as body.
    (re.compile(r"^(preface|foreword|prologue)\b"), ChunkKind.FRONT_MATTER),
    (re.compile(r"^(dedication|epigraph)\b"), ChunkKind.FRONT_MATTER),
    (re.compile(r"^(acknowledg(e)?ments?)\b"), ChunkKind.FRONT_MATTER),
    (re.compile(r"^(about\s+(the\s+)?(authors?|editors?|contributors?))\b"), ChunkKind.FRONT_MATTER),

    # Back matter
    (re.compile(r"^(glossary|errata|notes?\b\s*$|endnotes?)\b"), ChunkKind.BACK_MATTER),
    (re.compile(r"^(epilogue|afterword|postscript|coda)\b"), ChunkKind.BACK_MATTER),

    # Publisher boilerplate / promotional back-matter (Packt/Springer/etc.) —
    # code-bundle pages, "Join our Discord", review/marketing asides. These are
    # outline/link dumps with no answerable content.
    (re.compile(r"^(code\s+bundle|colou?red?\s+images?|download\s+the\s+(example|code|colou?r))"), ChunkKind.BACK_MATTER),
    (re.compile(r"^join\s+(our|the)\b.{0,40}\bdiscord"), ChunkKind.BACK_MATTER),
    (re.compile(r"^(why\s+subscribe|share\s+your\s+thoughts|leave\s+a\s+review|about\s+packt|get\s+in\s+touch)\b"), ChunkKind.BACK_MATTER),
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


# ─── Content-based fallback classifier ──────────────────────────────────────
# Heading-text rules cover heading-bound docs (tier_a/b/b+). They DON'T cover:
#   • OCR PDFs, where heading_path looks like ["page_178"] or ["pages_10-12"]
#   • tier_c (token-window over markdown), where heading_path is None
#
# For those cases the bibliography / index / TOC pages of a book look just
# like body content to a heading-only classifier. The content-based classifier
# below detects them by structural cues in the chunk text itself: density of
# citation patterns for biblio, dot-leader page-references for TOC, and
# short comma-page-list rows for index. It only fires when the heading is
# inconclusive — body chapters that *quote* a citation or two won't trip it.

_CITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\d+\]"),                                # [12], [42]
    re.compile(r"\(\d{4}[a-z]?\)"),                        # (2018), (2018a)
    re.compile(r"\b(?:doi|DOI)\s*:?\s*10\.\d+"),           # doi:10.xxx
    re.compile(r"\bpp?\.\s*\d+"),                          # p.12, pp.12-34
    re.compile(r"\b(?:vol|Vol|VOL)\.\s*\d+"),              # Vol. 12
    re.compile(r"\bet\s+al\.?", re.IGNORECASE),            # et al.
    re.compile(r"\bRetrieved\s+from\b", re.IGNORECASE),    # Retrieved from URL
    re.compile(r"\bISBN[-:\s]*[\d\-Xx]{9,}"),              # ISBN
)

# A line that ends in dot-leaders + page number is the canonical TOC shape.
_TOC_LINE_RE = re.compile(r"\.{3,}\s*\d+\s*$")
# A line that's a short label followed by comma-separated page numbers is
# the canonical index shape (e.g. "Apple, 23, 45-47").
_INDEX_LINE_RE = re.compile(
    r"^\s*[A-Za-z][A-Za-z\s,'\-&]{1,40},\s*\d+(?:[\s,\-]+\d+)*\s*$"
)
# Pt 8c — modern ebook TOC lines using markdown anchor links instead of
# dot-leaders + page numbers. Example: `[Chapter 1: Introduction](#ch01)`.
# Common in epub-converted markdown where docling preserves the link
# syntax. Triggers when ≥30% of lines match.
_MD_ANCHOR_TOC_LINE_RE = re.compile(r"\[[^\]]{1,100}\]\(#[\w\-]+\)")
# Pt 8c — numbered cheat-sheet / structured-listing pattern, e.g.
#   1.1: Fill out the Habits Scorecard
#   1.2: Use implementation intentions
#   2.1: Use temptation bundling
# Found in back-matter cheat sheets, lecture-note recap pages,
# multi-level structured summaries. Triggers when ≥50% of lines match.
_CHEATSHEET_LINE_RE = re.compile(r"^\s*\d+\.\d+\s*[:.\-]\s+\S")
# Pt 8c — glossary / dictionary-entry pattern, e.g.
#   atomic — an extremely small amount of a thing
#   habit — a routine or practice performed regularly
# Term followed by em-dash / en-dash / hyphen-with-spaces, then a lowercase
# definition. Triggers when ≥30% of lines match.
_GLOSSARY_LINE_RE = re.compile(
    r"^\s*[A-Za-z][\w\s\-']{1,40}\s+[—–\-]\s+[a-z]"
)
# "see X" / "see also X" cross-reference — the canonical *partial-index*
# shape that has terms but no page numbers (Design Patterns chunk_0501
# is this: "adapter, see adapter, object | granularity of, see also
# FLYWEIGHT | composition, see object composition"). Page-numbered
# index lines and cross-reference lines often intermix in the same
# chunk, so detecting either signal alone underclassifies the page.
_INDEX_SEE_RE = re.compile(r"\bsee\s+(?:also\s+)?[A-Z]", re.IGNORECASE)
# Lines that don't end in sentence punctuation — used as a secondary
# signal to confirm a chunk is reference material rather than prose.
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?\s*$")

# Reference-LIST markers that body prose with a few inline citations does NOT
# produce: Springer/pandoc "::: Citation" entry blocks and PubMed/Crossref link
# tags. High-precision — lets us catch a reference list sitting inside a
# body-headed section (below the citation-density threshold) WITHOUT flagging a
# chapter that merely quotes "(Asch, 1956)" in running prose.
_CITATION_ENTRY_RE = re.compile(r":{2,}\s*Citation", re.IGNORECASE)
_PMC_TAG_RE = re.compile(r"\[(?:PubMed|Crossref|Google\s+Scholar)\]", re.IGNORECASE)
# External-link / "resources" list — lines dominated by http(s) URLs (markdown
# `](http…)` or bare). Distinct from in-document `#anchor` links (which are TOC
# / inline citation cross-refs, handled elsewhere).
_EXTERNAL_LINK_RE = re.compile(r"\]\(https?://|(?:^|\s)https?://\S")


def _is_reference_list(text: str | None) -> bool:
    """True for a bibliographic reference LIST (≥2 explicit entry markers).
    Keyed on list markers, not citation count, so prose citing a couple of
    sources stays BODY."""
    if not text:
        return False
    sample = text[:3000]
    markers = len(_CITATION_ENTRY_RE.findall(sample)) + len(_PMC_TAG_RE.findall(sample))
    return markers >= 2


# Strengthened reference-LIST signals — the dominant markers of an academic
# reference block that the inline-citation density rule (classify_content) misses:
# author-initial lists ("Furnas, G. W."), numbered entry lines ("- [3] Author …"),
# and proceedings / arXiv / page-range markers ("In: Proceedings of", "arXiv:1234",
# "Pages 234–241 of:"). Validated 0 false positives on 800 clean-prose body chunks
# at threshold 4 — a real chapter quoting a source or two stays BODY.
_AUTHOR_INITIAL_RE = re.compile(r"[A-Z][a-zA-Z'’\-]+,\s+(?:[A-Z]\.\s*){1,3}")
_NUMBERED_REF_RE = re.compile(r"(?m)^\s*[-*]?\s*\[\d+\]\s+[A-Z]")
_STRONG_REF_RE = re.compile(
    r"\bIn:\s|\bProceedings of\b|\bAdvances in Neural\b|\barXiv:\s?\d|"
    r"arxiv\.org/abs/|\bPages?\s+\d+\s*[–\-]\s*\d+\s+of:",
    re.IGNORECASE,
)
_REF_SIGNAL_THRESHOLD = 4

# Inline markdown reference/bibliography heading at the TOP of a chunk
# ("## References", "# Bibliography", "## Works Cited") — academic reference
# sections whose heading lives in the chunk TEXT, not heading_path, so
# classify_heading never saw it. The (?!\s+to\s) guard mirrors the heading-path
# rule so "References to the Linnaean system" stays BODY.
_INLINE_REF_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*"
    r"(?:references?\b(?!\s+to\s)|bibliography|works?\s+cited|further\s+reading)",
    re.IGNORECASE,
)


def reference_signal_count(text: str | None) -> int:
    """Count strengthened reference-list markers (author lists + numbered entries
    + proceedings/arXiv/page-range). Public so the corrective reclassification
    tool reuses the exact same detector as ingestion."""
    if not text:
        return 0
    sample = text[:3000]
    return (
        len(_AUTHOR_INITIAL_RE.findall(sample))
        + len(_NUMBERED_REF_RE.findall(sample))
        + len(_STRONG_REF_RE.findall(sample))
    )


def _is_link_list(lines: list[str]) -> bool:
    """True for an external-link / resources dump (≥4 URL lines AND ≥40% of
    lines carry an external URL)."""
    if len(lines) < 5:
        return False
    link_hits = sum(1 for ln in lines if _EXTERNAL_LINK_RE.search(ln))
    return link_hits >= 4 and link_hits / len(lines) >= 0.40


# A "Resources" / "External links" SUB-heading (the deepest heading segment) is
# a strong signal — these sections are link/resource dumps. We require it to be
# the deepest segment (a real chapter rarely ENDS a heading path on "resources")
# plus a bulleted body, so a substantive "Computational Resources" prose section
# is not caught. Catches resource lists whose URLs were stripped at ingest.
_RESOURCES_HEADING_RE = re.compile(
    r"\b(resources?|external\s+links?|useful\s+links?|online\s+resources?|web\s+resources?)\s*$"
)
_BULLET_LINE_RE = re.compile(r"^\s*[-*•]\s")


def _is_resources_list(heading_path: Iterable[str] | None, text: str | None) -> bool:
    segs = [_normalize_heading(h) for h in (heading_path or [])]
    segs = [s for s in segs if s]
    if not segs or not _RESOURCES_HEADING_RE.search(segs[-1]):
        return False
    if not text:
        return False
    lines = [ln for ln in text[:2000].split("\n") if ln.strip()]
    bullets = sum(1 for ln in lines if _BULLET_LINE_RE.match(ln))
    return bullets >= 3


def _heading_is_inconclusive(heading_path: Iterable[str] | None) -> bool:
    """Heading provides no semantic signal — page-style or empty."""
    if not heading_path:
        return True
    head = next((str(h) for h in heading_path if h), None)
    if not head:
        return True
    head_lc = head.strip().lower()
    return bool(re.match(r"^pages?_\d", head_lc)) or not head_lc


def classify_content(text: str | None) -> str:
    """Classify a chunk by structural cues in its text. Returns BODY when
    no confident classification is found.

    Thresholds picked conservatively — body chapters that mention a few
    citations should NOT trip biblio. We require:
      • biblio: ≥4 citation-pattern hits per ~1500 chars window
      • toc:    ≥30% of non-empty lines end in dot-leader + page number AND
                there are ≥5 such lines
      • index:  ≥40% of non-empty lines match the comma-page-list shape AND
                there are ≥5 such lines
    """
    if not text:
        return ChunkKind.BODY
    sample = text[:2000]
    if not sample.strip():
        return ChunkKind.BODY

    # Inline "## References" / "# Bibliography" heading at the chunk top — a
    # reference section whose heading is in the text, not heading_path.
    if _INLINE_REF_HEADING_RE.match(text):
        return ChunkKind.BIBLIOGRAPHY

    # Explicit reference-list markers (Springer "::: Citation" / PubMed tags) —
    # high precision, catches sparse ref blocks the density rule below misses.
    if _is_reference_list(text):
        return ChunkKind.BIBLIOGRAPHY

    # Strengthened reference-list detection — author-initial lists, numbered
    # academic entries, and proceedings/arXiv/page-range markers that the inline-
    # citation density rule below misses (the dominant signal in academic
    # reference blocks). Validated high-precision (0 FP on 800 clean-prose chunks).
    if reference_signal_count(text) >= _REF_SIGNAL_THRESHOLD:
        return ChunkKind.BIBLIOGRAPHY

    # Citation density → bibliography
    citation_hits = sum(len(p.findall(sample)) for p in _CITATION_PATTERNS)
    # 4 hits in a 1500-char sample is dense; scale linearly.
    threshold_biblio = max(3, int(len(sample) / 1500 * 4))
    if citation_hits >= threshold_biblio:
        return ChunkKind.BIBLIOGRAPHY

    lines = [ln for ln in sample.split("\n") if ln.strip()]
    if len(lines) >= 5:
        toc_hits = sum(1 for ln in lines if _TOC_LINE_RE.search(ln))
        if toc_hits / len(lines) >= 0.30:
            return ChunkKind.TOC
        # Pt 8c — modern ebook TOC (markdown anchor links). Same TOC bucket;
        # we don't need a separate "ebook_toc" kind because the downstream
        # skip-policy treats them identically.
        md_anchor_hits = sum(1 for ln in lines if _MD_ANCHOR_TOC_LINE_RE.search(ln))
        if md_anchor_hits / len(lines) >= 0.30:
            return ChunkKind.TOC
        # Pt 8c — numbered cheat-sheet / structured-listing rows. These
        # surface in back-matter sections like "Habits Cheat Sheet" or
        # multi-law recap pages. Treated as back_matter.
        cheat_hits = sum(1 for ln in lines if _CHEATSHEET_LINE_RE.match(ln))
        if cheat_hits / len(lines) >= 0.50:
            return ChunkKind.BACK_MATTER
        # Pt 8c — glossary / dictionary entries (front-matter for most
        # technical books, back-matter for some). Pattern: short term +
        # em-dash + lowercase definition, repeated.
        glossary_hits = sum(1 for ln in lines if _GLOSSARY_LINE_RE.match(ln))
        if glossary_hits / len(lines) >= 0.30:
            return ChunkKind.FRONT_MATTER
        index_hits = sum(1 for ln in lines if _INDEX_LINE_RE.match(ln))
        if index_hits / len(lines) >= 0.40:
            return ChunkKind.INDEX
        if _is_partial_index(lines):
            return ChunkKind.INDEX
        if _is_link_list(lines):
            return ChunkKind.LINKS

    return ChunkKind.BODY


def _is_partial_index(lines: list[str]) -> bool:
    """Detect index pages dominated by 'see X' cross-references and short
    term labels rather than name-page-number lines.

    Design Patterns chunk_0501 was the canonical miss for the original
    page-numbered index detector: most lines were "adapter, see adapter,
    object" or bare term labels with no page reference. This catches
    that shape.

    Two conjunctive requirements keep body chapters with stray
    "see also …" mentions from tripping:
      1. At least one cross-reference signal per ~10 lines, OR multiple
         comma-page-list lines (mixed indices have both).
      2. At least 60% of lines are short (<80 chars) AND don't end in
         sentence punctuation — body chapters fail this because their
         lines are sentences.
    """
    if len(lines) < 5:
        return False
    see_hits = sum(1 for ln in lines if _INDEX_SEE_RE.search(ln))
    index_hits = sum(1 for ln in lines if _INDEX_LINE_RE.match(ln))
    short_non_sentence = sum(
        1 for ln in lines
        if len(ln.strip()) < 80 and not _SENTENCE_END_RE.search(ln)
    )
    return (
        (see_hits + index_hits) >= max(2, int(len(lines) * 0.10))
        and short_non_sentence / len(lines) >= 0.60
    )


def classify_chunk(
    heading_path: Iterable[str] | None,
    text: str | None = None,
) -> str:
    """Combined classifier: heading first, then content classification.

    Precedence:
      1. If the heading produces a confident non-BODY classification, trust
         it (Bibliography heading on a content chunk wins over content cues).
      2. Otherwise — heading says BODY OR heading is inconclusive — run the
         content classifier. The content thresholds are conservative enough
         that body chapters quoting a citation or two don't trip biblio, and
         only structurally-reference-shaped content trips index/TOC.
      3. Default to BODY if no signal.

    The earlier version only ran the content classifier when the heading
    was "inconclusive" (None, empty, or page-style). That missed
    mid-section index pages where the heading carries forward from the
    enclosing chapter and classifies as BODY (Design Patterns chunk_0501
    was the canonical miss).
    """
    heading_kind = classify_heading(heading_path)
    if heading_kind != ChunkKind.BODY:
        return heading_kind
    if not text:
        return ChunkKind.BODY
    if _heading_is_inconclusive(heading_path):
        # No semantic signal in the heading — defer entirely to content.
        return classify_content(text)
    # Heading says BODY confidently. Only let content override for high-precision
    # noise shapes a body chapter literally cannot produce: a reference LIST
    # (Springer "::: Citation"/PubMed markers), a partial index (lines that don't
    # end in sentence punctuation), or an external-link dump. Citation-DENSITY
    # biblio detection is intentionally NOT run here — it false-positives on
    # citation-rich prose (e.g. a chapter quoting "(Asch, 1956)" several times).
    if _is_resources_list(heading_path, text):
        return ChunkKind.LINKS
    if _is_reference_list(text):
        return ChunkKind.BIBLIOGRAPHY
    lines = [ln for ln in text[:2000].split("\n") if ln.strip()]
    if _is_partial_index(lines):
        return ChunkKind.INDEX
    if _is_link_list(lines):
        return ChunkKind.LINKS
    return ChunkKind.BODY
