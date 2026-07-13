"""Audit Delta 2 (P1.2/P1.5) — strong vocabulary matches reach planning
regardless of applicability-tier position or list position."""

from services.retriever.query_plan import build_query_plan_v2
from services.retriever.vocabulary import (
    VOCAB_STRONG_LANE_BONUS,
    grounded_vocabulary_lanes,
    select_strong_vocabulary_matches,
)


def _match(i, *, score, applicability="direct", corpus="c1"):
    return {
        "lexicon_id": f"lex-{i}",
        "term": f"Concept {i}",
        "canonical_name": f"Concept {i}",
        "aliases": [],
        "retrieval_gloss": f"Concept {i} gloss for retrieval.",
        "definitions": [{"text": f"definition {i}"}],
        "application_contexts": [{"predicate": "used_for", "target": "work"}],
        "support_count": 10,
        "source_document_ids": [f"doc-{i}"],
        "source_chunk_ids": [f"chunk-{i}"],
        "source_parent_ids": [f"parent-{i}"],
        "corpus_id": corpus,
        "applicability": applicability,
        "evidence_adjusted_score": score,
        "score": score,
        "overlap_count": 1,
    }


def test_select_strong_matches_orders_by_strength_and_fills_by_rank():
    matches = [_match(i, score=0.4 + i * 0.01) for i in range(10)]
    matches.append(_match("expert", score=0.909))
    strong = select_strong_vocabulary_matches(matches)
    assert strong and strong[0]["lexicon_id"] == "lex-expert"
    filled = select_strong_vocabulary_matches(matches, cap=4, fill_by_rank=True)
    assert len(filled) == 4
    assert filled[0]["lexicon_id"] == "lex-expert"
    assert filled[1]["lexicon_id"] == "lex-0"  # rank fill preserves original order


def test_strong_match_at_deep_position_gains_translation_lane():
    # 6 weak direct-tier matches in distinct corpora exhaust the tier cap (3);
    # a strong exploratory-tier match at deep position must still gain a lane.
    matches = [
        _match(i, score=0.45, corpus=f"corpus-{i}") for i in range(6)
    ]
    matches.append(
        _match(
            "facs-like",
            score=0.909,
            applicability="source_term_overlap",
            corpus="corpus-9",
        )
    )
    matches[-1]["support_count"] = 24  # passes source_term_overlap eligibility
    resolution = {"matches": matches}
    plan = build_query_plan_v2("How should the character's face move in the ad?")
    lanes, diagnostics = grounded_vocabulary_lanes(plan, resolution)
    assert "lex-facs-like" in diagnostics["introduced_lexicon_ids"]
    assert diagnostics["strong_admission"]["admitted_lexicon_ids"] == [
        "lex-facs-like"
    ]
    assert diagnostics["strong_admission"]["bound"] is not None
    assert len(diagnostics["strong_admission"]["admitted_lexicon_ids"]) <= (
        VOCAB_STRONG_LANE_BONUS
    )


def test_weak_matches_do_not_trigger_strength_bonus():
    matches = [_match(i, score=0.4, corpus=f"c{i}") for i in range(5)]
    resolution = {"matches": matches}
    plan = build_query_plan_v2("How do teams coordinate work?")
    lanes, diagnostics = grounded_vocabulary_lanes(plan, resolution)
    # top match trivially clears top*ratio but not necessarily... the bound is
    # max(0.55, 0.4*0.75)=0.55 > 0.4 so nothing is strength-admitted.
    assert diagnostics["strong_admission"]["admitted_lexicon_ids"] == []
