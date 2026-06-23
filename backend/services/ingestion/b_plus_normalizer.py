"""
B+ tier synthetic header injection.

Phase 7.6 — classification responsibility moved to the docling sidecar +
docling_adapter._classify_tier(). This module now only provides the
PRE-PARSE injector that turns plain-text chapter/section markers into real
markdown `#`/`##` headers. The adapter calls inject_synthetic_headers
BEFORE handing the bytes to docling so docling promotes them into proper
section_header items.

Pattern families (most-specific first; first match wins):
  • PART, Chapter, Appendix          → H1
  • Numbered sections (1.2, Section X) → H2
  • Plain-text semantic headings (ALL CAPS line, "Title:")
                                      → H1 / H2 (softer signals)

Every injection is logged in `injected_headers` for auditability. The audit
list rides through the docling response into the document record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Pattern families — ordered from most- to least-specific. First match wins
# per line. Each pattern maps to an MD heading level (1 or 2).
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    # Book-level divisions — promote to H1
    (
        "part_roman",
        re.compile(r"^\s*PART\s+[IVXLCDM]+(?:\s*[:.\-–—]\s*.+)?\s*$", re.IGNORECASE),
        1,
    ),
    (
        "part_numeric",
        re.compile(r"^\s*Part\s+\d+(?:\s*[:.\-–—]\s*.+)?\s*$"),
        1,
    ),
    (
        "chapter",
        re.compile(
            r"^\s*Chapter\s+(?:\d+|[IVXLCDM]+)(?:\s*[:.\-–—]\s*.+)?\s*$",
            re.IGNORECASE,
        ),
        1,
    ),
    (
        "appendix",
        re.compile(r"^\s*Appendix\s+[A-Z]?\d*(?:\s*[:.\-–—]\s*.+)?\s*$"),
        1,
    ),
    # Section divisions — H2
    (
        "section_dotted",
        re.compile(r"^\s*\d+\.\d+(?:\.\d+)*\s+\S.+$"),
        2,
    ),
    (
        "section_word",
        re.compile(r"^\s*Section\s+\d+(?:\.\d+)*(?:\s*[:.\-–—]\s*.+)?\s*$"),
        2,
    ),
    # Plain-text semantic headings — softer signals, listed last so the
    # specific chapter/section patterns above always win first.
    (
        "caps_h1",
        re.compile(r"^\s*[A-Z][A-Z0-9 &/—\-–.,()#]{3,80}\s*$"),
        1,
    ),
    (
        "colon_h2",
        re.compile(r"^\s*[A-Z][A-Za-z0-9 &/—\-–.,()]{2,98}:\s*$"),
        2,
    ),
]


_DOTTED_PREFIX_RE = re.compile(r"^\s*(?P<prefix>\d+(?:\.\d+)+)\b")
_DECIMAL_VALUE_RE = re.compile(r"^\d+\.\d{1,2}$")
_PRICE_OR_MEASUREMENT_HINT_RE = re.compile(
    r"\b(?:price|cost|discount|dollar|usd|current|sale|retail|percent|"
    r"rating|score|version|seconds?|minutes?|hours?|views?|subscribers?)\b",
    re.IGNORECASE,
)


def _is_probable_decimal_value_line(line: str) -> bool:
    """Reject price/timestamp/measurement prose that looks like `1.2 Heading`.

    `section_dotted` exists for real section titles such as "3.2 Retrieval".
    Transcripts and commerce tutorials often contain lines like
    "89.99 as the current price." after timestamp cleanup; those are content,
    not structure, and promoting them destroys retrieval evidence.
    """
    match = _DOTTED_PREFIX_RE.match(line)
    if not match:
        return False

    prefix = match.group("prefix")
    parts = prefix.split(".")
    if len(parts) != 2:
        return False

    left, right = parts
    if not right.isdigit():
        return False

    decimalish = _DECIMAL_VALUE_RE.match(prefix) is not None
    large_or_clocklike = int(left) > 20 or len(right) == 2
    return bool(decimalish and large_or_clocklike and _PRICE_OR_MEASUREMENT_HINT_RE.search(line))


def _should_inject_header(pattern_name: str, line: str) -> bool:
    if pattern_name == "section_dotted" and _is_probable_decimal_value_line(line):
        return False
    return True


@dataclass
class InjectedHeader:
    """One record of a synthetic header injected into the text."""

    line_no: int           # 1-based line number in the normalized text (after injection)
    level: int             # 1 or 2
    pattern: str           # which pattern family matched
    original_line: str     # verbatim line text before the `#` prefix


def inject_synthetic_headers(
    text: str,
) -> tuple[str, list[InjectedHeader]]:
    """
    Scan `text` line-by-line and prepend `#`/`##` to any line matching a
    structural pattern. Returns the normalized text plus an audit list.

    Idempotent — lines already starting with `#` are skipped.
    """
    audit: list[InjectedHeader] = []
    out_lines: list[str] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()

        # Never double-inject on top of existing MD headers
        if stripped.startswith("#"):
            out_lines.append(raw_line)
            continue

        matched = False
        for name, regex, level in _PATTERNS:
            if regex.match(stripped) and _should_inject_header(name, stripped):
                prefix = "#" * level
                normalized = f"{prefix} {stripped}"
                out_lines.append(normalized)
                audit.append(
                    InjectedHeader(
                        line_no=len(out_lines),
                        level=level,
                        pattern=name,
                        original_line=stripped,
                    )
                )
                matched = True
                break

        if not matched:
            out_lines.append(raw_line)

    return "\n".join(out_lines), audit
