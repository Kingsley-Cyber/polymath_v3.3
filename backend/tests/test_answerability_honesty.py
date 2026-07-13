"""P0.4 — answerability is honest: lane coverage is telemetry, refusal is a
decision grounded in evidence, internal plumbing never leaks to users.

Covers:
  - the synthetic fallback probe (query_plan.FALLBACK_PROBE_ID) is never a
    refusal-critical atom and never appears in user-facing refusal text;
  - the grounded-lane filter enforces its documented >=2-required-sides
    activation guard (single broad lane fails open);
  - coverage thresholds calibrate by answer shape, not one universal value;
  - the chat gate surfaces lane coverage separately from answerability and
    names the nearest retrieved material in a refusal.
"""

from models.schemas import SourceChunk
from services.answerability_tuning import coverage_threshold
from services.chat_orchestrator import (
    _build_retrieval_answerability_gate,
    _format_answerability_short_circuit_response,
    _friendly_missing_atom,
)
from services.retriever.planned_fusion import filter_grounded_planned_candidates
from services.retriever.query_plan import FALLBACK_PROBE_ID


def _chunk(chunk_id: str, *, score: float, doc_name: str, lanes: dict | None = None) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"p-{chunk_id}",
        doc_id=f"doc-{chunk_id}",
        corpus_id="alpha",
        doc_name=doc_name,
        text=f"evidence text {chunk_id}",
        score=score,
        source_tier="tier_a",
        metadata=(
            {"planned_lane_grounding": lanes, "planned_lanes": list(lanes)}
            if lanes
            else {}
        ),
    )


# ── shape-calibrated thresholds ──────────────────────────────────────────────


def test_coverage_threshold_calibrates_by_answer_shape():
    base = coverage_threshold()
    assert base == coverage_threshold("single_fact")
    assert coverage_threshold("broad_synthesis") < base
    assert coverage_threshold("enumeration") < base
    assert coverage_threshold("comparison") < base
    assert coverage_threshold("broad_synthesis") >= 0.40


# ── grounded-lane filter activation guard ────────────────────────────────────


def test_grounded_filter_fails_open_for_single_required_lane():
    chunks = [
        _chunk("a1", score=0.9, doc_name="book-a.md", lanes={FALLBACK_PROBE_ID: 0.9}),
        _chunk("a2", score=0.2, doc_name="book-b.md"),
        _chunk("a3", score=0.1, doc_name="book-c.md"),
    ]
    filtered, diagnostics = filter_grounded_planned_candidates(
        chunks, [FALLBACK_PROBE_ID]
    )
    assert filtered == chunks
    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "synthetic_fallback_lane_fail_open"
    assert diagnostics["synthetic_lanes_excluded"] == [FALLBACK_PROBE_ID]


# ── plumbing never leaks to users ────────────────────────────────────────────


def test_friendly_missing_atom_masks_fallback_probe():
    assert (
        _friendly_missing_atom(f"concept:{FALLBACK_PROBE_ID}")
        == "the main subject of the question"
    )
    assert _friendly_missing_atom("concept:focus_protocol") == "focus protocol"


def test_refusal_text_names_nearest_material_not_plumbing():
    gate = {
        "missing_atoms": [f"concept:{FALLBACK_PROBE_ID}"],
        "source_count": 2,
    }
    sources = [
        _chunk("s1", score=0.4, doc_name="deep-work-notes.md"),
        _chunk("s2", score=0.3, doc_name="rendering-handbook.md"),
    ]
    text = _format_answerability_short_circuit_response(
        gate, query="broad question", sources=sources
    )
    assert "primary" not in text
    assert "deep-work-notes.md" in text
    assert "rendering-handbook.md" in text


# ── gate: evidence-based answerability for undecomposed queries ──────────────


def _diagnostics(sufficiency: dict, lane_coverage: dict | None = None) -> dict:
    selection: dict = {"sufficiency": sufficiency}
    if lane_coverage is not None:
        selection["lane_coverage"] = lane_coverage
    return {"selection": selection, "answer_shape": "broad_synthesis"}


def test_gate_answers_undecomposed_query_from_evidence_sufficiency():
    sufficiency = {
        "required_atoms": ["concept:focus", "concept:attention"],
        "covered_required_atoms": ["concept:focus", "concept:attention"],
        "missing_atoms": [],
        "missing_critical_atoms": [],
        "required_coverage": 1.0,
        "answerable": True,
        "source": "evidence_atom_sufficiency",
    }
    lane_coverage = {
        "required_lane_ids": [FALLBACK_PROBE_ID],
        "supported_lane_ids": [],
        "coverage": 0.0,
        "synthetic_lane_ids": [FALLBACK_PROBE_ID],
    }
    gate = _build_retrieval_answerability_gate(
        query="What practical advice does this material give about improving focus?",
        diagnostics=_diagnostics(sufficiency, lane_coverage),
        sources=[_chunk("s1", score=0.6, doc_name="a.md")],
        facts=[],
        corpus_ids=["c1", "c2"],
        web_search_enabled=False,
        evidence_plan_meta=None,
    )
    assert gate["status"] == "answerable"
    # lane coverage rides separately, un-conflated with the decision
    assert gate["lane_coverage"] == lane_coverage
    assert gate["answer_shape"] == "broad_synthesis"
    assert gate["coverage_threshold"] < coverage_threshold("single_fact")


def test_gate_still_refuses_when_evidence_atoms_missing():
    sufficiency = {
        "required_atoms": ["concept:boiling_point", "concept:tungsten"],
        "covered_required_atoms": [],
        "missing_atoms": ["concept:boiling_point", "concept:tungsten"],
        "missing_critical_atoms": ["concept:boiling_point"],
        "required_coverage": 0.0,
        "answerable": False,
        "source": "evidence_atom_sufficiency",
    }
    gate = _build_retrieval_answerability_gate(
        query="What is the boiling point of tungsten in kelvin?",
        diagnostics=_diagnostics(sufficiency),
        sources=[_chunk("s1", score=0.2, doc_name="rendering.md")],
        facts=[],
        corpus_ids=["c1"],
        web_search_enabled=False,
        evidence_plan_meta=None,
    )
    assert gate["status"] == "unanswerable"
    assert gate["answerable"] is False
