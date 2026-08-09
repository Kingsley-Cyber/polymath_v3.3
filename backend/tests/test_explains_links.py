"""Deterministic EXPLAINS adjacency rows emitted by tier_chunker.chunk().

Mixed-content book lane (Slice 1): a prose block immediately before or
after a CODE block under the same heading forms one EXPLAINS pair. The
rows ride the code parent's metadata.explains_links so the worker's Mongo
checkpoint carries them to the graph writer unchanged.
"""

from types import SimpleNamespace

from models.schemas import SourceTier
from services.ingestion import tier_chunker
from services.ingestion.section_classifier import ChunkKind


def _section(text, *, element_type="paragraph", heading_path=None, language=None, level=None, metadata=None):
    return SimpleNamespace(
        heading_path=heading_path or [],
        text=text,
        element_type=element_type,
        level=level,
        language=language,
        metadata=metadata or {},
    )


def _tier_a(sections, heading="Sec"):
    return SimpleNamespace(
        source_tier=SourceTier.tier_a,
        text="",
        markdown="",
        sections=sections,
        pages=None,
        injected_headers_audit=[],
        language=None,
        filename="book.md",
    )


def _code_fence(symbol="f"):
    return f"```python\ndef {symbol}():\n    return 1\n```"


def _chunk(sections, doc_id="docX"):
    parents, children, _ = tier_chunker.chunk(
        _tier_a(sections), doc_id=doc_id, corpus_id="corpusX"
    )
    return parents, children


def _explains_rows(parents):
    rows = []
    for parent in parents:
        rows.extend((parent.metadata or {}).get("explains_links") or [])
    return rows


def test_explains_pairs_prose_before_code():
    sections = [
        _section("Sec", element_type="section_heading", heading_path=["Sec"], level=1),
        _section("Prose that explains the example below. " * 12, heading_path=["Sec"]),
        _section(_code_fence("before"), element_type="code_block", heading_path=["Sec"], language="python"),
    ]
    parents, children = _chunk(sections, doc_id="docBefore")
    rows = _explains_rows(parents)
    assert len(rows) == 1
    row = rows[0]
    assert row["relation"] == "EXPLAINS"
    assert row["basis"] == "adjacency"

    body_ids = {
        c.chunk_id for p in parents if p.chunk_kind == ChunkKind.BODY for c in p.children
    }
    code_ids = {
        c.chunk_id for p in parents if p.chunk_kind == ChunkKind.CODE for c in p.children
    }
    assert row["explains_chunk_id"] in body_ids
    assert row["code_chunk_id"] in code_ids
    # The row rides the code parent's metadata.
    code_parents = [p for p in parents if p.chunk_kind == ChunkKind.CODE]
    assert code_parents[0].metadata["explains_links"] == rows


def test_explains_pairs_prose_after_code():
    sections = [
        _section("Sec", element_type="section_heading", heading_path=["Sec"], level=1),
        _section(_code_fence("after"), element_type="code_block", heading_path=["Sec"], language="python"),
        _section("Prose that explains the example above. " * 12, heading_path=["Sec"]),
    ]
    parents, _children = _chunk(sections, doc_id="docAfter")
    rows = _explains_rows(parents)
    assert len(rows) == 1
    assert rows[0]["relation"] == "EXPLAINS"


def test_explains_no_pair_for_adjacent_code_blocks():
    sections = [
        _section("Sec", element_type="section_heading", heading_path=["Sec"], level=1),
        _section(_code_fence("first"), element_type="code_block", heading_path=["Sec"], language="python"),
        _section(_code_fence("second"), element_type="code_block", heading_path=["Sec"], language="python"),
    ]
    parents, _children = _chunk(sections, doc_id="docCodeCode")
    assert _explains_rows(parents) == []


def test_explains_scoped_per_heading():
    # Prose under heading A immediately followed (document-order) by code
    # under heading B — different sections, no EXPLAINS pair.
    sections = [
        _section("A", element_type="section_heading", heading_path=["A"], level=1),
        _section("Prose under section A. " * 12, heading_path=["A"]),
        _section("B", element_type="section_heading", heading_path=["B"], level=1),
        _section(_code_fence("other"), element_type="code_block", heading_path=["B"], language="python"),
    ]
    parents, _children = _chunk(sections, doc_id="docScoped")
    assert _explains_rows(parents) == []


def test_explains_empty_for_prose_only_doc():
    sections = [
        _section("Sec", element_type="section_heading", heading_path=["Sec"], level=1),
        _section("Only prose here, no code at all. " * 12, heading_path=["Sec"]),
        _section("More prose follows. " * 12, heading_path=["Sec"]),
    ]
    parents, _children = _chunk(sections, doc_id="docProseOnly")
    assert _explains_rows(parents) == []
