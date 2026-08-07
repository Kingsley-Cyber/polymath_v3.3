"""unit.kind representation router — the factory's first station.

Owner-ratified contract (2026-08-07): every survey block is classified into a
representation class from syntax/format/structure ONLY — no corpus vocabulary,
no benchmark names, no document-specific headings, no gold-derived exceptions.
Classification changes EXECUTION ELIGIBILITY, never evidence preservation:
every source byte keeps its block, span, and provenance; only the routing of
semantic NLP changes.

    prose       → GLiNER2 + OpenIE
    definition  → GLiNER2 + OpenIE (+ structural subject/context downstream)
    navigation  → provenance preserved; semantic NLP OFF
    metadata    → deterministic structured (key:value) parser
    code        → code/structured lane; semantic NLP OFF
    table       → deterministic table lane; semantic NLP OFF

Precedence: code > table > metadata/definition (value-shape decides) >
navigation > prose. All thresholds are versioned constants of this module and
pinned by the freeze manifest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from models.graphify_contracts import NormalizedDocumentV1
from services.extraction.graphify_survey import DocumentSurveyV1, SurveyBlockV1

UNIT_KIND_RELEASE = "graphify-unit-kind-router-v1"
SEMANTIC_KINDS = frozenset({"prose", "definition"})

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_INDENTED_RE = re.compile(r"^(?:\t| {4})\S")
_CODE_LINE_END_RE = re.compile(r"[;{}]\s*$")
_TABLE_ROW_RE = re.compile(r"\|.*\|")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_KV_LINE_RE = re.compile(r"^\s*[#>*\-\s]*[\w][\w .\-/()]{0,60}:\s+\S")
_KV_SPLIT_RE = re.compile(r":\s+", re.UNICODE)
_URL_RE = re.compile(r"https?://\S+|\[[^\]]{1,120}\]\([^)]{1,400}\)", re.I)
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s+\S")
_TAG_ONLY_LINE_RE = re.compile(r"^\s*(?:<[^<>]+>\s*)+$")
_NUMBERED_LINE_RE = re.compile(r"^\s*\d{1,4}[.)]\s+\S")
_SENTENCE_PUNCT_RE = re.compile(r"[.!?](?:\s|$)")
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)

# Structural thresholds (versioned; format-derived, corpus-blind).
_CODE_LINE_RATIO = 0.5
_TABLE_MIN_ROWS = 2
_KV_LINE_RATIO = 0.6
_KV_MIN_LINES = 2
_METADATA_VALUE_MAX_WORDS = 5
_NAV_LINK_RATIO = 0.5
_NAV_MIN_LINES = 2
_NAV_INDEX_MIN_LINES = 3
_NAV_INDEX_MAX_CHARS = 72


@dataclass(frozen=True)
class ClassifiedBlock:
    block_id: str
    start: int
    end: int
    kind: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id, "start": self.start, "end": self.end,
            "kind": self.kind, "reasons": list(self.reasons),
            "router_release": UNIT_KIND_RELEASE,
        }


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _kv_value_words(line: str) -> int | None:
    parts = _KV_SPLIT_RE.split(line.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    return len(_WORD_RE.findall(parts[1]))


def classify_block(block: SurveyBlockV1, definition_spans: Sequence[tuple[int, int]]) -> ClassifiedBlock:
    text = block.text
    lines = _lines(text)
    total = len(lines) or 1

    def result(kind: str, *reasons: str) -> ClassifiedBlock:
        return ClassifiedBlock(block.block_id, block.start, block.end, kind, tuple(reasons))

    # code — fenced, indent-dominant, or statement-terminated line shapes.
    fence_lines = sum(bool(_FENCE_RE.match(line)) for line in lines)
    indented = sum(bool(_INDENTED_RE.match(line)) for line in lines)
    code_ends = sum(bool(_CODE_LINE_END_RE.search(line)) for line in lines)
    if fence_lines >= 1 and (fence_lines >= 2 or lines and _FENCE_RE.match(lines[0])):
        return result("code", "fenced_block")
    if lines and (indented / total >= _CODE_LINE_RATIO or code_ends / total >= _CODE_LINE_RATIO):
        return result("code", "statement_line_shape")

    # table — pipe rows or a rule separator.
    pipe_rows = sum(bool(_TABLE_ROW_RE.search(line)) for line in lines)
    if pipe_rows >= _TABLE_MIN_ROWS or any(_TABLE_RULE_RE.match(line) and "|" in line for line in lines):
        return result("table", "pipe_row_shape")

    # metadata vs definition — key:value line density; value shape decides.
    kv_lines = [line for line in lines if _KV_LINE_RE.match(line)]
    if len(kv_lines) >= _KV_MIN_LINES and len(kv_lines) / total >= _KV_LINE_RATIO:
        value_words = [words for words in (_kv_value_words(line) for line in kv_lines) if words is not None]
        value_words.sort()
        median = value_words[len(value_words) // 2] if value_words else 0
        if median <= _METADATA_VALUE_MAX_WORDS:
            return result("metadata", "kv_line_density_short_values")
        return result("definition", "term_colon_prose_values")

    # single-line Term: prose definition, or survey-detected definition span.
    if len(lines) == 1 and _KV_LINE_RE.match(lines[0]):
        words = _kv_value_words(lines[0]) or 0
        if words > _METADATA_VALUE_MAX_WORDS:
            return result("definition", "term_colon_prose")
        return result("metadata", "single_kv_line")
    if any(start < block.end and block.start < end for start, end in definition_spans):
        return result("definition", "survey_definition_span")

    # navigation — heading-only blocks, link-dense blocks, short numbered
    # index lines. Numbered PROSE lists (sentence punctuation, long lines)
    # stay semantic.
    if lines and all(_HEADING_LINE_RE.match(line) for line in lines):
        return result("navigation", "heading_only_block")
    # Bare markup lines (HTML anchors, standalone tags) are document
    # machinery, never prose — same class whether alone or interleaved
    # with headings.
    if lines and all(
        _TAG_ONLY_LINE_RE.match(line) or _HEADING_LINE_RE.match(line)
        for line in lines
    ):
        return result("navigation", "markup_only_block")
    link_lines = sum(bool(_URL_RE.search(line)) for line in lines)
    if len(lines) >= _NAV_MIN_LINES and link_lines / total >= _NAV_LINK_RATIO:
        return result("navigation", "link_density")
    numbered = [line for line in lines if _NUMBERED_LINE_RE.match(line)]
    if (
        len(numbered) >= _NAV_INDEX_MIN_LINES
        and len(numbered) / total >= 0.8
        and all(len(line.strip()) <= _NAV_INDEX_MAX_CHARS for line in numbered)
        and not any(_SENTENCE_PUNCT_RE.search(line) for line in numbered)
    ):
        return result("navigation", "short_numbered_index")

    return result("prose", "default_sentence_text")


def classify_document_blocks(
    document: NormalizedDocumentV1, survey: DocumentSurveyV1,
) -> tuple[ClassifiedBlock, ...]:
    del document  # classification is block-local by design
    definition_spans = [(item.start, item.end) for item in survey.definitions]
    return tuple(classify_block(block, definition_spans) for block in survey.blocks)


def kind_span_index(classified: Sequence[ClassifiedBlock]) -> list[tuple[int, int, str]]:
    return [(item.start, item.end, item.kind) for item in classified]


def kind_for_span(index: Sequence[tuple[int, int, str]], start: int, end: int) -> str:
    """Kind of the block with the largest overlap with [start, end)."""
    best_kind, best_overlap = "prose", 0
    for block_start, block_end, kind in index:
        overlap = min(end, block_end) - max(start, block_start)
        if overlap > best_overlap:
            best_kind, best_overlap = kind, overlap
    return best_kind


def semantic_segments(
    classified: Sequence[ClassifiedBlock], text_length: int,
) -> tuple[tuple[int, int], ...]:
    """Char ranges eligible for semantic NLP (prose + definition), merged.

    Unclassified gaps between survey blocks stay semantic — exclusion is
    only ever explicit, so no source region silently loses eligibility.
    """
    excluded = sorted(
        (item.start, item.end) for item in classified if item.kind not in SEMANTIC_KINDS
    )
    segments: list[tuple[int, int]] = []
    cursor = 0
    for start, end in excluded:
        if start > cursor:
            segments.append((cursor, min(start, text_length)))
        cursor = max(cursor, end)
    if cursor < text_length:
        segments.append((cursor, text_length))
    return tuple((start, end) for start, end in segments if end > start)
