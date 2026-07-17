"""Chat-side corpus-scope v2 invariants.

The strict retriever sufficiency result is input only. These tests prove the
versioned chat arbiter cannot upgrade generic lexical overlap into an answer,
while grounded positive and non-eligible lay-language paths remain unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import services.answerability_tuning as tuning
from models.schemas import SourceChunk
from services.chat_orchestrator import (
    _build_retrieval_answerability_gate,
    _should_short_circuit_answerability,
)


def _settings(*, enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        RELATIONSHIP_GATE="lenient",
        RELATIONSHIP_MIN_DISTINCT_DOCS=1,
        RELATIONSHIP_LANE_MIN_SOURCES=1,
        LANE_STRONG_SCORE=8,
        ANSWERABILITY_COVERAGE_THRESHOLD=0.80,
        ANSWERABILITY_TEXT_HELP_THRESHOLD=0.50,
        ANSWERABILITY_PARTIAL_FLOOR=0.50,
        ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED=enabled,
        ANSWERABILITY_CORPUS_SCOPE_V2_MIN_TERMS=2,
        ANSWERABILITY_CORPUS_SCOPE_V2_MIN_COVERAGE=0.60,
    )


def _source(text: str) -> SourceChunk:
    return SourceChunk(
        chunk_id="chunk-1",
        parent_id="parent-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        doc_name="nearby-but-not-answering.md",
        text=text,
        score=0.8,
        source_tier="tier_a",
    )


def _diagnostics(*, required: list[str], covered: list[str]) -> dict:
    return {
        "answer_shape": "synthesis",
        "selection": {
            "sufficiency": {
                "required_atoms": required,
                "covered_required_atoms": covered,
                "missing_atoms": [item for item in required if item not in covered],
                "missing_critical_atoms": [],
                "required_coverage": len(covered) / len(required),
                "answerable": False,
            }
        },
    }


def _gate(
    monkeypatch,
    *,
    enabled: bool,
    query: str,
    required: list[str],
    covered: list[str],
    text: str,
) -> dict:
    monkeypatch.setattr(tuning, "get_settings", lambda: _settings(enabled=enabled))
    return _build_retrieval_answerability_gate(
        query=query,
        diagnostics=_diagnostics(required=required, covered=covered),
        sources=[_source(text)],
        facts=[],
        corpus_ids=["corpus-1"],
        web_search_enabled=False,
    )


def test_scope_v2_refuses_generic_overlap_for_absent_genomics(monkeypatch) -> None:
    required = ["concept:crispr", "concept:guide", "concept:rna", "concept:sequence"]
    gate = _gate(
        monkeypatch,
        enabled=True,
        query="What CRISPR guide RNA sequence should be used to edit the CFTR gene?",
        required=required,
        covered=["concept:guide", "concept:sequence"],
        text="The production guide describes a shot sequence for visual effects.",
    )
    assert gate["status"] == "unanswerable"
    assert gate["answerable"] is False
    assert gate["corpus_scope_guard"]["applied"] is True
    assert gate["corpus_scope_guard"]["matched_terms"] == ["sequence"]
    assert _should_short_circuit_answerability(
        gate, web_search_enabled=False, selected_tools=[]
    )


def test_scope_v2_refuses_two_adjacent_out_of_scope_shapes(monkeypatch) -> None:
    quantum = _gate(
        monkeypatch,
        enabled=True,
        query=(
            "Which quantum error-correcting code in these documents gives the "
            "best logical qubit threshold?"
        ),
        required=[
            "concept:best",
            "concept:code",
            "concept:error-correcting",
            "concept:quantum",
        ],
        covered=["concept:best", "concept:code"],
        text=(
            "The best production code keeps camera metadata below a display "
            "threshold."
        ),
    )
    tax = _gate(
        monkeypatch,
        enabled=True,
        query="What was the statutory wool tax rate in fourteenth-century Burgundy?",
        required=[
            "concept:rate",
            "concept:statutory",
            "concept:tax",
            "concept:wool",
        ],
        covered=["concept:statutory", "concept:tax"],
        text="A statutory production tax can affect a vendor's rate.",
    )
    assert quantum["status"] == "unanswerable"
    assert tax["status"] == "unanswerable"
    assert (
        quantum["corpus_scope_guard"]["coverage"]
        < quantum["corpus_scope_guard"]["min_coverage"]
    )
    assert (
        tax["corpus_scope_guard"]["coverage"]
        < tax["corpus_scope_guard"]["min_coverage"]
    )


def test_scope_v2_preserves_grounded_direct_partial(monkeypatch) -> None:
    gate = _gate(
        monkeypatch,
        enabled=True,
        query="What factors make up Walter Murch's Rule of Six for deciding a cut?",
        required=[
            "concept:murch's",
            "concept:rule",
            "concept:up",
            "concept:walter",
        ],
        covered=["concept:murch's", "concept:walter"],
        text="Walter Murch explains the Rule of Six as a guide for deciding a cut.",
    )
    assert gate["status"] in {"answerable", "partial"}
    assert gate["corpus_scope_guard"]["supported"] is True
    assert gate["corpus_scope_guard"]["applied"] is False


def test_scope_v2_preserves_noneligible_lay_language_path(monkeypatch) -> None:
    gate = _gate(
        monkeypatch,
        enabled=True,
        query="Why can a movie cut feel natural instead of jolting the viewer?",
        required=["concept:cut", "concept:feel", "concept:movie", "concept:natural"],
        covered=["concept:cut", "concept:movie"],
        text="A movie cut can guide a viewer's attention.",
    )
    assert gate["status"] == "partial"
    assert gate["corpus_scope_guard"]["eligible"] is False
    assert gate["corpus_scope_guard"]["applied"] is False


def test_scope_v2_default_off_preserves_legacy_and_versions_trace(monkeypatch) -> None:
    gate = _gate(
        monkeypatch,
        enabled=False,
        query="What CRISPR guide RNA sequence should be used to edit the CFTR gene?",
        required=["concept:crispr", "concept:guide", "concept:rna", "concept:sequence"],
        covered=["concept:guide", "concept:sequence"],
        text="The production guide describes a shot sequence.",
    )
    assert gate["status"] == "partial"
    assert gate["answerability_policy_version"] == "baseline_live_v0"
    assert gate["corpus_scope_guard"]["enabled"] is False
    assert gate["corpus_scope_guard"]["applied"] is False
