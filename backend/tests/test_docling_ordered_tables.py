from types import SimpleNamespace

import pytest

try:
    from docling_svc.main import _walk_sections
except ImportError as exc:  # local host env can lack sidecar-pinned deps
    pytest.skip(f"docling sidecar imports unavailable: {exc}", allow_module_level=True)


class FakeTable:
    label = "table"
    self_ref = "#/tables/0"
    parent = None

    def export_to_markdown(self, doc=None):
        return """| Component | Model |
| --- | --- |
| Embedder | Qwen3-Embedding-0.6B |
| Reranker | Qwen3-Reranker-0.6B |
"""


def _text(text, *, label="paragraph", parent_ref=""):
    parent = SimpleNamespace(cref=parent_ref) if parent_ref else None
    return SimpleNamespace(label=label, text=text, parent=parent, self_ref="")


def test_walk_sections_interleaves_docling_tables_in_order():
    items = [
        _text("Evaluation Results {#evaluation-results}", label="section_header"),
        _text("Intro before table."),
        FakeTable(),
        _text("Qwen3-Embedding-0.6B", parent_ref="#/tables/0"),
        _text("After table prose."),
    ]
    doc = SimpleNamespace(iterate_items=lambda **_kwargs: ((item, 1) for item in items))

    sections, h1, h2 = _walk_sections(doc)

    assert h1 == 1
    assert h2 == 0
    assert [s.element_type for s in sections] == [
        "section_heading",
        "paragraph",
        "table",
        "paragraph",
    ]
    assert sections[0].text == "Evaluation Results"
    assert sections[2].heading_path == ["Evaluation Results"]
    assert sections[2].metadata["columns"] == ["Component", "Model"]
    assert sections[2].metadata["row_count"] == 2
    assert "Model=Qwen3-Reranker-0.6B" in sections[2].text
    assert sections[3].text == "After table prose."
