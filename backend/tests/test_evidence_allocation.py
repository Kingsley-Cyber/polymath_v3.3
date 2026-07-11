"""Asserting tests for source-side evidence allocation (A-D of the retrieval fix).

Pure: no DB, no retriever, no pydantic. Runnable two ways:
    pytest backend/tests/test_evidence_allocation.py
    PYTHONPATH=backend python3 backend/tests/test_evidence_allocation.py   # self-asserting, exit!=0 on fail
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

# Allow `python3 backend/tests/test_evidence_allocation.py` from the repo root.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.retriever.evidence_plan import (  # noqa: E402
    build_evidence_plan,
    build_evidence_plan_from_sides,
    parse_llm_sides,
)
from services.retriever.query_semantics import (  # noqa: E402
    concept_groups,
    is_curated_concept,
    split_query_sides,
)
from services.retriever.evidence_allocation import (  # noqa: E402
    DEFAULT_PER_SIDE_SOURCES,
    STRONG_LANE_SCORE,
    WEAK_LANE_SUPPORT_FLOOR,
    cap_chunks_per_doc,
    lane_alias_score,
    lane_coverage,
    per_doc_cap_for_plan,
    select_lane_support,
)


@dataclass
class Chunk:
    """Minimal duck-typed chunk for the pure allocation functions."""

    chunk_id: str
    doc_id: str
    text: str = ""
    score: float = 0.5
    metadata: dict = field(default_factory=dict)


# Field extractors / scorer used to drive the pure functions in tests.
def _cid(c):
    return str(c.chunk_id or "")


def _did(c):
    return str(c.doc_id or "")


def _base(c):
    return float(c.score or 0.0)


def _score(c, lane):
    return lane_alias_score(c.text, lane)


# A realistic two-side plan: "the art of seduction" + a personality framework.
PLAN = build_evidence_plan(
    "how does different personality correlate to the art of seduction"
)
PERSONALITY = PLAN.lanes[0]
SEDUCTION = PLAN.lanes[1]


def test_plan_assigns_per_side_min_sources_of_two():
    # A multi-concept plan must require >=2 distinct sources per side, otherwise
    # one stray chunk can satisfy a side.
    assert PLAN.mode.startswith("multi_concept")
    for lane in PLAN.required_lanes:
        assert lane.min_sources >= 2, (lane.name, lane.min_sources)


def test_select_lane_support_picks_distinct_documents():
    # Five personality candidates, three of them from distinct books.
    candidates = [
        Chunk("p1", "four_tendencies", "The four tendencies personality framework."),
        Chunk("p2", "four_tendencies", "More on the four tendencies framework."),
        Chunk("p3", "handbook_personality", "The handbook of personality and traits."),
        Chunk("p4", "gifts_differing", "Myers Briggs personality type theory."),
        Chunk("s1", "art_of_seduction", "The seducer plays a role."),  # off-lane
    ]
    picks = select_lane_support(
        candidates,
        lane=PERSONALITY,
        target_k=2,
        existing_chunk_ids=set(),
        existing_doc_ids=set(),
        semantic_doc_ids=set(),
        score_fn=_score,
        chunk_id_fn=_cid,
        doc_id_fn=_did,
        base_score_fn=_base,
    )
    assert len(picks) == 2, [p.chunk_id for p in picks]
    docs = {_did(p) for p in picks}
    assert len(docs) == 2, docs  # distinct documents, never two from four_tendencies
    assert "art_of_seduction" not in docs  # off-lane chunk never selected


def test_select_lane_support_prefers_new_and_semantic_docs():
    candidates = [
        Chunk("p1", "already_used", "personality framework", score=0.9),
        Chunk("p2", "fresh_book", "personality type assessment", score=0.4),
        Chunk("p3", "ingest_match", "temperament typology", score=0.3),
    ]
    picks = select_lane_support(
        candidates,
        lane=PERSONALITY,
        target_k=2,
        existing_chunk_ids=set(),
        existing_doc_ids={"already_used"},  # this doc is already in the context
        semantic_doc_ids={"ingest_match"},  # ingestion says this doc is on-side
        score_fn=_score,
        chunk_id_fn=_cid,
        doc_id_fn=_did,
        base_score_fn=_base,
    )
    docs = [_did(p) for p in picks]
    # The ingest-confirmed doc and the fresh doc beat the already-used doc.
    assert "ingest_match" in docs
    assert "fresh_book" in docs
    assert "already_used" not in docs


def test_coverage_requires_two_distinct_docs_for_a_two_source_side():
    # One personality chunk (from one doc) must NOT mark the side covered when
    # min_sources == 2 — this is the exact "one chunk = covered" bug.
    one_chunk = [Chunk("p1", "four_tendencies", "the four tendencies framework")]
    cov = lane_coverage(one_chunk, PLAN, score_fn=_score, doc_id_fn=_did)
    assert PERSONALITY.name in cov["missing_lanes"], cov

    two_docs = [
        Chunk("p1", "four_tendencies", "the four tendencies framework"),
        Chunk("p2", "handbook_personality", "the handbook of personality"),
    ]
    cov2 = lane_coverage(two_docs, PLAN, score_fn=_score, doc_id_fn=_did)
    assert PERSONALITY.name in cov2["covered_lanes"], cov2


def test_coverage_ignores_weak_term_only_match_from_foreign_doc():
    # A seduction passage that merely contains the word "type"/"character"
    # scores below STRONG_LANE_SCORE and must not count toward the personality
    # side. (Two such foreign chunks still leave the side missing.)
    foreign = [
        Chunk("s1", "art_of_seduction", "the seducer adopts a character and a type"),
        Chunk("s2", "art_of_seduction_copy", "a certain type of charm and character"),
    ]
    cov = lane_coverage(foreign, PLAN, score_fn=_score, doc_id_fn=_did)
    assert PERSONALITY.name in cov["missing_lanes"], cov


def test_select_lane_support_rejects_weak_foreign_docs():
    candidates = [
        Chunk("s1", "strategy_book", "generic tactics and types", score=0.95),
        Chunk(
            "p1",
            "personality_book",
            "personality assessment and big five traits",
            score=0.5,
        ),
    ]
    picks = select_lane_support(
        candidates,
        lane=PERSONALITY,
        target_k=2,
        existing_chunk_ids=set(),
        existing_doc_ids=set(),
        semantic_doc_ids=set(),
        score_fn=_score,
        chunk_id_fn=_cid,
        doc_id_fn=_did,
        base_score_fn=_base,
    )

    assert [p.chunk_id for p in picks] == ["p1"]


def test_select_lane_support_admits_weak_but_present_match_above_floor():
    # Regression fix H: a chunk scoring in [WEAK_LANE_SUPPORT_FLOOR,
    # STRONG_LANE_SCORE) is a weak-but-present lane match — now eligible to
    # COMPETE for a support slot (previously the hard STRONG floor rejected it
    # outright). A chunk below the weak floor stays out. Uses a controlled
    # score_fn to pin the boundary exactly.
    assert WEAK_LANE_SUPPORT_FLOOR < STRONG_LANE_SCORE
    fixed = {"weak": WEAK_LANE_SUPPORT_FLOOR + 1, "below": WEAK_LANE_SUPPORT_FLOOR - 1}
    candidates = [
        Chunk("weak", "adjacent_book", "adjacent-vocabulary personality passage"),
        Chunk("below", "noise_book", "barely related"),
    ]
    picks = select_lane_support(
        candidates,
        lane=PERSONALITY,
        target_k=2,
        existing_chunk_ids=set(),
        existing_doc_ids=set(),
        semantic_doc_ids=set(),
        score_fn=lambda c, lane: fixed[c.chunk_id],
        chunk_id_fn=_cid,
        doc_id_fn=_did,
        base_score_fn=_base,
    )
    ids = [p.chunk_id for p in picks]
    assert "weak" in ids, ids  # in [4, 8) -> now admitted
    assert "below" not in ids, ids  # < 4 -> still rejected


def test_strong_match_still_outranks_weak_support_when_slots_are_scarce():
    # Admitting weak matches must NOT let them displace a strong candidate: with
    # one slot, the STRONG (>=8) new-doc pick still wins.
    fixed = {"weak": WEAK_LANE_SUPPORT_FLOOR + 1, "strong": STRONG_LANE_SCORE + 1}
    candidates = [
        Chunk("weak", "weak_book", "weak adjacent match", score=0.99),
        Chunk("strong", "strong_book", "strong on-lane match", score=0.10),
    ]
    picks = select_lane_support(
        candidates,
        lane=PERSONALITY,
        target_k=1,
        existing_chunk_ids=set(),
        existing_doc_ids=set(),
        semantic_doc_ids=set(),
        score_fn=lambda c, lane: fixed[c.chunk_id],
        chunk_id_fn=_cid,
        doc_id_fn=_did,
        base_score_fn=_base,
    )
    assert [p.chunk_id for p in picks] == ["strong"], [p.chunk_id for p in picks]


def test_cap_chunks_per_doc_limits_dominant_doc_but_protects_support():
    # Final set: 4 from the title book + 2 reserved personality support chunks.
    sources = [
        Chunk("s1", "art_of_seduction", "seduction 1"),
        Chunk("s2", "art_of_seduction", "seduction 2"),
        Chunk("s3", "art_of_seduction", "seduction 3"),
        Chunk("s4", "art_of_seduction", "seduction 4"),
        Chunk(
            "p1",
            "four_tendencies",
            "personality 1",
            metadata={"support_role": "evidence_plan_lane"},
        ),
        Chunk(
            "p2",
            "handbook_personality",
            "personality 2",
            metadata={"support_role": "evidence_plan_lane"},
        ),
    ]

    def _protected(c):
        return (c.metadata or {}).get("support_role") == "evidence_plan_lane"

    capped = cap_chunks_per_doc(sources, cap=2, doc_id_fn=_did, protect_fn=_protected)
    by_doc = {}
    for c in capped:
        by_doc.setdefault(_did(c), []).append(c.chunk_id)
    assert len(by_doc["art_of_seduction"]) == 2, by_doc  # dominant book capped at 2
    # both reserved personality chunks survive
    assert {"p1"} <= set(by_doc["four_tendencies"])
    assert {"p2"} <= set(by_doc["handbook_personality"])


def test_per_doc_cap_for_plan_only_fires_for_multi_side():
    assert per_doc_cap_for_plan(PLAN, budget=8) >= DEFAULT_PER_SIDE_SOURCES
    single = build_evidence_plan("what is natural language processing")
    assert per_doc_cap_for_plan(single, budget=8) == 0  # single-side -> disabled


def test_end_to_end_distribution_for_seduction_plus_personality():
    """The whole point: a 2-side query must not be answered 4/5 by one book."""
    # Base retrieval (title-match dominated): 4 seduction + 1 personality.
    base = [
        Chunk("s1", "art_of_seduction", "the seducer creates desire", score=0.95),
        Chunk("s2", "art_of_seduction", "the rake and the ideal lover", score=0.93),
        Chunk("s3", "art_of_seduction", "soft seduction tactics", score=0.91),
        Chunk("s4", "art_of_seduction", "the seductive character type", score=0.90),
        Chunk("p1", "four_tendencies", "the four tendencies framework", score=0.55),
    ]
    # Personality side is under-covered (1 doc < min_sources 2) -> reserve more.
    cov = lane_coverage(base, PLAN, score_fn=_score, doc_id_fn=_did)
    assert PERSONALITY.name in cov["missing_lanes"]

    pool = [
        Chunk(
            "p2",
            "handbook_personality",
            "the handbook of personality traits",
            score=0.5,
        ),
        Chunk("p3", "gifts_differing", "myers briggs personality type", score=0.48),
    ]
    existing_docs = {_did(c) for c in base}
    support = select_lane_support(
        pool,
        lane=PERSONALITY,
        target_k=PERSONALITY.min_sources,
        existing_chunk_ids={_cid(c) for c in base},
        existing_doc_ids=existing_docs,
        semantic_doc_ids=set(),
        score_fn=_score,
        chunk_id_fn=_cid,
        doc_id_fn=_did,
        base_score_fn=_base,
    )
    for c in support:
        c.metadata = {"support_role": "evidence_plan_lane"}

    merged = base + support
    cap = per_doc_cap_for_plan(PLAN, budget=len(merged))
    final = cap_chunks_per_doc(
        merged,
        cap=cap,
        doc_id_fn=_did,
        protect_fn=lambda c: (c.metadata or {}).get("support_role")
        == "evidence_plan_lane",
    )

    by_doc = {}
    for c in final:
        by_doc.setdefault(_did(c), 0)
        by_doc[_did(c)] += 1
    personality_docs = [
        d
        for d in by_doc
        if d in {"four_tendencies", "handbook_personality", "gifts_differing"}
    ]

    # The exact fix: the title book drops from 4 chunks to the per-side cap (2),
    # the personality side is backed by >=2 distinct books, and no single
    # document dominates the packet.
    assert cap == DEFAULT_PER_SIDE_SOURCES, cap
    assert by_doc["art_of_seduction"] == 2, by_doc  # was 4/5 before the fix
    assert len(personality_docs) >= 2, by_doc
    assert max(by_doc.values()) <= cap, by_doc


def test_seduction_personality_prompt_stays_broadly_decomposed():
    plan = build_evidence_plan(
        "Which personality types are most vulnerable to the Art of Seduction tactics?"
    )
    lane_names = {lane.name for lane in plan.required_lanes}

    assert plan.mode.startswith("multi_concept")
    assert "seduction" in lane_names
    assert "personality_framework" in lane_names
    assert "tactics" not in lane_names
    assert "vulnerable" not in lane_names
    for lane in plan.required_lanes:
        assert lane.min_sources >= 2


def test_seduction_personality_across_books_does_not_make_modifier_lanes():
    plan = build_evidence_plan(
        "How do personality frameworks relate to the Art of Seduction tactics across these books?"
    )
    lane_names = {lane.name for lane in plan.required_lanes}

    assert "personality_framework" in lane_names
    assert "seduction" in lane_names
    assert "across" not in lane_names
    assert "books" not in lane_names
    assert "tactics" not in lane_names


def test_full_spectrum_request_does_not_anchor_on_scaffolding_words():
    plan = build_evidence_plan(
        "Give me a full spectrum overview across all personality and seduction books in this corpus."
    )
    lane_names = {lane.name for lane in plan.required_lanes}

    assert {"personality", "seduction"} <= lane_names
    assert "give" not in lane_names
    assert "full" not in lane_names
    assert "across" not in lane_names
    assert "spectrum" not in lane_names
    assert "all" not in lane_names
    assert "corpus" not in lane_names


def test_split_query_sides_generalizes_to_unseen_book_pair():
    # A query over books the alias table has NEVER seen must still split into two
    # source sides (the no-LLM generalization).
    sides = split_query_sides(
        "compare the strategies in The 48 Laws of Power "
        "versus the negotiation tactics in Never Split the Difference"
    )
    assert len(sides) == 2, sides
    blob = " ".join(
        s["name"] + " " + " ".join(s["search_terms"]) for s in sides
    ).lower()
    assert "laws" in blob or "power" in blob
    assert "split" in blob or "negotiation" in blob or "difference" in blob


def test_split_query_sides_handles_between_and_is_conservative():
    assert len(split_query_sides("the relationship between stoicism and buddhism")) == 2
    # A plain question with an incidental 'and' must NOT be force-split.
    assert split_query_sides("explain machine learning and why it matters") == []
    assert split_query_sides("what is natural language processing") == []


def test_build_evidence_plan_from_sides_makes_a_two_side_plan():
    sides = [
        {"name": "stoicism", "search_terms": ["stoicism", "marcus aurelius"]},
        {"name": "buddhism", "search_terms": ["buddhism", "the dhammapada"]},
    ]
    plan = build_evidence_plan_from_sides("stoicism versus buddhism", sides)
    assert plan.mode == "multi_concept_sourced"
    assert [l.name for l in plan.lanes] == ["stoicism", "buddhism"]
    for lane in plan.required_lanes:
        assert lane.min_sources >= 2  # inherits the per-side floor


def test_build_evidence_plan_from_sides_falls_back_when_underspecified():
    # Only one usable side -> fall back to the deterministic plan, never crash.
    plan = build_evidence_plan_from_sides(
        "what is natural language processing",
        [{"name": "nlp", "search_terms": ["nlp"]}],
    )
    assert plan.mode in {"single_concept", "unstructured"}


def test_query_plan_adapter_can_preserve_one_complete_objective():
    plan = build_evidence_plan_from_sides(
        "How should I prompt the opening scene?",
        [
            {
                "name": "opening_scene",
                "label": "How should I prompt the opening scene?",
                "query": "How should I prompt the opening scene?",
                "search_terms": ["opening scene", "video prompt"],
            }
        ],
        allow_single=True,
    )

    assert plan.mode == "single_objective_sourced"
    assert [lane.name for lane in plan.required_lanes] == ["opening_scene"]


def test_parse_llm_sides_is_tolerant():
    good = (
        '```json\n{"sides": [{"name": "A", "search_terms": ["a1", "a2"]}, '
        '{"name": "B", "terms": "b1"}]}\n```'
    )
    sides = parse_llm_sides(good)
    assert [s["name"] for s in sides] == ["A", "B"]
    assert sides[1]["search_terms"] == ["b1"]
    # Garbage / empty never raises and yields nothing.
    assert parse_llm_sides("not json at all") == []
    assert parse_llm_sides("") == []
    assert parse_llm_sides('{"sides": "oops"}') == []


def test_generic_token_does_not_become_a_standalone_lane():
    # "analysis" alone embeds near "data analysis" and must NOT anchor a lane;
    # the specific concept ("personality") still does.
    keys = [g.key for g in concept_groups("how does analysis relate to personality")]
    assert "analysis" not in keys, keys
    assert "personality" in keys, keys
    # A specific compound token is still kept.
    assert "metacognition" in [g.key for g in concept_groups("metacognition and habit")]


def test_is_curated_concept():
    assert is_curated_concept("personality")
    assert is_curated_concept("seduction")
    assert not is_curated_concept("metacognition")
    assert not is_curated_concept("")
    assert not is_curated_concept(None)


def _run_all():
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
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
