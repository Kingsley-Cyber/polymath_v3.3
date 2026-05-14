"""Phase 4 code graph — deterministic ExtractionResult synthesis from chunk
metadata. The helper turns AST-derived `metadata.symbols_defined` and
`metadata.imports` into Entity + MENTIONS rows for Neo4j via the existing
`write_document_graph` pipeline. No LLM call. No hallucination."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ingestion.section_classifier import ChunkKind
from services.ingestion.worker import _synthesize_code_extraction_results


def _chunk(
    *,
    chunk_id="c1", parent_id="p1", doc_id="d1", corpus_id="cor1",
    text="...", chunk_kind=ChunkKind.CODE, metadata=None,
):
    return SimpleNamespace(
        chunk_id=chunk_id, parent_id=parent_id, doc_id=doc_id,
        corpus_id=corpus_id, text=text, chunk_kind=chunk_kind,
        metadata=metadata or {},
    )


# ─── Happy path ─────────────────────────────────────────────────────────────

def test_synthesizes_one_extraction_per_code_chunk():
    children = [
        _chunk(chunk_id="c1", metadata={"symbols_defined": ["foo"], "imports": []}),
        _chunk(chunk_id="c2", metadata={"symbols_defined": ["bar"], "imports": ["numpy"]}),
    ]
    out = _synthesize_code_extraction_results(children)
    assert len(out) == 2
    assert {r.chunk_id for r in out} == {"c1", "c2"}
    for r in out:
        assert r.schema_version == "polymath.code.v1"
        assert r.entities
        assert r.relations == []
        assert r.facts == []


def test_symbols_defined_become_method_entities():
    c = _chunk(metadata={
        "symbols_defined": ["Combat.PunchAttack", "Combat.Hitbox", "VectorStore"],
    })
    out = _synthesize_code_extraction_results([c])
    assert len(out) == 1
    names_types = {(e.canonical_name, e.entity_type) for e in out[0].entities}
    assert ("Combat.PunchAttack", "Method") in names_types
    assert ("Combat.Hitbox", "Method") in names_types
    assert ("VectorStore", "Method") in names_types


def test_imports_become_artifact_entities():
    c = _chunk(metadata={
        "symbols_defined": [],
        "imports": ["import numpy as np", "from heapq import heappush"],
    })
    out = _synthesize_code_extraction_results([c])
    assert len(out) == 1
    types = {e.entity_type for e in out[0].entities}
    assert types == {"Artifact"}


def test_all_entities_have_confidence_one():
    c = _chunk(metadata={
        "symbols_defined": ["fn1", "fn2"],
        "imports": ["lib_a"],
    })
    out = _synthesize_code_extraction_results([c])
    for e in out[0].entities:
        assert e.confidence == 1.0


# ─── Filtering ──────────────────────────────────────────────────────────────

def test_skips_non_code_chunks():
    children = [
        _chunk(chunk_id="prose", chunk_kind=ChunkKind.BODY,
               metadata={"symbols_defined": ["should_be_ignored"]}),
        _chunk(chunk_id="biblio", chunk_kind=ChunkKind.BIBLIOGRAPHY,
               metadata={"symbols_defined": ["ignored_too"]}),
    ]
    out = _synthesize_code_extraction_results(children)
    assert out == []


def test_skips_code_chunks_with_no_metadata():
    children = [_chunk(metadata={})]
    out = _synthesize_code_extraction_results(children)
    assert out == []


def test_skips_code_chunks_with_only_empty_lists():
    children = [_chunk(metadata={"symbols_defined": [], "imports": []})]
    out = _synthesize_code_extraction_results(children)
    assert out == []


# ─── Deduplication ──────────────────────────────────────────────────────────

def test_dedupes_case_insensitively_within_a_chunk():
    c = _chunk(metadata={
        "symbols_defined": ["TweenService", "tweenservice", "TWEENSERVICE"],
    })
    out = _synthesize_code_extraction_results([c])
    assert len(out[0].entities) == 1
    assert out[0].entities[0].canonical_name == "TweenService"


def test_dedupes_across_symbols_and_imports():
    # If a symbol name happens to match an import label (uncommon but possible
    # in dynamic langs), the symbol wins because we iterate symbols_defined first.
    c = _chunk(metadata={
        "symbols_defined": ["foo"],
        "imports": ["foo"],
    })
    out = _synthesize_code_extraction_results([c])
    assert len(out[0].entities) == 1
    assert out[0].entities[0].entity_type == "Method"


def test_strips_whitespace_and_filters_blanks():
    c = _chunk(metadata={
        "symbols_defined": ["  foo  ", "", "   ", "bar"],
    })
    out = _synthesize_code_extraction_results([c])
    names = {e.canonical_name for e in out[0].entities}
    assert names == {"foo", "bar"}


# ─── Schema preservation ────────────────────────────────────────────────────

def test_extraction_result_carries_chunk_text():
    c = _chunk(text="function foo() return 1 end", metadata={
        "symbols_defined": ["foo"],
    })
    out = _synthesize_code_extraction_results([c])
    assert out[0].text == "function foo() return 1 end"


def test_extraction_result_propagates_ids():
    c = _chunk(
        chunk_id="custom_chunk", doc_id="custom_doc", corpus_id="custom_corpus",
        metadata={"symbols_defined": ["foo"]},
    )
    out = _synthesize_code_extraction_results([c])
    assert out[0].chunk_id == "custom_chunk"
    assert out[0].doc_id == "custom_doc"
    assert out[0].corpus_id == "custom_corpus"


# ─── Real-world fixture ─────────────────────────────────────────────────────

def test_jailbreak_style_luau_chunk_produces_entities():
    """Mirror of what the chunker emits for a real .luau file from a Roblox
    repo: dotted method names + nothing in `imports` (the pack's grammar
    classifies require() as a call, not an import)."""
    c = _chunk(
        chunk_id="luau_1",
        text=(
            "local TweenService = game:GetService('TweenService')\n"
            "function Combat.PunchAttack(player)\n"
            "    local effect = Instance.new('Part')\n"
            "    return true\n"
            "end\n"
        ),
        metadata={
            "symbols_defined": ["Combat.PunchAttack"],
            "symbols_called": ["Instance.new", "game:GetService"],
            "imports": [],
            "ast_signature": "function Combat.PunchAttack(player)",
            "file_path": "ReplicatedStorage/Combat/CombatModule.luau",
        },
    )
    out = _synthesize_code_extraction_results([c])
    assert len(out) == 1
    assert any(e.canonical_name == "Combat.PunchAttack" for e in out[0].entities)
