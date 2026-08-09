"""
q9 deterministic parsing contract (owner directive v1.0, 2026-08-05).

The parsing stage is fully deterministic: it loads ZERO AI models and runs
ZERO OCR runtime paths. Supported formats parse through native deterministic
adapters; text PDFs parse through a deterministic text-layer extractor.
Documents whose required content cannot be extracted without OCR (scanned,
image-only, or mixed PDFs with unreadable pages) terminate with a
non-retryable ``unsupported_by_policy`` status instead of being partially
ingested.

Model-stage separation invariant:
    parsing    -> no models
    extraction -> GLiNER-Relex only
    embedding  -> configured embedding model
    synthesis  -> configured chat LLM
Removing parser models must never disable extraction, embedding, or
synthesis.
"""

from __future__ import annotations

from typing import Any

# ── Binding invariants ──────────────────────────────────────────────────────
# These are policy constants, not configuration: q9 acceptance asserts them.
OCR_SUPPORTED = False
OCR_ENABLED = False
OCR_RUNTIME_DEPENDENCIES = 0
VISION_MODELS_LOADED_BY_PARSER = 0
PARSER_AI_MODELS = 0

# Deterministic parser assignment per supported format (owner directive).
SUPPORTED_FORMATS: dict[str, str] = {
    "markdown": "native_markdown_parser",
    "txt": "native_text_parser",
    "html": "deterministic_html_parser",
    "pdf": "deterministic_pdf_text_inspector",
    "docx": "deterministic_native_ooxml_parser",
    "epub": "deterministic_zip_xhtml_parser",
    "json": "structured_parser_with_embedded_fragment_detection",
    "yaml": "structured_parser_with_embedded_fragment_detection",
}

# Terminal document types under the no-OCR policy.
OCR_DEPENDENT_DOCUMENT_TYPES = (
    "scanned_document",
    "image_only_document",
    "ocr_dependent_document",
)

STATUS_UNSUPPORTED_BY_POLICY = "unsupported_by_policy"
REASON_DOCUMENT_REQUIRES_OCR = "document_requires_ocr"


def unsupported_by_policy_result(
    *,
    reason: str = REASON_DOCUMENT_REQUIRES_OCR,
) -> dict[str, Any]:
    """Terminal contract for documents that cannot be parsed without OCR.

    Non-retryable by design: re-running the pipeline cannot change the
    outcome because OCR stays disabled by policy. Mixed PDFs must be rejected
    wholesale rather than silently ingesting only the readable pages.
    """
    return {
        "status": STATUS_UNSUPPORTED_BY_POLICY,
        "reason": reason,
        "retryable": False,
        "ocr_enabled": OCR_ENABLED,
        "ocr_supported": OCR_SUPPORTED,
        "parser_model_loads": PARSER_AI_MODELS,
    }


def pdf_text_is_usable(result: Any) -> bool:
    """Whether a parsed PDF carries a retrieval-usable deterministic text layer.

    Delegates to the adapter's usability heuristic (character volume, average
    chars per page, non-empty page ratio, replacement-character ratio). A
    False verdict means the required content cannot be extracted without OCR,
    so the document must terminate with ``unsupported_by_policy``.
    """
    from services.ingestion.docling_adapter import _fast_pdf_text_is_usable

    if result is None:
        return False
    return _fast_pdf_text_is_usable(result)


def pdf_requires_ocr_rejection(result: Any, filename: str, mime: str) -> bool:
    """q9 mixed/scanned PDF rule.

    True only when the upload is a PDF AND its deterministic text extraction
    falls below the usability threshold. Digital text PDFs (even short ones)
    stay supported; scanned, image-only, and partially-readable mixed PDFs
    are rejected in full — never partially ingested.
    """
    from services.ingestion.docling_adapter import _looks_like_pdf

    if not _looks_like_pdf(filename or "", mime or ""):
        return False
    return not pdf_text_is_usable(result)
