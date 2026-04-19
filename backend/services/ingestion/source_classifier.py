"""
DEPRECATED — Phase 7.6.

Source-tier classification has moved into the docling sidecar pipeline:
  • Parsing + structure detection happens inside docling.
  • The docling adapter (`services.ingestion.docling_adapter`) maps the
    parsed document to a `SourceTier` via `_classify_tier(...)`.

This module is retained ONLY as a compatibility shim — `classify(...)`
delegates to docling so any straggling caller still works (none in-tree).
The legacy regex heuristics that used to live here (looks_like_b_plus +
_likely_structured + native-heading counters) are gone.
"""

from __future__ import annotations

from models.schemas import SourceTier


def classify(
    text: str,
    source_mime: str,
    pages: list[str] | None = None,
) -> SourceTier:
    """
    No-op fallback used only when something calls the legacy classifier
    without the docling pipeline. Returns tier_c so the caller drops to
    plain token-budget chunking instead of crashing. Real classification
    comes from `docling_adapter.parse_document(...).source_tier`.
    """
    if source_mime == "application/pdf" and pages and len(pages) > 1:
        return SourceTier.ocr_ast
    if source_mime in ("text/html", "application/xhtml+xml"):
        return SourceTier.tier_b
    return SourceTier.tier_c
