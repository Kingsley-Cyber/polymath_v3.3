"""Loss-aware source normalization with reversible character boundaries."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from models.graphify_contracts import NormalizedDocumentV1

NORMALIZATION_RELEASE = "graphify-normalization-v1"


@dataclass(frozen=True)
class OriginalSpan:
    start: int
    end: int
    exact: bool


def _normalize_unit(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def normalize_document(document_id: str, text: str, source_uri: str = "") -> NormalizedDocumentV1:
    """Normalize line endings and compatibility characters without trimming."""
    output: list[str] = []
    boundaries: list[int] = [0]
    index = 0
    while index < len(text):
        if text[index] == "\r":
            consumed = 2 if index + 1 < len(text) and text[index + 1] == "\n" else 1
            normalized = "\n"
        else:
            consumed = 1
            normalized = _normalize_unit(text[index])
        output.append(normalized)
        for offset in range(len(normalized)):
            boundaries.append(index + consumed if offset == len(normalized) - 1 else index)
        index += consumed
    normalized_text = "".join(output)
    return NormalizedDocumentV1(
        document_id=document_id,
        source_uri=source_uri,
        original_text=text,
        normalized_text=normalized_text,
        normalized_to_original=tuple(boundaries),
        normalization_release=NORMALIZATION_RELEASE,
        original_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        normalized_sha256=hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
    )


def to_original_span(document: NormalizedDocumentV1, start: int, end: int) -> OriginalSpan:
    if start < 0 or end < start or end > len(document.normalized_text):
        raise ValueError("normalized span is outside the document")
    original_start = document.normalized_to_original[start]
    original_end = document.normalized_to_original[end]
    original_slice = document.original_text[original_start:original_end]
    exact = normalize_document("span-check", original_slice).normalized_text == document.normalized_text[start:end]
    return OriginalSpan(original_start, original_end, exact)


def assert_round_trip(document: NormalizedDocumentV1, start: int, end: int) -> OriginalSpan:
    span = to_original_span(document, start, end)
    if not span.exact:
        raise ValueError("normalized span does not map to one exact original slice")
    return span

