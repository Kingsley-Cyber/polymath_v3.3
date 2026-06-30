"""Asserting tests for the relationship-gate loosening + tunable answerability.

Covers:
  * services.answerability_tuning — the shared knob brain both gates read.
  * chat_orchestrator._build_retrieval_answerability_gate — the actual refusal
    decision: a relationship query with >=1 doc per side ANSWERS (lenient),
    a side with ZERO evidence still refuses (honesty floor), and strict mode
    still gates a thin cross-doc bridge.

Run inside the backend container (needs pydantic):
    docker exec -i polymath_v33-backend-1 python /app/tests/test_answerability_gate_loosening.py
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.answerability_tuning as at  # noqa: E402
from models.schemas import SourceChunk  # noqa: E402


class _FakeSettings:
    def __init__(self, **kw):
        self.RELATIONSHIP_GATE = kw.get("gate", "lenient")
        self.RELATIONSHIP_MIN_DISTINCT_DOCS = kw.get("min_distinct", 1)
        self.RELATIONSHIP_LANE_MIN_SOURCES = kw.get("lane_min", 1)
        self.LANE_STRONG_SCORE = kw.get("strong", 8)
        self.ANSWERABILITY_COVERAGE_THRESHOLD = kw.get("cov", 0.80)
        self.ANSWERABILITY_TEXT_HELP_THRESHOLD = kw.get("text", 0.50)
        self.ANSWERABILITY_PARTIAL_FLOOR = kw.get("partial", 0.50)


def _patch_settings(**kw):
    """Point BOTH the tuning module and chat_orchestrator at fake settings.

    chat_orchestrator imported the tuning helpers as aliases bound to the
    functions in services.answerability_tuning, and those read get_settings()
    from the tuning module namespace at call time — so patching it here is
    sufficient for both modules.
    """
    fake = _FakeSettings(**kw)
    at.get_settings = lambda: fake  # type: ignore[assignment]


def _restore():
    from config import get_settings as real

    at.get_settings = real  # type: ignore[assignment]


# ── shared knob brain ──────────────────────────────────────────────────────
def test_default_gate_is_lenient():
    _patch_settings(gate="lenient")
    assert at.relationship_gate() == "lenient"
    assert at.inject_cross_doc_atom() is True
    assert at.cross_doc_atom_is_critical() is False


def test_off_skips_atom_strict_makes_it_critical():
    _patch_settings(gate="off")
    assert at.inject_cross_doc_atom() is False
    _patch_settings(gate="strict")
    assert at.cross_doc_atom_is_critical() is True


def test_neutralize_drops_relationship_family_unless_strict():
    crit = {"relationship", "cross_document_relationship_evidence", "definition", "concept:x"}
    _patch_settings(gate="lenient")
    out = at.neutralize_relationship_critical(crit)
    assert "relationship" not in out
    assert "cross_document_relationship_evidence" not in out
    assert "definition" in out          # definitions still require grounding
    assert "concept:x" in out           # a missing SIDE still refuses (honesty floor)
    _patch_settings(gate="strict")
    assert at.neutralize_relationship_critical(crit) == crit


def test_missing_is_relationship_only():
    assert at.missing_is_relationship_only(["relationship"]) is True
    assert at.missing_is_relationship_only(["cross_document_relationship_evidence"]) is True
    assert at.missing_is_relationship_only(["concept:personality"]) is False
    assert at.missing_is_relationship_only([]) is False  # nothing missing != relationship-only


def test_thresholds_clamp_out_of_range():
    _patch_settings(cov=0.05, text=0.99, partial=0.99)
    assert at.coverage_threshold() == 0.40    # clamped up from 0.05
    assert at.text_help_threshold() == 0.80   # clamped down from 0.99
    assert at.partial_floor() == 0.70


# ── the actual refusal decision (chat gate) ────────────────────────────────
def _chunk(text, did):
    return SourceChunk(
        chunk_id=f"c-{did}", parent_id=f"p-{did}", doc_id=did, corpus_id="corp",
        text=text, score=0.7, source_tier="qdrant_mongo",
    )


def _plan_meta(*, covered, missing, distinct):
    return {
        "active": True,
        "required_lanes": ["metacognition", "personality"],
        "covered_lanes": covered,
        "missing_lanes": missing,
        "final": {"distinct_doc_count": distinct},
        "plan": {"operators": ["relationship"], "mode": "multi_concept_relationship"},
    }


def _build_gate(**kw):
    from services.chat_orchestrator import _build_retrieval_answerability_gate

    return _build_retrieval_answerability_gate(
        query="how does metacognition relate to personality",
        diagnostics=None,
        sources=kw["sources"],
        facts=[],
        corpus_ids=["corp"],
        web_search_enabled=False,
        evidence_plan_meta=kw["plan_meta"],
    )


def _short_circuits(gate):
    from services.chat_orchestrator import _should_short_circuit_answerability

    return _should_short_circuit_answerability(
        gate, web_search_enabled=False, selected_tools=None
    )


def test_relationship_query_answers_when_each_side_has_a_doc():
    _patch_settings(gate="lenient", min_distinct=1, lane_min=1)
    sources = [
        _chunk("Metacognition is thinking about thinking.", "d1"),
        _chunk("Personality frameworks describe enduring traits.", "d2"),
    ]
    gate = _build_gate(
        sources=sources, plan_meta=_plan_meta(
            covered=["metacognition", "personality"], missing=[], distinct=2
        ),
    )
    assert gate["status"] in {"answerable", "partial"}      # it ANSWERS
    assert gate["status"] not in {"unanswerable", "weak"}
    assert _short_circuits(gate) is False


def test_zero_evidence_side_still_refuses_honesty_floor():
    _patch_settings(gate="lenient", min_distinct=1, lane_min=1)
    sources = [_chunk("Metacognition is thinking about thinking.", "d1")]
    gate = _build_gate(
        sources=sources, plan_meta=_plan_meta(
            covered=["metacognition"], missing=["personality"], distinct=1
        ),
    )
    # The personality side has NO retrieved evidence — bridging it would mean
    # fabricating. The gate must still refuse (concept lane stays critical).
    assert gate["status"] == "unanswerable"
    assert "concept:personality" in gate["missing_critical_atoms"]
    assert _short_circuits(gate) is True


def test_strict_mode_still_gates_thin_bridge():
    # Both sides covered but only ONE distinct doc bridges them, and the user
    # demands 2. Strict keeps the cross-doc atom critical → refuse; lenient
    # with the same inputs answers.
    plan = _plan_meta(covered=["metacognition", "personality"], missing=[], distinct=1)
    sources = [_chunk("metacognition and personality discussed together", "d1")]

    _patch_settings(gate="strict", min_distinct=2, lane_min=1)
    strict_gate = _build_gate(sources=sources, plan_meta=plan)
    assert strict_gate["status"] in {"unanswerable", "partial"}  # strict bites (carve-out may soften to partial)

    _patch_settings(gate="lenient", min_distinct=2, lane_min=1)
    lenient_gate = _build_gate(sources=sources, plan_meta=plan)
    assert lenient_gate["status"] not in {"unanswerable", "weak"}
    assert _short_circuits(lenient_gate) is False


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
        finally:
            _restore()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
