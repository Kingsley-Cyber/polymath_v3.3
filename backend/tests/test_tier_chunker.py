from types import SimpleNamespace

from models.schemas import IngestionConfig, SourceTier
from services.ingestion import tier_chunker
from services.ingestion.docling_adapter import _markdown_sections, _parse_local_text_document


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


def test_tier_a_child_chunks_respect_configured_child_max_tokens():
    text = ("alpha beta gamma delta epsilon. " * 260).strip()
    cfg = IngestionConfig(
        parent_chunk_tokens={
            "min_tokens": 100,
            "target_tokens": 800,
            "max_tokens": 1200,
        },
        child_chunk_tokens={
            "min_tokens": 100,
            "target_tokens": 200,
            "max_tokens": 500,
        },
        chunk_overlap=0,
    )

    _, children, _ = tier_chunker.chunk(
        _parse_result(source_tier=SourceTier.tier_a, text=text),
        doc_id="doc",
        corpus_id="corpus",
        config=cfg,
    )

    assert len(children) > 1
    assert max(c.token_count for c in children) <= 500


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


def test_scrub_strips_bare_pandoc_div_fences():
    raw = "::: Para\nReal body text here.\n:::"
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert "::: Para" not in cleaned
    assert ":::" not in cleaned
    assert cleaned == "Real body text here."


def test_scrub_preserves_visible_text_from_inline_spans():
    raw = "The system uses [Qwen3-Embedding]{.product} for vectors."
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert cleaned == "The system uses Qwen3-Embedding for vectors."
    assert "{.product}" not in cleaned


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


def test_scrub_splits_pathological_epub_mega_lines():
    raw = "Before\n" + ("alpha beta gamma " * 500).strip() + "\nAfter"
    cleaned = tier_chunker._scrub_markup_noise(raw)
    assert "Before" in cleaned and "After" in cleaned
    assert max(len(line) for line in cleaned.splitlines()) <= 2500
    assert "\n\n" in cleaned


def test_table_splitter_normalizes_calibre_layout_line_without_rows():
    table_text = "Table: layout\n" + ("cell value | " * 700).strip()
    groups = tier_chunker._split_table_rows_for_children(
        table_text,
        {},
        child_target_tokens=200,
        child_max_tokens=500,
    )
    assert len(groups) == 1
    text, _ = groups[0]
    assert max(len(line) for line in text.splitlines()) <= 2500


def test_local_markdown_heading_anchor_is_removed_from_heading_path():
    md = "# Embedding Pipeline {#embedding-pipeline}\n\nBody text."
    sections, _, _ = _markdown_sections(md)
    assert sections[0].heading_path == ["Embedding Pipeline"]
    assert sections[1].heading_path == ["Embedding Pipeline"]


def test_sections_to_blocks_strips_heading_anchor_metadata():
    sections = [
        _section(
            "Embedding Pipeline {#embedding-pipeline}",
            element_type="section_heading",
            heading_path=["Embedding Pipeline {#embedding-pipeline}"],
            level=1,
        ),
        _section("Body text.", element_type="paragraph", heading_path=["Embedding Pipeline {#embedding-pipeline}"]),
    ]
    blocks = tier_chunker._sections_to_parent_blocks(sections)
    assert blocks[0][0] == ["Embedding Pipeline"]


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


def test_child_min_coalesce_prefers_previous_without_exceeding_max():
    long_enough = ("alpha " * 120).strip()
    tiny = "tail"
    out = tier_chunker._coalesce_small_child_texts(
        [long_enough, tiny],
        child_min_tokens=50,
        child_max_tokens=200,
    )
    assert len(out) == 1
    assert "tail" in out[0]
    assert tier_chunker._count_tokens(out[0]) <= 200


def test_child_min_coalesce_uses_next_when_previous_would_exceed_max():
    near_max = ("alpha " * 200).strip()
    tiny = "short"
    next_text = ("beta " * 40).strip()
    out = tier_chunker._coalesce_small_child_texts(
        [near_max, tiny, next_text],
        child_min_tokens=50,
        child_max_tokens=200,
    )
    assert len(out) == 2
    assert out[0] == near_max
    assert out[1].startswith("short")
    assert "beta" in out[1]
    assert all(tier_chunker._count_tokens(text) <= 200 for text in out)


def test_child_min_coalesce_leaves_tiny_when_both_neighbors_exceed_max():
    near_max_a = ("alpha " * 200).strip()
    tiny = ("short " * 10).strip()
    near_max_b = ("beta " * 200).strip()
    out = tier_chunker._coalesce_small_child_texts(
        [near_max_a, tiny, near_max_b],
        child_min_tokens=50,
        child_max_tokens=200,
    )
    assert out == [near_max_a, tiny, near_max_b]


# ─────────────────────────────────────────────────────────────────────────
# Code lane (Phase 1) — code-file ingest, markdown fence routing, AST
# packing, embedder-safety contract, and metadata propagation.
# ─────────────────────────────────────────────────────────────────────────

from services.ingestion.section_classifier import ChunkKind  # noqa: E402


def _section(text, *, element_type="paragraph", heading_path=None, language=None, level=None, metadata=None):
    return SimpleNamespace(
        heading_path=heading_path or [],
        text=text,
        element_type=element_type,
        level=level,
        language=language,
        metadata=metadata or {},
    )


def _code_parse_result(*, sections, language=None, filename="sample.py", source_tier=SourceTier.tier_code):
    return SimpleNamespace(
        source_tier=source_tier,
        text="\n\n".join(s.text for s in sections),
        markdown="\n\n".join(s.text for s in sections),
        sections=sections,
        pages=None,
        injected_headers_audit=[],
        language=language,
        filename=filename,
    )


def test_tier_code_routes_through_code_splitter():
    src = "def hello():\n    return 1\n\nclass X:\n    pass\n"
    parents, children, _ = tier_chunker.chunk(
        _code_parse_result(
            sections=[_section(src, element_type="code_block", heading_path=["foo.py"], language="python")],
            language="python",
            filename="foo.py",
        ),
        doc_id="doc1",
        corpus_id="corpus1",
    )
    assert len(parents) >= 1
    for p in parents:
        assert p.chunk_kind == ChunkKind.CODE
        assert p.language == "python"
        assert p.source_tier == SourceTier.tier_code.value
    # metadata.file_path stamped from the parse result's filename
    assert any(c.metadata.get("file_path") == "foo.py" for c in children)
    # symbols_defined populated by code_splitter
    defined = set()
    for c in children:
        defined.update(c.metadata.get("symbols_defined", []))
    assert "hello" in defined or "X" in defined


def test_tier_code_no_child_exceeds_embedder_cap(monkeypatch):
    # Build a fat Python listing that would blow past the cap if not packed.
    src = "import numpy as np\n\n" + "\n\n".join(
        f"def fn_{i}(x):\n    return x + {i}\n" for i in range(80)
    )
    cap = 200
    # Monkeypatch the safety cap getter so the test isn't tied to Settings.
    monkeypatch.setattr(tier_chunker, "_embedder_safe_max_tokens", lambda: cap)

    parents, children, _ = tier_chunker.chunk(
        _code_parse_result(
            sections=[_section(src, element_type="code_block", language="python", heading_path=["fat.py"])],
            language="python",
            filename="fat.py",
        ),
        doc_id="doc2",
        corpus_id="corpus2",
    )
    assert children, "expected at least one child chunk"
    for c in children:
        # Hard contract: every child fits the embedder cap.
        assert c.token_count <= cap, (
            f"child {c.chunk_id} token_count={c.token_count} > cap={cap}"
        )


def test_tier_code_unknown_language_still_under_cap(monkeypatch):
    # Unsupported language → pack returns sentinel → caller hard-splits.
    src = "MOVE 1 TO X.\n" * 200  # ~600 tokens of fake COBOL
    cap = 100
    monkeypatch.setattr(tier_chunker, "_embedder_safe_max_tokens", lambda: cap)
    parents, children, _ = tier_chunker.chunk(
        _code_parse_result(
            sections=[_section(src, element_type="code_block", language="cobol")],
            language="cobol",
            filename="legacy.cob",
        ),
        doc_id="doc3",
        corpus_id="corpus3",
    )
    for c in children:
        assert c.token_count <= cap


def test_tier_a_markdown_with_code_fence_emits_code_parent(monkeypatch):
    # Simulate sections that the markdown walker would produce: a heading
    # + a code_block + a paragraph + a code_block.
    sections = [
        _section("Intro", element_type="section_heading", heading_path=["Intro"], level=1),
        _section("Prose paragraph one. " * 20, element_type="paragraph", heading_path=["Intro"]),
        _section(
            "```python\ndef foo():\n    return 1\n```",
            element_type="code_block",
            heading_path=["Intro"],
            language="python",
        ),
        _section("Prose paragraph two. " * 20, element_type="paragraph", heading_path=["Intro"]),
    ]
    pr = SimpleNamespace(
        source_tier=SourceTier.tier_a,
        text="",
        markdown="",
        sections=sections,
        pages=None,
        injected_headers_audit=[],
        language=None,
        filename="book.md",
    )
    parents, children, _ = tier_chunker.chunk(pr, doc_id="doc4", corpus_id="corpus4")
    code_parents = [p for p in parents if p.chunk_kind == ChunkKind.CODE]
    body_parents = [p for p in parents if p.chunk_kind == ChunkKind.BODY]
    assert code_parents, "expected at least one CODE parent"
    assert body_parents, "expected at least one BODY parent"
    for p in code_parents:
        assert p.language == "python"


def test_coalesce_does_not_merge_code_into_prose():
    blocks = [
        (["sec"], "prose one " * 10, ChunkKind.BODY, None, {}),
        (["sec"], "```python\ndef f(): pass\n```", ChunkKind.CODE, "python", {}),
        (["sec"], "prose two " * 10, ChunkKind.BODY, None, {}),
    ]
    out = tier_chunker._coalesce_small_blocks(
        blocks, min_parent_tokens=1000, max_parent_tokens=4000
    )
    kinds = [b[2] for b in out]
    # CODE must remain its own block; the two BODY blocks may have merged.
    assert ChunkKind.CODE in kinds
    code_count = sum(1 for k in kinds if k == ChunkKind.CODE)
    assert code_count == 1


def test_sections_to_parent_blocks_emits_code_blocks():
    sections = [
        _section("H1", element_type="section_heading", heading_path=["H1"], level=1),
        _section("prose body", element_type="paragraph", heading_path=["H1"]),
        _section(
            "```python\ndef g(): pass\n```",
            element_type="code_block",
            heading_path=["H1"],
            language="python",
        ),
    ]
    blocks = tier_chunker._sections_to_parent_blocks(sections)
    kinds = [b[2] for b in blocks]
    languages = [b[3] for b in blocks]
    assert ChunkKind.CODE in kinds
    assert "python" in languages


def test_markdown_sections_detects_pipe_table_with_caption():
    md = """# Evaluation Results

Intro paragraph before the table.

Table 2. Double Qwen performance on MeetingBank.

| Component | Model | Size | Role |
| --- | --- | ---: | --- |
| Embedder | Qwen3-Embedding-0.6B | 0.6B | vector embeddings |
| Reranker | Qwen3-Reranker-0.6B | 0.6B | cross-encoder reranking |

After table prose.
"""
    sections, h1, h2 = _markdown_sections(md)

    assert h1 == 1
    assert h2 == 0
    assert [s.element_type for s in sections] == [
        "section_heading",
        "paragraph",
        "table",
        "paragraph",
    ]
    table = sections[2]
    assert table.heading_path == ["Evaluation Results"]
    assert table.metadata["caption"] == "Table 2. Double Qwen performance on MeetingBank."
    assert table.metadata["columns"] == ["Component", "Model", "Size", "Role"]
    assert table.metadata["row_count"] == 2
    assert "Columns: Component | Model | Size | Role" in table.text
    assert "Model=Qwen3-Embedding-0.6B" in table.text
    assert "Model=Qwen3-Reranker-0.6B" in table.text


def test_table_section_emits_table_parent_and_child_metadata():
    md = """# Evaluation Results

Table 2. Double Qwen performance on MeetingBank.

| Component | Model | Size | Role |
| --- | --- | ---: | --- |
| Embedder | Qwen3-Embedding-0.6B | 0.6B | vector embeddings |
| Reranker | Qwen3-Reranker-0.6B | 0.6B | cross-encoder reranking |
"""
    sections, _, _ = _markdown_sections(md)
    pr = SimpleNamespace(
        source_tier=SourceTier.tier_a,
        text=md,
        markdown=md,
        sections=sections,
        pages=None,
        injected_headers_audit=[],
        language=None,
        filename="double-qwen.md",
    )

    parents, children, _ = tier_chunker.chunk(pr, doc_id="doc_table", corpus_id="corpus")

    table_parents = [p for p in parents if p.chunk_kind == ChunkKind.TABLE]
    table_children = [c for c in children if c.chunk_kind == ChunkKind.TABLE]
    assert len(table_parents) == 1
    assert len(table_children) == 1
    assert table_parents[0].metadata["columns"] == ["Component", "Model", "Size", "Role"]
    assert table_parents[0].metadata["row_count"] == 2
    assert table_children[0].metadata["caption"].startswith("Table 2.")
    assert table_children[0].metadata["row_start"] == 1
    assert table_children[0].metadata["row_end"] == 2
    assert "Row 2: Component=Reranker" in table_children[0].text


def test_plain_text_table_promotes_to_section_aware_tier():
    text = """Table 1. Double Qwen roles.

| Component | Model |
| --- | --- |
| Embedder | Qwen3-Embedding-0.6B |
| Reranker | Qwen3-Reranker-0.6B |
"""
    parsed = _parse_local_text_document(text.encode("utf-8"), "notes.txt", "text/plain")

    assert parsed is not None
    assert parsed.source_tier == SourceTier.tier_b
    assert parsed.has_structure is True
    assert any(s.element_type == "table" for s in parsed.sections)


def test_large_table_splits_by_row_group_and_repeats_context():
    rows = "\n".join(
        f"| Metric {i} | Qwen3-Embedding-0.6B | Qwen3-Reranker-0.6B | {i * 2}.5 |"
        for i in range(1, 18)
    )
    md = f"""# Evaluation Results

Table 3. Double Qwen metric comparison.

| Metric | Embedder | Reranker | Score |
| --- | --- | --- | ---: |
{rows}
"""
    sections, _, _ = _markdown_sections(md)
    cfg = IngestionConfig(
        parent_chunk_tokens={"min_tokens": 100, "target_tokens": 220, "max_tokens": 700},
        child_chunk_tokens={"min_tokens": 100, "target_tokens": 200, "max_tokens": 500},
        chunk_overlap=0,
    )
    pr = SimpleNamespace(
        source_tier=SourceTier.tier_a,
        text=md,
        markdown=md,
        sections=sections,
        pages=None,
        injected_headers_audit=[],
        language=None,
        filename="large-table.md",
    )

    parents, children, _ = tier_chunker.chunk(pr, doc_id="doc_large_table", corpus_id="corpus", config=cfg)
    table_children = [c for c in children if c.chunk_kind == ChunkKind.TABLE]

    assert len(table_children) > 1
    assert all("Columns: Metric | Embedder | Reranker | Score" in c.text for c in table_children)
    assert table_children[0].metadata["row_start"] == 1
    assert table_children[-1].metadata["row_end"] == 17
    assert all(c.metadata["caption"] == "Table 3. Double Qwen metric comparison." for c in table_children)


def test_chunk_kind_filters_through_parent_dataclass():
    # Regression — confirm the new fields default cleanly when missing on
    # rehydrated data (chunk_kind=BODY, language=None, metadata={}).
    sections = [_section("hello", element_type="paragraph", heading_path=["H"])]
    pr = SimpleNamespace(
        source_tier=SourceTier.tier_a,
        text="hello",
        markdown="hello",
        sections=sections,
        pages=None,
        injected_headers_audit=[],
        language=None,
        filename=None,
    )
    parents, children, _ = tier_chunker.chunk(pr, doc_id="doc5", corpus_id="corpus5")
    if parents:
        assert parents[0].chunk_kind in (ChunkKind.BODY, ChunkKind.FRONT_MATTER, ChunkKind.BACK_MATTER, ChunkKind.TOC, ChunkKind.BIBLIOGRAPHY, ChunkKind.INDEX, ChunkKind.APPENDIX)
        assert parents[0].language is None
        assert parents[0].metadata == {}


def test_describe_chunking_reports_ast_bound_for_tier_code():
    pr = SimpleNamespace(
        source_tier=SourceTier.tier_code,
        text="def f(): pass\n",
        markdown="def f(): pass\n",
        sections=[],
        pages=None,
        injected_headers_audit=[],
    )
    desc = tier_chunker.describe_chunking(pr)
    assert desc["parent_strategy"] == "ast_bound_code"

