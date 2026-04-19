"""
Docling adapter — thin httpx client for the docling sidecar.

The sidecar (`docling_svc/`) wraps IBM Docling so the backend image stays
free of torch / transformers / accelerate. Backend code only ever sees the
adapter's `parse_document(...) -> DoclingParseResult` contract.

Responsibilities split:
  • Sidecar parses bytes → DoclingDocument → flat sections + markdown.
  • Adapter does ONE pre-processing step: when the upload looks like a
    structurally-implicit `.txt` file, run `inject_synthetic_headers` first
    so docling sees real `#`/`##` markers and can promote tier_b_plus.
  • Adapter then asks the sidecar to parse the (possibly augmented) bytes
    and packages the response into a `DoclingParseResult` ready for
    `source_classifier` and `tier_chunker` to consume.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx

from models.schemas import SourceTier
from services.ingestion.b_plus_normalizer import inject_synthetic_headers

logger = logging.getLogger(__name__)

DOCLING_URL = os.getenv("DOCLING_URL", "http://docling:8500")
DOCLING_TIMEOUT_SECONDS = float(os.getenv("DOCLING_TIMEOUT_SECONDS", "300"))

_PLAIN_TEXT_MIMES = {"text/plain", "application/octet-stream"}
_PLAIN_TEXT_EXTS = {".txt", ".text", ".log"}


@dataclass
class Section:
    heading_path: list[str]
    text: str
    element_type: str
    level: int | None = None


@dataclass
class DoclingParseResult:
    text: str
    markdown: str
    sections: list[Section]
    pages: list[str] | None
    has_structure: bool
    source_tier: SourceTier
    h1_count: int = 0
    h2_count: int = 0
    num_pages: int = 1
    source_format: str = ""
    augmented_with_synthetic_headers: bool = False
    injected_headers_audit: list[dict] = field(default_factory=list)


def _looks_like_plain_text(filename: str, mime: str) -> bool:
    if (mime or "").lower() in _PLAIN_TEXT_MIMES:
        return True
    lower = (filename or "").lower()
    return any(lower.endswith(ext) for ext in _PLAIN_TEXT_EXTS)


def _maybe_augment_plaintext(
    raw_bytes: bytes, filename: str, mime: str
) -> tuple[bytes, str, str, bool, list[dict]]:
    """Pre-step for plain-text uploads: try `inject_synthetic_headers` and
    re-route the (now markdown) bytes to docling under a `.md` filename so
    docling promotes the structure into `section_header` items.

    Returns:
        (bytes, filename, mime, augmented_flag, audit_list)
    """
    if not _looks_like_plain_text(filename, mime):
        return raw_bytes, filename, mime, False, []
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return raw_bytes, filename, mime, False, []

    normalized, audit = inject_synthetic_headers(text)
    if not audit:
        return raw_bytes, filename, mime, False, []

    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    augmented_filename = f"{base}.md"
    audit_dicts = [
        {
            "line_no": h.line_no,
            "level": h.level,
            "pattern": h.pattern,
            "original_line": h.original_line,
        }
        for h in audit
    ]
    logger.info(
        "Pre-augmented plain text %s with %d synthetic headers → %s",
        filename, len(audit), augmented_filename,
    )
    return normalized.encode("utf-8"), augmented_filename, "text/markdown", True, audit_dicts


def _classify_tier(
    *,
    original_mime: str,
    original_filename: str,
    augmented: bool,
    h1_count: int,
    h2_count: int,
    num_pages: int,
    has_structure: bool,
) -> SourceTier:
    """Mirror the rules of the legacy source_classifier but feed off docling
    output instead of regex hits.

    Order:
      1. Multi-page PDF → ocr_ast (page-layout chunking)
      2. HTML / XHTML  → tier_b
      3. Augmented plain text with structure → tier_b_plus
      4. Native MD/DOCX with structure → tier_a
      5. Otherwise → tier_c
    """
    mime = (original_mime or "").lower()
    fname = (original_filename or "").lower()

    if mime == "application/pdf" and num_pages > 1:
        return SourceTier.ocr_ast
    if mime in ("text/html", "application/xhtml+xml") or fname.endswith((".html", ".htm", ".xhtml")):
        return SourceTier.tier_b
    if augmented and has_structure:
        return SourceTier.tier_b_plus
    if has_structure:
        return SourceTier.tier_a
    return SourceTier.tier_c


async def parse_document(
    raw_bytes: bytes,
    filename: str,
    mime: str,
    do_ocr: bool = True,
) -> DoclingParseResult:
    """Hand the upload to the docling sidecar and return a structured
    DoclingParseResult. Plain-text uploads are pre-augmented with synthetic
    headers when `inject_synthetic_headers` finds qualifying markers.
    """
    aug_bytes, aug_filename, aug_mime, augmented, audit = _maybe_augment_plaintext(
        raw_bytes, filename, mime
    )

    files = {"file": (aug_filename, aug_bytes, aug_mime)}
    data = {"do_ocr": "true" if do_ocr else "false"}

    async with httpx.AsyncClient(
        base_url=DOCLING_URL,
        timeout=httpx.Timeout(DOCLING_TIMEOUT_SECONDS, connect=30.0),
    ) as client:
        resp = await client.post("/parse", files=files, data=data)
        resp.raise_for_status()
        payload = resp.json()

    sections = [
        Section(
            heading_path=list(s.get("heading_path") or []),
            text=s.get("text", "") or "",
            element_type=s.get("element_type", "paragraph"),
            level=s.get("level"),
        )
        for s in payload.get("sections", [])
    ]

    h1 = int(payload.get("h1_count", 0))
    h2 = int(payload.get("h2_count", 0))
    has_structure = bool(payload.get("has_structure", (h1 + h2) >= 2))
    num_pages = int(payload.get("num_pages", 1))

    tier = _classify_tier(
        original_mime=mime,
        original_filename=filename,
        augmented=augmented,
        h1_count=h1,
        h2_count=h2,
        num_pages=num_pages,
        has_structure=has_structure,
    )

    return DoclingParseResult(
        text=payload.get("text", "") or "",
        markdown=payload.get("markdown", "") or "",
        sections=sections,
        pages=payload.get("pages"),
        has_structure=has_structure,
        source_tier=tier,
        h1_count=h1,
        h2_count=h2,
        num_pages=num_pages,
        source_format=payload.get("source_format", "") or "",
        augmented_with_synthetic_headers=augmented,
        injected_headers_audit=audit,
    )
