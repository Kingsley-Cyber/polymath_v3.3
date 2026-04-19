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


@pytest.mark.asyncio
async def test_docx_with_headings_classifies_with_structure(adapter):
    """A DOCX built with proper heading styles should classify as tier_a or
    tier_b_plus and yield non-empty sections. We synthesize a tiny .docx
    inline rather than shipping a binary fixture.
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
