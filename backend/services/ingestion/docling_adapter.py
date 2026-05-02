"""
Docling adapter — thin httpx client for the docling sidecar.

The sidecar (`docling_svc/`) wraps IBM Docling so the backend image stays
free of torch / transformers / accelerate. Backend code only ever sees the
adapter's `parse_document(...) -> DoclingParseResult` contract.

Responsibilities split:
  • Sidecar parses bytes → DoclingDocument → flat sections + markdown.
  • PDF uploads are text-first. Local pypdf extraction is the only PDF text
    recovery path; OCR is disabled by policy and `do_ocr=True` is ignored.
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
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from models.schemas import SourceTier
from services.ingestion.b_plus_normalizer import inject_synthetic_headers

logger = logging.getLogger(__name__)

DOCLING_URL = os.getenv("DOCLING_URL", "http://docling:8500")
# Sidecar timeout. OCR is disabled; this mainly protects large layout parses
# for non-PDF formats and unusual PDFs routed to Docling without OCR.
DOCLING_TIMEOUT_SECONDS = float(os.getenv("DOCLING_TIMEOUT_SECONDS", "600"))

_PLAIN_TEXT_MIMES = {"text/plain"}
_PLAIN_TEXT_EXTS = {".txt", ".text", ".log"}
_MARKDOWN_MIMES = {"text/markdown", "text/x-markdown"}
_MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
_HTML_MIMES = {"text/html", "application/xhtml+xml"}
_HTML_EXTS = {".html", ".htm", ".xhtml"}
_BINARY_DOC_EXTS = {
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".epub", ".odt",
    ".ods", ".odp", ".rtf", ".msg", ".eml",
}
_FAST_PDF_MIN_TOTAL_CHARS = 1200
_FAST_PDF_MIN_AVG_CHARS_PER_PAGE = 80
_FAST_PDF_MIN_NONEMPTY_PAGE_RATIO = 0.25
_FAST_PDF_MAX_REPLACEMENT_RATIO = 0.03


def _looks_like_pdf(filename: str, mime: str) -> bool:
    return (mime or "").lower() == "application/pdf" or (filename or "").lower().endswith(".pdf")


def _extension(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _looks_like_markdown(filename: str, mime: str) -> bool:
    return (mime or "").lower() in _MARKDOWN_MIMES or _extension(filename) in _MARKDOWN_EXTS


def _looks_like_html(filename: str, mime: str) -> bool:
    return (mime or "").lower() in _HTML_MIMES or _extension(filename) in _HTML_EXTS


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
    ext = _extension(filename)
    mime_l = (mime or "").lower()
    if mime_l in _PLAIN_TEXT_MIMES:
        return True
    if ext in _PLAIN_TEXT_EXTS:
        return True
    # Browsers sometimes upload markdown/text as octet-stream. Accept only
    # known text-like extensions here so office files do not get decoded as
    # mojibake just because the client supplied a vague MIME.
    return mime_l == "application/octet-stream" and ext in (_PLAIN_TEXT_EXTS | _MARKDOWN_EXTS)


def _markdown_sections(markdown: str) -> tuple[list[Section], int, int]:
    """Small local Markdown section walker.

    This is intentionally modest: headings define the hierarchy, and paragraph
    text inherits the active heading path. It is enough for tier_chunker to keep
    Markdown/Text ingestion independent from the Docling sidecar.
    """
    sections: list[Section] = []
    heading_stack: list[str] = []
    current_path: list[str] = []
    paragraph: list[str] = []
    h1_count = 0
    h2_count = 0
    in_code_block = False
    code_fence_char: str | None = None
    code_fence_len = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        text = "\n".join(paragraph).strip()
        if text:
            sections.append(
                Section(
                    heading_path=list(current_path),
                    text=text,
                    element_type="paragraph",
                )
            )
        paragraph = []

    for line in markdown.splitlines():
        stripped = line.strip()
        fence = re.match(r"^(`{3,}|~{3,})", stripped)
        if in_code_block:
            paragraph.append(line)
            if (
                code_fence_char
                and stripped.startswith(code_fence_char * code_fence_len)
                and re.match(rf"^{re.escape(code_fence_char)}{{{code_fence_len},}}\s*$", stripped)
            ):
                in_code_block = False
                code_fence_char = None
                code_fence_len = 0
            continue
        if fence:
            fence_text = fence.group(1)
            in_code_block = True
            code_fence_char = fence_text[0]
            code_fence_len = len(fence_text)
            paragraph.append(line)
            continue

        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        if not match:
            paragraph.append(line)
            continue

        flush_paragraph()
        level = len(match.group(1))
        title = match.group(2).strip()
        if not title:
            continue
        if level == 1:
            h1_count += 1
        elif level == 2:
            h2_count += 1
        heading_stack = heading_stack[: level - 1]
        while len(heading_stack) < level - 1:
            heading_stack.append("")
        heading_stack.append(title)
        current_path = [part for part in heading_stack if part]
        sections.append(
            Section(
                heading_path=list(current_path),
                text=title,
                element_type="section_heading",
                level=level,
            )
        )

    flush_paragraph()
    return sections, h1_count, h2_count


def _parse_local_text_document(raw_bytes: bytes, filename: str, mime: str) -> DoclingParseResult | None:
    """Parse text/markdown/html without the Docling sidecar.

    Local parsing keeps default Docker startup API-first. Docling remains an
    explicit profile for formats that truly need layout-aware conversion.
    """
    if _looks_like_html(filename, mime):
        from services.ingestion.format_router import route

        decoded = route(raw_bytes, filename=filename, mime_hint=mime)
        text = decoded.text or ""
        return DoclingParseResult(
            text=text,
            markdown=text,
            sections=[],
            pages=None,
            has_structure=False,
            source_tier=SourceTier.tier_b,
            source_format="local_html",
        )

    if _looks_like_markdown(filename, mime):
        markdown = raw_bytes.decode("utf-8", errors="replace")
        sections, h1, h2 = _markdown_sections(markdown)
        has_structure = (h1 + h2) > 0
        return DoclingParseResult(
            text=markdown,
            markdown=markdown,
            sections=sections,
            pages=None,
            has_structure=has_structure,
            source_tier=SourceTier.tier_a if has_structure else SourceTier.tier_c,
            h1_count=h1,
            h2_count=h2,
            source_format="local_markdown",
        )

    if not _looks_like_plain_text(filename, mime):
        return None

    aug_bytes, aug_filename, aug_mime, augmented, audit = _maybe_augment_plaintext(
        raw_bytes, filename, mime
    )
    text = aug_bytes.decode("utf-8", errors="replace")
    sections, h1, h2 = _markdown_sections(text) if augmented else ([], 0, 0)
    has_structure = augmented and (h1 + h2) > 0
    return DoclingParseResult(
        text=text,
        markdown=text,
        sections=sections,
        pages=None,
        has_structure=has_structure,
        source_tier=SourceTier.tier_b_plus if has_structure else SourceTier.tier_c,
        h1_count=h1,
        h2_count=h2,
        source_format="local_text",
        augmented_with_synthetic_headers=augmented,
        injected_headers_audit=audit,
    )


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


def _parse_pdf_fast_text(raw_bytes: bytes, filename: str, mime: str) -> DoclingParseResult:
    """Fast local PDF text extraction for digital PDFs when OCR is disabled.

    This preserves the adapter contract while bypassing Docling's layout/OCR
    sidecar. The chunker still gets page text, so large digital books can be
    searched quickly without paying the layout/OCR cost.
    """
    from services.ingestion.format_router import route

    decoded = route(raw_bytes, filename=filename, mime_hint=mime)
    text = decoded.text or ""
    pages = decoded.pages or ([text] if text.strip() else [])
    num_pages = max(1, len(pages))
    source_tier = SourceTier.ocr_ast if num_pages > 1 else SourceTier.tier_c
    logger.info(
        "Fast PDF text path for %s pages=%d chars=%d",
        filename,
        num_pages,
        len(text),
    )
    return DoclingParseResult(
        text=text,
        markdown=text,
        sections=[],
        pages=pages,
        has_structure=False,
        source_tier=source_tier,
        num_pages=num_pages,
        source_format="pypdf_fast_text",
    )


def _fast_pdf_text_is_usable(result: DoclingParseResult) -> bool:
    """Heuristic gate retained for diagnostics.

    Digital PDFs usually expose plenty of text via pypdf; scanned PDFs and
    image-heavy documents return empty or sparse pages. OCR is disabled, so a
    sparse result is returned as sparse text rather than falling into OCR.
    """
    text = result.text or result.markdown or ""
    compact = "".join(text.split())
    if not compact:
        return False

    pages = result.pages or [text]
    page_count = max(1, len(pages))
    nonempty_pages = [
        p for p in pages if len("".join((p or "").split())) >= _FAST_PDF_MIN_AVG_CHARS_PER_PAGE
    ]
    avg_chars = len(compact) / page_count
    nonempty_ratio = len(nonempty_pages) / page_count
    replacement_ratio = text.count("\ufffd") / max(1, len(text))
    min_total_chars = 300 if page_count <= 2 else _FAST_PDF_MIN_TOTAL_CHARS

    return (
        len(compact) >= min_total_chars
        and avg_chars >= _FAST_PDF_MIN_AVG_CHARS_PER_PAGE
        and nonempty_ratio >= _FAST_PDF_MIN_NONEMPTY_PAGE_RATIO
        and replacement_ratio <= _FAST_PDF_MAX_REPLACEMENT_RATIO
    )


async def parse_document(
    raw_bytes: bytes,
    filename: str,
    mime: str,
    do_ocr: bool = False,
) -> DoclingParseResult:
    """Hand the upload to the docling sidecar and return a structured
    DoclingParseResult. Plain-text uploads are pre-augmented with synthetic
    headers when `inject_synthetic_headers` finds qualifying markers.
    """
    if do_ocr:
        logger.warning("Ignoring do_ocr=True for %s; OCR is disabled by policy", filename)
        do_ocr = False

    if _looks_like_pdf(filename, mime):
        fast_result = _parse_pdf_fast_text(raw_bytes, filename, mime)
        if not do_ocr or _fast_pdf_text_is_usable(fast_result):
            return fast_result

    local_result = _parse_local_text_document(raw_bytes, filename, mime)
    if local_result is not None:
        return local_result

    aug_bytes, aug_filename, aug_mime, augmented, audit = _maybe_augment_plaintext(
        raw_bytes, filename, mime
    )

    files = {"file": (aug_filename, aug_bytes, aug_mime)}
    data = {"do_ocr": "false"}

    async with httpx.AsyncClient(
        base_url=DOCLING_URL,
        timeout=httpx.Timeout(DOCLING_TIMEOUT_SECONDS, connect=30.0),
    ) as client:
        try:
            resp = await client.post("/parse", files=files, data=data)
        except httpx.RequestError as exc:
            raise RuntimeError(
                "Docling parser sidecar is unavailable. Markdown, text, HTML, "
                "and digital PDFs parse locally; this file type needs the "
                "`local-parser` profile. Start it with "
                "`docker compose --profile local-parser up -d docling`, or "
                "convert the file to .md/.txt before ingest."
            ) from exc
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
