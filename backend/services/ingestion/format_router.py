"""
Format router — MIME detection and document decoding (Phase 1).

Produces DecodeResult with decoded text and stable doc_id (SHA-256 of normalized text).

Fast paths (explicit decoders, no heavy deps loaded at call time):
  PDF   (application/pdf)       → pypdf page-text extraction
  HTML  (text/html, xhtml+xml)  → BeautifulSoup boilerplate strip
  TEXT  (text/plain, markdown)  → UTF-8 decode pass-through

Catch-all fallback (Phase 10.16+):
  Any other MIME → unstructured.partition.auto.partition()
                   Handles docx, pptx, xlsx, epub, odt, rtf, msg, eml, csv, …
                   (unstructured carries its own MIME→partitioner mapping; we
                    do not reimplement it here.)

Only truly UTF-8-decodable text (plain/markdown) skips the decoder side. Every
other binary format flows through an extraction pass before chunking.
"""

import hashlib
import io
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# File extensions that should bypass the text pass-through and go to the
# unstructured partitioner even when `mimetypes.guess_type` returns nothing
# useful (many office/ebook formats guess to application/octet-stream).
_UNSTRUCTURED_EXTS = frozenset(
    {
        ".docx", ".doc",
        ".pptx", ".ppt",
        ".xlsx", ".xls",
        ".epub",
        ".odt", ".ods", ".odp",
        ".rtf",
        ".msg", ".eml",
        ".csv", ".tsv",
        ".rst", ".org",
    }
)

# MIME prefixes that should go to unstructured even if the extension is unknown.
_UNSTRUCTURED_MIME_PREFIXES = (
    "application/vnd.openxmlformats-officedocument",  # docx, pptx, xlsx
    "application/vnd.ms-",                            # legacy ms office
    "application/vnd.oasis.opendocument",             # odt, ods, odp
    "application/epub",
    "application/rtf",
    "message/rfc822",                                 # eml
)


@dataclass
class DecodeResult:
    text: str
    source_mime: str
    doc_id: str
    pages: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _route_pdf(data: bytes) -> DecodeResult:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(p for p in pages if p.strip())
    except Exception as exc:
        logger.warning("pypdf failed, raw UTF-8 fallback: %s", exc)
        text = data.decode("utf-8", errors="replace")
        pages = [text]
    return DecodeResult(
        text=text,
        source_mime="application/pdf",
        doc_id=_sha256(_normalize(text)),
        pages=pages,
    )


def _route_html(data: bytes) -> DecodeResult:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
    except Exception as exc:
        logger.warning("BeautifulSoup failed, raw UTF-8 fallback: %s", exc)
        text = data.decode("utf-8", errors="replace")
    return DecodeResult(
        text=text,
        source_mime="text/html",
        doc_id=_sha256(_normalize(text)),
    )


def _route_text(data: bytes, source_mime: str) -> DecodeResult:
    text = data.decode("utf-8", errors="replace")
    return DecodeResult(
        text=text,
        source_mime=source_mime,
        doc_id=_sha256(_normalize(text)),
    )


def _route_unstructured(
    data: bytes, filename: str, source_mime: str
) -> DecodeResult:
    """
    Catch-all decoder: delegate to `unstructured.partition.auto.partition`.

    Handles docx, pptx, xlsx, epub, odt, rtf, msg, eml, csv, tsv, etc.
    `unstructured` inspects the bytes + filename and picks the right partitioner
    internally — we do not re-implement the MIME-to-partitioner table.

    Fallback: on any partition failure (missing optional dep, corrupt file),
    fall through to UTF-8 decode so the pipeline still yields something usable.
    """
    try:
        from unstructured.partition.auto import partition

        elements = partition(file=io.BytesIO(data), metadata_filename=filename or None)
        text = "\n\n".join(
            (el.text or "").strip() for el in elements if getattr(el, "text", None)
        )
        if not text.strip():
            raise ValueError("unstructured returned no text")
    except Exception as exc:
        logger.warning(
            "unstructured partition failed for %s (%s) — UTF-8 fallback",
            filename or "<unnamed>",
            exc,
        )
        text = data.decode("utf-8", errors="replace")

    return DecodeResult(
        text=text,
        source_mime=source_mime or "application/octet-stream",
        doc_id=_sha256(_normalize(text)),
    )


def _should_use_unstructured(ext: str, mime: str) -> bool:
    """True when the file looks binary/office/ebook and needs partitioning."""
    if ext in _UNSTRUCTURED_EXTS:
        return True
    if mime and any(mime.startswith(p) for p in _UNSTRUCTURED_MIME_PREFIXES):
        return True
    return False


def route(data: bytes, filename: str = "", mime_hint: str = "") -> DecodeResult:
    """
    Route document bytes to the correct decoder.

    Returns DecodeResult with normalized text and stable doc_id (SHA-256).
    doc_id is deterministic — same content always yields the same id.

    Dispatch order (most-specific first):
      1. PDF       → _route_pdf (pypdf)
      2. HTML      → _route_html (BeautifulSoup)
      3. Office/ebook/other binary → _route_unstructured (partition.auto)
      4. Everything else → _route_text (UTF-8 pass-through, treats markdown same as text)
    """
    mime = mime_hint
    if not mime and filename:
        guessed, _ = mimetypes.guess_type(filename)
        mime = guessed or ""

    ext = Path(filename).suffix.lower() if filename else ""

    if mime == "application/pdf" or ext == ".pdf":
        return _route_pdf(data)
    if mime in ("text/html", "application/xhtml+xml") or ext in (".html", ".htm"):
        return _route_html(data)
    if _should_use_unstructured(ext, mime):
        return _route_unstructured(data, filename, mime)

    detected_mime = mime or "text/plain"
    if ext in (".md", ".markdown"):
        detected_mime = "text/markdown"
    return _route_text(data, detected_mime)
