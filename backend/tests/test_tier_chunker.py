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
    assert config["hard_token_split_enabled"] is True
    assert config["page_ranges_preserved"] is True


def test_giant_single_paragraph_is_hard_split_to_child_budget():
    giant = " ".join(f"token{i}" for i in range(2500))
    cfg = IngestionConfig(
        parent_chunk_tokens={
            "min_tokens": 100,
            "target_tokens": 300,
            "max_tokens": 500,
        },
        child_chunk_tokens={
            "min_tokens": 100,
            "target_tokens": 220,
            "max_tokens": 500,
        },
        chunk_overlap=40,
    )

    parents, children, _ = tier_chunker.chunk(
        _parse_result(source_tier=SourceTier.tier_c, text=giant),
        doc_id="doc",
        corpus_id="corpus",
        config=cfg,
    )

    assert len(parents) > 1
    assert len(children) > 3
    assert max(tier_chunker._count_tokens(parent.text) for parent in parents) <= 500
    assert max(child.token_count for child in children) <= 500


def test_overlap_does_not_recreate_oversized_children():
    text = "\n\n".join([" ".join(["alpha"] * 130) for _ in range(12)])
    cfg = IngestionConfig(
        parent_chunk_tokens={"min_tokens": 100, "target_tokens": 300, "max_tokens": 500},
        child_chunk_tokens={"min_tokens": 100, "target_tokens": 220, "max_tokens": 500},
        chunk_overlap=100,
    )

    _parents, children, _ = tier_chunker.chunk(
        _parse_result(source_tier=SourceTier.tier_c, text=text),
        doc_id="doc",
        corpus_id="corpus",
        config=cfg,
    )

    assert children
    assert max(child.token_count for child in children) <= 500
