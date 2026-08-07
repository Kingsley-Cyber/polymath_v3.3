"""Credit and metadata fragment patterns (P2B construction family).

This is a dedicated TOKEN-PATTERN lane, NOT dependency parsing. Credit lines
are document-structure artifacts that follow predictable formats:

    A CHRISTMAS STORY
    (screenplay by Jean Shepherd & Leigh Brown & Bob Clark, 1983)

    Written by Jane Doe
    Director: John Smith
    Author — Jane Doe

The dependency parser produces nothing useful for these fragments because they
are not running prose. This module uses regex-based token patterns to extract
credit relations with high precision.

Output: syntax records compatible with join_syntax_evidence(), tagged with
    source_family: TOKEN_PATTERN
    dependency_parse_used: False

Promotion gate: these enter as SHADOW_SYNTAX_CANDIDATE until adjudicated
precision >= 0.95 on a representative sample.

Run: cd backend && pytest tests/extraction/test_credit_patterns.py -q
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Credit marker patterns
# ---------------------------------------------------------------------------

# Markers that indicate creative authorship. The person name follows.
_CREDIT_MARKERS = re.compile(
    r"\b("
    r"screenplay\s+by"
    r"|written\s+by"
    r"|directed\s+by"
    r"|director\s*[:\u2014\-]"
    r"|author\s*[:\u2014\-]"
    r"|story\s+by"
    r"|novel\s+.*?\bby"  # "novel TITLE by Person"
    r")\s+",
    re.IGNORECASE,
)

# Person name pattern: capitalized words, possibly with initials, hyphens.
# Stops at "&", ",", ")", year digits, or lowercase function words.
# Order matters: try full capitalized words BEFORE single initials.
_PERSON_NAME = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+[A-Z]\.?)?)"
)

# Coordination: names joined by "&", ",", "and"
_NAME_SEPARATOR = re.compile(r"\s*(?:&|,|\band\b)\s*")

# Year pattern (to stop name extraction)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")

# Prose subject+verb patterns that precede a credit marker in ordinary
# sentences. These distinguish "The screenplay by X was praised" (prose)
# from "Screenplay by X" (metadata). A function word followed by at least
# one content word signals a prose noun phrase before the marker.
_PROSE_SUBJECT_RE = re.compile(
    r"\b(?:the|a|an|this|that|her|his|their|its|she|he|it|they)\b"
    r"\s+\w+",
    re.IGNORECASE,
)

# Verb phrases that follow a person name in ordinary prose but NOT in
# metadata lines: "Jean Shepherd was praised", "Jean Shepherd in her lecture".
_PROSE_CONTINUATION_RE = re.compile(
    r"^\s+(?:was|were|is|are|has|had|have|in|on|at|for|from|by|to)\b",
    re.IGNORECASE,
)

# Work title: preceding ALLCAPS or Title-Case line (within 200 chars before marker)
_WORK_TITLE_LINE = re.compile(
    r"^([A-Z][A-Z\s]{2,}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*$",
    re.MULTILINE,
)


def extract_credit_patterns(
    text: str,
    entities: list[dict[str, Any]],
    chunk_id: str,
) -> list[dict[str, Any]]:
    """Extract credit/metadata fragment relations via token patterns.

    entities: list of entity dicts with keys: start, end, text, type.
        Used to identify work titles and person names at known spans.

    Returns syntax records compatible with join_syntax_evidence().
    """
    if not text or not entities:
        return []

    records: list[dict[str, Any]] = []

    # Build entity indices
    person_entities = [
        e for e in entities
        if (e.get("type") or "").lower() in ("person", "concept")
        and _looks_like_person_name(e.get("text", ""))
    ]
    work_entities = [
        e for e in entities
        if (e.get("type") or "").lower() in ("concept", "document", "software", "artifact")
        and not _looks_like_person_name(e.get("text", ""))
    ]

    # Strategy 1: Find credit markers and match nearby entities
    for match in _CREDIT_MARKERS.finditer(text):
        marker_text = match.group(1).lower().strip()
        marker_end = match.end()

        # Document-structure qualification: reject markers embedded in
        # ordinary prose or quotation. Credit patterns are metadata.
        if not _is_metadata_context(text, match.start(), match.end()):
            continue

        # Determine the canonical predicate from the marker
        canonical = _marker_to_predicate(marker_text)
        if not canonical:
            continue

        # Find person names after the marker (within 150 chars)
        search_region = text[marker_end:marker_end + 150]
        # Stop at closing paren or newline
        paren_idx = search_region.find(")")
        newline_idx = search_region.find("\n")
        if paren_idx >= 0:
            search_region = search_region[:paren_idx]
        if newline_idx >= 0:
            search_region = search_region[:newline_idx]

        # Extract person names from the region
        person_names = _extract_person_names(search_region)

        # Find the work title: look backwards from the marker
        work_title, work_span = _find_preceding_work_title(
            text, match.start(), work_entities
        )

        if not work_title or not person_names:
            continue

        # Match person names to entity spans
        for person_name in person_names:
            person_span = _find_entity_span(person_name, person_entities, text)
            if person_span is None:
                # Try direct text location
                person_span = _locate_in_text(person_name, text, marker_end)
            if person_span is None:
                continue

            records.append({
                "chunk_id": chunk_id,
                "subject_start": work_span[0],
                "subject_end": work_span[1],
                "subject_text": work_title,
                "object_start": person_span[0],
                "object_end": person_span[1],
                "object_text": person_name,
                "canonical_predicate": canonical,
                "surface_predicate": marker_text,
                "pattern_id": f"CREDIT:{marker_text.replace(' ', '_')[:24]}",
                "confidence": 1.0,
                "negated": False,
                "modal": False,
                "source_family": "TOKEN_PATTERN",
                "source": "credit_pattern",
                "dependency_parse_used": False,
                "structural_confidence": "high",
            })

    return records


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_metadata_context(text: str, marker_start: int, marker_end: int) -> bool:
    """Determine whether a credit marker occurs in a document-metadata context.

    Strong metadata signals (any one suffices):
      - line begins with the credit marker
      - marker inside a parenthetical on a short line
      - colon/dash field structure (marker starts the line)

    Prose rejection signals (any one suffices):
      - marker is inside quotation marks
      - a full subject+verb clause precedes the marker on the same line
      - a verb phrase follows the person-name region (predicate continuation)
    """
    # Extract the line containing the marker.
    line_start = text.rfind("\n", 0, marker_start) + 1
    line_end = text.find("\n", marker_start)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end].strip()
    marker_col = marker_start - line_start

    # --- Quotation guard: marker inside quotes is discussion, not metadata. ---
    before = text[max(0, marker_start - 80):marker_start]
    after = text[marker_end:marker_end + 200]
    if (before.count('"') % 2 == 1 and '"' in after):
        return False
    if (before.count("\u201c") > before.count("\u201d")):
        return False

    # --- Strong metadata signals ---
    stripped_before = text[line_start:marker_start].strip()

    # Line begins with the marker (possibly after whitespace or '(').
    if not stripped_before or stripped_before == "(":
        return True

    # Short parenthetical: "(screenplay by X, 1983)" on its own line.
    if "(" in line and len(line) < 120:
        return True

    # --- Prose rejection signals ---

    # A full subject+verb clause precedes the marker on the same line.
    # "The screenplay by X" / "The film was inspired by a screenplay by X"
    if len(stripped_before) > 3 and _PROSE_SUBJECT_RE.search(stripped_before):
        return False

    # Verb continuation after the credit region: "by Jane Smith was praised"
    credit_region_end = min(marker_end + 60, line_end)
    remainder = text[marker_end:credit_region_end]
    # Skip past the person name (capitalized words).
    name_match = re.match(
        r"\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:&|and)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)*",
        remainder,
    )
    if name_match:
        after_name = remainder[name_match.end():]
        if _PROSE_CONTINUATION_RE.match(after_name):
            return False

    # Default: isolated line with a credit marker is metadata.
    return True


def _marker_to_predicate(marker: str) -> str:
    """Map a credit marker to its canonical predicate."""
    if "screenplay" in marker or "written" in marker or "story" in marker:
        return "created_by"
    if "directed" in marker or "director" in marker:
        return "created_by"  # directed_by not in ontology; created_by is closest
    if "author" in marker:
        return "created_by"
    if "novel" in marker:
        return "created_by"
    return ""


def _looks_like_person_name(text: str) -> bool:
    """Heuristic: 2-3 capitalized words, no ALLCAPS, not a known work pattern."""
    if not text or text.isupper():
        return False
    words = text.split()
    if len(words) < 2 or len(words) > 4:
        return False
    # All words start with uppercase
    if not all(w[0].isupper() for w in words if w):
        return False
    # No digits (work titles often have years/numbers)
    if any(c.isdigit() for c in text):
        return False
    return True


def _extract_person_names(region: str) -> list[str]:
    """Extract person names from a credit region (after the marker)."""
    # Split by coordination markers
    parts = _NAME_SEPARATOR.split(region)
    names = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Stop at year
        year_match = _YEAR.search(part)
        if year_match:
            part = part[:year_match.start()].strip()
        if not part:
            continue
        # Match a person name pattern
        name_match = _PERSON_NAME.match(part)
        if name_match:
            name = name_match.group(1).strip()
            if len(name.split()) >= 2 and _looks_like_person_name(name):
                names.append(name)
    return names


def _find_preceding_work_title(
    text: str,
    marker_start: int,
    work_entities: list[dict],
) -> tuple[str, tuple[int, int]] | tuple[None, None]:
    """Find the work title that precedes a credit marker.

    Looks for the nearest work entity within 300 chars before the marker.
    """
    best: tuple[str, tuple[int, int]] | None = None
    best_distance = 300  # max lookback

    for ent in work_entities:
        ent_end = int(ent.get("end", 0))
        ent_start = int(ent.get("start", 0))
        # Entity must be BEFORE the marker
        if ent_end > marker_start:
            continue
        distance = marker_start - ent_end
        if distance < best_distance:
            best_distance = distance
            best = (ent.get("text", ""), (ent_start, ent_end))

    if best:
        return best[0], best[1]
    return None, None


def _find_entity_span(
    name: str,
    person_entities: list[dict],
    text: str,
) -> tuple[int, int] | None:
    """Find the entity span for a person name."""
    name_lower = name.lower().strip()
    for ent in person_entities:
        ent_text = (ent.get("text") or "").lower().strip()
        if ent_text == name_lower:
            return (int(ent["start"]), int(ent["end"]))
    return None


def _locate_in_text(
    name: str,
    text: str,
    search_from: int = 0,
) -> tuple[int, int] | None:
    """Locate a name in the text starting from a position."""
    idx = text.find(name, search_from)
    if idx >= 0:
        return (idx, idx + len(name))
    # Case-insensitive fallback
    lower_text = text.lower()
    idx = lower_text.find(name.lower(), search_from)
    if idx >= 0:
        return (idx, idx + len(name))
    return None
