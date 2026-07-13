"""P0.5 facet decontamination tests (offline, fakes only).

Covers the shared invariant, not one corpus's wording:
  - normalizer no longer lets corpus-lens-derived facet ids attach to a
    document or chunk row without that row's own content evidence;
  - evidenced lens facets still flow end to end;
  - non-lens facets (filename/heading/document-content) inherit unchanged;
  - classify_facets edge cases (missing fields, empty lists, both evidence
    channels).
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from services.facets.normalizer import (
    build_ingest_facet_profile,
    heading_local_facet_ids,
    schema_lens_facet_ids,
)

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "p0_5_facet_decontamination.py"
)
_spec = importlib.util.spec_from_file_location(
    "p0_5_facet_decontamination", _SCRIPT_PATH
)
decon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decon)
classify_facets = decon.classify_facets


def _parent(parent_id="p1", text="", heading_path=None):
    return SimpleNamespace(
        parent_id=parent_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
        heading_path=heading_path or [],
        source_tier="tier_a",
        children=[],
        metadata={},
    )


def _child(chunk_id="c1", parent_id="p1", text="", heading_path=None):
    return SimpleNamespace(
        chunk_id=chunk_id,
        parent_id=parent_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
        heading_path=heading_path or [],
        source_tier="tier_b",
        token_count=12,
        metadata={},
    )


# ---------------------------------------------------------------------------
# Normalizer: lens facets need per-row content evidence
# ---------------------------------------------------------------------------


def test_unevidenced_lens_facets_do_not_attach_to_document_or_rows():
    schema_lens = {
        "corpus_domains": ["emotional_patterns"],
        "canonical_families": ["agentic_ai"],
        "object_kinds": ["App"],
    }
    lens_before = {k: list(v) for k, v in schema_lens.items()}
    parents = [
        _parent(
            text=(
                "Creating tables in SQLite requires a schema declaration and "
                "primary key selection before inserting rows."
            ),
            heading_path=["Working With Tables"],
        )
    ]
    children = [
        _child(
            text="The INSERT statement adds rows into an SQLite table.",
            heading_path=["Insert Statements"],
        )
    ]
    profile = build_ingest_facet_profile(
        filename="learning_sqlite_for_ios.md",
        doc_id="doc-1",
        corpus_id="corpus-1",
        schema_lens=schema_lens,
        parents=parents,
        children=children,
    )

    for lens_fid in ("emotional_patterns", "agentic_ai", "app"):
        assert lens_fid not in profile["facet_ids"]
        assert lens_fid not in profile["parent_facets"]["p1"]["facet_ids"]
        assert lens_fid not in profile["child_facets"]["c1"]["facet_ids"]

    # Non-lens flow is intact: filename + own headings still attach
    # (stopwords like "for"/"with" are dropped by the id normalizer).
    assert "learning_sqlite_ios" in profile["facet_ids"]
    assert "working_tables" in profile["parent_facets"]["p1"]["facet_ids"]
    # The corpus-level lens record itself is untouched.
    assert schema_lens == lens_before


def test_evidenced_lens_facet_flows_to_document_and_evidenced_rows_only():
    schema_lens = {"canonical_families": ["retrieval_augmented_generation"]}
    evidenced = _parent(
        parent_id="p-rag",
        text=(
            "Retrieval augmented generation combines a retriever with a "
            "generator. Retrieval augmented generation systems ground every "
            "answer in retrieved sources."
        ),
        heading_path=["Grounded Answering"],
    )
    unrelated = _parent(
        parent_id="p-falcon",
        text=(
            "Medieval falconry manuals describe training hawks with lures, "
            "jesses, and daily weight checks."
        ),
        heading_path=["Falconry Training"],
    )
    profile = build_ingest_facet_profile(
        filename="mixed_notes.md",
        doc_id="doc-1",
        corpus_id="corpus-1",
        schema_lens=schema_lens,
        parents=[evidenced, unrelated],
        children=[],
    )

    # Document keeps the lens facet: its content shows evidence.
    assert "retrieval_augmented_generation" in profile["facet_ids"]
    # The evidenced row carries it, attributed by its own content facets.
    rag_meta = profile["parent_facets"]["p-rag"]
    assert "retrieval_augmented_generation" in rag_meta["facet_ids"]
    assert "retrieval_augmented_generation" in rag_meta["content_facet_ids"]
    # The unrelated row in the same document does NOT inherit it.
    assert (
        "retrieval_augmented_generation"
        not in profile["parent_facets"]["p-falcon"]["facet_ids"]
    )


def test_heading_evidence_admits_lens_facet_for_that_row_only():
    schema_lens = {"canonical_families": ["business_strategy"]}
    strategy_parent = _parent(
        parent_id="p-strat",
        text="Notes about planning the quarter and allocating budget.",
        heading_path=["Business Strategy"],
    )
    other_parent = _parent(
        parent_id="p-other",
        text="A recipe collection for sourdough starters and baking times.",
        heading_path=["Sourdough Basics"],
    )
    profile = build_ingest_facet_profile(
        filename="planning_notes.md",
        doc_id="doc-1",
        corpus_id="corpus-1",
        schema_lens=schema_lens,
        parents=[strategy_parent, other_parent],
        children=[],
    )

    assert "business_strategy" in profile["facet_ids"]
    assert "business_strategy" in profile["parent_facets"]["p-strat"]["facet_ids"]
    assert (
        "business_strategy" not in profile["parent_facets"]["p-other"]["facet_ids"]
    )


def test_non_lens_doc_facets_still_inherit_without_row_evidence():
    # No schema lens at all: the pre-existing inheritance contract holds and
    # rows still inherit document facets (filename) without needing evidence.
    parents = [
        _parent(
            text="Plain prose without any special vocabulary.",
            heading_path=["Chapter One"],
        )
    ]
    profile = build_ingest_facet_profile(
        filename="field_manual_for_gardeners.md",
        doc_id="doc-1",
        corpus_id="corpus-1",
        parents=parents,
        children=[],
    )
    assert "field_manual_gardeners" in profile["facet_ids"]
    assert (
        "field_manual_gardeners"
        in profile["parent_facets"]["p1"]["facet_ids"]
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_schema_lens_facet_ids_uses_production_mapping():
    fids = schema_lens_facet_ids(
        {
            "corpus_domains": ["agentic_ai"],
            "canonical_families": ["retrieval_augmented_generation"],
            "object_kinds": ["Report"],
        }
    )
    assert "agentic_ai" in fids
    assert "retrieval_augmented_generation" in fids
    # Generic-suffix object kinds produce no facet id at all.
    assert "report" not in fids
    assert schema_lens_facet_ids(None) == []
    assert schema_lens_facet_ids({}) == []


def test_heading_local_facet_ids_mirror_chunk_local_ids():
    assert heading_local_facet_ids(["Business Strategy", "Quarter Plan", "Third"]) == [
        "business_strategy",
        "quarter_plan",
    ]
    assert heading_local_facet_ids(None) == []
    assert heading_local_facet_ids([]) == []


# ---------------------------------------------------------------------------
# classify_facets edge cases
# ---------------------------------------------------------------------------


def test_classify_facets_missing_fields_and_empty_lists():
    assert classify_facets({}) == (set(), set())
    assert classify_facets({}, {"agentic_ai"}) == (set(), set())
    assert classify_facets(
        {"facet_ids": [], "content_facet_ids": [], "heading_path": []},
        {"agentic_ai"},
    ) == (set(), set())
    assert classify_facets({"facet_ids": None, "content_facet_ids": None}) == (
        set(),
        set(),
    )


def test_classify_facets_partitions_lens_ids_without_evidence():
    row = {
        "facet_ids": ["learning_sqlite_for_ios", "agentic_ai", "working_with_tables"],
        "content_facet_ids": [],
        "heading_path": ["Working With Tables"],
    }
    kept, removed = classify_facets(row, {"agentic_ai"})
    assert removed == {"agentic_ai"}
    assert kept == {"learning_sqlite_for_ios", "working_with_tables"}


def test_classify_facets_keeps_lens_id_evidenced_by_content_facets():
    row = {
        "facet_ids": ["agentic_ai"],
        "content_facet_ids": ["agentic_ai"],
        "heading_path": [],
    }
    kept, removed = classify_facets(row, {"agentic_ai"})
    assert removed == set()
    assert kept == {"agentic_ai"}


def test_classify_facets_keeps_lens_id_evidenced_by_heading():
    row = {
        "facet_ids": ["agentic_ai"],
        "content_facet_ids": [],
        "heading_path": ["Agentic AI"],
    }
    kept, removed = classify_facets(row, {"agentic_ai"})
    assert removed == set()
    assert kept == {"agentic_ai"}


def test_classify_facets_without_lens_ids_removes_nothing():
    row = {"facet_ids": ["anything_at_all"], "content_facet_ids": []}
    kept, removed = classify_facets(row, set())
    assert kept == {"anything_at_all"}
    assert removed == set()
    # Single-argument spec signature: lens ids default from the row (absent
    # here), so nothing is classified lens-inherited.
    kept, removed = classify_facets(row)
    assert kept == {"anything_at_all"}
    assert removed == set()


def test_classify_facets_reads_row_embedded_lens_ids_single_arg():
    row = {
        "facet_ids": ["agentic_ai", "own_heading"],
        "content_facet_ids": [],
        "heading_path": ["Own Heading"],
        "schema_lens_facet_ids": ["agentic_ai"],
    }
    kept, removed = classify_facets(row)
    assert removed == {"agentic_ai"}
    assert kept == {"own_heading"}
