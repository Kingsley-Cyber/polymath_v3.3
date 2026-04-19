"""
Docling sidecar — wraps IBM Docling behind a single FastAPI POST /parse.

Why a sidecar instead of in-backend:
  • Docling pulls torch + torchvision + accelerate (~2 GB) and downloads
    layout/OCR model weights on first run (~1.5 GB more).
  • Backend currently has zero ML deps — adding them would ~5x the image
    size and risk httpx version conflicts (docling needs >=0.28, backend
    pinned to 0.25).
  • Mirrors the existing embedder/reranker sidecar pattern.

Single endpoint:
  POST /parse  multipart upload (`file`) + optional flags
    →  { markdown, text, sections[], pages[]|null, has_structure,
         num_pages, h1_count, h2_count, source_format }

The backend's `services/ingestion/docling_adapter.py` is the only caller.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import (
    DocumentConverter,
    DocumentStream,
    PdfFormatOption,
)

# CUDA pin — explicit beats relying on device='auto'. When CUDA isn't
# available (e.g. running this image on a CPU-only host for dev), fall
# back to CPU so the sidecar still boots.
try:
    import torch  # noqa: F401  (used by the .is_available() check)
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    _CUDA_OK = torch.cuda.is_available()
except Exception:  # pragma: no cover - defensive
    AcceleratorDevice = None  # type: ignore[assignment]
    AcceleratorOptions = None  # type: ignore[assignment]
    _CUDA_OK = False

logger = logging.getLogger("docling_svc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Polymath Docling Sidecar", version="1.0.0")

# Hard cap on upload size — fail cleanly with HTTP 413 instead of OOMing
# the worker on a 500 MB garbage payload. Real-world DOCX/PDF tops out
# around 100 MB; 150 MB leaves headroom.
MAX_UPLOAD_BYTES = 150 * 1024 * 1024


class Section(BaseModel):
    heading_path: list[str]
    text: str
    element_type: str  # "section_heading" | "paragraph" | "list_item" | ...
    level: int | None = None  # heading level when element_type == "section_heading"


class ParseResponse(BaseModel):
    markdown: str
    text: str
    sections: list[Section]
    pages: list[str] | None  # filled for multi-page PDFs
    has_structure: bool      # >= 2 section_heading nodes
    h1_count: int
    h2_count: int
    num_pages: int
    source_format: str       # docling's detected InputFormat name


# ── Single shared converter — instantiating per-request would re-load models.
# OCR-disabled variant is built lazily on demand.
_converters: dict[bool, DocumentConverter] = {}


def _get_converter(do_ocr: bool) -> DocumentConverter:
    if do_ocr in _converters:
        return _converters[do_ocr]
    pdf_opts = PdfPipelineOptions()
    pdf_opts.do_ocr = do_ocr
    pdf_opts.do_table_structure = True
    if _CUDA_OK and AcceleratorOptions is not None:
        pdf_opts.accelerator_options = AcceleratorOptions(
            device=AcceleratorDevice.CUDA,
            num_threads=4,
        )
    conv = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts),
        }
    )
    _converters[do_ocr] = conv
    logger.info(
        "DocumentConverter built (do_ocr=%s, cuda=%s)", do_ocr, _CUDA_OK
    )
    return conv


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "ocr_default": True, "cuda": _CUDA_OK}


def _walk_sections(doc) -> tuple[list[Section], int, int]:
    """Walk the DoclingDocument and assemble flat (heading_path, text, type)
    records by accumulating paragraph/list text under the most recent heading
    stack. Heading levels track an in-progress path: a level-N heading pops
    everything at level >= N before pushing.
    """
    sections: list[Section] = []
    path: list[tuple[int, str]] = []  # (level, title)
    h1 = h2 = 0

    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        text = "\n\n".join(s for s in buf if s).strip()
        if not text:
            buf.clear()
            return
        sections.append(
            Section(
                heading_path=[t for _, t in path],
                text=text,
                element_type="paragraph",
            )
        )
        buf.clear()

    # Docling exposes typed lists via doc.texts in document order. iterate_items
    # walks the body tree but we want every textual element including those
    # tucked under groups/list containers.
    for item in getattr(doc, "texts", []) or []:
        label = (getattr(item, "label", None) or "").lower()
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue

        if label == "section_header" or label == "title":
            level = int(getattr(item, "level", 1) or 1)
            if label == "title":
                level = 1
            flush()
            # Pop any equal-or-deeper levels.
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, text))
            sections.append(
                Section(
                    heading_path=[t for _, t in path],
                    text=text,
                    element_type="section_heading",
                    level=level,
                )
            )
            if level == 1:
                h1 += 1
            elif level == 2:
                h2 += 1
        else:
            buf.append(text)

    flush()
    return sections, h1, h2


def _per_page_markdown(doc) -> list[str] | None:
    """For multi-page documents, materialize per-page markdown. Falls back
    to None on single-page or non-paginated formats.
    """
    pages_attr = getattr(doc, "pages", None) or {}
    if not pages_attr or len(pages_attr) <= 1:
        return None
    pages: list[str] = []
    for page_no in sorted(pages_attr.keys()):
        try:
            pages.append(doc.export_to_markdown(page_no=page_no))
        except Exception:
            pages.append("")
    return pages


@app.post("/parse", response_model=ParseResponse)
async def parse(
    file: UploadFile = File(...),
    do_ocr: bool = Form(True),
) -> ParseResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"payload too large: {len(raw)} bytes (max {MAX_UPLOAD_BYTES})",
        )

    converter = _get_converter(do_ocr)
    stream = DocumentStream(name=file.filename or "upload", stream=BytesIO(raw))

    try:
        result = converter.convert(stream)
    except Exception as exc:
        logger.exception("docling convert failed")
        raise HTTPException(status_code=422, detail=f"docling parse failure: {exc}")

    doc = result.document
    try:
        markdown = doc.export_to_markdown()
    except Exception:
        markdown = ""
    try:
        text = doc.export_to_markdown(strict_text=True)
    except Exception:
        text = markdown

    sections, h1, h2 = _walk_sections(doc)
    pages = _per_page_markdown(doc)
    num_pages = len(getattr(doc, "pages", None) or {}) or (len(pages) if pages else 1)
    source_format = getattr(getattr(result, "input", None), "format", None)
    source_format_name = source_format.name if hasattr(source_format, "name") else str(source_format or "")

    return ParseResponse(
        markdown=markdown,
        text=text or markdown,
        sections=sections,
        pages=pages,
        has_structure=(h1 + h2) >= 2,
        h1_count=h1,
        h2_count=h2,
        num_pages=num_pages,
        source_format=source_format_name,
    )
