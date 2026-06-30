"""Asserting tests for C3 (heading penalty) + B4 (answer-bearingness) re-rank
and B2 (query-guided parent excerpt). Pure functions, no I/O.

Run inside the backend container (needs pydantic):
    docker exec -i polymath_v33-backend-1 python /app/tests/test_metadata_rerank.py
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from models.schemas import RetrievalTier, SourceChunk  # noqa: E402
from services.retriever.hydrate import (  # noqa: E402
    _assemble_hydrated_text,
    _query_guided_excerpt,
)
from services.retriever.ranking_policy import (  # noqa: E402
    _answer_bearingness,
    _apply_metadata_signals,
    _heading_section_penalty,
    apply_query_grounding,
)


def _chunk(text, *, score=0.5, heading_path=None, chunk_kind="body", cid="c", did="d", pid="p"):
    return SourceChunk(
        chunk_id=cid,
        parent_id=pid,
        doc_id=did,
        corpus_id="corp",
        text=text,
        score=score,
        source_tier="qdrant_mongo",
        chunk_kind=chunk_kind,
        heading_path=heading_path,
    )


# ── C3: heading-section penalty ───────────────────────────────────────────
def test_core_content_has_no_penalty():
    c = _chunk("body text", heading_path=["Chapter 2", "The Core Argument"])
    assert _heading_section_penalty(c) == 0.0


def test_footnote_heading_is_penalised():
    c = _chunk("aside", heading_path=["Chapter 2", "Footnotes"])
    assert _heading_section_penalty(c) == 0.08


def test_peripheral_chunk_kind_is_penalised():
    c = _chunk("a figure label", chunk_kind="caption", heading_path=["Body"])
    assert _heading_section_penalty(c) == 0.04


def test_heading_and_kind_penalties_stack():
    c = _chunk("x", heading_path=["Appendix A"], chunk_kind="caption")
    assert abs(_heading_section_penalty(c) - 0.12) < 1e-9


def test_empty_heading_path_is_safe():
    assert _heading_section_penalty(_chunk("x", heading_path=None)) == 0.0


# ── B4: answer-bearingness ─────────────────────────────────────────────────
def test_distinct_and_density_counted():
    distinct, density = _answer_bearingness(
        "metacognition shapes metacognition and personality", ("metacognition", "personality")
    )
    assert distinct == 2          # both terms present
    assert density == 3           # metacognition x2 + personality x1


def test_punctuation_tolerant_word_boundary():
    distinct, _ = _answer_bearingness("the eggs.", ("eggs",))
    assert distinct == 1          # trailing period must not block the match


def test_substring_does_not_match():
    distinct, _ = _answer_bearingness("the air is cold", ("ai",))
    assert distinct == 0          # 'ai' must NOT match inside 'air'


def test_no_terms_is_zero():
    assert _answer_bearingness("anything", ()) == (0, 0)
    assert _answer_bearingness("", ("x",)) == (0, 0)


# ── B4/C3 score folding ────────────────────────────────────────────────────
def test_bounded_penalty_demotes_and_clamps():
    c = _chunk("unrelated", heading_path=["Footnotes"])
    adj, sig = _apply_metadata_signals(0.05, c, ("zzz",), bounded=True)
    assert adj < 0.05             # heading penalty demotes
    assert adj >= 0.0             # clamped, never negative
    assert sig["heading_penalty"] == 0.08


def test_bounded_bonus_lifts_for_coverage():
    c = _chunk("metacognition and personality together")
    adj, _ = _apply_metadata_signals(0.5, c, ("metacognition", "personality"), bounded=True)
    assert adj > 0.5              # full coverage lifts the score
    assert adj <= 1.0


def test_unbounded_penalty_is_sign_safe():
    # An unbounded grounded score can be negative; a penalty must make it MORE
    # negative (demote), never flip its sign upward.
    c = _chunk("unrelated", heading_path=["Bibliography"])
    adj, _ = _apply_metadata_signals(-1.25, c, ("zzz",), bounded=False)
    assert adj < -1.25


# ── apply_query_grounding end-to-end: peripheral loses the tie ─────────────
def test_core_chunk_outranks_footnote_on_equal_coverage():
    query = "what is metacognition"
    body = _chunk(
        "Metacognition is thinking about thinking.",
        score=0.6, heading_path=["Chapter 1"], cid="body", pid="pb",
    )
    note = _chunk(
        "Metacognition footnote reference only.",
        score=0.6, heading_path=["Footnotes"], cid="note", pid="pn",
    )
    ranked = apply_query_grounding(
        [note, body], query=query, tier=RetrievalTier.qdrant_mongo, score_scale="probability"
    )
    # Same concept coverage, but the footnote is penalised → body ranks first.
    assert ranked[0].chunk_id == "body"
    body_out = next(c for c in ranked if c.chunk_id == "body")
    note_out = next(c for c in ranked if c.chunk_id == "note")
    assert body_out.score > note_out.score


# ── B2: query-guided parent excerpt ────────────────────────────────────────
def _long_parent():
    return "\n\n".join(
        [
            "Intro paragraph about nothing in particular and filler words here.",   # 0
            "The seduction strategy relies on mystery and timing to draw a target.",  # 1 (child)
            "An unrelated digression about weather patterns and clouds and rain.",   # 2
            "Personality frameworks like the Big Five describe enduring traits.",    # 3 (bearing)
            "More filler that does not touch the query at all, padding padding.",     # 4
        ]
        + ["Padding sentence number %d to blow past the char budget cap." % i for i in range(40)]
    )


def test_short_parent_returned_whole():
    p = "one small paragraph"
    assert _query_guided_excerpt(p, child_text="x", query="q", max_chars=1600) == p


def test_excerpt_keeps_child_and_respects_budget():
    parent = _long_parent()
    child = "The seduction strategy relies on mystery and timing to draw a target."
    out = _query_guided_excerpt(
        parent, child_text=child, query="seduction strategy and personality frameworks", max_chars=400
    )
    assert "seduction strategy" in out.lower()    # the matched child survived
    assert len(out) <= 400 + 80                   # roughly within budget (+ markers)
    assert len(out) < len(parent)                 # actually trimmed
    assert "personality frameworks" in out.lower()  # answer-bearing paragraph pulled in


def test_excerpt_marks_elision():
    # The answer-bearing paragraph sits FAR from the child block, so the kept
    # blocks are non-contiguous → an elision marker must separate them.
    paras = (
        [
            "Intro filler one two three four five six seven.",                 # 0
            "CHILDMARKER the matched child passage sits right here now okay.",  # 1 (child)
            "More filler about clouds and weather and nothing useful here.",    # 2
        ]
        + ["Pure padding paragraph %d with no query terms inside it." % i for i in range(20)]
        + ["Personality frameworks describe enduring human traits clearly."]    # far bearing
    )
    parent = "\n\n".join(paras)
    child = "CHILDMARKER the matched child passage sits right here now okay."
    out = _query_guided_excerpt(
        parent, child_text=child, query="personality frameworks", max_chars=300
    )
    assert "CHILDMARKER" in out                   # child block kept
    assert "personality frameworks" in out.lower()  # far bearing paragraph pulled in
    assert "[…]" in out                           # non-contiguous blocks are marked


def test_assemble_parent_excerpt_gated_by_flag():
    parent = _long_parent()
    child = "The seduction strategy relies on mystery and timing to draw a target."
    # Flag off → full parent body unchanged.
    full = _assemble_hydrated_text(
        "parent", child_text=child, parent_text=parent, summary="",
        query="seduction", excerpt_enabled=False, excerpt_max_chars=300,
    )
    assert full == parent
    # Flag on → trimmed.
    trimmed = _assemble_hydrated_text(
        "parent", child_text=child, parent_text=parent, summary="",
        query="seduction strategy", excerpt_enabled=True, excerpt_max_chars=300,
    )
    assert len(trimmed) < len(parent)
    assert "seduction strategy" in trimmed


def test_child_summary_mode_ignores_excerpt():
    # child_summary must be unaffected by B2 plumbing.
    out = _assemble_hydrated_text(
        "child_summary", child_text="precise child", parent_text="big parent body",
        summary="the section summary", query="anything",
        excerpt_enabled=True, excerpt_max_chars=10,
    )
    assert out == "precise child\n\n[Section context: the section summary]"


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
