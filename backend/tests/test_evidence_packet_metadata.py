"""M1 metadata-into-model-input (2026-07-02): the answer-model packet must
carry Title/Section/Domain/Kind and NEVER leak internal doc_id/chunk_id."""

from services.chat_orchestrator import (
    _clean_source_label,
    _is_taxonomy_domain,
    _source_section_label,
    _source_title,
)


def test_source_title_never_leaks_internal_ids():
    # doc_name/title/filename/url all absent -> generic label, NOT the id.
    data = {"doc_id": "f8a0aa85-6cb4-...", "chunk_id": "abc123_0042"}
    label = _source_title(data)
    assert "f8a0aa85" not in label
    assert "abc123" not in label
    assert label == "Untitled source"


def test_source_title_cleans_provenance_tail():
    data = {"doc_name": "The Art of Seduction -- Robert Greene -- 2005 -- Anna’s Archive.md"}
    assert _source_title(data) == "The Art of Seduction"
    data2 = {"doc_name": "SQLite Internals{Abdur}(2022)libgen.li.pdf"}
    assert _source_title(data2).startswith("SQLite Internals")
    assert "libgen" not in _source_title(data2)


def test_taxonomy_domain_gate_rejects_cluster_placeholders():
    assert _is_taxonomy_domain("psychology") is True
    assert _is_taxonomy_domain("software_engineering") is True
    assert _is_taxonomy_domain("Cluster 3") is False
    assert _is_taxonomy_domain("Outliers") is False
    assert _is_taxonomy_domain("other") is False
    assert _is_taxonomy_domain(None) is False
    assert _is_taxonomy_domain("") is False


def test_section_label_takes_last_two_heading_segments():
    data = {"heading_path": ["The Art of Seduction", "Part 1", "Chapter 3", "The Charmer"]}
    assert _source_section_label(data) == "Chapter 3 › The Charmer"
    assert _source_section_label({"heading_path": []}) == ""
    assert _source_section_label({}) == ""
