"""
Phase 7.6 — docling adapter smoke tests.

These hit the running docling sidecar over HTTP (DOCLING_URL env var). Run
inside the backend container after `docker compose up -d docling backend`:

    docker compose exec backend pytest backend/tests/test_docling_adapter.py -v

Each test exercises one classifier path:
  • Markdown with native H1/H2          → tier_a, has_structure=True
  • Plain .txt with semantic ALL CAPS   → tier_b_plus (via inject_synthetic_headers
                                          pre-augmentation), heading_path filled
  • DOCX with real heading styles       → tier_a (or tier_b_plus when augmented)
"""

from __future__ import annotations

import io
import os

import pytest

# Skip the whole module when DOCLING_URL is unset and the sidecar isn't
# reachable — keeps CI happy outside of docker compose.
DOCLING_URL = os.getenv("DOCLING_URL", "http://docling:8500")


@pytest.fixture(scope="module")
def adapter():
    from services.ingestion import docling_adapter
    return docling_adapter


@pytest.mark.asyncio
async def test_pdf_with_ocr_disabled_uses_fast_text_path(adapter, monkeypatch):
    """PDF + do_ocr=False bypasses Docling and uses local pypdf extraction."""
    from services.ingestion import format_router

    called = {}

    def fake_route(data: bytes, filename: str = "", mime_hint: str = ""):
        called["args"] = (data, filename, mime_hint)
        return format_router.DecodeResult(
            text="Page one text.\n\nPage two text.",
            source_mime="application/pdf",
            doc_id="fake",
            pages=["Page one text.", "Page two text."],
        )

    monkeypatch.setattr(format_router, "route", fake_route)

    result = await adapter.parse_document(
        raw_bytes=b"%PDF fake",
        filename="book.pdf",
        mime="application/pdf",
        do_ocr=False,
    )

    assert called["args"] == (b"%PDF fake", "book.pdf", "application/pdf")
    assert result.source_format == "pypdf_fast_text"
    assert result.source_tier.value == "ocr_ast"
    assert result.sections == []
    assert result.pages == ["Page one text.", "Page two text."]


@pytest.mark.asyncio
async def test_pdf_do_ocr_true_is_ignored_by_policy(adapter, monkeypatch):
    """OCR is dead-off: even legacy true values stay on the pypdf text path."""
    from services.ingestion import format_router

    def fake_route(data: bytes, filename: str = "", mime_hint: str = ""):
        return format_router.DecodeResult(
            text="Sparse but available text.",
            source_mime="application/pdf",
            doc_id="fake",
            pages=["Sparse but available text."],
        )

    async def fail_post(*_args, **_kwargs):  # pragma: no cover - should never run
        raise AssertionError("Docling sidecar should not be called for PDF OCR")

    monkeypatch.setattr(format_router, "route", fake_route)
    monkeypatch.setattr(adapter.httpx.AsyncClient, "post", fail_post, raising=False)

    result = await adapter.parse_document(
        raw_bytes=b"%PDF fake",
        filename="scanned.pdf",
        mime="application/pdf",
        do_ocr=True,
    )

    assert result.source_format == "pypdf_fast_text"
    assert result.text == "Sparse but available text."


def test_fast_pdf_text_gate_accepts_digital_pdf(adapter):
    text = " ".join(["usable digital pdf text"] * 100)
    result = adapter.DoclingParseResult(
        text=text,
        markdown=text,
        sections=[],
        pages=[text[:900], text[900:]],
        has_structure=False,
        source_tier=adapter.SourceTier.ocr_ast,
        num_pages=2,
        source_format="pypdf_fast_text",
    )

    assert adapter._fast_pdf_text_is_usable(result)


def test_fast_pdf_text_gate_rejects_sparse_scanned_pdf(adapter):
    result = adapter.DoclingParseResult(
        text="  page 1  ",
        markdown="  page 1  ",
        sections=[],
        pages=["", "page 1", ""],
        has_structure=False,
        source_tier=adapter.SourceTier.ocr_ast,
        num_pages=3,
        source_format="pypdf_fast_text",
    )

    assert not adapter._fast_pdf_text_is_usable(result)


def test_markdown_sections_ignore_headings_inside_fenced_code(adapter):
    md = (
        "# Real Section\n\n"
        "Intro paragraph.\n\n"
        "```python\n"
        "# not a real heading\n"
        "class MyModel:\n"
        "    pass\n"
        "```\n\n"
        "~~~bash\n"
        "## also not a real heading\n"
        "echo hello\n"
        "~~~~\n\n"
        "## Real Child Section\n\n"
        "Body paragraph.\n"
    )

    sections, h1_count, h2_count = adapter._markdown_sections(md)

    headings = [s.text for s in sections if s.element_type == "section_heading"]
    paragraphs = [s.text for s in sections if s.element_type == "paragraph"]
    assert headings == ["Real Section", "Real Child Section"]
    assert h1_count == 1
    assert h2_count == 1
    assert any("```python" in p and "# not a real heading" in p for p in paragraphs)
    assert any("~~~bash" in p and "## also not a real heading" in p for p in paragraphs)


def test_markdown_sections_unclosed_fence_does_not_leak_between_documents(adapter):
    first_sections, first_h1, first_h2 = adapter._markdown_sections(
        "```python\n# not a heading\n"
    )
    second_sections, second_h1, second_h2 = adapter._markdown_sections("# Real Heading\n")

    assert [s.text for s in first_sections if s.element_type == "section_heading"] == []
    assert first_h1 == 0
    assert first_h2 == 0
    assert [s.text for s in second_sections if s.element_type == "section_heading"] == [
        "Real Heading"
    ]
    assert second_h1 == 1
    assert second_h2 == 0


@pytest.mark.asyncio
async def test_markdown_with_headings_is_tier_a(adapter):
    """Native MD headings → tier_a, has_structure True, sections populated."""
    md = (
        "# Project Overview\n\n"
        "This is the intro paragraph.\n\n"
        "## Goals\n\n"
        "Goal one paragraph.\n\n"
        "## Architecture\n\n"
        "Architecture paragraph.\n\n"
        "# Conclusion\n\n"
        "Wrap-up paragraph.\n"
    )
    result = await adapter.parse_document(
        raw_bytes=md.encode("utf-8"),
        filename="overview.md",
        mime="text/markdown",
        do_ocr=False,
    )
    assert result.has_structure, "expected docling to detect >=2 headings"
    assert result.source_tier.value == "tier_a", f"got {result.source_tier}"
    # Walk the sections and confirm at least one section_heading + non-empty
    # heading_path on a paragraph.
    headings = [s for s in result.sections if s.element_type == "section_heading"]
    paragraphs = [s for s in result.sections if s.element_type != "section_heading"]
    assert len(headings) >= 2
    assert any(p.heading_path for p in paragraphs), "paragraph missing heading_path"


@pytest.mark.asyncio
async def test_plaintext_caps_lines_promote_to_tier_b_plus(adapter):
    """Plain .txt with ALL CAPS standalone "headings" should be pre-augmented
    by the adapter, parsed by docling as a markdown doc, and classified as
    tier_b_plus.
    """
    txt = (
        "Onboarding:\n\n"
        "The First Glimpse onboarding flow runs in 10 minutes.\n\n"
        "ONBOARDING & COLD START (#1-3)\n\n"
        "The core problem is that your most powerful moment is locked.\n\n"
        "ALTERNATIVELY THIS CARD IS LOCAL\n\n"
        "Local fallback paragraph here.\n"
    )
    result = await adapter.parse_document(
        raw_bytes=txt.encode("utf-8"),
        filename="Onboarding.txt",
        mime="text/plain",
        do_ocr=False,
    )
    assert result.augmented_with_synthetic_headers, "adapter should pre-augment .txt"
    assert result.source_tier.value == "tier_b_plus", f"got {result.source_tier}"
    assert result.has_structure
    assert any(s.heading_path for s in result.sections if s.element_type != "section_heading"), \
        "expected at least one paragraph with a heading_path"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docx_with_headings_classifies_with_structure(adapter):
    """A DOCX built with proper heading styles should classify as tier_a or
    tier_b_plus and yield non-empty sections. We synthesize a tiny .docx
    inline rather than shipping a binary fixture.

    Marked integration: DOCX parsing is delegated to the docling sidecar
    (`http://docling:8500`) — there's no local Python path for .docx in this
    build. Run with `docker compose --profile local-parser up -d docling`
    then `pytest -m integration tests/test_docling_adapter.py`.
    """
    pytest.importorskip("docx")  # python-docx
    from docx import Document

    doc = Document()
    doc.add_heading("Quarterly Plan", level=1)
    doc.add_paragraph("This quarter we focus on retrieval quality.")
    doc.add_heading("Workstreams", level=2)
    doc.add_paragraph("Workstream A: ingestion. Workstream B: ranking.")
    doc.add_heading("Risks", level=2)
    doc.add_paragraph("Risk one: docling first-run latency.")

    buf = io.BytesIO()
    doc.save(buf)
    raw = buf.getvalue()

    result = await adapter.parse_document(
        raw_bytes=raw,
        filename="plan.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        do_ocr=False,
    )
    assert result.has_structure, "DOCX with >=2 headings should report has_structure"
    assert result.source_tier.value in {"tier_a", "tier_b_plus"}, \
        f"unexpected tier {result.source_tier}"
    # Heading text must round-trip into the section walk.
    titles = {s.text.strip() for s in result.sections if s.element_type == "section_heading"}
    assert "Quarterly Plan" in titles
