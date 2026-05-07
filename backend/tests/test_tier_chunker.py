from types import SimpleNamespace

from models.schemas import IngestionConfig, SourceTier
from services.ingestion import tier_chunker


def _parse_result(*, source_tier: SourceTier, text: str = "", pages=None):
    return SimpleNamespace(
        source_tier=source_tier,
        text=text,
        markdown=text,
        sections=[],
        pages=pages,
        injected_headers_audit=[],
    )


def test_pdf_pages_group_into_token_sized_parents_with_page_ranges():
    pages = [("machine learning on device " * 18).strip() for _ in range(9)]
    cfg = IngestionConfig(
        parent_chunk_tokens={
            "min_tokens": 100,
            "target_tokens": 220,
            "max_tokens": 500,
        },
        child_chunk_tokens={
            "min_tokens": 100,
            "target_tokens": 220,
            "max_tokens": 500,
        },
        chunk_overlap=0,
    )

    parents, children, _ = tier_chunker.chunk(
        _parse_result(source_tier=SourceTier.ocr_ast, pages=pages),
        doc_id="doc",
        corpus_id="corpus",
        config=cfg,
    )

    assert 1 < len(parents) < len(pages)
    assert parents[0].page_start == 1
    assert parents[0].page_end and parents[0].page_end > parents[0].page_start
    assert parents[0].heading_path == [f"pages_{parents[0].page_start}-{parents[0].page_end}"]
    assert children
    assert all(c.page_start is not None and c.page_end is not None for c in children)


def test_chunking_config_reports_auto_policy_and_semantic_split_as_hint():
    cfg = IngestionConfig(child_chunk_algorithm="semantic_split")
    parsed = _parse_result(source_tier=SourceTier.ocr_ast, pages=["alpha beta"])

    config = tier_chunker.describe_chunking(parsed, cfg)

    assert config["mode"] == "auto"
    assert config["parent_strategy"] == "pdf_page_grouped"
    assert config["requested_child_strategy"] == "semantic_split"
    assert config["child_strategy"] == "sentence_merge"
    assert config["semantic_split_enabled"] is False
    assert config["page_ranges_preserved"] is True


# ─────────────────────────────────────────────────────────────────────────
# Markup scrub + hard-split safety nets
# ─────────────────────────────────────────────────────────────────────────


def test_scrub_strips_pandoc_div_fences():
    raw = (
        "::: {.section aria-label=\"chapter opening\"}\n"
        "Real body text here.\n"
        ":::"
    )
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert "::: {" not in cleaned
    assert ":::" not in cleaned
    assert "Real body text here." in cleaned


def test_scrub_strips_pandoc_anchors_and_pagebreaks():
    raw = '[]{#b05.xhtml_Page_1219 .pagebreak aria-label="1219" role="doc-pagebreak"}Index {#b05.xhtml_index1}'
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert "[]{#" not in cleaned
    assert "{#" not in cleaned
    assert cleaned.startswith("Index")


def test_scrub_strips_image_markdown():
    raw = "Before\n\n![cover](images/9781119695455.jpg)\n\nAfter"
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert "![" not in cleaned
    assert "Before" in cleaned and "After" in cleaned


def test_scrub_strips_html_img_and_figure():
    raw = (
        "<figure><img src=\"x.jpg\" class=\"cover\" epub:type=\"cover\" "
        "role=\"doc-cover\" alt=\"Professional C++,\"/></figure>\n\n"
        "Real paragraph."
    )
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert "<img" not in cleaned
    assert "<figure" not in cleaned
    assert "Real paragraph." in cleaned


def test_scrub_preserves_inner_text_of_span_and_a():
    raw = '<span id="cover.xhtml_coverstart">welcome</span> <a href="#x">link text</a>'
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert "welcome" in cleaned
    assert "link text" in cleaned
    assert "<span" not in cleaned and "<a" not in cleaned


def test_scrub_idempotent():
    raw = "::: {.section}\nbody\n:::\n\n![alt](x.jpg)"
    once = tier_chunker._scrub_markup_noise(raw)
    twice = tier_chunker._scrub_markup_noise(once)
    assert once == twice


def test_hard_split_breaks_oversize_chunk():
    # A single 2000-token blob with no paragraph breaks — the boundary
    # splitter would leave it intact; the hard-split must break it at the
    # max_tokens cap so the embedder doesn't silently truncate.
    long_text = ("alpha beta gamma " * 800).strip()  # ~2400 tokens
    out = tier_chunker._hard_split_oversize([long_text], max_tokens=700)
    assert len(out) >= 3, "expected at least 3 sub-chunks"
    for piece in out:
        assert tier_chunker._count_tokens(piece) <= 700


def test_hard_split_passthrough_when_under_cap():
    short = "alpha beta gamma."
    out = tier_chunker._hard_split_oversize([short], max_tokens=700)
    assert out == [short]


def test_hard_split_handles_multiple_chunks_mixed_sizes():
    short = "tiny chunk."
    long = ("word " * 1200).strip()  # ~1200 tokens
    out = tier_chunker._hard_split_oversize([short, long, short], max_tokens=500)
    # short chunks pass through; long one becomes >=3 pieces (1200/500≈3)
    assert short in out
    assert sum(1 for c in out if c == short) == 2
    assert len(out) >= 5

