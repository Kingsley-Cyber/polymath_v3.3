"""Pt 11.1 — verify the symbols_called backfill loop wires through
to BM25 via _searchable_text.

The full worker integration (run_ingest_job → embed → sparse → Qdrant
→ Neo4j) is too heavy to test end-to-end without live services. Instead
we test the contract the backfill depends on: when the augmenter
returns a non-empty `chunk_calls` map, and we apply the backfill loop
the worker does, the resulting `_searchable_text(chunk)` includes the
backfilled tokens.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.code_graph_augmenter import GraphifyEnrichment
from services.ingestion.worker import _searchable_text


def _chunk(chunk_id, text, language="luau", metadata=None):
    return SimpleNamespace(
        chunk_id=chunk_id,
        text=text,
        language=language,
        metadata=metadata or {},
    )


def _apply_backfill(chunks, enrichment):
    """Mirror of the backfill loop in run_ingest_job. Kept in sync
    manually — if the worker logic changes, update this test too.
    The contract under test: after this loop runs, _searchable_text
    sees the populated symbols_called."""
    for c in chunks:
        called = enrichment.chunk_calls.get(c.chunk_id, [])
        if not called:
            continue
        existing = list(c.metadata.get("symbols_called", []) or [])
        seen = {x.lower() for x in existing}
        for sym in called:
            if sym.lower() in seen or len(existing) >= 60:
                continue
            existing.append(sym)
            seen.add(sym.lower())
        c.metadata["symbols_called"] = existing


def test_backfilled_calls_appear_in_searchable_text():
    """The whole point of Pt 11.1 — once graphify's call edges are
    backfilled into metadata.symbols_called, BM25's input text via
    _searchable_text contains the called symbol names."""
    chunks = [
        _chunk(
            "doc1_0001",
            "function Combat.PunchAttack(player)\n  ts:Create(player)\nend",
            metadata={"symbols_defined": ["Combat.PunchAttack"]},
        ),
    ]
    enrichment = GraphifyEnrichment(
        entity_communities={},
        call_edges=[],
        community_labels={},
        chunk_calls={"doc1_0001": ["TweenService", "Instance.new"]},
        node_count=3,
        edge_count=2,
    )
    _apply_backfill(chunks, enrichment)
    augmented = _searchable_text(chunks[0])
    # symbols_called appears in the augmented BM25 input
    assert "TweenService" in augmented
    assert "Instance.new" in augmented
    # Original body text is still there
    assert "Combat.PunchAttack" in augmented


def test_backfill_dedupes_against_existing_symbols_called():
    """If symbols_called was already populated (e.g. by future tree-sitter
    pack v2), graphify's contributions are merged not overwritten."""
    chunks = [
        _chunk(
            "doc1_0001", "body",
            metadata={"symbols_called": ["pre_existing", "TweenService"]},
        ),
    ]
    enrichment = GraphifyEnrichment(
        entity_communities={},
        call_edges=[],
        community_labels={},
        chunk_calls={"doc1_0001": ["TweenService", "Instance.new", "pre_existing"]},
        node_count=1,
        edge_count=0,
    )
    _apply_backfill(chunks, enrichment)
    result = chunks[0].metadata["symbols_called"]
    # pre_existing is kept; TweenService is not duplicated; Instance.new is added
    assert result.count("pre_existing") == 1
    assert result.count("TweenService") == 1
    assert "Instance.new" in result


def test_backfill_dedupe_is_case_insensitive():
    """Graphify might emit 'tweenService' while metadata already had
    'TweenService' — same identity, don't double-list."""
    chunks = [_chunk("c1", "x", metadata={"symbols_called": ["TweenService"]})]
    enrichment = GraphifyEnrichment(
        entity_communities={}, call_edges=[], community_labels={},
        chunk_calls={"c1": ["tweenservice", "TWEENSERVICE"]},
        node_count=0, edge_count=0,
    )
    _apply_backfill(chunks, enrichment)
    assert chunks[0].metadata["symbols_called"] == ["TweenService"]


def test_backfill_caps_at_sixty():
    """Match _extract_metadata's list-size cap so we don't bloat the
    BM25 input on pathological inputs."""
    chunks = [_chunk("c1", "x", metadata={})]
    enrichment = GraphifyEnrichment(
        entity_communities={}, call_edges=[], community_labels={},
        chunk_calls={"c1": [f"sym_{i}" for i in range(200)]},
        node_count=0, edge_count=0,
    )
    _apply_backfill(chunks, enrichment)
    assert len(chunks[0].metadata["symbols_called"]) == 60


def test_backfill_skips_chunks_with_no_graphify_calls():
    """A chunk that graphify didn't surface any calls for keeps its
    existing symbols_called (possibly empty)."""
    chunks = [
        _chunk("doc_with_calls", "a", metadata={}),
        _chunk("doc_no_calls", "b", metadata={"symbols_called": ["existing"]}),
    ]
    enrichment = GraphifyEnrichment(
        entity_communities={}, call_edges=[], community_labels={},
        chunk_calls={"doc_with_calls": ["NewSymbol"]},
        node_count=1, edge_count=0,
    )
    _apply_backfill(chunks, enrichment)
    assert chunks[0].metadata["symbols_called"] == ["NewSymbol"]
    # doc_no_calls left untouched (existing symbols_called preserved as-is)
    assert chunks[1].metadata["symbols_called"] == ["existing"]


def test_empty_enrichment_is_no_op():
    """When graphify is disabled / fails, GraphifyEnrichment.empty() is
    returned. The backfill loop applied to empty enrichment must not
    mutate chunks."""
    chunks = [_chunk("c1", "x", metadata={"symbols_called": ["existing"]})]
    _apply_backfill(chunks, GraphifyEnrichment.empty())
    assert chunks[0].metadata["symbols_called"] == ["existing"]
