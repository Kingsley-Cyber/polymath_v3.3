"""
B+ tier synthetic header injection.

Takes plain text that has recoverable structure (chapter/section markers like
"Chapter 1", "Section 2.3", "PART III") but no native Markdown headers, and
injects `#` / `##` headers in front of each detected marker line so the
downstream tier_a chunker can split at heading boundaries.

Why a separate tier:
  tier_a — native MD headers already present → split as-is
  tier_b — HTML/structured, different preprocessing
  tier_b_plus — this module's target: prose with implicit structure
  tier_c — truly flat prose, no markers → token-budget split

Every injection is logged in `injected_headers` for auditability and migration
debugging. The audit list is attached to the document record by the worker.
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


@dataclass
class InjectedHeader:
    """One record of a synthetic header injected into the text."""

    line_no: int           # 1-based line number in the normalized text (after injection)
    level: int             # 1 or 2
    pattern: str           # which pattern family matched
    original_line: str     # verbatim line text before the `#` prefix


def _likely_structured(text: str) -> bool:
    """
    Quick heuristic: does the text have enough markers to warrant B+ treatment?

    Qualifies on any of:
      - >=2 chapter/part/appendix markers
      - >=3 numbered section markers
      - >=2 plain-text semantic-heading markers (ALL CAPS / colon-suffixed)
      - >=1 chapter marker combined with >=2 soft markers
    """
    ch_hits = 0
    sect_hits = 0
    soft_hits = 0
    for line in text.splitlines():
        for name, regex, _level in _PATTERNS:
            if regex.match(line):
                if name.startswith(("chapter", "part", "appendix")):
                    ch_hits += 1
                elif name.startswith(("caps_", "colon_")):
                    soft_hits += 1
                else:
                    sect_hits += 1
                break
    return (
        ch_hits >= 2
        or sect_hits >= 3
        or soft_hits >= 2
        or (ch_hits >= 1 and soft_hits >= 2)
    )


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
            if regex.match(stripped):
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


def looks_like_b_plus(text: str, has_native_headings: bool) -> bool:
    """
    Classifier helper — True when the text has NO native MD headings but has
    enough chapter/section markers that B+ injection would recover structure.
    """
    if has_native_headings:
        return False
    return _likely_structured(text)
