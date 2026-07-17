"""Asserting tests for the deterministic hydration waterfall (B1, §5.2/§5.4).

Every owner rule + byte-identical determinism. Pure — runs anywhere:
    docker exec -i polymath_v33-backend-1 python /app/tests/test_waterfall.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.retriever.waterfall import (  # noqa: E402
    OrphanChild,
    ParentCandidate,
    SharedEntity,
    allocate,
)
from config import Settings  # noqa: E402

W = lambda s: len(s.split())  # deterministic word-count tokenizer for tests


def _parents(n=8, full_words=300, sum_words=40, lane=""):
    return [
        ParentCandidate(
            parent_id=f"p{i}",
            doc_id=f"d{i % 4}",
            score=1.0 - i * 0.05,
            full_text=" ".join(f"p{i}w{j}" for j in range(full_words)),
            summary=" ".join(f"p{i}s{j}" for j in range(sum_words)),
            lane=lane,
        )
        for i in range(n)
    ]


# ── Rule 1+2 of the ladder: ranks 1-4 full, 5-8 summaries at ~60% budget ───
def test_ladder_full_then_summaries():
    pkt = allocate(_parents(8), budget_tokens=1450, count_tokens=W)
    kinds = [(it.ref_id, it.kind) for it in pkt.items]
    assert kinds[:4] == [("p0", "full"), ("p1", "full"), ("p2", "full"), ("p3", "full")]
    assert all(k == "summary" for _, k in kinds[4:8])
    assert [it.hydration_level for it in pkt.items[:8]] == [
        "full",
        "full",
        "full",
        "full",
        "summary",
        "summary",
        "summary",
        "summary",
    ]
    assert pkt.used_tokens <= 1450


# ── Determinism: byte-identical packet twice ────────────────────────────────
def test_byte_identical_packets():
    args = dict(
        budget_tokens=1500,
        orphans=[OrphanChild("c1", "px", "d9", 0.5, "orphan fragment text here")],
        entities=[SharedEntity("e1", "entity: tensorflow relates_to python")],
        count_tokens=W,
    )
    a = allocate(_parents(8), **args)
    b = allocate(_parents(8), **args)
    assert a.packet_hash == b.packet_hash
    assert [i.text for i in a.items] == [i.text for i in b.items]
    assert [asdict(d) for d in a.hydration_decisions] == [
        asdict(d) for d in b.hydration_decisions
    ]


def test_waterfall_feature_flag_defaults_off():
    assert Settings.model_fields["WATERFALL_ASSEMBLY"].default is False


# ── Rule 4: overflow swaps to summary, NEVER truncates ─────────────────────
def test_overflow_swaps_never_truncates():
    ps = _parents(3, full_words=900, sum_words=30)
    pkt = allocate(ps, budget_tokens=1000, count_tokens=W)
    by = {i.ref_id: i for i in pkt.items}
    assert by["p0"].kind == "full"
    assert by["p1"].kind == "summary"  # 900 doesn't fit → summary
    originals = {p.parent_id: (p.full_text, p.summary) for p in ps}
    for it in pkt.items:  # verbatim forms only
        assert it.text in originals[it.ref_id]


def test_global_rank_tiers_do_not_return_to_full_after_summary():
    ps = [
        ParentCandidate("p0", "d0", 0.9, " ".join(["a"] * 60), "a summary"),
        ParentCandidate("p1", "d1", 0.8, " ".join(["b"] * 90), "b summary"),
        ParentCandidate("p2", "d2", 0.7, " ".join(["c"] * 10), "c summary"),
    ]
    pkt = allocate(ps, budget_tokens=80, count_tokens=W)
    assert [(it.ref_id, it.kind) for it in pkt.items] == [
        ("p0", "full"),
        ("p1", "summary"),
        ("p2", "summary"),
    ]


def test_every_ranked_parent_has_full_summary_or_skip_decision():
    ps = [
        ParentCandidate("p0", "d0", 0.9, " ".join(["a"] * 80), "a summary"),
        ParentCandidate("p1", "d1", 0.8, " ".join(["b"] * 80), "b summary"),
        ParentCandidate("p2", "d2", 0.7, " ".join(["c"] * 80), " ".join(["s"] * 50)),
        ParentCandidate("p3", "d3", 0.6, "", ""),
    ]
    pkt = allocate(ps, budget_tokens=100, count_tokens=W)
    assert [
        (decision.rank, decision.parent_id, decision.hydration_level)
        for decision in pkt.hydration_decisions
    ] == [
        (0, "p0", "full"),
        (1, "p1", "summary"),
        (2, "p2", "skip"),
        (3, "p3", "skip"),
    ]
    assert pkt.hydration_decisions[2].reason == "summary_exceeds_remaining_budget"
    assert pkt.hydration_decisions[3].reason == "no_hydratable_text"
    assert pkt.diagnostics["hydration_counts"] == {
        "full": 1,
        "summary": 1,
        "skip": 2,
    }
    assert pkt.diagnostics["skipped_parents"] == 2


# ── Rule 5: surplus promotes next summary → full ───────────────────────────
def test_surplus_promotes_summary_to_full():
    ps = _parents(2, full_words=100, sum_words=10)
    # budget fits p0 full + p1 summary initially… then surplus promotes p1.
    pkt = allocate(ps, budget_tokens=250, count_tokens=W)
    kinds = {i.ref_id: i.kind for i in pkt.items}
    assert kinds == {"p0": "full", "p1": "full"}  # 100+100 <= 250
    assert pkt.diagnostics["summaries_promoted"] >= 1 or all(
        k == "full" for k in kinds.values()
    )


# ── Rule 6 + 3a: orphan dedupe against included parents ────────────────────
def test_orphan_child_of_included_parent_dropped():
    orphans = [
        OrphanChild("c_in", "p0", "d0", 0.9, "child of included parent"),
        OrphanChild("c_out", "p_foreign", "d8", 0.8, "cross domain fragment kept"),
    ]
    pkt = allocate(
        _parents(4, full_words=50, sum_words=10),
        budget_tokens=400,
        orphans=orphans,
        count_tokens=W,
    )
    ids = [i.ref_id for i in pkt.items if i.kind == "child"]
    assert ids == ["c_out"]
    assert pkt.diagnostics["orphans_dropped_parent_included"] == 1


# ── Rule 3b: entities last, only if budget remains ─────────────────────────
def test_entities_fill_last():
    pkt = allocate(
        _parents(2, full_words=50, sum_words=10),
        budget_tokens=120,
        entities=[
            SharedEntity("e1", "a b c d e"),
            SharedEntity("e2", " ".join(["x"] * 500)),
        ],
        count_tokens=W,
    )
    kinds = [i.kind for i in pkt.items]
    assert kinds[-1] == "entity" and kinds.count("entity") == 1  # e2 too big
    assert pkt.items[-1].ref_id == "e1"


# ── Rule 2: two-lane quota + threshold spillover ───────────────────────────
def test_two_lane_guaranteed_anchor_slots():
    anchors = _parents(4, full_words=100, sum_words=10, lane="anchor")
    expansion = _parents(8, full_words=100, sum_words=10)
    exp = [
        ParentCandidate(f"x{i}", p.doc_id, p.score - 0.5, p.full_text, p.summary)
        for i, p in enumerate(expansion)
    ]
    pkt = allocate(anchors + exp, budget_tokens=1000, anchor_quota=0.6, count_tokens=W)
    lanes = {i.lane for i in pkt.items}
    assert "anchor" in lanes and "expansion" in lanes
    anchor_fulls = [i for i in pkt.items if i.lane == "anchor" and i.kind == "full"]
    assert len(anchor_fulls) >= 4  # guaranteed grounding slots
    assert pkt.diagnostics["mode"] == "two_lane"


def test_spillover_when_anchors_below_threshold():
    anchors = [
        ParentCandidate("a0", "d0", 0.9, " ".join(["a"] * 100), "s a", "anchor"),
        ParentCandidate(
            "a1", "d0", 0.2, " ".join(["b"] * 100), "s b", "anchor"
        ),  # below thr
    ]
    exp = _parents(8, full_words=100, sum_words=10)
    pkt = allocate(
        anchors + exp,
        budget_tokens=1000,
        anchor_quota=0.6,
        spillover_threshold=0.5,
        count_tokens=W,
    )
    anchor_ids = {i.ref_id for i in pkt.items if i.lane == "anchor"}
    assert anchor_ids == {"a0"}  # a1 filtered by threshold
    decision = next(d for d in pkt.hydration_decisions if d.parent_id == "a1")
    assert decision.hydration_level == "skip"
    assert decision.reason == "below_spillover_threshold"
    assert pkt.diagnostics["spilled_tokens"] > 0  # unspent anchor budget spilled
    exp_fulls = sum(1 for i in pkt.items if i.lane == "expansion" and i.kind == "full")
    assert exp_fulls >= 8  # expansion got the spill


def test_no_anchors_collapses_to_single_lane():
    pkt = allocate(_parents(4), budget_tokens=1500, count_tokens=W)
    assert pkt.diagnostics["mode"] == "single_lane"


# ── Degenerate: zero budget / empty inputs ─────────────────────────────────
def test_degenerate_inputs():
    assert allocate([], budget_tokens=1000, count_tokens=W).items == []
    zero = allocate(_parents(3), budget_tokens=0, count_tokens=W)
    assert zero.items == []
    assert [d.hydration_level for d in zero.hydration_decisions] == [
        "skip",
        "skip",
        "skip",
    ]


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
